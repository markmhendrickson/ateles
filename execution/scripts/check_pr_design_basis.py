#!/usr/bin/env python3
"""Fail a pull request whose body does not state its design basis.

Every PR names, in its body, the foundation document and section it conforms
to, or says that no design applies and why (``docs/foundation/conformance.md``,
"Design basis"). A missing or empty line is a blocking ``design-basis`` finding
from the pm lens; before this check it surfaced only after a lens had been
spent on the PR. This is the mechanical pre-check, run in CI on every PR.

One grammar, not two
--------------------

The rules for what counts as a valid basis live in
``execution/daemons/apis/foundation.py::check_design_basis`` -- the function
the pm and arch lens prompts embed. This script finds the ``Design basis:``
line in the PR body and hands its VALUE to that function, so CI and the lens
agree on:

* a cited ``docs/foundation/<doc>.md[#section]`` that exists on this checkout;
* a cited document that does NOT exist is INVALID;
* ``no design applies`` as the declared alternative.

The one rule added here, taken from the documented form
(``Design basis: no design applies -- <one line why>``): the no-design form
must carry a reason. ``check_design_basis`` accepts the bare phrase because
the arch lens then judges whether the declaration is true; the reason is what
gives it something to judge, and conformance.md names it as part of the form.

Where the line is found
-----------------------

A line starting (after optional list marker, quote marker, or bold) with
``Design basis:``. HTML comments and fenced code blocks are ignored, so the
template's own instructions and an example quoted in a code block never count
as the statement. If several lines are present the PR passes when at least one
is valid; otherwise every failure is reported.

Exemption (documented, mechanical)
----------------------------------

A PR is exempt only when EVERY changed file is one of:

* a generated render target -- never hand-authored, so there is no author
  decision to cite a basis for: ``docs/taxonomy.md``, ``docs/phases.md``,
  ``docs/architecture.md``, ``docs/foundation/decision_state.md``,
  ``docs/foundation/projection/*.md``, ``docs/agents/*.md``,
  ``.claude/skills/*/SKILL.md`` (CLAUDE.md names these as render targets); or
* a markdown file under ``docs/`` outside ``docs/foundation/`` (docs-only).

``docs/foundation/**`` hand-authored files are NOT exempt: they ARE the design,
and a change to one is exactly where a stated basis matters. An empty or
unreadable changed-file list is NOT exempt (fail closed): an exemption must be
proven from the list, never assumed from its absence.

Inputs: the PR body is read from the ``pull_request`` event payload
(``--event``, normally ``$GITHUB_EVENT_PATH``), not from a fetched copy.
Exit 0 = pass or exempt, 1 = fail, 2 = could not run (not a pass).

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# foundation.py is stdlib-only and is where the lenses' grammar lives.
sys.path.insert(0, str(REPO_ROOT / "execution" / "daemons" / "apis"))
from foundation import NO_DESIGN_APPLIES, check_design_basis  # noqa: E402

# Generated render targets (CLAUDE.md "Standing constraints" and the generated
# headers on each file).
GENERATED_EXACT = frozenset(
    {
        "docs/taxonomy.md",
        "docs/phases.md",
        "docs/architecture.md",
        "docs/foundation/decision_state.md",
    }
)
GENERATED_PATTERNS = (
    re.compile(r"^docs/foundation/projection/[^/]+\.md$"),
    re.compile(r"^docs/agents/[^/]+\.md$"),
    re.compile(r"^\.claude/skills/[^/]+/SKILL\.md$"),
)
DOCS_ONLY = re.compile(r"^docs/.+\.md$")

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_LINE_RE = re.compile(
    r"^[ \t]*(?:>[ \t]*)*(?:[-*+][ \t]+)?(?:\*\*|__)?[ \t]*design[ \t]+basis[ \t]*:"
    r"(?:\*\*|__)?(?P<value>.*)$",
    re.IGNORECASE,
)
_SEPARATORS = " \t:;,.-–—*_`"


def is_exempt_path(path: str) -> bool:
    p = path.strip()
    if p.startswith("./"):
        p = p[2:]
    if p in GENERATED_EXACT or any(r.match(p) for r in GENERATED_PATTERNS):
        return True
    return bool(DOCS_ONLY.match(p)) and not p.startswith("docs/foundation/")


def exemption(changed: list[str]) -> str | None:
    """Why the PR is exempt, or None. An empty list is never exempt."""
    paths = [c for c in (x.strip() for x in changed) if c]
    if not paths:
        return None
    if all(is_exempt_path(p) for p in paths):
        return (
            f"all {len(paths)} changed file(s) are generated render targets "
            "or docs-only (markdown under docs/, outside docs/foundation/)"
        )
    return None


def design_basis_lines(body: str | None) -> list[str]:
    """Values of every ``Design basis:`` line outside comments and code fences."""
    text = _COMMENT_RE.sub("", body or "")
    values: list[str] = []
    fenced = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = _LINE_RE.match(line)
        if m:
            values.append(m.group("value").strip(" \t*_"))
    return values


def _no_design_reason(value: str) -> str:
    i = value.lower().find(NO_DESIGN_APPLIES)
    rest = value[i + len(NO_DESIGN_APPLIES) :] if i >= 0 else ""
    return rest.strip(_SEPARATORS)


def judge_value(value: str, root: Path | None = None) -> tuple[bool, str]:
    """(ok, reason) for one ``Design basis:`` value, via the lenses' own check."""
    if not value.strip():
        return False, "the `Design basis:` line is empty"
    check = check_design_basis(value, root)
    if not check.ok:
        return False, check.summary
    if not check.citations:  # accepted only as the no-design declaration
        reason = _no_design_reason(value)
        if len(re.findall(r"[A-Za-z]{2,}", reason)) < 2:
            return False, (
                f"INVALID -- `{NO_DESIGN_APPLIES}` needs a one-line reason "
                f"(`Design basis: {NO_DESIGN_APPLIES} -- <why>`)"
            )
    return True, check.summary


