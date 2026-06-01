#!/usr/bin/env python3
"""Reusable MCP identity proxy.

Bridges a Cursor-style stdio MCP client to a downstream HTTP MCP endpoint,
injecting a stable agent identity on the wire. Built to sit between the
Cursor launcher (`mcp.json` command) and Neotoma's HTTP `/mcp` endpoint so
writes carry attribution.

Layers (matches `cursor_mcp_proxy_4a68cb45.plan.md` Phase 1/2):

- upstream adapter: stdio ↔ newline-delimited JSON-RPC
- downstream adapter: HTTP StreamableHTTP MCP (Neotoma today)
- identity middleware: injects `clientInfo` fallback and leaves an AAuth hook
- session manager: preserves `Mcp-Session-Id` across calls
- verification client: optional preflight to Neotoma's `/session` surface
  defined in `proxy_identity_enhancements_4c5ecc8e.plan.md`

Non-goals:

- proxy-owned attribution policy (Neotoma owns that per its plan)
- non-Neotoma downstreams (the downstream adapter is pluggable but only
  the Neotoma HTTP target is implemented here)

Usage:

    python mcp_identity_proxy.py \
        --downstream-url http://localhost:3080/mcp \
        --client-name cursor-neotoma-proxy \
        --client-version 0.1.0

Environment variables (preferred over flags for launcher integration):

    MCP_PROXY_DOWNSTREAM_URL   Downstream HTTP MCP base, e.g. http://localhost:3080/mcp
    MCP_PROXY_CLIENT_NAME      clientInfo.name injected on initialize
    MCP_PROXY_CLIENT_VERSION   clientInfo.version injected on initialize
    MCP_PROXY_AGENT_LABEL      Optional repo/env label appended to clientInfo.name
    MCP_PROXY_BEARER_TOKEN     Optional Bearer token forwarded to downstream
    MCP_PROXY_CONNECTION_ID    Optional X-Connection-Id forwarded to downstream
    MCP_PROXY_SESSION_PREFLIGHT  "1" to hit `/session` on startup for trust check
    MCP_PROXY_SESSION_PREFLIGHT_BASE  Explicit base URL for `/session` if different
    MCP_PROXY_FAIL_CLOSED      "1" to abort when preflight reports anonymous tier
    MCP_PROXY_LOG_FILE         File to write structured diagnostics to

    MCP_PROXY_AAUTH            "1" to AAuth-sign every downstream request (RFC 9421
                               + Signature-Key profile). When enabled, the
                               configuration below is required.
    NEOTOMA_AAUTH_SUB          AAuth subject claim, e.g. cursor@markmhendrickson.com
    NEOTOMA_AAUTH_ISS          AAuth issuer claim, e.g. https://markmhendrickson.com
    NEOTOMA_AAUTH_KID          Optional kid override (defaults to JWK's `kid`)
    NEOTOMA_AAUTH_PRIVATE_JWK_PATH  Override path to the private JWK (defaults
                                    to .creds/aauth_agent_cursor.private.jwk)
    NEOTOMA_AAUTH_TOKEN_TTL_SEC     aa-agent+jwt lifetime, default 300s
    NEOTOMA_AAUTH_AUTHORITY_OVERRIDE  Force the @authority canonicalization
                                      (must match Neotoma's NEOTOMA_AUTH_AUTHORITY)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

try:
    import aiohttp
except ImportError:
    print(
        "Error: aiohttp is required. Install with: pip3 install --user aiohttp",
        file=sys.stderr,
    )
    sys.exit(1)

# Signer is loaded lazily so the proxy still runs without AAuth deps installed.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:  # pragma: no cover - exercised by Phase 4 cutover
    from aauth_signer import (  # type: ignore[import-not-found]
        SignerConfig,
        SignerConfigError,
        build_signed_headers,
        load_signer_config_from_env,
    )

    _AAUTH_IMPORT_ERROR: Optional[Exception] = None
except Exception as _aauth_import_exc:  # pragma: no cover - import-time guard
    SignerConfig = None  # type: ignore[assignment]
    SignerConfigError = Exception  # type: ignore[assignment]
    build_signed_headers = None  # type: ignore[assignment]
    load_signer_config_from_env = None  # type: ignore[assignment]
    _AAUTH_IMPORT_ERROR = _aauth_import_exc


DEFAULT_CLIENT_NAME = "cursor-neotoma-proxy"
DEFAULT_CLIENT_VERSION = "0.1.0"
DEFAULT_DOWNSTREAM_URL = "http://localhost:3080/mcp"
SESSION_HEADER_CANONICAL = "Mcp-Session-Id"


@dataclass
class ProxyConfig:
    downstream_url: str
    client_name: str
    client_version: str
    agent_label: Optional[str] = None
    bearer_token: Optional[str] = None
    connection_id: Optional[str] = None
    session_preflight: bool = False
    session_preflight_base: Optional[str] = None
    fail_closed: bool = False
    log_file: Optional[str] = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    aauth_enabled: bool = False
    aauth_signer: Optional[SignerConfig] = None  # populated when aauth_enabled

    @property
    def effective_client_name(self) -> str:
        if self.agent_label:
            return f"{self.client_name}+{self.agent_label}"
        return self.client_name

    @property
    def session_base_url(self) -> str:
        if self.session_preflight_base:
            return self.session_preflight_base.rstrip("/")
        parsed = urlparse(self.downstream_url)
        return f"{parsed.scheme}://{parsed.netloc}"


def _configure_logging(config: ProxyConfig) -> logging.Logger:
    logger = logging.getLogger("mcp_identity_proxy")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler: logging.Handler
    if config.log_file:
        handler = logging.FileHandler(config.log_file)
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [mcp_identity_proxy] %(levelname)s %(message)s")
    )
    if not logger.handlers:
        logger.addHandler(handler)
    return logger


def _build_base_headers(config: ProxyConfig) -> dict[str, str]:
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": f"{config.effective_client_name}/{config.client_version}",
    }
    if config.bearer_token:
        headers["Authorization"] = f"Bearer {config.bearer_token}"
    if config.connection_id:
        headers["X-Connection-Id"] = config.connection_id
    if config.aauth_signer is not None:
        # Even before AAuth verification kicks in, naming the sub here lets
        # Neotoma's `unverified_client` resolution surface our identity, and
        # gives `NEOTOMA_STRICT_AAUTH_SUBS` a label to pin once strict mode
        # is enabled on the resource side.
        headers.setdefault("X-Agent-Label", config.aauth_signer.sub)
    headers.update(config.extra_headers)
    return headers


def _maybe_inject_client_info(
    message: dict[str, Any], config: ProxyConfig, logger: logging.Logger
) -> None:
    """For `initialize` requests, inject clientInfo fallback attribution.

    AAuth signing is not implemented here yet (see `proxy_identity_enhancements_4c5ecc8e.plan.md`
    and Neotoma docs/subsystems/agent_attribution_integration.md for the
    preferred path). When AAuth is wired into this proxy it should replace
    this block or layer on top of it.
    """
    if message.get("method") != "initialize":
        return
    params = message.setdefault("params", {})
    client_info = params.setdefault("clientInfo", {})
    existing_name = client_info.get("name")
    if (
        not existing_name
        or not isinstance(existing_name, str)
        or not existing_name.strip()
    ):
        client_info["name"] = config.effective_client_name
    if not client_info.get("version"):
        client_info["version"] = config.client_version
    logger.info(
        "initialize clientInfo injected: name=%s version=%s",
        client_info.get("name"),
        client_info.get("version"),
    )


class SessionState:
    """Session manager. Holds the `Mcp-Session-Id` returned after initialize."""

    def __init__(self) -> None:
        self.session_id: Optional[str] = None

    def attach(self, headers: dict[str, str]) -> None:
        if self.session_id:
            headers[SESSION_HEADER_CANONICAL] = self.session_id

    def capture(self, response_headers: Any) -> None:
        for candidate in (SESSION_HEADER_CANONICAL, "mcp-session-id"):
            value = response_headers.get(candidate)
            if value:
                self.session_id = value
                return


async def _run_preflight(
    session: aiohttp.ClientSession,
    config: ProxyConfig,
    logger: logging.Logger,
) -> None:
    """Optional call to Neotoma's `/session` introspection surface.

    Defined in `proxy_identity_enhancements_4c5ecc8e.plan.md` Phase 1. The
    endpoint is read-only; this is a trust-check, not a write.
    """
    url = f"{config.session_base_url}/session"
    headers = _build_base_headers(config)
    try:
        async with session.get(url, headers=headers) as resp:
            status = resp.status
            body_text = await resp.text()
    except aiohttp.ClientError as exc:
        logger.warning("Preflight /session unreachable at %s: %s", url, exc)
        if config.fail_closed:
            raise SystemExit(
                f"[mcp_identity_proxy] fail-closed: /session unreachable at {url}"
            )
        return

    if status != 200:
        logger.warning(
            "Preflight /session returned status=%s body=%s", status, body_text[:200]
        )
        if config.fail_closed:
            raise SystemExit(
                f"[mcp_identity_proxy] fail-closed: /session status {status}"
            )
        return

    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError:
        logger.warning("Preflight /session returned non-JSON body: %s", body_text[:200])
        return

    attribution = payload.get("attribution", {}) if isinstance(payload, dict) else {}
    tier = attribution.get("tier", "unknown")
    thumbprint = attribution.get("agent_thumbprint")
    eligible = (
        payload.get("eligible_for_trusted_writes")
        if isinstance(payload, dict)
        else None
    )
    logger.info(
        "Preflight /session: tier=%s thumbprint=%s eligible_for_trusted_writes=%s",
        tier,
        thumbprint or "<none>",
        eligible,
    )
    if config.fail_closed and tier == "anonymous":
        raise SystemExit(
            "[mcp_identity_proxy] fail-closed: Neotoma resolved anonymous attribution"
        )


async def _forward_json_response(
    response: aiohttp.ClientResponse,
    stdout_writer: asyncio.StreamWriter,
    logger: logging.Logger,
) -> None:
    body = await response.read()
    if not body:
        return
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.error("Failed to decode JSON response: %s", exc)
        return
    _emit_json(stdout_writer, payload)


async def _forward_sse_response(
    response: aiohttp.ClientResponse,
    stdout_writer: asyncio.StreamWriter,
    logger: logging.Logger,
) -> None:
    """Extract JSON-RPC messages from an SSE stream and write to stdout.

    MCP StreamableHTTP SSE frames use `event: message` + `data: <json>`. Any
    other event types are ignored.
    """
    current_event = "message"
    async for raw_line in response.content:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            current_event = "message"
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            current_event = line[len("event:") :].strip()
            continue
        if line.startswith("data:"):
            data = line[len("data:") :].strip()
            if current_event != "message":
                continue
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                logger.warning("SSE data frame is not JSON: %s", data[:200])
                continue
            _emit_json(stdout_writer, payload)


def _emit_json(stdout_writer: asyncio.StreamWriter, payload: Any) -> None:
    line = json.dumps(payload, ensure_ascii=False)
    stdout_writer.write((line + "\n").encode("utf-8"))


async def _dispatch_message(
    session: aiohttp.ClientSession,
    state: SessionState,
    config: ProxyConfig,
    logger: logging.Logger,
    stdout_writer: asyncio.StreamWriter,
    message: dict[str, Any],
) -> None:
    _maybe_inject_client_info(message, config, logger)
    headers = _build_base_headers(config)
    state.attach(headers)
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if config.aauth_signer is not None and build_signed_headers is not None:
        try:
            headers = build_signed_headers(
                method="POST",
                url=config.downstream_url,
                body=payload,
                base_headers=headers,
                config=config.aauth_signer,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("AAuth signing failed: %s", exc)
            if config.fail_closed:
                raise SystemExit(
                    "[mcp_identity_proxy] fail-closed: AAuth signing error"
                ) from exc
            # Fall through unsigned; downstream will resolve as unverified_client
            # or anonymous depending on label coverage.
    try:
        async with session.post(
            config.downstream_url, data=payload, headers=headers
        ) as resp:
            state.capture(resp.headers)
            content_type = resp.headers.get("Content-Type", "")
            if resp.status >= 400:
                body_text = await resp.text()
                logger.error(
                    "Downstream error status=%s content_type=%s body=%s",
                    resp.status,
                    content_type,
                    body_text[:500],
                )
                _emit_error_response(stdout_writer, message, resp.status, body_text)
                return
            if "text/event-stream" in content_type:
                await _forward_sse_response(resp, stdout_writer, logger)
            else:
                await _forward_json_response(resp, stdout_writer, logger)
    except aiohttp.ClientError as exc:
        logger.error("Downstream transport error: %s", exc)
        _emit_error_response(stdout_writer, message, 502, str(exc))


def _emit_error_response(
    stdout_writer: asyncio.StreamWriter,
    original_message: dict[str, Any],
    status: int,
    detail: str,
) -> None:
    request_id = original_message.get("id")
    if request_id is None:
        return
    error_code = -32000 if status < 500 else -32001
    response = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": error_code,
            "message": f"mcp_identity_proxy downstream error ({status})",
            "data": {"detail": detail[:500]},
        },
    }
    _emit_json(stdout_writer, response)


class _ThreadedStdoutWriter:
    """Minimal sync stdout writer that matches the interface used here.

    We intentionally keep stdout writes synchronous (wrapped with a simple
    lock) rather than going through asyncio's pipe transport so the proxy
    works identically whether Cursor hands us a pipe, a tty, or a redirected
    file. MCP stdio traffic is low-volume enough that blocking writes are
    not a throughput concern.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def write(self, data: bytes) -> None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    async def drain(self) -> None:  # noqa: D401 - matches asyncio StreamWriter API
        return


