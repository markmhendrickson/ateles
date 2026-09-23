#!/usr/bin/env python3
"""PreToolUse hook — refuse operator-behalf `gh` APPROVE unless current-head all-clear.

Hazard (ateles#1181, 2026-09-23): a PR was approved on the operator's behalf
claiming four lenses had cleared the current head; none had, and security's
live verdict was a confirmed `[BLOCKING]` finding. In-place-edited verdict
comments hide staleness when judged by creation order, and "panel ran" was
treated as "panel clean." The standing rule ("current-head, all-clear, or
don't approve") lived only in prose until this hook.

This hook intercepts, at PreToolUse on Bash:

  - `gh pr review <n> --approve` / `-a`
  - `gh api …/repos/{owner}/{repo}/pulls/{n}/reviews` with `event=APPROVE`

Scoped to `markmhendrickson/ateles` and `markmhendrickson/neotoma` only.
Out-of-scope repos and non-approve `gh` calls exit 0 silently (no panel I/O).

For a matched scoped approve it resolves live `head.sha`, seats lenses via
`review_panel.select_panel` (never a hardcoded roster), and tabulates each
lens's latest-edited `<!-- review:<lens> commit=<sha> -->` marker. It blocks
when any seated lens lacks a current-head verdict, has `REQUEST_CHANGES` /
`[BLOCKING]` at head, or when verdict/panel/head data is unreadable.

Fail-closed divergence from sibling hooks (`gh_identity_guard`,
`gmail_send_gate`): those fail-open on unparseable stdin / internal errors.
This guard's safety field IS the verdict data — unreadable/missing/malformed
→ deny, never permit (docs/foundation/principles.md#5). Matched scoped
approve + bad stdin/JSON → deny with `verdict data unreadable:`.

No ambient override env. Escape hatch = human GitHub UI approve, or
temporarily unregister this hook — never a session-exported permit
(gmail-gate lesson).

Mental model: current-head all-clear, or don't approve.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from panel_verdicts import (  # noqa: E402
    Decision,
    VerdictDataUnreadable,
    evaluate,
    format_deny_reason,
    get_seated_panel,
    matches_approve_call,
    resolve_head_sha,
    tabulate_verdicts,
)


def log(msg: str) -> None:
    try:
        print(f"[gh_approve_verdict_guard] {msg}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def deny(reason: str) -> int:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    print(f"[gh_approve_verdict_guard] {reason}", file=sys.stderr)
    return 2


def _unreadable_reason(owner: str, repo: str, pr: int, head: str, cause: str) -> str:
    return format_deny_reason(
        owner,
        repo,
        pr,
        head,
        Decision(
            allowed=False,
            reason_kind="unreadable",
            unreadable_cause=cause,
        ),
    )


def _looks_like_scoped_approve(text: str) -> bool:
    """Best-effort scan when JSON is unreadable — fail closed if it smells like approve."""
    if not text:
        return False
    if matches_approve_call(text) is not None:
        return True
    # Raw garbage may still contain an approve invocation string.
    lower = text.lower()
    if "event=approve" in lower or "event\": \"approve" in lower:
        if "markmhendrickson/ateles" in lower or "markmhendrickson/neotoma" in lower:
            return True
    if ("--approve" in lower or " -a " in lower) and "pr review" in lower:
        if "markmhendrickson/ateles" in lower or "markmhendrickson/neotoma" in lower:
            return True
    return False


def main() -> int:
    raw = sys.stdin.read()
    parse_error: str | None = None
    payload: dict = {}
    try:
        loaded = json.loads(raw) if raw.strip() else {}
        if isinstance(loaded, dict):
            payload = loaded
        else:
            parse_error = "hook stdin was not a JSON object"
    except Exception as exc:  # noqa: BLE001
        parse_error = f"hook stdin JSON parse failed: {exc}"

    if parse_error:
        if _looks_like_scoped_approve(raw):
            return deny(
                _unreadable_reason(
                    "markmhendrickson",
                    "ateles",
                    0,
                    "unknown",
                    parse_error,
                )
            )
        return 0

    if payload.get("tool_name") != "Bash":
        return 0

    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str):
        return 0

    target = matches_approve_call(command)
    if target is None:
        return 0

    try:
        head = resolve_head_sha(target.owner, target.repo, target.pr_number)
        seated = get_seated_panel(target.owner, target.repo, target.pr_number)
        verdicts = tabulate_verdicts(target.owner, target.repo, target.pr_number)
        decision = evaluate(seated, verdicts, head)
    except VerdictDataUnreadable as exc:
        log(f"blocking approve on {target.full_name}#{target.pr_number}")
        return deny(
            _unreadable_reason(
                target.owner,
                target.repo,
                target.pr_number,
                "unknown",
                exc.cause,
            )
        )
    except Exception as exc:  # noqa: BLE001 — fail closed on matched approve
        log(f"internal error on matched approve, failing closed: {exc}")
        return deny(
            _unreadable_reason(
                target.owner,
                target.repo,
                target.pr_number,
                "unknown",
                f"internal error: {exc}",
            )
        )

    if decision.allowed:
        return 0

    reason = format_deny_reason(
        target.owner,
        target.repo,
        target.pr_number,
        head,
        decision,
    )
    log(f"blocking approve on {target.full_name}#{target.pr_number}")
    return deny(reason)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f"internal error before/without match, failing open: {exc}")
        sys.exit(0)
