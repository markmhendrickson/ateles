#!/usr/bin/env python3
"""Operator CLI to approve or reject swarm checkpoint_briefs.

Signs the exact RFC 9421 headers with the resolver private JWK, then submits
them through the MCP server's ``_resolve_checkpoint`` verification path. The
private JWK stays in a 1Password item field. Each real run calls
``op signout --all`` and then ``op read --no-newline`` after confirmation,
requiring a fresh 1Password CLI authorization. The key exists only in this
process's memory while signing and is never printed, logged, or written.

In 1Password, store the complete resolver private JWK in a concealed field,
enable the desktop app's CLI integration, and copy that field's ``op://``
secret reference. Pass only the reference:

Usage:
    execution/scripts/resolve_checkpoint.sh \\
        <checkpoint_id> approve|reject --key-ref 'op://<vault>/<item>/<field>'

    execution/scripts/resolve_checkpoint.sh \\
        --batch <file> approve|reject \\
        --key-ref 'op://<vault>/<item>/<field>'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Mapping

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MCP_DIR = _REPO_ROOT / "execution" / "mcp" / "ateles"
for _path in (_REPO_ROOT, _MCP_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from lib.daemon_runtime.aauth_httpsig import AAuthSigningError  # noqa: E402
from lib.daemon_runtime.checkpoint_resolution import (  # noqa: E402
    sign_checkpoint_resolution,
)

import server as ateles_server  # noqa: E402

ENV_KEY_REF = "ATELES_CHECKPOINT_RESOLVER_OP_REF"
TITLE_MAX = 60
BATCH_ITEM_DELAY_SECONDS = 2.0
OP_ENV_ALLOWLIST = (
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "LANG",
    "TERM",
)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


class ResolveError(Exception):
    """Operator-visible refusal; message must never carry secrets."""


def _sanitize_terminal_text(value: object) -> str:
    """Escape characters that can alter terminal state or displayed direction."""
    safe: list[str] = []
    for character in str(value):
        if unicodedata.category(character) in {"Cc", "Cf"}:
            codepoint = ord(character)
            if codepoint <= 0xFFFF:
                safe.append(f"\\u{codepoint:04x}")
            else:
                safe.append(f"\\U{codepoint:08x}")
        else:
            safe.append(character)
    return "".join(safe)


def _truncate_title(title: object) -> str:
    text = " ".join(_sanitize_terminal_text(title or "").split())
    if len(text) <= TITLE_MAX:
        return text
    return text[: TITLE_MAX - 1] + "…"


def _resolve_key_ref(explicit: str | None) -> str:
    raw = (explicit or os.environ.get(ENV_KEY_REF) or "").strip()
    if not raw:
        raise ResolveError(
            f"resolver key reference required via --key-ref or {ENV_KEY_REF}"
        )
    if not raw.startswith("op://") or raw == "op://":
        raise ResolveError("resolver key reference must be an op:// reference")
    return raw


def _forbidden_op_auth_environment_names(
    environ: Mapping[str, str],
) -> list[str]:
    return sorted(
        name
        for name in environ
        if name == "OP_SERVICE_ACCOUNT_TOKEN"
        or name.startswith("OP_CONNECT_")
        or name.startswith("OP_SESSION_")
    )


def _require_biometric_only_op_environment() -> None:
    forbidden = _forbidden_op_auth_environment_names(os.environ)
    if forbidden:
        raise ResolveError(
            "refusing 1Password access while "
            f"{forbidden[0]} is present in the caller environment"
        )


def _op_environment() -> dict[str, str]:
    """Return the complete, minimal environment inherited by every op child."""
    return {name: os.environ[name] for name in OP_ENV_ALLOWLIST if name in os.environ}


def _clear_completed_output(completed: subprocess.CompletedProcess[str]) -> None:
    completed.stdout = ""
    completed.stderr = ""


def _sign_out_1password() -> None:
    """Invalidate cached CLI sessions before the one permitted key read."""
    try:
        completed = subprocess.run(
            ["op", "signout", "--all"],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            env=_op_environment(),
        )
    except OSError:
        raise ResolveError(
            "could not clear cached 1Password CLI sessions (op exit code unavailable)"
        ) from None

    try:
        exit_code = completed.returncode
    finally:
        _clear_completed_output(completed)
    if exit_code != 0:
        raise ResolveError(
            f"could not clear cached 1Password CLI sessions (op exit code {exit_code})"
        )


def _read_key_from_1password(key_ref: str) -> dict[str, Any]:
    """Read and parse the resolver JWK without exposing 1Password output."""
    try:
        completed = subprocess.run(
            ["op", "read", "--no-newline", key_ref],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            env=_op_environment(),
        )
    except OSError:
        raise ResolveError(
            "could not read resolver key from 1Password (op exit code unavailable)"
        ) from None

    try:
        exit_code = completed.returncode
        if exit_code != 0:
            raise ResolveError(
                f"could not read resolver key from 1Password (op exit code {exit_code})"
            )
        try:
            key = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            raise ResolveError(
                f"could not read resolver key from 1Password (op exit code {exit_code})"
            ) from None
    finally:
        _clear_completed_output(completed)

    if not isinstance(key, dict):
        raise ResolveError(
            f"could not read resolver key from 1Password (op exit code {exit_code})"
        )
    return key


def _read_batch_ids(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ResolveError(f"could not read batch file: {exc.strerror}") from exc
    ids: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        ids.append(stripped)
    if not ids:
        raise ResolveError("batch file contains no checkpoint ids")
    return ids


def load_checkpoint(checkpoint_id: str) -> tuple[dict, dict, str, str]:
    """Load a resolvable checkpoint and its required resolver authority.

    Uses the same Neotoma read helpers and authority extraction as the MCP
    server. Returns ``(entity, snapshot, required_sub, required_jkt)``.
    """
    data = ateles_server._get(f"/entities/{checkpoint_id}")
    if data is None:
        raise ResolveError(
            f"checkpoint {checkpoint_id} not found or Neotoma unreachable"
        )
    if (
        ateles_server._entity_type_of(data) != "checkpoint_brief"
    ):  # vocab-ok: the stored entity type is still checkpoint_brief; the server checks the same literal
        raise ResolveError(f"entity {checkpoint_id} is not a checkpoint_brief")

    snap = ateles_server._snapshot_of(data)
    status = str(snap.get("status", "")).strip().lower()
    if status != "awaiting_operator":
        raise ResolveError(
            f"checkpoint is '{status or '(empty)'}', not 'awaiting_operator'"
        )

    authority = ateles_server._checkpoint_resolver_authority(checkpoint_id, data)
    if authority is None:
        raise ResolveError(
            "checkpoint has no authenticated required-resolver authority"
        )
    required_sub, required_jkt, _tenant = authority
    return data, snap, required_sub, required_jkt


def _sign_headers(
    checkpoint_id: str,
    action: str,
    *,
    private_jwk: dict[str, Any],
    required_sub: str,
    required_jkt: str,
) -> dict[str, str]:
    try:
        return sign_checkpoint_resolution(
            checkpoint_id,
            action,
            private_jwk=private_jwk,
            expected_sub=required_sub,
            expected_jkt=required_jkt,
            issuer=ateles_server.CHECKPOINT_RESOLVER_ISSUER,
            neotoma_base_url=ateles_server.NEOTOMA_BASE_URL,
        )
    except AAuthSigningError as exc:
        # Signer messages name the problem class only — never key material.
        raise ResolveError(str(exc)) from exc


def _submit_resolution(
    checkpoint_id: str,
    action: str,
    headers: dict[str, str],
) -> dict:
    return asyncio.run(
        ateles_server._resolve_checkpoint(
            checkpoint_id,
            action,
            resolver_aauth_headers=headers,
        )
    )


def _read_back_status(checkpoint_id: str) -> str:
    data = ateles_server._get(f"/entities/{checkpoint_id}")
    if data is None:
        return ""
    snap = ateles_server._snapshot_of(data)
    return str(snap.get("status", "")).strip().lower()


def _print_result_line(
    checkpoint_id: str,
    title: object,
    status: str,
    *,
    failed: bool = False,
    detail: str | None = None,
) -> None:
    safe_checkpoint_id = _sanitize_terminal_text(checkpoint_id)
    label = _truncate_title(title) or "(no title)"
    safe_status = _sanitize_terminal_text(status)
    if failed:
        reason = _sanitize_terminal_text(detail or "resolution failed")
        print(f"{safe_checkpoint_id} {label} -> FAILED ({reason})")
    elif detail:
        safe_detail = _sanitize_terminal_text(detail)
        print(f"{safe_checkpoint_id} {label} -> {safe_status} ({safe_detail})")
    else:
        print(f"{safe_checkpoint_id} {label} -> {safe_status}")


class ReportedResolveError(ResolveError):
    """ResolveError whose operator-facing result line was already printed."""


def dry_run_one(checkpoint_id: str) -> None:
    _data, snap, _sub, _jkt = load_checkpoint(checkpoint_id)
    title = _truncate_title(snap.get("title")) or "(no title)"
    safe_checkpoint_id = _sanitize_terminal_text(checkpoint_id)
    task = _sanitize_terminal_text(snap.get("task_entity_id") or "(none)")
    blast = _sanitize_terminal_text(snap.get("blast_radius") or "(none)")
    print(f"{safe_checkpoint_id} title={title} task={task} blast_radius={blast}")


def resolve_one(
    checkpoint_id: str,
    action: str,
    *,
    private_jwk: dict[str, Any],
) -> None:
    """Resolve one checkpoint. Raises ResolveError on refusal or failed effect."""
    try:
        _data, snap, required_sub, required_jkt = load_checkpoint(checkpoint_id)
    except ResolveError as exc:
        _print_result_line(checkpoint_id, "", "", failed=True, detail=str(exc))
        raise ReportedResolveError(str(exc)) from exc

    title = snap.get("title", "")
    expected = "approved" if action == "approve" else "rejected"

    try:
        headers = _sign_headers(
            checkpoint_id,
            action,
            private_jwk=private_jwk,
            required_sub=required_sub,
            required_jkt=required_jkt,
        )
    except ResolveError as exc:
        _print_result_line(checkpoint_id, title, "", failed=True, detail=str(exc))
        raise ReportedResolveError(str(exc)) from exc

    result = _submit_resolution(checkpoint_id, action, headers)
    if isinstance(result, dict) and result.get("error"):
        detail = str(result["error"])
        _print_result_line(checkpoint_id, title, "", failed=True, detail=detail)
        raise ReportedResolveError(detail)

    status = _read_back_status(checkpoint_id)
    if status != expected:
        detail = f"read-back status is '{status or '(empty)'}', expected '{expected}'"
        _print_result_line(checkpoint_id, title, status, failed=True, detail=detail)
        raise ReportedResolveError(detail)

    # The server says separately whether the held task was released; an
    # approved checkpoint is not proof that the work resumed.
    action_taken = result.get("action_taken") if isinstance(result, dict) else None
    _print_result_line(
        checkpoint_id, title, status, detail=str(action_taken) if action_taken else None
    )


def _confirm(ids: list[str], action: str) -> None:
    what = (
        _sanitize_terminal_text(ids[0])
        if len(ids) == 1
        else f"{len(ids)} checkpoint(s)"
    )
    safe_action = _sanitize_terminal_text(action)
    prompt = f"Resolve {what} with action '{safe_action}'? [y/N] "
    try:
        answer = input(prompt)
    except EOFError as exc:
        raise ResolveError("confirmation required") from exc
    if answer.strip().lower() not in {"y", "yes"}:
        raise ResolveError("confirmation declined")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description=(
            "Approve or reject swarm checkpoint_briefs using the operator "
            "resolver JWK. Never prints key material or signed headers."
        ),
    )
    parser.add_argument(
        "checkpoint_id",
        nargs="?",
        help="checkpoint_brief entity id (omit when using --batch)",
    )
    parser.add_argument(
        "action",
        choices=("approve", "reject"),
        help="resolution action for every selected checkpoint",
    )
    parser.add_argument(
        "--batch",
        metavar="FILE",
        help="file of checkpoint ids (one per line; # comments and blanks ok)",
    )
    parser.add_argument(
        "--key-ref",
        metavar="OP_REFERENCE",
        help=(
            f"1Password op:// reference to the resolver private JWK "
            f"(or set {ENV_KEY_REF}; no default)"
        ),
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="in batch mode, continue after a failure",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be resolved; do not sign or submit",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return EXIT_USAGE if code not in (0, None) else EXIT_OK

    if bool(args.batch) == bool(args.checkpoint_id):
        print(
            "usage: provide exactly one of <checkpoint_id> or --batch FILE",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        if args.batch:
            ids = _read_batch_ids(Path(args.batch).expanduser())
        else:
            ids = [args.checkpoint_id]

        key_ref: str | None = None
        if args.key_ref is not None or not args.dry_run:
            key_ref = _resolve_key_ref(args.key_ref)

        if not args.dry_run:
            _require_biometric_only_op_environment()

        # Every real resolution, single or batch, is the operator's at a
        # terminal: a non-interactive process holding the key is refused.
        if not args.dry_run:
            if not sys.stdin.isatty():
                raise ResolveError(
                    "resolving a checkpoint requires a TTY "
                    "(run it yourself at a terminal, or use --dry-run)"
                )
            # Consent is bound to the exact selection: list every item read
            # from the record before asking.
            print(f"About to {args.action}:")
            for checkpoint_id in ids:
                try:
                    dry_run_one(checkpoint_id)
                except ResolveError as exc:
                    safe_id = _sanitize_terminal_text(checkpoint_id)
                    safe_error = _sanitize_terminal_text(exc)
                    print(f"{safe_id} (cannot be read: {safe_error})")
            _confirm(ids, args.action)

        private_jwk: dict[str, Any] | None = None
        if not args.dry_run:
            assert key_ref is not None
            _sign_out_1password()
            private_jwk = _read_key_from_1password(key_ref)

        done = skipped = failed = 0

        try:
            for index, checkpoint_id in enumerate(ids):
                if index > 0 and not args.dry_run:
                    time.sleep(BATCH_ITEM_DELAY_SECONDS)
                try:
                    if args.dry_run:
                        dry_run_one(checkpoint_id)
                        skipped += 1
                    else:
                        assert private_jwk is not None
                        resolve_one(
                            checkpoint_id,
                            args.action,
                            private_jwk=private_jwk,
                        )
                        done += 1
                except ResolveError as exc:
                    failed += 1
                    if not isinstance(exc, ReportedResolveError):
                        _print_result_line(
                            checkpoint_id,
                            "",
                            "",
                            failed=True,
                            detail=str(exc),
                        )
                    if args.batch and not args.keep_going:
                        break
        finally:
            private_jwk = None

        if args.batch or len(ids) > 1:
            print(f"done={done} skipped={skipped} failed={failed}")

        return EXIT_OK if failed == 0 else EXIT_FAILURE
    except ResolveError as exc:
        print(f"error: {_sanitize_terminal_text(exc)}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