async def _read_stdin_loop(
    session: aiohttp.ClientSession,
    state: SessionState,
    config: ProxyConfig,
    logger: logging.Logger,
) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Optional[str]] = asyncio.Queue()

    def _reader_thread() -> None:
        try:
            for raw_line in sys.stdin:
                loop.call_soon_threadsafe(queue.put_nowait, raw_line)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    import threading

    reader = threading.Thread(
        target=_reader_thread, name="mcp-stdin-reader", daemon=True
    )
    reader.start()

    stdout_writer = _ThreadedStdoutWriter()

    while True:
        item = await queue.get()
        if item is None:
            logger.info("stdin closed; exiting proxy loop")
            return
        line = item.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("Dropping non-JSON stdin line: %s (%s)", line[:200], exc)
            continue
        if not isinstance(message, dict):
            logger.warning("Dropping non-object JSON-RPC message: %s", line[:200])
            continue
        await _dispatch_message(session, state, config, logger, stdout_writer, message)


def _build_config_from_args(args: argparse.Namespace) -> ProxyConfig:
    downstream_url = (
        args.downstream_url
        or os.environ.get("MCP_PROXY_DOWNSTREAM_URL")
        or DEFAULT_DOWNSTREAM_URL
    )
    client_name = (
        args.client_name
        or os.environ.get("MCP_PROXY_CLIENT_NAME")
        or DEFAULT_CLIENT_NAME
    )
    client_version = (
        args.client_version
        or os.environ.get("MCP_PROXY_CLIENT_VERSION")
        or DEFAULT_CLIENT_VERSION
    )
    agent_label = args.agent_label or os.environ.get("MCP_PROXY_AGENT_LABEL")
    bearer_token = args.bearer_token or os.environ.get("MCP_PROXY_BEARER_TOKEN")
    connection_id = args.connection_id or os.environ.get("MCP_PROXY_CONNECTION_ID")
    session_preflight = bool(args.session_preflight) or (
        os.environ.get("MCP_PROXY_SESSION_PREFLIGHT", "").lower()
        in {"1", "true", "yes"}
    )
    session_preflight_base = args.session_preflight_base or os.environ.get(
        "MCP_PROXY_SESSION_PREFLIGHT_BASE"
    )
    fail_closed = bool(args.fail_closed) or (
        os.environ.get("MCP_PROXY_FAIL_CLOSED", "").lower() in {"1", "true", "yes"}
    )
    log_file = args.log_file or os.environ.get("MCP_PROXY_LOG_FILE")
    aauth_enabled = bool(args.aauth) or (
        os.environ.get("MCP_PROXY_AAUTH", "").lower() in {"1", "true", "yes"}
    )

    extra_headers: dict[str, str] = {}
    if args.extra_header:
        for header in args.extra_header:
            if ":" not in header:
                continue
            name, _, value = header.partition(":")
            extra_headers[name.strip()] = value.strip()

    return ProxyConfig(
        downstream_url=downstream_url,
        client_name=client_name,
        client_version=client_version,
        agent_label=agent_label,
        bearer_token=bearer_token,
        connection_id=connection_id,
        session_preflight=session_preflight,
        session_preflight_base=session_preflight_base,
        fail_closed=fail_closed,
        log_file=log_file,
        extra_headers=extra_headers,
        aauth_enabled=aauth_enabled,
    )


