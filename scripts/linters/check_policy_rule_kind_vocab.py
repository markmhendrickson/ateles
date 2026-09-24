#!/usr/bin/env python3
"""
check_policy_rule_kind_vocab.py — refuse a committed `agent_policy` snapshot
carrying a `rule_kind` outside {mandatory, advisory}. The vocabulary guard for
ateles#1245.

WHY THIS EXISTS:

  `rule_kind` on `agent_policy` is closed to `mandatory` or `advisory`, and
  `docs/foundation/data_model.md` states that absence or any other value reads
  as `mandatory` — the restrictive branch, because this is the field carrying
  the safety meaning (`principles.md`, principle 5). That default is correct
  in isolation, but it means an out-of-vocabulary row does not fail: it binds,
  silently, at the strictest level, with nobody having ruled it so.

  Measured on prod 2026-09-24: 16 of 60 live rows carried a value outside the
  two ({recommended} x6, {operating_discipline} x5, absent x3, {allow} x1,
  {issue_spec_contribution} x1). Each was over-binding by accident, not by
  anyone's ruling. This check closes the gap the fail-closed default cannot:
  it makes an out-of-vocabulary `rule_kind` a REFUSAL rather than a silent
  reclassification, at the same place a reader would otherwise apply the
  default.

WHAT IT DOES NOT DO:

  It does not call Neotoma at write time. Ateles owns no MCP server code for
  `agent_policy` — that write path lives in the Neotoma product, outside this
  repo. What this repo CAN enforce is a deliberately regenerated, committed
  SNAPSHOT of `agent_policy` rows checked against the closed vocabulary in CI,
  where no Neotoma token exists — the same posture ateles#1115/PR #1116's
  (unmerged, as of this writing) `check_policy_rule_kind.py` established for a
  related `rule_kind` contradiction check; if that PR lands first, prefer
  consolidating the two snapshots/fetchers rather than keeping two independent
  copies of the same fetch-and-screen shape. A row that fails this check is a
  row whose `rule_kind` must be ruled (mandatory or advisory) and re-synced
  before the snapshot can be regenerated clean. See
  docs/governance/agent_policy_rule_kind_vocab.json's `_comment` for the
  regeneration command.

  The remediation for ateles#1245's 16 rows already ran (see
  scripts/migrations/remediation_20260924_agent_policy_rule_kind.py); this
  check is what stops the next one from reaccumulating unnoticed.

HOW THE SNAPSHOT IS WRITE-PROTECTED:

  `--write` needs `NEOTOMA_BEARER_TOKEN` and `NEOTOMA_BASE_URL` (env-sourced,
  no baked-in default — CLAUDE.md's config-sourcing rule). It screens every
  string field for operator-PII shapes (BTC address, IBAN, phone, currency
  figure) before committing, and REFUSES the write rather than warning, naming
  the offending entity_id — see `_screen_row_for_operator_pii`.

  `title`/`canonical_name` is NEVER stored, deliberately, not merely
  truncated. A free-text title can name a real third party with no shape a
  regex screen catches — one live row's title names a person by first name in
  a specific meeting-date context ("... 2026 03 17 <name> call"), which no
  BTC/IBAN/phone/currency pattern would ever flag, and CLAUDE.md's people-data
  section forbids publishing third-party person-data without per-case
  operator approval. The guard's own logic never reads `title`; only
  `entity_id`, `rule_kind`, `scope`, and `status` are stored — a human
  auditing a violation looks the `entity_id` up directly in Neotoma, which is
  not public.

Usage:
  python3 scripts/linters/check_policy_rule_kind_vocab.py
  # checks the committed snapshot; exit 1 on any out-of-vocabulary row

  python3 scripts/linters/check_policy_rule_kind_vocab.py --write
  # regenerates the snapshot from live Neotoma (needs NEOTOMA_BEARER_TOKEN)

  python3 scripts/linters/check_policy_rule_kind_vocab.py --snapshot <path>
  # checks an alternate snapshot file (used by the tests)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = REPO_ROOT / "docs" / "governance" / "agent_policy_rule_kind_vocab.json"

RULE_KIND_VALUES = ("mandatory", "advisory")

_ABSENT = "(absent)"


def rejected_display(rule_kind) -> str:
    """The literal to echo back for a rejected value. Absence gets `(absent)`,
    never the string `"None"`/`"null"`/`"undefined"` — a caller reading that
    would reasonably conclude the guard itself is broken, per the UX contract
    (ateles#1245)."""
    if rule_kind is None or rule_kind == "":
        return _ABSENT
    return str(rule_kind)


def refusal_message(rule_kind) -> str:
    """The exact refusal shape ateles#1245 specifies: rejected value, the
    closed vocabulary, the safety-meaning reason, and a one-line fix — never a
    bare 'invalid enum', which invites the mechanical `recommended -> advisory`
    remap the issue's scope rules out."""
    vocab = "{" + ", ".join(RULE_KIND_VALUES) + "}"
    return (
        f'agent_policy.rule_kind refused: "{rejected_display(rule_kind)}" is not in {vocab}.\n'
        "rule_kind carries the safety meaning (principles.md #5) — absence or an\n"
        "out-of-vocabulary value is never defaulted, it must be ruled explicitly.\n"
        'Fix: set rule_kind to "mandatory" or "advisory" on this row and retry.'
    )


def is_out_of_vocabulary(rule_kind) -> bool:
    return rule_kind not in RULE_KIND_VALUES


def find_violations(rows: list[dict]) -> list[dict]:
    violations = []
    for row in rows:
        rk = row.get("rule_kind")
        if is_out_of_vocabulary(rk):
            violations.append(row)
    return violations


def load_snapshot(path: Path) -> list[dict]:
    if not path.exists():
        print(
            f"check_policy_rule_kind_vocab: snapshot not found at "
            f"{path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path}. "
            "Regenerate it with --write (needs NEOTOMA_BEARER_TOKEN).",
            file=sys.stderr,
        )
        raise SystemExit(2)
    data = json.loads(path.read_text())
    return data.get("policies", [])


# Operator-PII shapes. Mirrors check_policy_rule_kind.py's screen — a value
# matching one has no business in a snapshot committed to this PUBLIC repo,
# whatever field carried it.
_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "bitcoin address",
        re.compile(r"\b(?:[13][a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[a-z0-9]{25,62})\b"),
    ),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}\b")),
    (
        "phone number",
        re.compile(
            r"(?:\+|\b00)\d{1,3}[\s.\-/]?(?:\(?\d{1,4}\)?[\s.\-/]?){2,5}\d{2,4}\b"
        ),
    ),
    (
        "currency figure",
        re.compile(r"(?:[€$£]\s?\d[\d.,]*|\b\d[\d.,]*\s?(?:EUR|USD|GBP)\b)"),
    ),
)


