#!/usr/bin/env python3
"""
check_policy_rule_kind.py — fail when two `active` `agent_policy` rows state the
same normative subject at the same `scope` but disagree on `rule_kind`.

WHY THIS EXISTS:

  `agent_policy` rows are the swarm's standing rules. `rule_kind` is the field
  that says whether a rule BINDS (`mandatory`) or merely advises
  (`recommended`). When the same rule exists twice at the same scope under both
  values, an agent reading the record can truthfully conclude either that a
  safety constraint is binding or that it is optional. Which one it gets
  depends on retrieval order — that is, on nothing.

  The concrete instance (ateles#1114, ruled 2026-09-18): two `active` rows both
  titled "Agent Background Execution Rules", both `scope: global`, both saying
  "Never delete files without confirmation. Never modify foundational docs
  without human review. Never deploy without human approval. Never modify git
  history." — one `mandatory`, one `recommended`. A second pair, "Global Agent
  Rules for Neotoma", had the same shape. The operator's ruling is MANDATORY
  SURVIVES: the fail-closed reading, with the `recommended` twin retired.

  The ruling closed those instances. It does not stop the next one. This does.

WHAT IT CHECKS — two independent signals:

  1. **rule_kind split (lexical).** Over the committed snapshot of
     `agent_policy` rows (see WHY A SNAPSHOT below), group every row whose
     `status` is `active` by (scope, normative subject). Report any group
     holding more than one distinct `rule_kind`.

     A row with a non-`active` status — `retired` is the vocabulary the
     foundation corpus uses (`docs/foundation/vocabulary.md#retired-names`) —
     is out of scope. Retiring the twin is the remedy, so a retired row must
     not keep the check red.

  2. **Reversed `supersedes` (structural).** Report a `supersedes` edge
     pointing FROM a retired row TO an active one. `data_model.md` defines the
     edge as "the source decision reverses or replaces the target", so it
     belongs on the SURVIVOR naming what it replaced. Written backwards, a
     retired `recommended` rule asserts it replaced the `mandatory` one it lost
     to — the ruling inverted, on the edge a consumer traverses.

     This signal is STRICTLY STRONGER than (1): it is structural, so it does
     not depend on two rows being named alike and has no paraphrase blind spot.
     It exists because the first implementation of this very ruling wrote all
     three edges backwards, and nothing mechanical caught it.

HOW "SAME NORMATIVE SUBJECT" IS DECIDED — and what it misses:

  Heuristic: the **title stem**, defined as the title lowercased with any
  trailing bracketed qualifier (`[governance]`, `[strategy_governance]`)
  removed, then whitespace-collapsed. Rows with no title fall back to their
  `canonical_name`.

  This was chosen because it is exactly the shape the real defect took: both
  known pairs share a title stem and differ ONLY by the bracketed suffix naming
  the source document set they were imported from. It is mechanical, it needs
  no embedding model or network call, and it produces no false positives on the
  current record (verified: the only groups it forms with >1 member are the
  known pairs).

  WHAT IT WOULD MISS — stated plainly, because a check whose blind spots are
  undocumented invites false confidence:

  - **Paraphrase.** Two rows saying the same thing under genuinely different
    titles ("Agent Background Execution Rules" vs "Autonomous Execution
    Constraints") are invisible to it. Title similarity is a proxy for subject
    identity, not a measure of it.
  - **Homonyms.** Two rows sharing a title stem but governing genuinely
    different subjects would be a false positive. None exists today; the
    suppression comment below is the escape hatch if one ever does.
  - **Body-only divergence.** Rows whose titles match and whose `description`
    bodies have drifted apart are still grouped. That is deliberate — a drifted
    body under one title is its own defect — but this check reports the
    `rule_kind` disagreement, not the drift.
  - **Cross-scope contradiction.** A `mandatory` global rule and a
    `recommended` swarm-scoped rule on the same subject are NOT reported,
    because a narrower scope legitimately refines a broader one. Whether that
    is always true is a judgement this check does not make.

  Upgrading the heuristic (normalized description hashing, embedding
  similarity) is a strictly larger change and would need its own false-positive
  budget. The title-stem rule catches the class that actually occurred.

WHY A SNAPSHOT, NOT A LIVE QUERY:

  This runs in `scripts/lint.sh` and as a blocking step in CI. CI holds no
  `NEOTOMA_BEARER_TOKEN`, and a check that reaches the network would be
  skipped, flaky, or hang there — which is how a control stops binding without
  anyone noticing (CLAUDE.md, "A mechanism that does not bind is not a
  control"). So the check compares against a COMMITTED snapshot, regenerated
  deliberately with `--write` by someone holding a token. Same reasoning, and
  the same shape, as `.claude/hooks/hook_wiring_reference.py`.

  The snapshot going stale is a real cost and is accepted: a stale snapshot
  fails loudly on the rows it does hold, rather than silently passing on rows
  it cannot reach.

WHAT THE SNAPSHOT MAY CARRY — this repo is PUBLIC:

  `--write` renders record entities into a file that is committed publicly, so
  the boundary is filtered twice.

  MINIMIZED: only `entity_id`, `title_stem`, `scope`, `rule_kind`, `status` and
  `supersedes` are stored — exactly what the two checks compare. `body`,
  `description`, `rationale` and `summary` are never copied, and `title_stem`
  is truncated to STEM_MAX_CHARS, because rows with no `title` fall back to a
  `canonical_name` that on some rows is the entire policy body (~3,300
  characters on one). This cut the file by more than half.

  FAIL CLOSED: `--write` REFUSES, naming the offending entity_id, when any
  stored value matches an operator-PII shape (BTC address, IBAN, phone,
  currency figure). It cannot tell a rule ABOUT a payment address from one
  CONTAINING one, and `gitleaks` runs on the pull request rather than here — so
  without this the only thing between a PII-bearing row and a public commit is
  a reviewer reading a generated JSON blob, which is not a control. Other
  entity types in this instance (`standing_rule`) do carry live payment
  details; nothing stops a future policy row from doing the same.

  The remedy for a refusal is never to loosen the pattern: fix the row in
  Neotoma so the operator specifics live in a `payment_profile` / `contact` /
  `vendor_binding` entity referenced by type, then re-run.

SUPPRESSION: add the title stem to `ALLOWED_DIVERGENCE` below with a reason —
for a pair that genuinely shares a title while governing different subjects.
The usual correct action is NOT to suppress: it is to retire one row, keeping
the `mandatory` one, per the operator's 2026-09-18 ruling.

Usage:
  python3 scripts/linters/check_policy_rule_kind.py
  # checks the committed snapshot; exit 1 on a contradiction

  python3 scripts/linters/check_policy_rule_kind.py --write
  # regenerates the snapshot from live Neotoma (needs NEOTOMA_BEARER_TOKEN)

  python3 scripts/linters/check_policy_rule_kind.py --snapshot <path>
  # checks an alternate snapshot file (used by the tests)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = REPO_ROOT / "docs" / "governance" / "agent_policy_rule_kind.json"

# Title stems that legitimately carry more than one rule_kind at one scope.
# Map stem -> reason. Empty by design: the remedy is retirement, not exemption.
ALLOWED_DIVERGENCE: dict[str, str] = {}

# A trailing bracketed qualifier, e.g. "[governance]" / "[strategy_governance]".
# These name the source document set a row was imported from, not its subject.
_BRACKET_SUFFIX = re.compile(r"\s*\[[^\]]*\]\s*$")

ACTIVE_STATUS = "active"

# Upper bound on a stored title stem. Rows with no `title` fall back to
# `canonical_name`, which on some rows is the entire policy body. This bounds
# what a public snapshot can carry from any one row.
STEM_MAX_CHARS = 120


def subject_key(row: dict) -> str:
    """The normative-subject proxy: title stem, bracketed qualifier removed.

    Falls back to canonical_name when a row carries no title. See the module
    docstring for what this catches and what it misses.
    """
    # A committed row carries `title_stem`, already reduced at write time. A
    # live row (or a test fixture) carries `title`, falling back to
    # `canonical_name`. Both paths land on the same normalization.
    stem = (row.get("title_stem") or "").strip()
    if stem:
        return " ".join(stem.lower().split())
    raw = (row.get("title") or "").strip()
    if not raw:
        raw = (row.get("canonical_name") or "").strip()
        # canonical_name is "agent_policy:<title>" for these rows.
        if ":" in raw:
            raw = raw.split(":", 1)[1]
    stem = _BRACKET_SUFFIX.sub("", raw)
    stem = " ".join(stem.lower().split())
    # Some rows carry no title at all and their canonical_name is the entire
    # policy body — 948 chars on one row, ~3,300 on another. Truncating bounds
    # what reaches a public file AND keeps the subject key to the part that
    # actually identifies a subject; a body that long is not a title. Collisions
    # from truncation would only ever group rows already identical for the first
    # 120 characters, which is the behaviour wanted anyway.
    return stem[:STEM_MAX_CHARS].rstrip()


def is_active(row: dict) -> bool:
    """Only `active` rows bind. A retired row is the remedy, not the defect."""
    return (row.get("status") or "").strip().lower() == ACTIVE_STATUS


def find_contradictions(rows: list[dict]) -> list[dict]:
    """Group active rows by (scope, subject); report groups with >1 rule_kind."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        if not is_active(row):
            continue
        key = (str(row.get("scope") or ""), subject_key(row))
        if not key[1]:
            continue
        groups.setdefault(key, []).append(row)

    findings = []
    for (scope, subject), members in sorted(groups.items()):
        kinds = {str(m.get("rule_kind") or "") for m in members}
        if len(kinds) <= 1:
            continue
        if subject in ALLOWED_DIVERGENCE:
            continue
        findings.append(
            {
                "scope": scope,
                "subject": subject,
                "kinds": sorted(kinds),
                "members": sorted(members, key=lambda m: m.get("entity_id") or ""),
            }
        )
    return findings