def _maybe_load_signer(config: ProxyConfig, logger: logging.Logger) -> None:
    """Resolve the AAuth signer config when enabled.

    Attaches the loaded `SignerConfig` to `config.aauth_signer`. On failure,
    surfaces the error and either fails closed or downgrades to unsigned.
    """
    if not config.aauth_enabled:
        return
    if load_signer_config_from_env is None or build_signed_headers is None:
        msg = (
            "AAuth requested via MCP_PROXY_AAUTH but signer module failed to "
            f"import: {_AAUTH_IMPORT_ERROR!r}. "
            "Run `pip install http-message-signatures pyjwt http-sfv "
            "cryptography requests` in the proxy's virtualenv."
        )
        if config.fail_closed:
            raise SystemExit(f"[mcp_identity_proxy] fail-closed: {msg}")
        logger.error(msg)
        return
    try:
        signer = load_signer_config_from_env()
    except SignerConfigError as exc:
        msg = f"AAuth signer config invalid: {exc}"
        if config.fail_closed:
            raise SystemExit(f"[mcp_identity_proxy] fail-closed: {msg}") from exc
        logger.error("%s; continuing unsigned (unverified_client tier)", msg)
        return
    config.aauth_signer = signer
    logger.info(
        "AAuth signing enabled: sub=%s iss=%s kid=%s jkt=%s ttl=%ss",
        signer.sub,
        signer.iss,
        signer.kid,
        signer.jkt,
        signer.token_ttl_sec,
    )


