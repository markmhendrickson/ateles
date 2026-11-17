"""Count genuine review rounds from the swarm's author-scoped lens records.

A round starts at the first reviewed head. A later head starts another round
only after a blocking (or unreadable) verdict on a head in the current round.
The cap counts the whole PR verdict history: returning to an earlier head after
any blocking round spends a new round. Clear re-reviews and repeat runs on the
latest head do not. Missing or malformed verdict timestamps make round order
indeterminate and refuse approval unless a valid operator override is present.
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
class RoundStatus:
    current_head: str
    rounds: tuple[ReviewRound, ...]
    round_number: int
    cap: int = ROUND_CAP
    current_has_verdict: bool = False
    indeterminate_head: str = ""
    unplaced_heads: tuple[str, ...] = ()

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
                "round order indeterminate: a verdict timestamp is missing or malformed "
                f"for {self.indeterminate_head}; refusing unless the operator override is valid."
            )
        elif self.capped:
            lines.append(
                f"{CAP_REACHED}. Current head would be round {self.round_number}; "
                "split the unit, cut its scope, or obtain an operator override."
            )
        return "\n".join(lines)


def round_status(records: dict[str, list], current_head: str, *, cap: int = ROUND_CAP) -> RoundStatus:
    """Count all reviewed heads by their first valid UTC verdict time; unknown order denies."""
    current = (current_head or "").lower()
    now = datetime.now(timezone.utc)
    by_head: dict[str, dict] = {}
    for recs in records.values():
        for rec in recs:
            head = (getattr(rec, "head", "") or "").lower()
            if not head:
                continue
            timestamp = _verdict_time(getattr(rec, "created_at", None), now)
            entry = by_head.setdefault(head, {"created_at": timestamp, "blocking": False, "invalid": False})
            if timestamp is None:
                entry["invalid"] = True
            elif entry["created_at"] is None or timestamp < entry["created_at"]:
                entry["created_at"] = timestamp
            entry["blocking"] |= bool(getattr(rec, "blocking", not getattr(rec, "cleared", False)))

    rounds: list[ReviewRound] = []
    head_round: dict[str, int] = {}
    unplaced = tuple(sorted(head for head, item in by_head.items() if item["invalid"]))
    ordered = sorted(
        ((head, item) for head, item in by_head.items() if not item["invalid"]),
        key=lambda pair: (pair[1]["created_at"], pair[0]),
    )
    for head, item in ordered:
        if not rounds or rounds[-1].blocking:
            rounds.append(ReviewRound((head,), item["blocking"]))
        else:
            previous = rounds[-1]
            rounds[-1] = ReviewRound(previous.heads + (head,), item["blocking"])
        head_round[head] = len(rounds)

    if unplaced:
        # Never infer a cheaper order by placing an unreadable head first or
        # silently omitting it. The gate refuses regardless of the lower bound.
        return RoundStatus(current, tuple(rounds), max(1, len(rounds)), cap, current in by_head,
                           unplaced[0], unplaced)

    if current in head_round:
        if ordered[-1][0] != current and any(round_.blocking for round_ in rounds):
            number = len(rounds) + 1
            rounds.append(ReviewRound((current,), False))
        else:
            number = len(rounds)
    elif not rounds:
        number = 1
        rounds.append(ReviewRound((current,), False))
    elif rounds[-1].blocking:
        number = len(rounds) + 1
        rounds.append(ReviewRound((current,), False))
    else:
        number = len(rounds)
        last = rounds[-1]
        rounds[-1] = ReviewRound(last.heads + (current,), False)

    return RoundStatus(current, tuple(rounds), number, cap, current in by_head)


def operator_override(comments: list[dict], operator_login: str, repo: str, pr: int) -> dict | None:
    """Latest valid operator comment; caller supplies only this PR's comments."""
    valid = []
    for comment in comments:
        body = comment.get("body") or ""
        lines = body.splitlines()
        user = comment.get("user") or {}
        login = user.get("login") or ""
        association = (comment.get("author_association") or "").upper()
        created = comment.get("created_at") or ""
        updated = comment.get("updated_at") or ""
        reason = "\n".join(lines[1:]).strip()
        if (
            lines and lines[0].strip() == OVERRIDE_COMMAND
            and login == operator_login
            and user.get("type") == "User"
            and association not in {"BOT", "APP"}
            and created and updated == created
            and len(reason) >= 20
            and str(comment.get("html_url") or "").startswith(
                (f"https://github.com/{repo}/pull/{pr}#issuecomment-",
                 f"https://github.com/{repo}/issues/{pr}#issuecomment-")
            )
        ):
            valid.append(comment)
    return max(valid, key=lambda comment: comment["created_at"]) if valid else None
