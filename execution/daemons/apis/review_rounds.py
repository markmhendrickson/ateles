"""Count genuine review rounds from the swarm's author-scoped lens records.

A round starts at the first reviewed head. A later head starts another round
only after a blocking (or unreadable) verdict on a head in the current round.
Clear re-reviews and repeat runs on the same head do not spend another round.
The three-round cap can be overridden only by the operator's unedited,
PR-local GitHub comment; the approval gate verifies that comment separately.
"""

from __future__ import annotations

from dataclasses import dataclass

ROUND_CAP = 3
CAP_REACHED = "round cap reached: split or cut scope"
OVERRIDE_COMMAND = "/override-round-cap"


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

    @property
    def capped(self) -> bool:
        return self.round_number > self.cap

    @property
    def refused(self) -> bool:
        return self.capped

    def describe(self) -> str:
        lines = [f"review rounds: round {self.round_number} of {self.cap}"]
        lines.extend(f"  round {i}: {round_.describe()}" for i, round_ in enumerate(self.rounds, 1))
        if self.capped:
            lines.append(
                f"{CAP_REACHED}. Current head would be round {self.round_number}; "
                "split the unit, cut its scope, or obtain an operator override."
            )
        return "\n".join(lines)


def round_status(records: dict[str, list], current_head: str, *, cap: int = ROUND_CAP) -> RoundStatus:
    """Group verdict-bearing heads by first comment creation time."""
    current = (current_head or "").lower()
    by_head: dict[str, dict] = {}
    for recs in records.values():
        for rec in recs:
            head = (getattr(rec, "head", "") or "").lower()
            if not head:
                continue
            timestamp = getattr(rec, "created_at", "") or ""
            entry = by_head.setdefault(head, {"created_at": timestamp, "blocking": False})
            entry["created_at"] = min(entry["created_at"], timestamp)
            entry["blocking"] |= bool(getattr(rec, "blocking", not getattr(rec, "cleared", False)))

    rounds: list[ReviewRound] = []
    head_round: dict[str, int] = {}
    for head, item in sorted(by_head.items(), key=lambda pair: (pair[1]["created_at"], pair[0])):
        if not rounds or rounds[-1].blocking:
            rounds.append(ReviewRound((head,), item["blocking"]))
        else:
            previous = rounds[-1]
            rounds[-1] = ReviewRound(previous.heads + (head,), item["blocking"])
        head_round[head] = len(rounds)

    if current in head_round:
        number = head_round[current]
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
            and login.lower() == operator_login.lower()
            and user.get("type", "User").lower() != "bot"
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
