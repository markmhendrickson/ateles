"""Count review rounds from ordered episodes of author-scoped lens verdicts.

Contiguous verdicts for one head form an episode; returning to that head after
another head forms another episode because the poster keeps the return as a
new comment. Repeated lens/head comments at different history positions remain
distinct records. An episode after a blocking or unreadable episode starts a
round. Returning to a previously reviewed head after any earlier blocking
episode starts a new round, even when the immediately previous episode cleared;
otherwise an episode after an all-clear one stays in the same round. The fetched
comment list's source order governs; standalone records use parsed timestamps
and integer GitHub comment ids.
Missing or malformed timestamps and ties without usable integer comment ids
make order indeterminate and refuse approval unless a valid operator override
is present. The same override can lift the three-round cap, but never unresolved
or partial lens identity resolution. The operator's unedited, PR-local GitHub
comment is revalidated immediately before each approval POST.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

ROUND_CAP = 3
CAP_REACHED = "round cap reached: split or cut scope"
OVERRIDE_COMMAND = "/override-round-cap"
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")


def _verdict_time(value: object, now: datetime) -> datetime | None:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    parsed = parsed.astimezone(timezone.utc)
    return parsed if 2020 <= parsed.year <= 2100 and parsed <= now + timedelta(minutes=5) else None


@dataclass(frozen=True)
class ReviewRound:
    heads: tuple[str, ...]
    blocking: bool
    started_reason: str
    awaiting_head: str = ""

    def describe(self) -> str:
        heads = ", ".join(head[:7] for head in self.heads)
        reviewed = (f"heads {heads}; {'blocking lens findings' if self.blocking else 'all clear'}"
                    if self.heads else "")
        candidate = (f"candidate {self.awaiting_head[:7]} awaiting lens verdicts"
                     if self.awaiting_head else "")
        return "; ".join(part for part in (reviewed, candidate) if part)


@dataclass(frozen=True)
class ReviewEpisode:
    head: str
    blocking: bool


@dataclass(frozen=True)
class RoundStatus:
    current_head: str
    rounds: tuple[ReviewRound, ...]
    round_number: int
    cap: int = ROUND_CAP
    current_has_verdict: bool = False
    indeterminate_head: str = ""
    unplaced_heads: tuple[str, ...] = ()
    indeterminate_reason: str = "a verdict timestamp is missing or malformed"

    @property
    def capped(self) -> bool:
        return bool(self.indeterminate_head) or self.round_number > self.cap

    @property
    def refused(self) -> bool:
        return self.capped

    def describe(self) -> str:
        count = f"at least {self.round_number}" if self.indeterminate_head else f"round {self.round_number}"
        lines = [f"review rounds: {count} of {self.cap} across the whole verdict history"]
        lines.extend(f"  round {i} started: {round_.started_reason}; {round_.describe()}"
                     for i, round_ in enumerate(self.rounds, 1))
        if self.indeterminate_head:
            if not self.current_has_verdict:
                lines.append(f"  candidate {self.current_head[:7]} awaiting lens verdicts")
            lines.append(f"  unplaced reviewed heads: {', '.join(self.unplaced_heads)}")
            lines.append(
                f"round order indeterminate: {self.indeterminate_reason} "
                f"for {self.indeterminate_head}; refusing unless the timestamp/tie override is valid."
            )
        elif self.capped:
            lines.append(
                f"{CAP_REACHED}. Current head would be round {self.round_number}; "
                "split the unit, cut its scope, or obtain an operator override."
            )
        return "\n".join(lines)


def _comment_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def ordered_comments(comments: list[dict]) -> list[dict]:
    """GitHub's paginated issue-comment list is already in creation order."""
    return comments


def order_lens_records(records) -> tuple[list, bool]:
    """Order verdicts once for round counting and carry.

    Unique source positions take precedence over timestamps. Without positions,
    every timestamp must parse and ties need distinct comment ids.
    """
    items = list(records)
    positions = [getattr(r, "source_order", None) for r in items]
    if all(type(p) is int and p >= 0 for p in positions) and len(set(positions)) == len(items):
        return sorted(items, key=lambda r: r.source_order), True
    now = datetime.now(timezone.utc)
    times = [_verdict_time(getattr(r, "created_at", None), now) for r in items]
    if any(t is None for t in times):
        return items, False
    ids = [_comment_id(getattr(r, "comment_id", None)) for r in items]
    for stamp in set(times):
        tied = [i for i, value in enumerate(times) if value == stamp]
        if len(tied) > 1 and (any(ids[i] is None for i in tied) or
                              len({ids[i] for i in tied}) != len(tied)):
            return items, False
    indexed = sorted(range(len(items)), key=lambda i: (times[i], ids[i] or 0))
    return [items[i] for i in indexed], True


def verdict_class(body: str, lens: str) -> tuple[str, bool]:
    """Classify a retry with the counter's verdict kind and blocking state."""
    import swarm_dispatch
    from review_panel import LENSES

    agent = next((item.agent for item in LENSES if item.lens == lens), None)
    if agent is None:
        return "unreadable", True
    return swarm_dispatch.lens_verdict_classification(body, lens_agent=agent)


