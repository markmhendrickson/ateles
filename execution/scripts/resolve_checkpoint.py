#!/usr/bin/env python3
"""Operator CLI to approve or reject swarm checkpoint_briefs.

Signs the exact RFC 9421 headers with the resolver private JWK, then submits
them through the MCP server's ``_resolve_checkpoint`` verification path. The
private key never leaves this process and is never printed.

Usage:
    python3 execution/scripts/resolve_checkpoint.py \\
        <checkpoint_id> approve|reject --key <path-to-resolver-private-jwk>

    python3 execution/scripts/resolve_checkpoint.py \\
        --batch <file> approve|reject --key <path-to-resolver-private-jwk>
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

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

ENV_KEY_PATH = "ATELES_CHECKPOINT_RESOLVER_JWK"
TITLE_MAX = 60
BATCH_ITEM_DELAY_SECONDS = 2.0

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


class ResolveError(Exception):
    """Operator-visible refusal; message must never carry secrets."""


def _truncate_title(title: object) -> str:
    text = " ".join(str(title or "").split())
    if len(text) <= TITLE_MAX:
        return text
    return text[: TITLE_MAX - 1] + "…"


def _resolve_key_path(explicit: str | None) -> Path:
    raw = (explicit or os.environ.get(ENV_KEY_PATH) or "").strip()
    if not raw:
        raise ResolveError(
            f"resolver key path required via --key or {ENV_KEY_PATH}"
        )
    path = Path(raw).expanduser()
    if not path.is_file():
        raise ResolveError("resolver key path is not a readable file")
    return path


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
    if ateles_server._entity_type_of(data) != "checkpoint_brief":  # vocab-ok: the stored entity type is still checkpoint_brief; the server checks the same literal
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
    key_path: Path,
    required_sub: str,
    required_jkt: str,
) -> dict[str, str]:
    try:
        return sign_checkpoint_resolution(
            checkpoint_id,
            action,
            private_jwk_path=key_path,
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
    label = _truncate_title(title) or "(no title)"
    if failed:
        reason = detail or "resolution failed"
        print(f"{checkpoint_id} {label} -> FAILED ({reason})")
    else:
        print(f"{checkpoint_id} {label} -> {status}")


class ReportedResolveError(ResolveError):
    """ResolveError whose operator-facing result line was already printed."""


def dry_run_one(checkpoint_id: str) -> None:
    _data, snap, _sub, _jkt = load_checkpoint(checkpoint_id)
    title = _truncate_title(snap.get("title")) or "(no title)"
    task = snap.get("task_entity_id") or "(none)"
    blast = snap.get("blast_radius") or "(none)"
    print(
        f"{checkpoint_id} title={title} task={task} blast_radius={blast}"
    )


def resolve_one(
    checkpoint_id: str,
    action: str,
    *,
    key_path: Path,
) -> None:
    """Resolve one checkpoint. Raises ResolveError on refusal or failed effect."""
    try:
        _data, snap, required_sub, required_jkt = load_checkpoint(checkpoint_id)
    except ResolveError as exc:
        _print_result_line(
            checkpoint_id, "", "", failed=True, detail=str(exc)
        )
        raise ReportedResolveError(str(exc)) from exc

    title = snap.get("title", "")
    expected = "approved" if action == "approve" else "rejected"

    try:
        headers = _sign_headers(
            checkpoint_id,
            action,
            key_path=key_path,
            required_sub=required_sub,
            required_jkt=required_jkt,
        )
    except ResolveError as exc:
        _print_result_line(
            checkpoint_id, title, "", failed=True, detail=str(exc)
        )
        raise ReportedResolveError(str(exc)) from exc

    result = _submit_resolution(checkpoint_id, action, headers)
    if isinstance(result, dict) and result.get("error"):
        detail = str(result["error"])
        _print_result_line(
            checkpoint_id, title, "", failed=True, detail=detail
        )
        raise ReportedResolveError(detail)

    status = _read_back_status(checkpoint_id)
    if status != expected:
        detail = (
            f"read-back status is '{status or '(empty)'}', expected '{expected}'"
        )
        _print_result_line(
            checkpoint_id, title, status, failed=True, detail=detail
        )
        raise ReportedResolveError(detail)

    _print_result_line(checkpoint_id, title, status)


def _confirm_batch(count: int, action: str) -> None:
    prompt = f"Resolve {count} checkpoint(s) with action '{action}'? [y/N] "
    try:
        answer = input(prompt)
    except EOFError as exc:
        raise ResolveError("batch confirmation required") from exc
    if answer.strip().lower() not in {"y", "yes"}:
        raise ResolveError("batch confirmation declined")


def _require_yes_tty(*, dry_run: bool, yes: bool) -> None:
    if yes and not dry_run and not sys.stdin.isatty():
        raise ResolveError("--yes requires a TTY unless --dry-run")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Approve or reject swarm checkpoint_briefs using the operator "
            "resolver JWK. Never prints key material or signed headers."
        )
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
        "--key",
        metavar="PATH",
        help=(
            f"path to resolver private JWK "
            f"(or set {ENV_KEY_PATH}; no default path)"
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip interactive batch confirmation (requires a TTY)",
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
        _require_yes_tty(dry_run=args.dry_run, yes=args.yes)

        if args.batch:
            ids = _read_batch_ids(Path(args.batch).expanduser())
        else:
            ids = [args.checkpoint_id]

        key_path: Path | None = None
        if not args.dry_run:
            key_path = _resolve_key_path(args.key)

        if args.batch and not args.dry_run and not args.yes:
            if not sys.stdin.isatty():
                raise ResolveError(
                    "batch confirmation requires a TTY "
                    "(use --yes on a TTY, or --dry-run)"
                )
            _confirm_batch(len(ids), args.action)

        done = 0
        skipped = 0
        failed = 0

        for index, checkpoint_id in enumerate(ids):
            if index > 0 and not args.dry_run:
                time.sleep(BATCH_ITEM_DELAY_SECONDS)
            try:
                if args.dry_run:
                    dry_run_one(checkpoint_id)
                    skipped += 1
                else:
                    assert key_path is not None
                    resolve_one(checkpoint_id, args.action, key_path=key_path)
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

        if args.batch or len(ids) > 1:
            print(f"done={done} skipped={skipped} failed={failed}")

        return EXIT_OK if failed == 0 else EXIT_FAILURE
    except ResolveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
