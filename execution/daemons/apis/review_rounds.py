"""Count review rounds from ordered episodes of author-scoped lens verdicts.

Contiguous verdicts for one head form an episode; returning to that head after
another head forms another episode because the poster keeps the return as a
new comment. Repeated lens/head comments at different history positions remain
distinct records. An episode after a blocking or unreadable
episode starts a round, while an episode after an all-clear one stays in the
same round. Timestamp ties use integer GitHub comment ids, then source order.
Missing timestamps or ids needed to resolve a tie make order indeterminate and
refuse approval unless a valid operator override is present.
The three-round cap can be overridden only by the operator's unedited,
PR-local GitHub comment; the approval gate verifies that comment separately.
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

    def describe(self) -> str:
        heads = ", ".join(head[:7] for head in self.heads)
        return f"heads {heads}; {'blocking lens findings' if self.blocking else 'all clear'}"


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
        lines.extend(f"  round {i}: {round_.describe()}" for i, round_ in enumerate(self.rounds, 1))
        if self.indeterminate_head:
            lines.append(f"  unplaced reviewed heads: {', '.join(self.unplaced_heads)}")
            lines.append(
                f"round order indeterminate: {self.indeterminate_reason} "
                f"for {self.indeterminate_head}; refusing unless the operator override is valid."
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
    verdicts: list[tuple[datetime, int | None, int, str, bool]] = []
    unplaced: set[str] = set()
    invalid_timestamps: set[str] = set()
    reviewed: set[str] = set()
    for recs in records.values():
        for rec in recs:
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
                getattr(rec, "source_order", 0), head,
                bool(getattr(rec, "blocking", not getattr(rec, "cleared", False))),
            ))

    by_time: dict[datetime, list[tuple[datetime, int | None, int, str, bool]]] = {}
    for verdict in verdicts:
        by_time.setdefault(verdict[0], []).append(verdict)
    ambiguous_times = {
        timestamp for timestamp, tied in by_time.items()
        if len(tied) > 1 and any(verdict[1] is None for verdict in tied)
    }
    for timestamp in ambiguous_times:
        unplaced.update(verdict[3] for verdict in by_time[timestamp])
    ordered = sorted(
        (verdict for verdict in verdicts if verdict[0] not in ambiguous_times),
        key=lambda verdict: (verdict[0], verdict[1] if verdict[1] is not None else -1, verdict[2]),
    )
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
            rounds.append(ReviewRound((episode.head,), episode.blocking))
        else:
            last = rounds[-1]
            rounds[-1] = ReviewRound(last.heads + (episode.head,), episode.blocking)
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
            rounds.append(ReviewRound((current,), False))
        else:
            last = rounds[-1]
            rounds[-1] = ReviewRound(last.heads + (current,), False)
    return RoundStatus(current, tuple(rounds), len(rounds), cap, current in reviewed)


def operator_override(comments: list[dict], owner_login: str, operator_login: str, repo: str, pr: int) -> dict | None:
    """Latest valid operator comment when the human owner is the operator."""
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
            lines and lines[0].strip() == OVERRIDE_COMMAND
            and login == operator_login
            and user.get("type") == "User"
            and (association is None or isinstance(association, str))
            and (association or "").upper() not in {"BOT", "APP"}
            and created == updated and _verdict_time(created, now) is not None
            and len(reason) >= 20
            and match is not None
            and _comment_id(match.group(1)) is not None
            and (comment_id is None or _comment_id(comment_id) == _comment_id(match.group(1)))
        ):
            valid.append(comment)
    return max(valid, key=lambda comment: (_verdict_time(comment["created_at"], now),
                                           _comment_id(comment.get("id")) or 0)) if valid else None