def find_reversed_supersedes(rows: list[dict]) -> list[dict]:
    """Report a `supersedes` edge pointing FROM a retired row TO an active one.

    `data_model.md` defines the edge as "the source decision reverses or
    replaces the target", so it belongs on the SURVIVING row naming what it
    replaced. Written the other way, a retired `recommended` rule claims to have
    replaced the `mandatory` one it lost to — the exact inversion of the ruling,
    asserted on the edge a consumer traverses.

    This is a STRICTLY BETTER signal than the title-stem heuristic above: it is
    structural, so it does not depend on two rows being named alike, and it has
    no paraphrase blind spot. It catches a real error made while implementing
    this very ruling — the first pass wrote all three edges backwards, and
    nothing caught it but a human reading the diff.
    """
    by_id = {r.get("entity_id"): r for r in rows if r.get("entity_id")}
    findings = []
    for row in sorted(rows, key=lambda r: r.get("entity_id") or ""):
        target_id = (row.get("supersedes") or "").strip()
        if not target_id or is_active(row):
            continue
        target = by_id.get(target_id)
        if target is None or not is_active(target):
            continue
        findings.append(
            {
                "source": row.get("entity_id"),
                "source_status": row.get("status"),
                "source_kind": row.get("rule_kind"),
                "target": target_id,
                "target_status": target.get("status"),
                "target_kind": target.get("rule_kind"),
            }
        )
    return findings


