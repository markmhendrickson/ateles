"""execution/daemons/apis/limit_reset.py — read a provider's reset time out of its refusal.

A subscription CLI that has spent its session (or weekly) window refuses with a
message that says WHEN it comes back, e.g. Claude's::

    You've hit your session limit · resets 12:30pm (Europe/Madrid)

The router used to discard that and cool the provider for a flat hour in one
process's memory, which is why every later ``dispatch_role`` process (each one
a fresh interpreter) launched the dead CLI again and failed instantly.  This
module turns the refusal into a wall-clock instant so ``harness_router`` can
persist a cooling window that ends at the stated reset.

Formats understood (all timezone-aware; a zone named in parentheses wins, else
the machine's local zone, which under launchd is set by the plist ``TZ``):

* ``resets 12:30pm (Europe/Madrid)`` / ``resets 7pm`` / ``resets at 19:30`` —
  a time of day: the next occurrence of it in that zone.
* ``resets Oct 5 at 6:00pm`` / ``resets Oct 5, 2026, 6pm (Europe/Madrid)`` —
  a date and time.
* ``try again at 2026-10-03 20:12`` (Codex, as recorded in
  ``execution/scripts/harness_usage.py``) — an ISO-style date and time.
* ``resets in 2 hours`` / ``try again in 3 days 4 hours 12 minutes`` — relative.

Cursor's refusal wording is not known to this codebase and is NOT guessed:
``_parse_cursor`` is the named hook, returning ``None`` until a real message is
captured, at which point the generic parser is tried and then this hook.

Unparseable, past-dated (beyond clock skew) or implausibly distant resets return
``None``; the caller then applies the conservative default window and logs it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("apis.limit_reset")

# A reset this far out is a mis-parse, not a plan window (weekly windows are
# 7 days; leave headroom for a monthly cap).
MAX_RESET_HORIZON_SECONDS = 35 * 24 * 3600
# A time-of-day that has "just passed" is clock skew between us and the
# provider, not tomorrow's reset: hold a short floor instead of a day.
CLOCK_SKEW_SECONDS = 10 * 60
SKEW_FLOOR_SECONDS = 5 * 60

_MONTHS = {
    name: index
    for index, names in enumerate(
        (
            ("jan", "january"), ("feb", "february"), ("mar", "march"),
            ("apr", "april"), ("may",), ("jun", "june"), ("jul", "july"),
            ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"),
            ("nov", "november"), ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}

_ZONE = re.compile(r"\(\s*([A-Za-z_]+(?:/[A-Za-z_+\-0-9]+)+|UTC|GMT)\s*\)")
_TIME = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"
_LEAD = r"(?:resets?|try again|available again|renews?)"

# resets [at] <time>            -> next occurrence
_TIME_OF_DAY = re.compile(rf"\b{_LEAD}(?:\s+at)?\s+{_TIME}\b")
# resets [at] Oct 5[, 2026][,| at] <time>
_MONTH_DATE_TIME = re.compile(
    rf"\b{_LEAD}(?:\s+at)?\s+([a-z]{{3,9}})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:,?\s+(\d{{4}}))?(?:,|\s+at)?\s+{_TIME}\b"
)
# try again at 2026-10-03 20:12 / resets 2026-10-03T20:12[:00]
_ISO_DATE_TIME = re.compile(
    rf"\b{_LEAD}(?:\s+at)?\s+(\d{{4}})-(\d{{2}})-(\d{{2}})[t ]+(\d{{1,2}}):(\d{{2}})(?::(\d{{2}}))?"
)
# resets in 2 hours / try again in 3 days 4 hours 12 minutes
_RELATIVE = re.compile(rf"\b{_LEAD}\s+in\s+((?:\d+\s*(?:d|h|m|s|day|hour|hr|min|minute|sec|second)s?\b[\s,and]*)+)")
# `(?<!\d)` starts a part only where a digit run begins. A start inside a run
# sees the same text the run's own start already tried, so it cannot find a part
# the first attempt missed; skipping it avoids rescanning a long run once per digit.
_RELATIVE_PART = re.compile(r"(?<!\d)(\d+)\s*(d|h|m|s|day|hour|hr|min|minute|sec|second)s?\b")
_UNIT_SECONDS = {"d": 86400, "day": 86400, "h": 3600, "hour": 3600, "hr": 3600,
                 "m": 60, "min": 60, "minute": 60, "s": 1, "sec": 1, "second": 1}


@dataclass(frozen=True)
class ResetParse:
    """A parsed reset instant and the text it came from."""

    until_wall: float  # epoch seconds
    matched: str  # the matched fragment, for logs
    zone: str  # zone the instant was resolved in


def _local_zone() -> tzinfo:
    return datetime.now().astimezone().tzinfo or timezone.utc


def _resolve_zone(text: str) -> tuple[tzinfo, str]:
    match = _ZONE.search(text)
    if match:
        name = match.group(1)
        try:
            return ZoneInfo(name), name
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("[apis] limit reset names unknown zone %r; using local zone", name)
    zone = _local_zone()
    return zone, str(zone)


def _hour_minute(hour: str, minute: str | None, ampm: str | None) -> tuple[int, int] | None:
    h, m = int(hour), int(minute or 0)
    if ampm:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ampm == "pm" else 0)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


def _next_time_of_day(now: datetime, hour: int, minute: int) -> float:
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate > now:
        return candidate.timestamp()
    if (now - candidate).total_seconds() <= CLOCK_SKEW_SECONDS:
        return now.timestamp() + SKEW_FLOOR_SECONDS
    return (candidate + timedelta(days=1)).timestamp()


def _dated(
    now: datetime, zone: tzinfo, year: int | None, month: int, day: int, hour: int, minute: int,
    second: int = 0,
) -> float | None:
    try:
        if year is None:
            target = datetime(now.year, month, day, hour, minute, second, tzinfo=zone)
            # A dateless month/day that has already passed means next year.
            if target.timestamp() < now.timestamp() - CLOCK_SKEW_SECONDS:
                target = target.replace(year=now.year + 1)
        else:
            target = datetime(year, month, day, hour, minute, second, tzinfo=zone)
    except ValueError:
        return None
    return target.timestamp()


def _parse_cursor(text: str, now: datetime, zone: tzinfo) -> float | None:
    """Hook for Cursor's refusal wording.

    Deliberately empty: no real Cursor session/usage-limit message is recorded
    in this codebase or its tests, and inventing a format would cool a provider
    on a guess.  When one is captured, add its pattern here and a fixture test.
    """
    return None


_PROVIDER_PARSERS = {"cursor": _parse_cursor}


def _generic(text: str, now: datetime, zone: tzinfo) -> tuple[float, str] | None:
    lowered = text.lower()

    match = _ISO_DATE_TIME.search(lowered)
    if match:
        y, mo, d, h, mi, s = match.groups()
        wall = _dated(now, zone, int(y), int(mo), int(d), int(h), int(mi), int(s or 0))
        if wall is not None:
            return wall, match.group(0)

    match = _MONTH_DATE_TIME.search(lowered)
    if match:
        month_name, day, year, hour, minute, ampm = match.groups()
        month = _MONTHS.get(month_name)
        hm = _hour_minute(hour, minute, ampm)
        if month and hm:
            wall = _dated(now, zone, int(year) if year else None, month, int(day), *hm)
            if wall is not None:
                return wall, match.group(0)

    match = _RELATIVE.search(lowered)
    if match:
        seconds = sum(
            int(n) * _UNIT_SECONDS[unit]
            for n, unit in _RELATIVE_PART.findall(match.group(1))
        )
        if seconds > 0:
            return now.timestamp() + seconds, match.group(0).strip()

    match = _TIME_OF_DAY.search(lowered)
    if match:
        hm = _hour_minute(*match.groups())
        # A bare "resets 5" with no colon and no am/pm is not a time of day.
        if hm and (match.group(2) or match.group(3)):
            return _next_time_of_day(now, *hm), match.group(0)
    return None


def parse_limit_reset(
    *texts: str, provider: str | None = None, now_wall: float | None = None
) -> ResetParse | None:
    """Return the reset instant named in a provider's refusal, or ``None``.

    ``texts`` are the child's stdout/stderr/error (a session-limit refusal is
    printed on stdout with rc=1).  ``now_wall`` is injectable for tests.
    """
    import time

    text = " ".join(t for t in texts if t)
    if not text.strip():
        return None
    moment = time.time() if now_wall is None else now_wall
    zone, zone_name = _resolve_zone(text)
    now = datetime.fromtimestamp(moment, tz=zone)

    found = _generic(text, now, zone)
    if found is None and provider in _PROVIDER_PARSERS:
        wall = _PROVIDER_PARSERS[provider](text, now, zone)
        found = (wall, provider) if wall is not None else None
    if found is None:
        return None
    wall, matched = found
    if wall <= moment or wall - moment > MAX_RESET_HORIZON_SECONDS:
        log.warning(
            "[apis] limit reset %r resolved to an implausible instant; ignoring", matched
        )
        return None
    return ResetParse(until_wall=wall, matched=matched, zone=zone_name)


# ── Anchoring to the provider's own refusal ───────────────────────────────────
#
# A persisted cooling window is a cross-process hold-out, so the text that sets
# it must be the provider CLI's own refusal, not whatever a run happened to
# print.  A harness child's output can carry text written by others (a quoted
# PR body, a review thread, an agent explaining usage limits); codex echoes the
# dispatched prompt to stderr.  Three limits keep that text from deciding:
#
# * the caller only asks for a parse when the run FAILED (nonzero exit);
# * only the last ``REFUSAL_TAIL_LINES`` non-empty lines of each stream are
#   read, and a line must itself carry a limit phrase AND be short — a real
#   refusal is one short line at the end of the output;
# * whatever it states is clamped to the longest window that KIND of limit can
#   have, so even a forged reset costs hours, not weeks.

REFUSAL_TAIL_LINES = 5
MAX_REFUSAL_LINE_CHARS = 400

# Longest hold-out per kind: the provider's window plus a little slack.
KIND_CAP_SECONDS = {
    "session": 5 * 3600 + 10 * 60,
    "weekly": 7 * 86400 + 3600,
    "monthly": 32 * 86400,
    "usage": 7 * 86400 + 3600,  # kind not stated: the longest plan window seen
}

_LIMIT_PHRASE = re.compile(r"\blimit\b|\bquota\b|out of requests|no requests remaining")


def limit_kind(line: str) -> str:
    """Which window a refusal line says was spent (``usage`` when it doesn't say)."""
    lowered = line.lower()
    if re.search(r"\bsession\b|\b5[- ]?hour\b|\bfive[- ]?hour\b", lowered):
        return "session"
    if "week" in lowered:
        return "weekly"
    if "month" in lowered:
        return "monthly"
    return "usage"


@dataclass(frozen=True)
class Refusal:
    """The provider's own capacity refusal, as read from the tail of its output."""

    kind: str  # session | weekly | monthly | usage
    until_wall: float | None  # clamped to KIND_CAP_SECONDS; None: reset unreadable
    matched: str
    zone: str
    clamped: bool


def _tail_lines(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-REFUSAL_TAIL_LINES:]


def parse_refusal(
    stdout: str, stderr: str, *, provider: str | None = None, now_wall: float | None = None
) -> Refusal | None:
    """Return the capacity refusal at the end of a FAILED run's output, or ``None``.

    ``None`` means no line in the tails of stdout/stderr is a short limit
    refusal, so nothing should be persisted (the caller keeps its in-process
    cooldown).  A refusal whose reset cannot be read comes back with
    ``until_wall=None`` and the caller applies the default window.
    """
    import time

    moment = time.time() if now_wall is None else now_wall
    for stream in (stdout or "", stderr or ""):
        for line in reversed(_tail_lines(stream)):
            if len(line) > MAX_REFUSAL_LINE_CHARS or not _LIMIT_PHRASE.search(line.lower()):
                continue
            kind = limit_kind(line)
            parsed = parse_limit_reset(line, provider=provider, now_wall=moment)
            if parsed is None:
                return Refusal(kind, None, line[:120], "", False)
            cap = moment + KIND_CAP_SECONDS[kind]
            clamped = parsed.until_wall > cap
            if clamped:
                log.warning(
                    "[apis] %s limit refusal states a reset %.1f days out, longer than "
                    "a %s window can be; clamping to %.1f hours",
                    provider, (parsed.until_wall - moment) / 86400, kind,
                    KIND_CAP_SECONDS[kind] / 3600,
                )
            return Refusal(
                kind, min(parsed.until_wall, cap), parsed.matched, parsed.zone, clamped
            )
    return None
