"""One place that knows how an issue entity's GitHub number is spelled.

Issue entities in prod carry the GitHub issue number under FOUR different
field names. Measured against prod on 2026-09-02 across all 4,434 `issue`
entities (paged with a cursor — `offset` caps out at 2,000 with
`ERR_OFFSET_TOO_DEEP`, and a truncated read is what produced the false
readings this module exists to prevent):

    github_number         2,719   61.3%
    issue_number            385    8.7%
    github_issue_number      82    1.8%
    number                   55    1.2%
    (none of the four)    1,463   33.0%

The 1,463 with no number are not a gap: 1,457 of them are local/non-GitHub
issues carrying a `local_issue_id` instead, and legitimately have no GitHub
number at all. Counting only the 2,971 rows that DO carry a number:

    reachable by a `github_number`-only reader   2,719   91.5%
    INVISIBLE to a `github_number`-only reader     252    8.5%

That 8.5% is the defect. A reader keyed on one spelling does not error — it
returns nothing, and every gate/dispatch caller in this package fails CLOSED,
so the miss becomes a silent permanent block. Two separate agents independently
reported `source_url` resolving at 0% when the true rate was 94.8%, because
both keyed on `github_number` alone.

WHY WIDEN READERS RATHER THAN MIGRATE WRITERS
---------------------------------------------
`github_number` is canonical: it is what the `issue` schema declares as part
of the composite identity (`canonical_name_fields: [local_issue_id,
{composite: [github_number, repo]}, title]`), it is what every neotoma sync
path writes, and it already covers 91.5% of numbered rows. So new writes
should use `github_number` and nothing else.

But migrating the 252 stragglers would not let readers narrow, because:

  * The rows are append-only observations. A migration adds a correction; it
    does not remove the old field, so the other three spellings persist in the
    corpus and a narrowed reader would still miss anything written before the
    migration landed — including by any daemon running an older checkout.
  * Writers are spread across two repos and several daemons. Until every one
    of them is updated AND redeployed, new mis-spelled rows keep arriving. A
    narrowed reader would silently start missing them again.

So the fix is to make reading tolerant and writing canonical. That is strictly
safer than a migration: it is correct for rows written before it, after it, and
by any checkout that has not been updated. A migration remains worthwhile as
cleanup, but it is not what makes the readers correct, and it is not run here.

TYPES
-----
The same number is stored as both int and str (136 of the `github_number`
values are strings). Neotoma's server-side `snapshot_filters` coerces these —
verified against prod, `{"op":"eq","value":459}` and `{"op":"eq","value":"459"}`
return the identical three rows — so this is NOT a query hazard. It IS a hazard
for in-process comparisons: `snap.get("github_number") == 682` is False when
the stored value is `"682"`. Every comparison here goes through `str()`.
"""

from __future__ import annotations

# Ordered by measured prevalence, so the common case matches on the first try.
# `github_number` is canonical and leads; the rest are read-compatibility only
# and must NOT be used for new writes.
ISSUE_NUMBER_FIELDS: tuple[str, ...] = (
    "github_number",
    "issue_number",
    "github_issue_number",
    "number",
)

# The canonical field. New writes use this one and only this one.
CANONICAL_ISSUE_NUMBER_FIELD = "github_number"

# `repo` is canonical; `repository` appears on a minority of rows.
ISSUE_REPO_FIELDS: tuple[str, ...] = ("repo", "repository")


def extract_issue_number(snapshot: dict) -> int | None:
    """Return the GitHub issue number from *snapshot*, whichever field holds it.

    Returns None when the snapshot carries no number under any spelling — which
    for a local/non-GitHub issue is the correct answer, not a failure.

    Values may be stored as int or str; both are normalised to int here. A
    non-numeric value (empty string, None, a placeholder) is skipped rather
    than raising, so one malformed field cannot mask a good one that follows.
    """
    for field in ISSUE_NUMBER_FIELDS:
        value = snapshot.get(field)
        if value is None:
            continue
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            continue
    return None


def extract_issue_repo(snapshot: dict) -> str:
    """Return the repo slug from *snapshot*, tolerating `repo`/`repository`."""
    for field in ISSUE_REPO_FIELDS:
        value = snapshot.get(field)
        if value:
            return str(value)
    return ""


def issue_number_matches(snapshot: dict, issue_number: int) -> bool:
    """True when *snapshot* carries *issue_number* under ANY of the spellings.

    Compared as int after normalisation, so `"682"` and `682` both match — the
    string/int split in prod must not decide whether a gate can be read.
    """
    found = extract_issue_number(snapshot)
    return found is not None and found == int(issue_number)


def issue_matches(snapshot: dict, repo: str, issue_number: int) -> bool:
    """True when *snapshot* is the issue entity for ``repo#issue_number``."""
    if str(extract_issue_repo(snapshot)) != str(repo):
        return False
    return issue_number_matches(snapshot, issue_number)


def number_filter_candidates(issue_number: int, repo: str) -> list[dict]:
    """Server-side `snapshot_filters` to try, in prevalence order.

    Yields one filter per (number field x repo field) pair. Callers issue these
    in order and stop at the first that returns rows; a caller that tries only
    the first is back to the 91.5% reader this module exists to replace.

    The repo variants matter as much as the number ones: a filter hardcoding
    `repo` misses rows that carry only `repository`, and the miss is
    indistinguishable from a genuinely absent entity.
    """
    return [
        {
            number_field: {"op": "eq", "value": issue_number},
            repo_field: {"op": "eq", "value": repo},
        }
        for number_field in ISSUE_NUMBER_FIELDS
        for repo_field in ISSUE_REPO_FIELDS
    ]