def load_snapshot(path: Path) -> list[dict]:
    if not path.exists():
        print(
            f"check_policy_rule_kind: snapshot not found at "
            f"{path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path}. "
            "Regenerate it with --write (needs NEOTOMA_BEARER_TOKEN).",
            file=sys.stderr,
        )
        raise SystemExit(2)
    data = json.loads(path.read_text())
    return data.get("policies", [])


def fetch_live() -> list[dict]:
    """Pull agent_policy rows from Neotoma prod. Only used by --write."""
    # Both are env-sourced with no baked-in default: the instance host varies by
    # operator, and a hardcoded fallback is the drift CLAUDE.md's config-sourcing
    # rule forbids. Absent config fails loudly rather than silently pointing at
    # someone else's instance.
    token = os.environ.get("NEOTOMA_BEARER_TOKEN")
    base = os.environ.get("NEOTOMA_BASE_URL")
    if not token or not base:
        missing = [
            n
            for n, v in (("NEOTOMA_BEARER_TOKEN", token), ("NEOTOMA_BASE_URL", base))
            if not v
        ]
        print(
            f"check_policy_rule_kind: --write needs {' and '.join(missing)}.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    url = f"{base.rstrip('/')}/entities?entity_type=agent_policy&limit=500"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            # Cloudflare 1010-blocks urllib's default UA against this host.
            "User-Agent": "ateles-lint/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode())

    rows = []
    for ent in payload.get("entities", []):
        snap = ent.get("snapshot") or {}
        # MINIMIZATION. The check compares scope, rule_kind, status and a title
        # stem — nothing else. Everything else is dropped at the boundary rather
        # than copied and reviewed later, because this file lands in a PUBLIC
        # repo. `canonical_name` in particular carries full policy body text on
        # some rows (one is ~9,400 characters), so it is reduced to its stem
        # here; `subject_key` falls back to it, and the stem is all that fallback
        # ever reads. `body`, `description`, `rationale` and `summary` are never
        # copied at all.
        title = snap.get("title")
        row = {
            "entity_id": ent.get("entity_id"),
            "title_stem": subject_key(
                {"title": title, "canonical_name": ent.get("canonical_name")}
            ),
            "scope": snap.get("scope"),
            "rule_kind": snap.get("rule_kind"),
            "status": snap.get("status"),
            "supersedes": snap.get("supersedes") or "",
        }
        _screen_row_for_operator_pii(row, ent, snap)
        rows.append(row)
    rows.sort(key=lambda r: r.get("entity_id") or "")
    return rows


# Operator-PII shapes. These screen the MINIMIZED row that is about to be
# committed — a value matching any of them has no business in a public repo,
# whatever field carried it.
_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # BTC: legacy P2PKH/P2SH, and bech32.
    (
        "bitcoin address",
        re.compile(r"\b(?:[13][a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[a-z0-9]{25,62})\b"),
    ),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}\b")),
    # Phone: requires an international prefix (+NN) or a 00-prefixed trunk, and
    # then 7+ digits. An earlier, looser version matched the ISO date
    # "2026-06-24" inside an ordinary policy sentence and refused a clean row —
    # so it is anchored on the prefix, which a date never carries. A national
    # 9-digit string with no prefix is deliberately NOT matched: it is
    # indistinguishable from an ordinary number and would refuse constantly.
    # The IBAN, BTC and currency patterns carry the load for financial data.
    (
        "phone number",
        re.compile(
            r"(?:\+|\b00)\d{1,3}[\s.\-/]?(?:\(?\d{1,4}\)?[\s.\-/]?){2,5}\d{2,4}\b"
        ),
    ),
    # Currency: a symbol or ISO code adjacent to a figure. `\d{4}` alone is a
    # year, so a bare number is never matched.
    (
        "currency figure",
        re.compile(r"(?:[€$£]\s?\d[\d.,]*|\b\d[\d.,]*\s?(?:EUR|USD|GBP)\b)"),
    ),
)


