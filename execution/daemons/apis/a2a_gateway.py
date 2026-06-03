"""
execution/daemons/apis/a2a_gateway.py — A2A inbound gateway for Apis.

Exposes the Ateles swarm as an A2A server so external A2A-compatible agents can
discover its capabilities (via a signed Agent Card at /.well-known/agent.json)
and delegate tasks into it. Inbound tasks become Neotoma `task` entities (see
a2a_executor.ApisTaskBridge), which the existing Apis SSE path then routes to
T4 workers — A2A is purely an additional ingestion mouth on the same queue.

Layering:

  - build_agent_card() / sign_agent_card() / authorize_caller() are pure-stdlib
    and SDK-agnostic, so they are unit-testable without `a2a-sdk` installed.
  - serve() wires those into the official `a2a-sdk` server (imported lazily).
    If the SDK is absent, serve() raises a clear, actionable error rather than
    failing at import time — so the testable core always imports.

Run standalone:  python a2a_gateway.py
Env:
  APIS_A2A_ENABLE        "1" to allow serve() to start (default "0")
  APIS_A2A_HOST          bind host (default 127.0.0.1)
  APIS_A2A_PORT          bind port (default 8788)
  APIS_A2A_PUBLIC_URL    public URL advertised in the Agent Card's `url`
  APIS_A2A_REQUIRE_AUTH  "1" to require a verified caller + grant (default "1")
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

# ── Path bootstrap (standalone-script imports) ──────────────────────────────────
_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_DAEMON_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from a2a_executor import ApisTaskBridge  # noqa: E402
from routing import DOMAIN_ROUTES, SUPPORTED_DOMAINS  # noqa: E402

log = logging.getLogger("apis.a2a.gateway")

# ── Config ──────────────────────────────────────────────────────────────────
A2A_ENABLE = os.environ.get("APIS_A2A_ENABLE", "0") == "1"
A2A_HOST = os.environ.get("APIS_A2A_HOST", "127.0.0.1")
A2A_PORT = int(os.environ.get("APIS_A2A_PORT", "8788"))
A2A_PUBLIC_URL = os.environ.get(
    "APIS_A2A_PUBLIC_URL", f"http://{A2A_HOST}:{A2A_PORT}/"
)
A2A_REQUIRE_AUTH = os.environ.get("APIS_A2A_REQUIRE_AUTH", "1") == "1"

PROTOCOL_VERSION = "0.3.0"
AGENT_VERSION = "1.0.0"

# Capability required of a caller's agent_grant to delegate a task.
A2A_TASK_CAPABILITY = "a2a:task:create"


# ── Agent Card ──────────────────────────────────────────────────────────────


def build_agent_card(public_url: Optional[str] = None) -> dict[str, Any]:
    """
    Build the A2A Agent Card describing Apis's task-intake capability.

    The card advertises a single coarse `delegate-task` skill whose tags list
    the supported domains (derived from routing.DOMAIN_ROUTES). The internal
    domain→worker routing is intentionally not exposed, so the external
    contract stays stable as routing changes.
    """
    url = (public_url or A2A_PUBLIC_URL).rstrip("/") + "/"
    domains = SUPPORTED_DOMAINS
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": "Ateles Apis",
        "description": (
            "Task-intake gateway for the Ateles agent swarm. Delegate a task "
            "and Apis routes it to the appropriate specialist agent "
            "(engineering, ops, finance, comms, and more)."
        ),
        "url": url,
        "version": AGENT_VERSION,
        "provider": {
            "organization": "Ateles",
            "url": "https://github.com/markmhendrickson/ateles",
        },
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "securitySchemes": {
            "bearer": {
                "type": "http",
                "scheme": "bearer",
                "description": (
                    "Bearer token issued by the Ateles operator. The token "
                    "identifies the calling agent, which must hold an active "
                    f"agent_grant with the {A2A_TASK_CAPABILITY!r} capability."
                ),
            }
        },
        "security": [{"bearer": []}],
        "skills": [
            {
                "id": "delegate-task",
                "name": "Delegate a task to the Ateles swarm",
                "description": (
                    "Submit a task in natural language. Apis infers its domain "
                    "and dispatches it to the matching specialist agent. "
                    "Returns a task id you can poll for status."
                ),
                "tags": ["delegation", "orchestration", *domains],
                "examples": [
                    "Fix the failing CI build on the docker step.",
                    "Draft a newsletter announcing the new feature.",
                    "Pay the May studio rent invoice.",
                ],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
            }
        ],
    }


# ── Card signing (JWS, ES256 over canonical card) ───────────────────────────


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _canonicalize(card: dict[str, Any]) -> bytes:
    """Deterministic JSON serialization for signing (sorted keys, no spaces)."""
    return json.dumps(card, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_agent_card(card: dict[str, Any], signer: Any = None) -> dict[str, Any]:
    """
    Attach a JWS (ES256) signature over the canonicalized card so receivers can
    verify Ateles domain ownership (A2A v1.0 signed-card feature).

    Reuses Apis's existing P-256 keypair via lib.daemon_runtime.AAuthSigner
    when ``signer`` is None. If the keypair is not minted (stub) or signing is
    unavailable, the card is returned unsigned (a valid, if unverifiable, card)
    and a warning is logged — the gateway still functions.
    """
    try:
        if signer is None:
            from lib.daemon_runtime import AAuthSigner

            signer = AAuthSigner.from_key_file("apis")
        priv = getattr(signer, "_private_key", None)
        if priv is None:
            log.warning("Apis AAuth keypair not minted — serving unsigned Agent Card")
            return card

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils

        protected = {"alg": "ES256", "kid": getattr(signer, "key_id", ""), "typ": "JWS"}
        protected_b64 = _b64url(_canonicalize(protected))
        payload_b64 = _b64url(_canonicalize(card))
        signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")

        der_sig = priv.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der_sig)
        raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")

        signed = dict(card)
        signed["signatures"] = [
            {"protected": protected_b64, "signature": _b64url(raw_sig)}
        ]
        return signed
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Agent Card signing failed (%s) — serving unsigned", exc)
        return card


# ── Caller authorization ────────────────────────────────────────────────────


def authorize_caller(
    caller_sub: str,
    *,
    require_auth: Optional[bool] = None,
    grant_checker_factory: Any = None,
) -> tuple[bool, str]:
    """
    Decide whether ``caller_sub`` may delegate a task.

    Returns (allowed, reason). When auth is not required, always allows. When
    required, the caller must resolve to an active agent_grant carrying the
    ``a2a:task:create`` capability. ``grant_checker_factory`` is an injection
    seam for tests: a callable ``(aauth_sub) -> object`` exposing ``.is_active``
    and ``.has_capability(cap)`` (matching lib.daemon_runtime.AgentGrant).

    Grant enforcement is advisory in the current phase (mirrors grant_checker's
    own staging): a missing/unreachable checker logs and allows, rather than
    hard-blocking. Hard-block lands with the PS-layer AAuth integration.
    """
    require = A2A_REQUIRE_AUTH if require_auth is None else require_auth
    if not require:
        return True, "auth_not_required"
    if not caller_sub:
        return False, "missing_caller_identity"

    try:
        if grant_checker_factory is None:
            from lib.daemon_runtime import GrantChecker

            grant = GrantChecker(caller_sub).load()
        else:
            grant = grant_checker_factory(caller_sub)
    except Exception as exc:
        log.warning("grant check unavailable for %s (%s) — allowing (advisory)",
                    caller_sub, exc)
        return True, "grant_check_unavailable_advisory"

    if not getattr(grant, "is_active", False):
        return False, "grant_not_active"
    if not grant.has_capability(A2A_TASK_CAPABILITY):
        return False, "missing_capability"
    return True, "ok"


# ── SDK transport (thin, lazily imported) ───────────────────────────────────


def serve(bridge: Optional[ApisTaskBridge] = None) -> None:
    """
    Start the A2A server backed by ``bridge`` (default: a fresh ApisTaskBridge).

    Imports `a2a-sdk` lazily so the rest of this module stays importable (and
    testable) without it. Raises RuntimeError with install guidance if the SDK
    is missing or A2A is not enabled.
    """
    if not A2A_ENABLE:
        raise RuntimeError(
            "A2A gateway disabled. Set APIS_A2A_ENABLE=1 to start it."
        )

    try:
        import uvicorn
        from a2a.server.agent_execution import AgentExecutor, RequestContext
        from a2a.server.apps import A2AStarletteApplication
        from a2a.server.events import EventQueue
        from a2a.server.request_handlers import DefaultRequestHandler
        from a2a.server.tasks import InMemoryTaskStore
        from a2a.utils import new_agent_text_message
    except ImportError as exc:
        raise RuntimeError(
            "a2a-sdk (and uvicorn) are required to serve the A2A gateway. "
            "Install with: pip install a2a-sdk uvicorn. Original error: " f"{exc}"
        ) from exc

    bridge = bridge or ApisTaskBridge()
    card = sign_agent_card(build_agent_card())

    class _ApisExecutor(AgentExecutor):  # pragma: no cover - requires SDK
        async def execute(
            self, context: RequestContext, event_queue: EventQueue
        ) -> None:
            # Normalize inbound parts to dicts for the SDK-agnostic bridge.
            parts: list[dict[str, Any]] = []
            msg = getattr(context, "message", None)
            for p in getattr(msg, "parts", []) or []:
                root = getattr(p, "root", p)
                text = getattr(root, "text", None)
                if text:
                    parts.append({"kind": "text", "text": text})

            caller = getattr(context, "caller_id", "") or ""
            allowed, reason = authorize_caller(caller)
            if not allowed:
                await event_queue.enqueue_event(
                    new_agent_text_message(f"Rejected: {reason}")
                )
                return

            result = bridge.submit(parts, caller=caller)
            if result.ok:
                text = (
                    f"Task accepted (id={result.a2a_task_id}, "
                    f"neotoma={result.neotoma_entity_id}, "
                    f"routed_to={result.skill or 'unrouted'})."
                )
            else:
                text = f"Task submission failed: {result.error}"
            await event_queue.enqueue_event(new_agent_text_message(text))

        async def cancel(
            self, context: RequestContext, event_queue: EventQueue
        ) -> None:
            await event_queue.enqueue_event(
                new_agent_text_message("Cancellation is not supported.")
            )

    handler = DefaultRequestHandler(
        agent_executor=_ApisExecutor(),
        task_store=InMemoryTaskStore(),
    )
    app = A2AStarletteApplication(agent_card=card, http_handler=handler)

    log.info("Apis A2A gateway listening on %s:%s (public=%s)",
             A2A_HOST, A2A_PORT, A2A_PUBLIC_URL)
    uvicorn.run(app.build(), host=A2A_HOST, port=A2A_PORT)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    serve()