def evaluate(
    body: str | None, changed: list[str], root: Path | None = None
) -> tuple[bool, str]:
    why = exemption(changed)
    if why:
        return True, f"exempt: {why}"
    values = design_basis_lines(body)
    if not values:
        return False, "MISSING -- the PR body has no `Design basis:` line"
    results = [judge_value(v, root) for v in values]
    for ok, summary in results:
        if ok:
            return True, summary
    return False, "; ".join(summary for _, summary in results)


HELP_TEXT = """\
Add one line to the PR body, then edit the PR (this check re-runs on edit):

  Design basis: docs/foundation/<doc>.md#<section>
  Design basis: no design applies -- <one line why>

Exempt, with no line needed: PRs whose every changed file is a generated
render target (docs/taxonomy.md, docs/phases.md, docs/architecture.md,
docs/foundation/decision_state.md, docs/foundation/projection/*.md,
docs/agents/*.md, .claude/skills/*/SKILL.md) or a markdown file under docs/
outside docs/foundation/."""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="PR body must state its design basis")
    ap.add_argument("--event", required=True, help="pull_request event payload JSON")
    ap.add_argument(
        "--changed-files",
        required=True,
        help="file with one changed path per line (renames list both paths)",
    )
    args = ap.parse_args(argv)
    try:
        event = json.loads(Path(args.event).read_text(encoding="utf-8"))
        body = event["pull_request"].get("body")
        changed = Path(args.changed_files).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"::error::design-basis check could not run: {exc!r}")
        return 2
    ok, summary = evaluate(body, changed)
    if ok:
        print(f"design basis OK -- {summary}")
        return 0
    print(f"::error title=Design basis::{summary}")
    print(f"design basis FAILED -- {summary}\n\n{HELP_TEXT}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