async def _run(config: ProxyConfig, logger: logging.Logger) -> None:
    timeout = aiohttp.ClientTimeout(total=None, sock_read=None, sock_connect=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        logger.info(
            "Starting proxy: downstream=%s client_name=%s version=%s preflight=%s fail_closed=%s",
            config.downstream_url,
            config.effective_client_name,
            config.client_version,
            config.session_preflight,
            config.fail_closed,
        )
        if config.session_preflight:
            await _run_preflight(session, config, logger)
        state = SessionState()
        await _read_stdin_loop(session, state, config, logger)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reusable MCP identity proxy (stdio upstream, HTTP downstream)"
    )
    parser.add_argument("--downstream-url")
    parser.add_argument("--client-name")
    parser.add_argument("--client-version")
    parser.add_argument("--agent-label")
    parser.add_argument("--bearer-token")
    parser.add_argument("--connection-id")
    parser.add_argument("--session-preflight", action="store_true")
    parser.add_argument("--session-preflight-base")
    parser.add_argument("--fail-closed", action="store_true")
    parser.add_argument("--log-file")
    parser.add_argument(
        "--extra-header",
        action="append",
        help="Extra header to forward to downstream, formatted as 'Name: value'. Repeatable.",
    )
    parser.add_argument(
        "--aauth",
        action="store_true",
        help="AAuth-sign every downstream request using NEOTOMA_AAUTH_* env vars.",
    )
    args = parser.parse_args()
    config = _build_config_from_args(args)
    logger = _configure_logging(config)
    _maybe_load_signer(config, logger)
    _ = uuid.uuid4()
    try:
        asyncio.run(_run(config, logger))
    except KeyboardInterrupt:
        logger.info("proxy interrupted")


if __name__ == "__main__":
    main()