class OperatorPIIRefusal(SystemExit):
    """Raised when --write would commit a row matching an operator-PII shape."""


def _screen_row_for_operator_pii(row: dict, ent: dict, snap: dict) -> None:
    """Refuse — never warn — when a row about to be committed looks like PII.

    FAIL CLOSED, and deliberately so. `--write` cannot distinguish a rule ABOUT
    a payment address from a rule CONTAINING one, and guessing wrong in the
    permissive direction publishes operator data to a public repo. `gitleaks`
    runs on the pull request, not here, so without this the only thing between a
    PII-bearing policy row and a public commit is a reviewer reading a large
    generated JSON blob — which is not a control.

    A false positive costs one person one minute and a named entity_id. A false
    negative is irreversible and public. The asymmetry decides the default.

    The remedy is never to loosen the pattern: it is to fix the offending row in
    Neotoma (operator specifics belong in a `payment_profile` / `contact` /
    `vendor_binding` entity, referenced by type — CLAUDE.md), then re-run.
    """
    for field, value in row.items():
        if field == "entity_id" or not isinstance(value, str) or not value:
            continue
        for label, pattern in _PII_PATTERNS:
            if pattern.search(value):
                raise OperatorPIIRefusal(
                    "check_policy_rule_kind: REFUSING to write the snapshot.\n"
                    f"  entity_id : {ent.get('entity_id')}\n"
                    f"  field     : {field}\n"
                    f"  matched   : {label}\n"
                    "This file is committed to a PUBLIC repo. Operator specifics "
                    "belong in a Neotoma entity referenced by type, not inline in "
                    "a policy row. Fix the row in Neotoma, then re-run --write. "
                    "Do not loosen the pattern to get past this."
                )