class OperatorPIIRefusal(SystemExit):
    """Raised when --write would commit a row matching an operator-PII shape."""


def _screen_row_for_operator_pii(row: dict) -> None:
    for field, value in row.items():
        if field == "entity_id" or not isinstance(value, str) or not value:
            continue
        for label, pattern in _PII_PATTERNS:
            if pattern.search(value):
                raise OperatorPIIRefusal(
                    "check_policy_rule_kind_vocab: REFUSING to write the snapshot.\n"
                    f"  entity_id : {row.get('entity_id')}\n"
                    f"  field     : {field}\n"
                    f"  matched   : {label}\n"
                    "This file is committed to a PUBLIC repo. Operator specifics "
                    "belong in a Neotoma entity referenced by type, not inline in "
                    "a policy row. Fix the row in Neotoma, then re-run --write. "
                    "Do not loosen the pattern to get past this."
                )


def fetch_live() -> list[dict]:
    """Pull agent_policy rows from Neotoma prod. Only used by --write."""
    token = os.environ.get("NEOTOMA_BEARER_TOKEN")
    base = os.environ.get("NEOTOMA_BASE_URL")
    if not token or not base:
        missing = [
            n
            for n, v in (("NEOTOMA_BEARER_TOKEN", token), ("NEOTOMA_BASE_URL", base))
            if not v
        ]
        print(
            f"check_policy_rule_kind_vocab: --write needs {' and '.join(missing)}.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    url = f"{base.rstrip('/')}/entities?entity_type=agent_policy&limit=500"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "ateles-lint/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode())

    rows = []
    for ent in payload.get("entities", []):
        snap = ent.get("snapshot") or {}
        row = {
            "entity_id": ent.get("entity_id"),
            "rule_kind": snap.get("rule_kind"),
            "scope": snap.get("scope"),
            "status": snap.get("status"),
        }
        _screen_row_for_operator_pii(row)
        rows.append(row)
    rows.sort(key=lambda r: r.get("entity_id") or "")
    return rows


def write_snapshot(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "_comment": (
            "GENERATED by scripts/linters/check_policy_rule_kind_vocab.py --write. "
            "Do not hand-edit. This is the committed snapshot of agent_policy "
            "rule_kind values that the linter checks in CI, where no Neotoma "
            "token exists. Regenerate with: "
            "NEOTOMA_BEARER_TOKEN=... NEOTOMA_BASE_URL=... "
            "python3 scripts/linters/check_policy_rule_kind_vocab.py --write"
        ),
        "policies": rows,
    }
    path.write_text(json.dumps(body, indent=2, sort_keys=False) + "\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true", help="regenerate the snapshot from live Neotoma"
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=SNAPSHOT_PATH,
        help="alternate snapshot path (tests)",
    )
    args = parser.parse_args(argv)

    if args.write:
        rows = fetch_live()
        write_snapshot(args.snapshot, rows)
        violations = find_violations(rows)
        if violations:
            print(
                f"check_policy_rule_kind_vocab: snapshot written with "
                f"{len(violations)} out-of-vocabulary row(s) still present.",
                file=sys.stderr,
            )
            for row in violations:
                print(f"  entity_id={row.get('entity_id')}", file=sys.stderr)
                print(refusal_message(row.get("rule_kind")), file=sys.stderr)
            return 1
        return 0

    rows = load_snapshot(args.snapshot)
    violations = find_violations(rows)
    if not violations:
        return 0

    print(
        f"check_policy_rule_kind_vocab: {len(violations)} agent_policy row(s) "
        "carry an out-of-vocabulary rule_kind.",
        file=sys.stderr,
    )
    for row in violations:
        print(f"\nentity_id={row.get('entity_id')}", file=sys.stderr)
        print(refusal_message(row.get("rule_kind")), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
