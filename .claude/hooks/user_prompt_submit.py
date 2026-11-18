#!/usr/bin/env python3
"""UserPromptSubmit hook — turn accounting (layer 1, §5.1) and held-notice surface.

Increments the per-session turn counter in the session state file. The Stop
finalizer no longer reads it: since the operator ruling of 2026-10-27 (write
per state change, not per turn) integrity is judged on conversation capture
seen in the transcript, not on a turn count. The counter is kept only as a
cheap liveness signal.

It also prints a bounded notice to stdout (which the harness adds to model context)
when daemons hold operator notices that are waiting undelivered, and only when the
held total is non-zero and differs from the last total shown in this session.
The line is built from integers and regex-validated handler names returned by
lib/notify/held_view.py, never from notice text: notice text can embed
attacker-influenced strings (PR titles, issue text) and must not reach model
context from here. A keyed unreadable-usage notice adds fixed source and repair
text, never the queued message. Fail-open: any error means no output and exit 0.
"""
import importlib.util
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_integrity import read_hook_input, load_state, save_state, log  # noqa: E402


def _load_held_view():
    """Load held_view.py by file path: stdlib-only, no lib.notify package init."""
    path = Path(__file__).resolve().parent.parent.parent / "lib" / "notify" / "held_view.py"
    spec = importlib.util.spec_from_file_location("_ateles_held_view", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def held_notice_line(state: dict) -> str:
    """The one-line surface, or "". Updates ``state`` with the contents hash shown.

    Keyed on a hash of the queue contents, not the total, so different notices
    with the same count are announced again. Only the hash is kept in session
    state, never any notice text.
    """
    info = _load_held_view().summary()
    total = int(info["total"])
    unconfirmed = int(info.get("unconfirmed", 0))
    projections = int(info.get("projection_pending", 0))
    bad_sink = int(info.get("sink_state_unreadable", 0))
    bad_queues = int(info.get("queue_unreadable", 0))
    in_flight = int(info.get("sending_in_flight", 0))
    bad_journal = bool(info.get("dedupe_journal_unreadable"))
    bad_budget = bool(info.get("budget_unreadable"))
    orphaned = int(info.get("orphaned_held_receipts", 0))
    fingerprint = (str(info["fingerprint"]) + ":" +
                   str(info.get("unconfirmed_fingerprint", "")) + ":" +
                   str(bad_sink) + ":" + str(bad_queues) + ":" + str(bad_journal) +
                   ":" + str(bad_budget))
    if (total == 0 and unconfirmed == 0 and projections == 0 and bad_sink == 0
            and bad_queues == 0 and in_flight == 0 and not bad_journal
            and not bad_budget and orphaned == 0) or fingerprint == state.get("held_notices_hash"):
        state["held_notices_hash"] = fingerprint  # re-arm after the queue empties
        return ""
    state["held_notices_hash"] = fingerprint
    ranked = sorted(info["by_handler"].items(), key=lambda kv: (-kv[1], kv[0]))
    parts = ", ".join(f"{name}: {int(n)}" for name, n in ranked[:6])
    what = "operator notice is" if total == 1 else "operator notices are"
    held_line = (
        f"[held-notices] {total} {what} waiting undelivered ({parts}). In your "
        "reply, tell the operator what needs them: read them with python3 "
        "lib/notify/held_view.py, then clear what you showed with --clear."
    ) if total else ""
    recovery_line = (
        f"[unconfirmed alerts] {unconfirmed} alert outcome(s) are unknown. "
        "Read python3 lib/notify/held_view.py and tell the operator. "
        "After the operator decides to allow a resend, clear one with "
        "python3 lib/notify/held_view.py --clear-unconfirmed <key>."
    ) if unconfirmed else ""
    interrupted = [key for key in info.get("interrupted_keys", [])
                   if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9:._+\-]{1,220}", key)]
    interrupted_line = "\n".join(
        "[interrupted send] key=" + key + "; after a resend decision, run "
        "python3 lib/notify/held_view.py --clear-unconfirmed " + key
        for key in interrupted[:6]
    )
    flight_line = (
        f"[alerts in flight] {in_flight} send(s) are still within the send timeout. "
        "Read python3 lib/notify/held_view.py for their keys; do not clear them yet."
    ) if in_flight else ""
    usage_line = (
        "[usage-snapshot] The usage snapshot source (APIS_HARNESS_USAGE_FILE) "
        "is unreadable. Inspect it with python3 lib/notify/held_view.py, "
        "repair the file, then run python3 execution/scripts/harness_usage.py refresh."
    ) if int(info.get("usage_snapshot_unreadable", 0)) else ""
    journal_line = (
        "[dedupe-journal] The notification dedupe journal is unreadable. "
        "Reconcile unknown deliveries, then run python3 "
        "lib/notify/held_view.py --clear-unreadable-journal."
    ) if bad_journal else ""
    sink_line = (
        f"[notify-sink] {bad_sink} sink state file(s) are unreadable. "
        "Read python3 lib/notify/held_view.py and repair the preserved state "
        "before key migration or resolution."
    ) if bad_sink else ""
    queue_line = (
        f"[held-queue] {bad_queues} held notice queue file(s) are unreadable. "
        "Read python3 lib/notify/held_view.py; preserve and repair the file "
        "before clearing or draining notices."
    ) if bad_queues else ""
    budget_line = (
        "[held-email-budget] The budget file is unreadable. Read python3 "
        "lib/notify/held_view.py; repair it before draining email notices."
    ) if bad_budget else ""
    orphan_line = (
        f"[held-receipt] {orphaned} held receipt(s) have no queued notice. "
        "Read python3 lib/notify/held_view.py and repair the queue before resolving keys."
    ) if orphaned else ""
    projection_line = (
        f"[notify-projection] {projections} sink projection repair(s) are pending. "
        "Read python3 lib/notify/held_view.py and retry the named key migration "
        "or resolution; these records cannot be cleared as uncertain sends."
    ) if projections else ""
    return "\n".join(part for part in
                     (held_line, recovery_line, interrupted_line, flight_line,
                      projection_line, usage_line,
                      journal_line, sink_line, queue_line, budget_line, orphan_line)
                     if part)


def main() -> int:
    ev = read_hook_input()
    session_id = ev.get("session_id", "")
    state = load_state(session_id)
    state["turn_count"] = int(state.get("turn_count", 0)) + 1
    line = ""
    try:
        line = held_notice_line(state)
    except Exception as exc:  # noqa: BLE001 — the surface must never break a prompt
        log(f"held-notice surface error (ignored): {exc}")
    save_state(session_id, state)
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open
        log(f"user_prompt_submit hook error (ignored): {exc}")
        sys.exit(0)
