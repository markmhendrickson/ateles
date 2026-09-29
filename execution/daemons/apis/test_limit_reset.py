"""Reading a provider's reset time out of its usage-limit refusal.

Fixture strings are the real refusals the swarm has met (the Claude line is the
one that spent the 5-hour window on 2026-09-29 while headroom still read 0.65
against the weekly allowance).  The clock is injected everywhere; no test
depends on when it runs or on the host's timezone.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import limit_reset  # noqa: E402

MADRID = ZoneInfo("Europe/Madrid")
CLAUDE_REFUSAL = "You've hit your session limit · resets 12:30pm (Europe/Madrid)"


def _wall(*args, tz=MADRID) -> float:
    return datetime(*args, tzinfo=tz).timestamp()


def test_claude_session_refusal_ends_at_the_stated_reset() -> None:
    now = _wall(2026, 9, 29, 9, 0)
    parsed = limit_reset.parse_limit_reset(CLAUDE_REFUSAL, provider="claude", now_wall=now)
    assert parsed is not None
    assert parsed.until_wall == _wall(2026, 9, 29, 12, 30)
    assert parsed.zone == "Europe/Madrid"


def test_time_that_has_passed_today_means_tomorrow() -> None:
    now = _wall(2026, 9, 29, 13, 45)  # 12:30pm is 75 minutes ago, not skew
    parsed = limit_reset.parse_limit_reset(CLAUDE_REFUSAL, now_wall=now)
    assert parsed is not None
    assert parsed.until_wall == _wall(2026, 9, 30, 12, 30)


def test_a_just_passed_time_is_clock_skew_not_a_day_of_cooling() -> None:
    now = _wall(2026, 9, 29, 12, 32)
    parsed = limit_reset.parse_limit_reset(CLAUDE_REFUSAL, now_wall=now)
    assert parsed is not None
    assert parsed.until_wall == now + limit_reset.SKEW_FLOOR_SECONDS


def test_named_zone_wins_over_the_machine_zone() -> None:
    # 09:00 in Madrid is 16:00 in Tokyo; "resets 6pm (Asia/Tokyo)" is 11:00
    # in Madrid, whatever zone the host runs in.
    now = _wall(2026, 9, 29, 9, 0)
    parsed = limit_reset.parse_limit_reset(
        "You've hit your session limit · resets 6pm (Asia/Tokyo)", now_wall=now
    )
    assert parsed is not None
    assert parsed.until_wall == _wall(2026, 9, 29, 18, 0, tz=ZoneInfo("Asia/Tokyo"))


@pytest.mark.parametrize(
    "text",
    [
        "You've hit your weekly limit · resets Oct 5 at 6pm (Europe/Madrid)",
        "You've hit your weekly limit · resets Oct 5, 6:00pm (Europe/Madrid)",
        "usage limit reached, resets Oct 5th 2026 at 6pm (Europe/Madrid)",
    ],
)
def test_date_and_time_form(text: str) -> None:
    parsed = limit_reset.parse_limit_reset(text, now_wall=_wall(2026, 9, 29, 9, 0))
    assert parsed is not None
    assert parsed.until_wall == _wall(2026, 10, 5, 18, 0)


def test_codex_iso_form_uses_the_local_zone(monkeypatch) -> None:
    monkeypatch.setattr(limit_reset, "_local_zone", lambda: MADRID)
    text = "You've hit your usage limit. Try again at 2026-10-03 20:12."
    parsed = limit_reset.parse_limit_reset(
        text, provider="codex", now_wall=_wall(2026, 9, 29, 9, 0)
    )
    assert parsed is not None
    assert parsed.until_wall == _wall(2026, 10, 3, 20, 12)


def test_relative_form() -> None:
    now = _wall(2026, 9, 29, 9, 0)
    parsed = limit_reset.parse_limit_reset(
        "You've hit your weekly usage limit; resets in 2 hours", now_wall=now
    )
    assert parsed is not None and parsed.until_wall == now + 2 * 3600
    parsed = limit_reset.parse_limit_reset(
        "usage limit. Try again in 3 days 4 hours 12 minutes", now_wall=now
    )
    assert parsed is not None
    assert parsed.until_wall == now + 3 * 86400 + 4 * 3600 + 12 * 60


@pytest.mark.parametrize(
    "text",
    [
        "",
        "You've hit your session limit",  # no reset stated
        "resets soon",
        "the job resets 5 items",  # bare number, not a time of day
        "VERDICT: APPROVE",
    ],
)
def test_unparseable_reset_is_none_not_a_guess(text: str) -> None:
    assert limit_reset.parse_limit_reset(text, now_wall=_wall(2026, 9, 29, 9, 0)) is None


def test_cursor_wording_is_a_named_hook_that_does_not_guess() -> None:
    now = _wall(2026, 9, 29, 9, 0)
    assert limit_reset._PROVIDER_PARSERS["cursor"] is limit_reset._parse_cursor
    assert limit_reset.parse_limit_reset(
        "Cursor: you are out of requests", provider="cursor", now_wall=now
    ) is None


def test_implausibly_distant_or_past_dates_are_rejected() -> None:
    now = _wall(2026, 9, 29, 9, 0)
    assert limit_reset.parse_limit_reset("resets 2031-01-01 10:00", now_wall=now) is None
    assert limit_reset.parse_limit_reset("resets 2020-01-01 10:00", now_wall=now) is None


# ── anchoring to the provider's own refusal ────────────────────────────────


def test_refusal_is_read_from_the_tail_and_typed_by_kind() -> None:
    now = _wall(2026, 9, 29, 9, 0)
    out = limit_reset.parse_refusal(CLAUDE_REFUSAL, "", provider="claude", now_wall=now)
    assert out is not None
    assert out.kind == "session" and not out.clamped
    assert out.until_wall == _wall(2026, 9, 29, 12, 30)


def test_refusal_with_an_unreadable_reset_is_still_a_refusal() -> None:
    out = limit_reset.parse_refusal(
        "You've hit your session limit", "", now_wall=_wall(2026, 9, 29, 9, 0)
    )
    assert out is not None and out.until_wall is None and out.kind == "session"


def test_a_limit_phrase_outside_the_tail_is_not_a_refusal() -> None:
    text = "usage limit reached, resets in 2 hours\n" + "\n".join(
        f"line {i}" for i in range(10)
    )
    assert limit_reset.parse_refusal(text, "", now_wall=_wall(2026, 9, 29, 9, 0)) is None


def test_a_refusal_line_is_read_on_stderr_too() -> None:
    now = _wall(2026, 9, 29, 9, 0)
    out = limit_reset.parse_refusal(
        "", "log\nYou've hit your usage limit. Try again in 3 hours", now_wall=now
    )
    assert out is not None and out.kind == "usage" and out.until_wall == now + 3 * 3600


def test_reset_is_clamped_to_the_kind_of_window() -> None:
    now = _wall(2026, 9, 29, 9, 0)
    session = limit_reset.parse_refusal(
        "You've hit your session limit · resets Oct 20 at 6pm (Europe/Madrid)",
        "", now_wall=now,
    )
    assert session is not None and session.clamped
    assert session.until_wall == now + limit_reset.KIND_CAP_SECONDS["session"]
    weekly = limit_reset.parse_refusal(
        "weekly limit reached, resets in 30 days", "", now_wall=now
    )
    assert weekly is not None and weekly.clamped
    assert weekly.until_wall == now + limit_reset.KIND_CAP_SECONDS["weekly"]
