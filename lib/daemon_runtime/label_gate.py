"""
Swarm label gate (bootstrap mode / canary lane) — the one shared predicate.

`ATELES_SWARM_REQUIRE_LABEL` restricts the swarm's automatic issue/PR work to
items carrying one label (agent_policy ent_d0f1a840e549b3b299f62397,
ateles#1269). Two daemons start that work independently:

* **Apis** (`execution/daemons/apis/swarm_dispatch.py`,
  `github_gateway.py`) — GitHub webhooks and its periodic sweeps.
* **Anthus** (`execution/daemons/anthus/anthus.py`) — Neotoma `issue` /
  `pull_request` entity events, which it turns into gate-owner agent runs.

Both import the env var name, its normalization, the label parsing, the
match rule and the PR -> parent-issue link parser from here, so the two
cannot drift into different notions of "labelled". Each daemon keeps its own
I/O (Apis reads GitHub through its API client, Anthus through `gh`) and its
own logging, because those are where the two genuinely differ.

The rules this module fixes:

* **Unset means off.** An empty or whitespace-only value reads as "no gate",
  which is today's behaviour exactly.
* **Exact, case-sensitive match** on the label name, matching GitHub's own
  label-name convention. `Swarm-Canary` and `swarm-canary ` do not match.
* **Malformed label data denies, never raises.** Entries that are not a
  usable name are dropped, so bad data reads as "not labelled".
* **A PR inherits the label from its parent issue** named in its body by a
  closing keyword or a parentage form (`Closes #N`, `Part of #N`, `Refs #N`,
  ...). A cross-repo `owner/other#N` is ignored unless it names the PR's own
  repository, so a foreign repo's labelled issue cannot vouch for a PR here.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

REQUIRE_LABEL_ENV = "ATELES_SWARM_REQUIRE_LABEL"


def required_label(environ: Mapping[str, str] | None = None) -> str:
    """The configured gate label, or "" when the gate is off."""
    env = os.environ if environ is None else environ
    return (env.get(REQUIRE_LABEL_ENV) or "").strip()


def label_names(obj: Any) -> list[str]:
    """Label names from a GitHub issue/PR API object.

    GitHub's ``labels`` is a list of ``{"name": ...}`` dicts. Anything
    malformed — ``labels`` not a list, an entry that is not a dict, a name that
    is missing or not a string — is dropped, never raised on.
    """
    if not isinstance(obj, Mapping):
        return []
    raw = obj.get("labels")
    if not isinstance(raw, list):
        return []
    return [
        lbl["name"]
        for lbl in raw
        if isinstance(lbl, Mapping) and isinstance(lbl.get("name"), str) and lbl["name"]
    ]


def snapshot_label_names(raw: Any) -> list[str]:
    """Label names from a Neotoma ``issue`` / ``pull_request`` snapshot field.

    Neotoma stores ``labels`` as a list of strings on synced issues; other
    writers have used a JSON-encoded string, a comma-separated string, or a
    list of GitHub-shaped ``{"name": ...}`` dicts. All four are read. Anything
    else — ``None``, a number, an entry that is neither a string nor a named
    dict — contributes no label, so unreadable data denies rather than raises.
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                return []
        else:
            return [part.strip() for part in text.split(",") if part.strip()]
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for entry in raw:
        if isinstance(entry, str) and entry:
            names.append(entry)
        elif (
            isinstance(entry, Mapping)
            and isinstance(entry.get("name"), str)
            and entry["name"]
        ):
            names.append(entry["name"])
    return names


def carries_label(required: str, labels: Iterable[Any]) -> bool:
    """True when ``required`` is exactly one of ``labels``.

    Non-string entries never match. Callers check ``required`` first: an empty
    gate means the gate is off, which is not the same as "matches".
    """
    if not required:
        return False
    return any(isinstance(lbl, str) and lbl == required for lbl in labels)


# ── PR -> parent issue link ──────────────────────────────────────────────────
#
# Two DISTINCT questions, deliberately not the same regex (ateles#300):
#
#   CLOSURE_VERB  — "will GitHub auto-close the parent when this merges?"
#                   Exactly GitHub's own closing-keyword set, no more.
#   PARENT_LINK   — "which issue is this PR's parent, for GATE INHERITANCE?"
#                   A superset: every closure verb PLUS non-closing parentage
#                   forms (`Part of`, `Refs`, `Parent`, `Related to`).
#
# Bare `#N` is NOT a parent link: it is too easy to write incidentally, and a
# wrong parent is worse than none. The full history is on the re-exports in
# execution/daemons/apis/swarm_dispatch.py.
CLOSING_KEYWORDS = r"clos(?:e|es|ed)|fix(?:es|ed)?|resolv(?:e|es|ed)"
PARENTAGE_KEYWORDS = r"part\s+of|refs?|references?|parent|related\s+to"

# Optional `owner/repo` qualifier so a cross-repo parent is expressible.
ISSUE_REF = r"(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<number>\d+)"

PARENT_LINK = re.compile(
    rf"\b(?:{CLOSING_KEYWORDS}|{PARENTAGE_KEYWORDS})\s*:?\s+{ISSUE_REF}", re.I
)


def parent_issue_number(pr_body: str, repository: str = "") -> int | None:
    """The first same-repo parent issue a PR body declares, or None.

    A cross-repo reference (`owner/repo#N`) resolves ONLY when it names
    `repository`; every caller feeds the number to a same-repo lookup.
    """
    for m in PARENT_LINK.finditer(pr_body or ""):
        qualifier = m.group("repo")
        if qualifier and qualifier.lower() != (repository or "").lower():
            continue
        return int(m.group("number"))
    return None