def round_status(records: dict[str, list], current_head: str, *, cap: int = ROUND_CAP) -> RoundStatus:
    """Count contiguous review episodes in verdict order; unknown order denies."""
    current = (current_head or "").lower()
    now = datetime.now(timezone.utc)
    verdicts: list[tuple[datetime, int | None, int | None, str, bool]] = []
    unplaced: set[str] = set()
    invalid_timestamps: set[str] = set()
    reviewed: set[str] = set()
    ordered_records, order_known = order_lens_records(
        rec for recs in records.values() for rec in recs
    )
    for rec in ordered_records:
        head = (getattr(rec, "head", "") or "").lower()
        if not head:
            continue
        reviewed.add(head)
        timestamp = _verdict_time(getattr(rec, "created_at", None), now)
        if timestamp is None:
            unplaced.add(head)
            invalid_timestamps.add(head)
            continue
        verdicts.append((
            timestamp, _comment_id(getattr(rec, "comment_id", None)),
            getattr(rec, "source_order", None), head,
            bool(getattr(rec, "blocking", not getattr(rec, "cleared", False))),
        ))

    by_time: dict[datetime, list[tuple[datetime, int | None, int | None, str, bool]]] = {}
    for verdict in verdicts:
        by_time.setdefault(verdict[0], []).append(verdict)
    ambiguous_times = {
        timestamp for timestamp, tied in by_time.items()
        if len(tied) > 1 and any(verdict[1] is None for verdict in tied)
    }
    for timestamp in ambiguous_times:
        unplaced.update(verdict[3] for verdict in by_time[timestamp])
    if not order_known:
        unplaced.update(reviewed)
    ordered = [verdict for verdict in verdicts if verdict[0] not in ambiguous_times]
    episodes: list[ReviewEpisode] = []
    for _, _, _, head, blocking in ordered:
        if episodes and episodes[-1].head == head:
            episodes[-1] = ReviewEpisode(head, episodes[-1].blocking or blocking)
        else:
            episodes.append(ReviewEpisode(head, blocking))

    rounds: list[ReviewRound] = []
    previous: ReviewEpisode | None = None
    seen_heads: set[str] = set()
    any_blocking = False
    for episode in episodes:
        # A return to a head that was already reviewed, after any blocking
        # episode in the history, is a new round even when the episode just
        # before it was clear: the cap must not be reset by re-reviewing an
        # old head.
        returned = episode.head in seen_heads and any_blocking
        if previous is None or previous.blocking or returned:
            reason = ("first review episode" if previous is None else
                      "the previous episode was blocking" if previous.blocking else
                      f"returned to {episode.head[:7]} after an earlier blocking episode")
            rounds.append(ReviewRound((episode.head,), episode.blocking, reason))
        else:
            last = rounds[-1]
            rounds[-1] = ReviewRound(last.heads + (episode.head,), episode.blocking,
                                     last.started_reason)
        previous = episode
        seen_heads.add(episode.head)
        any_blocking = any_blocking or episode.blocking

    if unplaced:
        unknown = tuple(sorted(unplaced))
        reason = (
            "a verdict timestamp is missing or malformed"
            if invalid_timestamps else "tied verdict timestamps lack usable integer comment ids"
        )
        affected_head = min(invalid_timestamps) if invalid_timestamps else unknown[0]
        return RoundStatus(current, tuple(rounds), max(1, len(rounds)), cap, current in reviewed,
                           affected_head, unknown, reason)

    if previous is None or previous.head != current:
        # The current head has no latest episode yet. Even a previously reviewed
        # SHA is a new episode after verdicts on another head.
        if previous is None or previous.blocking or (current in seen_heads and any_blocking):
            reason = ("first review episode" if previous is None else
                      "the previous episode was blocking" if previous.blocking else
                      f"returned to {current[:7]} after an earlier blocking episode")
            rounds.append(ReviewRound((), False, reason, current))
        else:
            last = rounds[-1]
            rounds[-1] = ReviewRound(last.heads, last.blocking, last.started_reason, current)
    return RoundStatus(current, tuple(rounds), len(rounds), cap, current in reviewed)


def operator_override(comments: list[dict], owner_login: str, operator_login: str, repo: str, pr: int) -> dict | None:
    """Latest PR-local operator comment for the cap or timestamp/tie uncertainty.

    Lens identity remains an independent refusal; this comment cannot lift it.
    """
    if (not isinstance(owner_login, str) or not isinstance(operator_login, str)
            or not owner_login or owner_login != operator_login):
        return None
    valid = []
    now = datetime.now(timezone.utc)
    url_pattern = re.compile(rf"https://github\.com/{re.escape(repo)}/pull/{pr}#issuecomment-([0-9]+)\Z")
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        lines = body.splitlines()
        user = comment.get("user")
        if not isinstance(user, dict):
            continue
        login = user.get("login")
        association = comment.get("author_association")
        created = comment.get("created_at")
        updated = comment.get("updated_at")
        url = comment.get("html_url")
        match = url_pattern.fullmatch(url) if isinstance(url, str) else None
        comment_id = comment.get("id")
        reason = "\n".join(lines[1:]).strip()
        if (
            lines and lines[0].rstrip(" \t") == OVERRIDE_COMMAND
            and login == operator_login
            and user.get("type") == "User"
            and isinstance(association, str) and bool(association)
            and association.upper() not in {"BOT", "APP"}
            and created == updated and _verdict_time(created, now) is not None
            and len(reason) >= 20
            and match is not None
            and _comment_id(match.group(1)) is not None
            and type(comment_id) is int and comment_id > 0
            and comment_id == _comment_id(match.group(1))
        ):
            valid.append(comment)
    return max(valid, key=lambda comment: (_verdict_time(comment["created_at"], now),
                                           _comment_id(comment.get("id")) or 0)) if valid else None
