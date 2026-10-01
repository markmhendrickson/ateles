#!/usr/bin/env python3
"""Shared helpers for the Ateles session-integrity hooks.

Implements layer 1 of docs/session_integrity.md (the Claude Code lifecycle
hooks). These hooks are MECHANICAL and FAIL-OPEN: any unexpected error, a
missing bearer token, or an unreachable Neotoma must never crash a session
or block a clean exit on a false negative. Enforcement (the Stop finalizer)
blocks ONLY when it can positively determine the session is write-bearing
and unlinked/turn-less.

Design contract (see docs/session_integrity.md):
  - A session is "write-bearing" if it issued any non-bookkeeping Neotoma
    write. We approximate this from the transcript by detecting any Neotoma
    store/correct/create_relationship/submit_* tool use whose entity_type is
    not purely conversation/conversation_message bookkeeping.
  - An integral session has (a) >=1 bound plan and (b) >=1 stored turn pair.
  - Genuine no-op sessions (no writes) are exempt — never blocked (grace path).

State is kept in a per-session JSON file under the project .claude/.session_state/
keyed by session_id, so SessionStart / Stop share context without re-parsing.

No third-party imports — stdlib only (urllib), so the hook runs in any
environment without a venv.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# SUPERSEDED for new work on 2026-09-21 by operator decision: this plan accepts
# no new todos, decisions, or tasks. It is deliberately NOT deleted — principles.md
# cites three of its decision keys by key (operator_only_is_never_auto_executable_
# not_merely_high_blast and unclassified_action_type_fails_closed_and_loudly at
# invariant 5; release_terminal_status_must_be_read_back at invariant 2), so the id
# must stay resolvable.
#
# It is kept HERE only as a substring probe for _mentions_plan() below — a session
# that touches this plan is still touching a plan. It is no longer a binding
# default: binding every session to one hardcoded plan regardless of workstream is
# the exact mis-binding the 2026-09-21 decision was made to prevent.
SUPERSEDED_PLAN_ID = "ent_99ace4dd6673aa36ed08b1fe"  # read-only; see above
FOUNDATION_PLAN_ID = "ent_81aadb43caf2fa493361e8ed"  # plan:lay-the-foundation-master
BOOKKEEPING_TYPES = {"conversation", "conversation_message", "agent_message"}
# Durable insight artifacts whose presence means the session captured a learning
# (used by the Stop hook's /end nudge — task #3 of the task-spine plan).
LEARNING_TYPES = {
    "learning", "note", "standing_rule", "architectural_decision",
    "strategy_drift_signal", "lesson", "recap_message",
}
BEARER_ENV = "NEOTOMA_BEARER_TOKEN"  # gitleaks:allow
_NEOTOMA_ENV_PATH = Path.home() / ".config" / "neotoma" / ".env"


def log(msg: str) -> None:
    """Diagnostic to stderr — never pollutes hook stdout (which is context/JSON)."""
    sys.stderr.write(f"[session-integrity] {msg}\n")


# ---------------------------------------------------------------------------
# Neotoma credentials (ateles#1261 follow-up, audit ent_b66293f0dcc8c887d4fdbeae)
# ---------------------------------------------------------------------------
#
# stop_finalizer.py skipped ALL 45 runs in the audited session with "no
# bearer token" — the Stop hook's environment simply never carries
# NEOTOMA_BEARER_TOKEN, so the session-integrity layer never ran at all for
# that session's whole lifetime. `emit_harness_event_raw` used to read only
# `os.environ.get(BEARER_ENV)` and silently return when it was empty.
#
# `neotoma_credentials()` is the ONE place that changes: a complete env pair
# first, then a complete pair from `~/.config/neotoma/.env` (same file, same two keys,
# `execution/scripts/sync_skills.py`'s `_load_env` and
# `execution/scripts/session_language.py`'s `_bearer_token` already read this
# way — reusing that shape rather than inventing a third). A partial source is
# never completed from another source: without an explicit verified binding,
# an endpoint and bearer are safe to use only as the pair they were configured
# as. Parses only NEOTOMA_BASE_URL / NEOTOMA_BEARER_TOKEN; never logs either
# value.
#
# Read at CALL time, not import time — a test (or a long-lived process) that
# changes the environment or the file between calls must see the change.


def _read_neotoma_env_file() -> dict[str, str]:
    """Parse `NEOTOMA_BASE_URL` / `NEOTOMA_BEARER_TOKEN` out of
    ~/.config/neotoma/.env, if it exists and is readable. Never raises —
    an absent, unreadable, malformed, or non-UTF-8 file yields {} (fail
    open; the caller falls back to the process environment, which may also
    be empty — that is the "still missing" case the warning covers, not a
    condition this function itself needs to distinguish).

    TWO independent layers make "never raises" true rather than merely
    documented (Loxia review + qa/security lenses on PR #1298, round 1: the
    original version caught only `OSError`, so a non-UTF-8-encoded `.env`
    raised an unhandled `UnicodeDecodeError` — a `ValueError` subclass, not
    an `OSError` — straight out of this function and every caller above it,
    including `emit_harness_event_raw`, the shared chokepoint the whole PR
    exists to make robust. Confirmed reproducible by both lenses, not
    hypothesized):

      1. `errors="replace"` on `read_text` — a decode error becomes U+FFFD
         replacement characters in the offending line rather than a raised
         exception, so malformed encoding degrades the SAME way a malformed
         line already does (skipped or read as best-effort garbage that
         fails the key check below), not a crash.
      2. `except Exception`, not `except OSError` — belt and suspenders for
         (1): this function's only job is "return a best-effort dict or
         empty," so nothing it can do file I/O or string processing over
         should ever propagate past it. A bug INSIDE this function is still
         a bug, but it must never be the reason a Stop hook crashes a
         session — that guarantee belongs at this boundary, not left to
         whichever caller happens to wrap it (today's callers both do, by
         a top-level `main()` guard and a local `try/except`, but neither
         is part of THIS function's contract and a future caller should not
         have to know that to trust "never raises").
    """
    found: dict[str, str] = {}
    try:
        if not _NEOTOMA_ENV_PATH.exists():
            return found
        text = _NEOTOMA_ENV_PATH.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key not in ("NEOTOMA_BASE_URL", BEARER_ENV):
                continue
            found[key] = value.strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001 — this function's whole contract is "never raises"
        return {}
    return found


def neotoma_credentials() -> tuple[str, str]:
    """Return one complete, provenance-bound ``(base_url, token)`` pair.

    A complete environment pair wins. Otherwise a complete dotenv pair is
    used. Partial pairs are ignored rather than combined across sources; this
    module has no verified binding that proves an endpoint from one source and
    a bearer from another belong to the same Neotoma instance. When neither
    source is complete, return two empty strings so callers cannot accidentally
    issue a cross-source request.
    """
    env_pair = (
        os.environ.get("NEOTOMA_BASE_URL", "").strip(),
        os.environ.get(BEARER_ENV, "").strip(),
    )
    if all(env_pair):
        return env_pair

    from_file = _read_neotoma_env_file()
    file_pair = (
        from_file.get("NEOTOMA_BASE_URL", "").strip(),
        from_file.get(BEARER_ENV, "").strip(),
    )
    if all(file_pair):
        return file_pair

    return "", ""


def missing_credentials_warning_text(log_tag: str) -> str:
    """Pure text builder for the missing-credentials notice — no I/O, no
    dedup state. Shared by the stderr path (`_warn_missing_credentials_once`)
    and any caller that presents the notice on a different surface (e.g.
    `stop_finalizer.py`'s `systemMessage`, round-4 UX finding: the prior
    design marked the notice "delivered" the instant stderr was written,
    even though a normal successful Stop has no visible stderr and the
    harness never feeds stderr back into the session)."""
    return (
        f"[{log_tag}] WARNING: no complete NEOTOMA_BASE_URL / "
        "NEOTOMA_BEARER_TOKEN pair was found in either the environment or "
        "~/.config/neotoma/.env — harness_event audit emission is being "
        "skipped. Stop-hook enforcement still runs from local session "
        "evidence and may warn or block according to "
        "ATELES_SESSION_INTEGRITY_ENFORCE. Set both values in the same "
        "source to restore audit emission."
    )


def credentials_warning_already_delivered(session_id: str) -> bool:
    """True once this session's missing-credentials notice has been
    delivered on ANY surface (stderr or a caller-owned presentation such as
    `systemMessage`). Read-only — does not mark anything delivered."""
    state = load_state(session_id) if session_id else {}
    return bool(state.get("warned_missing_neotoma_credentials"))


def mark_credentials_warning_delivered(session_id: str) -> None:
    """Record that this session's missing-credentials notice has been
    delivered. Callers must invoke this ONLY after the delivery itself
    succeeded (e.g. after `print()` for a `systemMessage` returns without
    raising) — marking it before a presentation attempt can fail is exactly
    the round-4 UX defect this split exists to close: a failed stdout write
    must not consume the one-shot notice, or the operator never sees it."""
    if not session_id:
        return
    state = load_state(session_id)
    state["warned_missing_neotoma_credentials"] = True
    save_state(session_id, state)


def _warn_missing_credentials_once(session_id: str, log_tag: str) -> None:
    """One VISIBLE stderr warning per session when no complete pair exists
    after checking both the environment and the dotenv fallback —
    replacing the silent per-invocation skip audit ent_b66293f0dcc8c887d4fdbeae
    found (45 skips, zero visible warnings, nothing stored or checked for the
    whole session). Tracked in this session's state file so a Stop hook that
    fires more than once (multiple sibling hooks, or a retried Stop) warns
    exactly once rather than once per hook per stop.

    This is the DEFAULT stderr presentation, used directly by
    `emit_harness_event_raw` (and so by `decision_shape_gate.py`, which has
    no `systemMessage`-capable surface). `stop_finalizer.py` presents the
    SAME text via `systemMessage` instead — see
    `missing_credentials_warning_text` / `mark_credentials_warning_delivered`
    — and marks delivery itself before calling into this function, so within
    ONE hook invocation the notice reaches only one surface.

    NOT an atomic cross-process guarantee: `stop_finalizer.py` and
    `decision_shape_gate.py` are two independent Stop-hook subprocesses
    (`.claude/settings.json`, same `Stop` matcher) that each do their own
    read-modify-write of the shared per-session state file with no lock. If
    both happen to run concurrently on the same missing-credentials session,
    both can read "not yet delivered" before either writes its mark, and the
    notice reaches both surfaces for that one session. This degrades to a
    harmless duplicate notice, never a lost one — the failure mode this
    dedup exists to prevent is silence, not a double print — so it is left
    unfixed rather than adding file locking for a cosmetic duplicate.
    """
    if credentials_warning_already_delivered(session_id):
        return
    sys.stderr.write(missing_credentials_warning_text(log_tag) + "\n")
    mark_credentials_warning_delivered(session_id)


def read_hook_input() -> dict:
    """Claude Code passes a JSON event object on stdin. Fail-open to {}.

    The harness contract is always a JSON object, but `json.loads` will
    happily parse a top-level array/string/number/null too — every caller
    immediately does `ev.get(...)`, so a non-dict result must be coerced to
    `{}` here rather than left to raise `AttributeError` in six different
    call sites. Found while adding `test_decision_shape_gate.py`: the fix
    was first patched locally into that one hook's own stdin-parsing inline
    copy, which is the "extend the mechanism that already generalizes, not a
    parallel one" mistake CLAUDE.md names — moved here so every caller of
    this shared helper gets it, not just one.
    """
    try:
        raw = sys.stdin.read()
        ev = json.loads(raw) if raw.strip() else {}
        return ev if isinstance(ev, dict) else {}
    except Exception as exc:  # noqa: BLE001 — fail open
        log(f"could not parse hook stdin: {exc}")
        return {}


def state_dir() -> Path:
    # CLAUDE_PROJECT_DIR is set by Claude Code but by no other harness (Codex
    # sets neither it nor an equivalent). Falling back to cwd would key this
    # hook's state on whatever directory a Codex hook happened to run from —
    # unstable across invocations rather than tied to the checkout this file
    # ships in. Resolve from THIS FILE's own location instead, matching
    # session_rule_index.py's "never cwd/CLAUDE_PROJECT_DIR" rule, so a
    # harness that never sets the env var still gets one stable state root.
    root = os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parent.parent.parent
    d = Path(root) / ".claude" / ".session_state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path(session_id: str) -> Path:
    safe = "".join(c for c in (session_id or "unknown") if c.isalnum() or c in "-_")
    return state_dir() / f"{safe or 'unknown'}.json"


def load_state(session_id: str) -> dict:
    p = state_path(session_id)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_state(session_id: str, state: dict) -> None:
    try:
        state_path(session_id).write_text(json.dumps(state))
    except Exception as exc:  # noqa: BLE001
        log(f"could not persist state: {exc}")


# ---------------------------------------------------------------------------
# Transcript inspection — the source of truth for "did this session write?"
# ---------------------------------------------------------------------------
def scan_transcript(transcript_path: str | None) -> dict:
    """Walk the JSONL transcript and summarize integrity-relevant signals.

    Returns: {turns: int, wrote_domain: bool, bound_plan: bool, write_types: set}
    Fail-open: an unreadable transcript yields a conservative no-op summary
    (turns=0, wrote_domain=False) so the finalizer does not block on it.
    """
    summary = {
        "turns": 0, "wrote_domain": False, "bound_plan": False,
        "bound_task": False, "captured_learning": False, "write_types": set(),
    }
    if not transcript_path or not os.path.exists(transcript_path):
        return summary
    try:
        with open(transcript_path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                _inspect_event(ev, summary)
    except Exception as exc:  # noqa: BLE001
        log(f"transcript scan failed (fail-open): {exc}")
    return summary


def _inspect_event(ev: dict, summary: dict) -> None:
    role = ev.get("role") or ev.get("type")
    if role in ("user", "assistant"):
        summary["turns"] += 1
    # Tool uses may be nested in message content blocks.
    for block in _iter_tool_uses(ev):
        name = (block.get("name") or "").lower()
        if not name:
            continue
        is_neotoma_write = (
            "store" in name or "correct" in name
            or "create_relationship" in name or "submit_" in name
        )
        if not is_neotoma_write:
            continue
        payload = block.get("input") or {}
        etypes = _entity_types_in(payload)
        # A write counts as domain (non-bookkeeping) if it touches any type
        # outside the bookkeeping set, OR it's a correct/relationship/submit
        # on a domain entity. Plan binding is detected from any plan touch.
        domain_types = etypes - BOOKKEEPING_TYPES
        if "plan" in etypes or _mentions_plan(payload):
            summary["bound_plan"] = True
        # Plan-optionality (task-spine plan): a session may anchor to a TASK
        # instead of a plan. A PART_OF link alongside a task entity counts.
        if _mentions_task_binding(payload):
            summary["bound_task"] = True
        if etypes & LEARNING_TYPES:
            summary["captured_learning"] = True
        if domain_types or "correct" in name or "submit_" in name or "create_relationship" in name:
            # create_relationship between two bookkeeping msgs is itself
            # bookkeeping; only count it as domain if a non-bookkeeping id appears.
            if "create_relationship" in name and not domain_types and not _mentions_plan(payload):
                continue
            summary["wrote_domain"] = True
            summary["write_types"].update(domain_types)


def _iter_tool_uses(ev: dict):
    content = ev.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block
    msg = ev.get("message")
    if isinstance(msg, dict):
        yield from _iter_tool_uses(msg)


def _entity_types_in(payload: dict) -> set:
    types: set = set()
    if not isinstance(payload, dict):
        return types
    if isinstance(payload.get("entity_type"), str):
        types.add(payload["entity_type"])
    for ent in payload.get("entities", []) or []:
        if isinstance(ent, dict) and isinstance(ent.get("entity_type"), str):
            types.add(ent["entity_type"])
    return types


def _mentions_plan(payload: dict) -> bool:
    blob = json.dumps(payload) if payload else ""
    return ('"plan"' in blob or "plan_id" in blob
            or SUPERSEDED_PLAN_ID in blob or FOUNDATION_PLAN_ID in blob)


def _mentions_task_binding(payload: dict) -> bool:
    """True when a write anchors the session to a task: a PART_OF relationship
    alongside a task entity in the same payload (the 'self-contained task stands
    alone' case). Loose by design, mirroring _mentions_plan — the goal is not to
    flag a session that legitimately anchored its work to a task instead of a
    plan. Linking to a pre-existing task by id alone is not detected here."""
    if not isinstance(payload, dict):
        return False
    rels = payload.get("relationships") or []
    has_part_of = any(
        "part_of" in str(r.get("relationship_type", "")).lower()
        for r in rels
        if isinstance(r, dict)
    )
    return has_part_of and "task" in _entity_types_in(payload)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect rather than following it.

    Security/arch/QA finding on PR #1298 (round 4, confirmed with a live
    two-loopback-server reproduction): stdlib's default
    `HTTPRedirectHandler` follows 301/302/303 for a POST and forwards the
    original request's headers verbatim — including `Authorization` — to
    whatever origin the `Location` header names, with no same-origin check.
    `base_url` is trusted local config (env or ~/.config/neotoma/.env), but
    nothing upstream constrains where a *response* from that configured
    endpoint can redirect to: a compromised/misconfigured endpoint, an
    on-path proxy, or a hijacked DNS resolution is enough to exfiltrate the
    bearer token to an arbitrary third-party origin, silently.

    The `/store` endpoint this hook calls has no legitimate reason to
    redirect at all, so the fix is to refuse redirects outright rather than
    attempt a same-origin allowance (which would still need to get the
    origin comparison exactly right to avoid the same class of bug). Raising
    from every `redirect_request` override makes `urlopen` propagate the
    redirect as a normal `HTTPError`, caught by the existing best-effort
    `except Exception` below — so a redirecting endpoint degrades to "audit
    emission failed, non-fatal" exactly like any other transport failure,
    never a followed cross-origin request.

    Same fix, same bug class, as `_RefuseRedirect` in
    `lib/daemon_runtime/policy_skill_renderer.py` (ateles#1268 round 3,
    Falco security) — that module's own docstring cites it as precedent.
    Not imported from here: `.claude/hooks/*.py` is deliberately stdlib-only
    with no imports from `lib/` (see this file's module docstring), so the
    two Neotoma-bearer-token callers in this checkout each carry their own
    ~10-line copy rather than share a module across that boundary. If a
    third caller needs this, that is the trigger to extract a genuinely
    shared, dependency-free helper both sides can import.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802 - stdlib API
        raise urllib.error.HTTPError(
            newurl, code, f"refusing to follow redirect ({msg})", headers, fp,
        )


# Installed as the module-level default opener so the existing
# `urllib.request.urlopen(req, timeout=...)` call site — and every test that
# monkeypatches `si.urllib.request.urlopen` directly, the pre-existing
# convention throughout this suite — keeps working unchanged, while the
# redirect refusal applies whenever the REAL opener is used.
urllib.request.install_opener(urllib.request.build_opener(_NoRedirectHandler))


# ---------------------------------------------------------------------------
# harness_event audit emission (best-effort, fail-open)
# ---------------------------------------------------------------------------
def emit_harness_event_raw(
    idempotency_slug: str, entity_fields: dict, log_tag: str = "harness-event",
    session_id: str = "", suppress_stderr_warning: bool = False,
) -> None:
    """POST one `harness_event` entity to Neotoma. Shared by every Stop hook
    in this checkout that emits an audit row — `emit_harness_event` below
    (session-integrity) and `decision_shape_gate.py`'s decision-shape check.

    `idempotency_slug` becomes `harness-event-{idempotency_slug}-{timestamp}`.
    `entity_fields` is merged onto the required `entity_type`/`harness` keys
    as-is, so each caller owns its own event vocabulary (e.g. `event_type`,
    `integrity_status`) rather than this function imposing one shape on all
    callers — the two current callers intentionally use different schemas for
    what "the outcome" means (a tri-state enum vs. a bool + finding list).
    `log_tag` names the caller in stderr diagnostics, so a missing-token or
    network-failure line points at the hook that actually failed rather than
    always reading "[session-integrity]" regardless of which hook called in.
    `session_id`, when given, scopes the once-per-session missing-credentials
    warning (see `_warn_missing_credentials_once`) — omitted, the warning is
    still printed but not deduplicated across calls. `suppress_stderr_warning`
    lets a caller that will present the SAME notice itself on a different
    surface (`stop_finalizer.py`'s `systemMessage`) skip this function's own
    stderr write entirely, rather than racing to be the first to mark
    delivery — the caller remains responsible for calling
    `mark_credentials_warning_delivered` itself, only after its own
    presentation actually succeeds.

    CREDENTIALS (ateles#1261 follow-up, audit ent_b66293f0dcc8c887d4fdbeae):
    `neotoma_credentials()` checks for a complete environment pair first,
    then a complete ~/.config/neotoma/.env pair — the same fallback file
    `sync_skills.py` / `session_language.py` already use. It never combines
    an endpoint and bearer from different sources without an explicit verified
    binding. When neither source is complete, this emits one VISIBLE warning
    per session (not a silent skip) and returns without making a request.

    Schema 689230f4-cd83-49b6-baa7-a752cf70629d. Best-effort otherwise: a
    network error is logged and swallowed — never blocks the hook.
    """
    base_url, token = neotoma_credentials()
    if not base_url or not token:
        if not suppress_stderr_warning:
            _warn_missing_credentials_once(session_id, log_tag)
        return
    body = {
        "idempotency_key": f"harness-event-{idempotency_slug}-{int(time.time())}",
        "observation_source": "workflow_state",
        "entities": [{
            "entity_type": "harness_event",
            "harness": "claude_code",
            **entity_fields,
        }],
    }
    try:
        req = urllib.request.Request(
            f"{base_url}/store",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        # The module-level opener installed above refuses redirects rather
        # than following them with the Authorization header attached — see
        # _NoRedirectHandler.
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        # _NoRedirectHandler.redirect_request is the ONLY raise site that
        # uses this exact message — matching on it (rather than treating
        # every 3xx as a refused redirect) avoids mislabeling a genuine 3xx
        # response urllib never routes through redirect_request at all
        # (e.g. 300, 304, 305, 306 go straight to HTTPErrorProcessor).
        if "refusing to follow redirect" in str(exc):
            sys.stderr.write(
                f"[{log_tag}] harness_event emission failed (non-fatal): refused "
                f"a redirect ({exc.code}) rather than forwarding credentials "
                "cross-origin\n"
            )
        else:
            sys.stderr.write(f"[{log_tag}] harness_event emission failed (non-fatal): {exc}\n")
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[{log_tag}] harness_event emission failed (non-fatal): {exc}\n")


def emit_harness_event(
    session_id: str, summary: dict, integrity_status: str,
    suppress_stderr_warning: bool = False,
) -> None:
    """Write one harness_event recording the session integrity outcome.

    Schema 689230f4-cd83-49b6-baa7-a752cf70629d. Best-effort: no token or a
    network error is logged and swallowed — never blocks the hook (enforced
    by emit_harness_event_raw). `suppress_stderr_warning` forwards to
    `emit_harness_event_raw` — see its docstring; used by `stop_finalizer.py`,
    which presents the missing-credentials notice itself via `systemMessage`.
    """
    emit_harness_event_raw(f"session-integrity-{session_id}", {
        "event_type": "session_integrity_check",
        "session_id": session_id,
        "integrity_status": integrity_status,  # integral | violated | exempt
        "turns": summary.get("turns", 0),
        "wrote_domain": summary.get("wrote_domain", False),
        "bound_plan": summary.get("bound_plan", False),
        "bound_task": summary.get("bound_task", False),
        "captured_learning": summary.get("captured_learning", False),
        "write_types": sorted(summary.get("write_types", []) or []),
    }, log_tag="session-integrity", session_id=session_id,
        suppress_stderr_warning=suppress_stderr_warning)
