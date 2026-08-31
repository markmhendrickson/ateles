"""
Characterization tests for Monedula's consent gate under CHANNEL FAILURE (#554).

These tests document current behaviour. They change nothing about the payment
path — they pin down two facts established while investigating #554:

  1. GOOD NEWS (fail-closed): when the Telegram channel cannot deliver a reply,
     `_parse_reply` approves nothing, so `main()`'s
     `if handler.name not in approved: continue` skips every payment. Money does
     NOT move without approval. This test exists so that safety property can
     never regress silently.

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
