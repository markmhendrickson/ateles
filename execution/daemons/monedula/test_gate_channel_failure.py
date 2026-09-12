"""
Characterization tests for Monedula's consent gate under CHANNEL FAILURE (#554).

These tests document current behaviour. They change nothing about the payment
path — they pin down two facts established while investigating #554:

  1. FAIL-CLOSED, AT THE PARSER LAYER ONLY: when the Telegram channel cannot
     deliver a reply, `_parse_reply` approves nothing. Read the scope of that
     claim carefully — every assertion in this file is on `_parse_reply`'s
     return value. This file does NOT invoke `main()`, so it does NOT assert
     the payment-blocking effect (`monedula.py:685`,
     `if handler.name not in approved: continue`). If that line were deleted
     tomorrow, every test here would still pass. This file is therefore NOT a
     regression guard on the fail-closed property.

     Most assertions below also restate coverage already committed on `main`
     in `test_parse_reply.py` — `test_none_and_empty_skip_all`,
     `test_no_skips_all`, `test_unrecognised_reply_skips_all`,
     `test_attended_all_approves_everything`, `test_attended_single_session`.
     The one input not covered there is whitespace-only `"   "`, which takes
     the "unrecognised reply" branch rather than the empty-string branch. The
     rest are restated here for narrative continuity with the #554
     investigation, not because the behaviour was untested.

  2. THE DEFECT: a channel failure (HTTP 409 Conflict / poll timeout) and a
     deliberate operator decline produce *byte-identical* results — both yield
     an empty approval set, and `main()` logs the same
     `No payments approved (reply=...)` line and returns cleanly. There is no
     value `_parse_reply` can return that distinguishes "the operator said no"
     from "we were never able to ask". That indistinguishability is why a gate
     with a 0/480 success rate went unnoticed for ten weeks.

The real-world cause of the 409s: Telegram permits exactly ONE `getUpdates`
consumer per bot token. Cyphorhinus runs KeepAlive (a permanent long-poll)
while Monedula wakes every 900s and polls the same Bot API, so Monedula's poll
loses the race and is rejected with HTTP 409 Conflict.

NOT the #554 definition-of-done. Acceptance criteria 6 (effect-verified fix)
and 7 (cross-surface parity) on #554 are entirely unsatisfied by this file and
remain owed by the implementation PR. The channel-failure EFFECT tests — no
`handler.execute`, escalation POST body, `_notify(priority="blocker")`, exit
code 1 — belong in `execution/daemons/monedula/test_telegram_gate.py` per
#554's QA plan. That file is started alongside this one with the two
characterization pins the QA lens required (poll layer returns None on 409;
a dead channel executes no payment); the escalation, exit-code, and dead-gate
items of the QA plan's sign-off condition are still outstanding there.

Run with: pytest execution/daemons/monedula/test_gate_channel_failure.py -v
"""

from __future__ import annotations

from monedula import _parse_reply

NAMES = ["yoga", "therapy"]


# ── 1. Fail-closed: a dead channel approves nothing ──────────────────────────


def test_poll_timeout_approves_nothing() -> None:
    """`telegram_long_poll_once` returns None on timeout — approve nothing."""
    assert _parse_reply(None, NAMES) == set()


def test_http_409_conflict_approves_nothing() -> None:
    """A 409 exhausts the retry loop and yields None — approve nothing.

    This is the actual production failure: 433 recorded 409s, 2026-06-13
    onward. Money must not move on it.

    Deliberately the same assertion as `test_poll_timeout_approves_nothing`,
    kept under a second name to label the production failure mode. That the
    poll layer really does turn a 409 into `None` is pinned separately in
    `test_telegram_gate.py::test_http_409_returns_none_from_poll_layer`; this
    one only covers what the parser does with that `None`.
    """
    assert _parse_reply(None, NAMES) == set()


def test_empty_reply_approves_nothing() -> None:
    """A blank/whitespace reply must not approve a payment."""
    assert _parse_reply("", NAMES) == set()
    assert _parse_reply("   ", NAMES) == set()


# ── 2. The defect: channel failure is indistinguishable from decline ─────────


def test_channel_failure_is_indistinguishable_from_decline() -> None:
    """The core defect of #554.

    `_parse_reply` collapses two operationally opposite situations into the
    same empty set:

      - the operator saw the prompt and declined  → a correct, healthy outcome
      - the channel was never able to ask at all  → an outage needing escalation

    Because both return `set()`, `main()` cannot branch on the difference, so a
    permanently broken gate looks exactly like a well-behaved daemon awaiting
    instruction. Fixing #554 means introducing a channel-outcome type ABOVE
    this parser — not changing the parser, whose behaviour here is correct.
    """
    declined = _parse_reply("no", NAMES)          # operator refused
    channel_dead = _parse_reply(None, NAMES)      # 409 / timeout: never asked

    assert declined == channel_dead == set()

    # There is no signal in the return value that separates them. When #554 is
    # fixed, the distinction must live in the poll layer, and THIS assertion is
    # expected to stay true (the parser stays a pure reply parser).


def test_unrecognised_reply_also_collapses_to_skip() -> None:
    """A garbled reply is also treated as skip — safe, but equally silent."""
    assert _parse_reply("wat", NAMES) == set()


# ── 3. Control: a real approval still works, so the gate isn't just "off" ────


def test_genuine_attendance_reply_still_approves() -> None:
    """The gate is fail-closed, not stuck-closed: a real reply does approve.

    This proves the 0/480 failure rate is a CHANNEL problem, not a parser that
    rejects everything.
    """
    assert _parse_reply("attended all", NAMES) == {"yoga", "therapy"}
    assert _parse_reply("attended yoga", NAMES) == {"yoga"}
