"""
Unit tests for Monedula's attendance-gated reply parser (`_parse_reply`).

Recurring session payments (yoga via BTC, therapy via Wise) are
attendance-gated: a calendar event is NOT proof the operator attended, so a
bare "yes" must never approve a payment. These tests lock that contract in so
the gate can't silently regress into the old "yes pays everything" behavior.

Run with: pytest execution/daemons/monedula/test_parse_reply.py -v
"""

from __future__ import annotations

from monedula import _is_bare_affirmative, _parse_reply

# Handler names as Monedula passes them in (order-independent; parser returns a set).
NAMES = ["yoga", "therapy"]


# ── Bare affirmatives are REJECTED (the core gate) ────────────────────────────


def test_bare_yes_approves_nothing() -> None:
    assert _parse_reply("yes", NAMES) == set()


def test_bare_yes_all_approves_nothing() -> None:
    assert _parse_reply("yes all", NAMES) == set()


def test_y_and_y_all_approve_nothing() -> None:
    assert _parse_reply("y", NAMES) == set()
    assert _parse_reply("y all", NAMES) == set()


# ── Attendance-naming affirmatives approve ────────────────────────────────────


def test_attended_all_approves_everything() -> None:
    assert _parse_reply("attended all", NAMES) == {"yoga", "therapy"}


def test_paid_all_approves_everything() -> None:
    assert _parse_reply("paid all", NAMES) == {"yoga", "therapy"}


def test_attended_single_session() -> None:
    assert _parse_reply("attended yoga", NAMES) == {"yoga"}
    assert _parse_reply("attended therapy", NAMES) == {"therapy"}


def test_session_attended_word_order_variant() -> None:
    assert _parse_reply("yoga attended", NAMES) == {"yoga"}


def test_paid_single_session() -> None:
    assert _parse_reply("paid therapy", NAMES) == {"therapy"}


# ── Per-handler short forms still work as explicit confirmations ──────────────


def test_yes_session_short_form_still_works() -> None:
    assert _parse_reply("yes yoga", NAMES) == {"yoga"}
    assert _parse_reply("y therapy", NAMES) == {"therapy"}


def test_bare_session_name_confirms_that_session() -> None:
    assert _parse_reply("yoga", NAMES) == {"yoga"}


# ── Skips and noise ───────────────────────────────────────────────────────────


def test_no_skips_all() -> None:
    assert _parse_reply("no", NAMES) == set()
    assert _parse_reply("no all", NAMES) == set()


def test_skipped_synonyms_skip_all() -> None:
    for reply in ("skip", "skip all", "skipped", "n", "didn't go", "did not attend"):
        assert _parse_reply(reply, NAMES) == set(), reply


def test_none_and_empty_skip_all() -> None:
    assert _parse_reply(None, NAMES) == set()
    assert _parse_reply("", NAMES) == set()


def test_unrecognised_reply_skips_all() -> None:
    assert _parse_reply("maybe later", NAMES) == set()


# ── Case / whitespace insensitivity ───────────────────────────────────────────


def test_case_and_whitespace_insensitive() -> None:
    assert _parse_reply("  ATTENDED ALL  ", NAMES) == {"yoga", "therapy"}
    assert _parse_reply("Attended Yoga", NAMES) == {"yoga"}


# ── Bare-affirmative detection (drives the "attendance-gated" nudge) ───────────


def test_bare_affirmative_detects_yes_forms() -> None:
    for reply in ("yes", "yes all", "y", "y all", "  YES ALL  "):
        assert _is_bare_affirmative(reply) is True, reply


def test_bare_affirmative_false_for_attendance_and_skips() -> None:
    for reply in (
        "attended all",
        "attended yoga",
        "yes yoga",
        "no",
        "skipped",
        "maybe later",
        "",
        None,
    ):
        assert _is_bare_affirmative(reply) is False, reply