def write_snapshot(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "_comment": (
            "GENERATED by scripts/linters/check_policy_rule_kind.py --write. "
            "Do not hand-edit. This is the committed snapshot of agent_policy "
            "rule_kind values that the linter checks in CI, where no Neotoma "
            "token exists."
        ),
        "policies": rows,
    }
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--snapshot", type=Path, default=SNAPSHOT_PATH)
    args = parser.parse_args(argv)

    if args.write:
        rows = fetch_live()
        write_snapshot(args.snapshot, rows)
        print(f"check_policy_rule_kind: wrote {len(rows)} rows to {args.snapshot}")
        return 0

    rows = load_snapshot(args.snapshot)
    findings = find_contradictions(rows)
    reversed_edges = find_reversed_supersedes(rows)
    if not findings and not reversed_edges:
        return 0

    for f in findings:
        print(
            f"agent_policy rule_kind contradiction at scope '{f['scope']}' "
            f"for subject '{f['subject']}': {', '.join(f['kinds'])}"
        )
        for m in f["members"]:
            print(
                f"    {m.get('entity_id')}  rule_kind={m.get('rule_kind')}  "
                f"stem={subject_key(m)}"
            )

    for e in reversed_edges:
        print(
            f"agent_policy reversed `supersedes`: {e['source']} "
            f"({e['source_status']}/{e['source_kind']}) claims to supersede "
            f"{e['target']} ({e['target_status']}/{e['target_kind']})"
        )

    if findings:
        print(
            f"\n{len(findings)} rule_kind contradiction(s) found.\n"
            "Two active agent_policy rows state the same rule at the same scope "
            "and disagree on whether it binds. An agent reading the record can "
            "truthfully conclude either.\n"
            "REMEDY (operator ruling, 2026-09-18, ateles#1114): MANDATORY "
            "SURVIVES. Keep the `mandatory` row and correct the other's `status` "
            "to 'retired'. Write `supersedes` on the SURVIVING row naming the "
            "retired one — `data_model.md` defines the edge as the source "
            "replacing the target, so the survivor is the source. Then re-run "
            "with --write.\n"
            "agent_policy is a GATED governance type — if a write is refused, "
            "that is a correct outcome; report it rather than working around it.",
            file=sys.stderr,
        )
    if reversed_edges:
        print(
            f"\n{len(reversed_edges)} reversed `supersedes` edge(s) found.\n"
            "A retired row carries `supersedes` pointing at an active one, which "
            "asserts that the retired rule replaced its own replacement — the "
            "inversion of whatever ruling retired it, stated on the edge a "
            "consumer actually traverses.\n"
            "REMEDY: clear `supersedes` on the retired row and write it on the "
            "SURVIVOR, naming the retired row. Then re-run with --write.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
