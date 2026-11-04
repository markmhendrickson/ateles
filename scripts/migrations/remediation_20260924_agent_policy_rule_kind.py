#!/usr/bin/env python3
"""
remediation_20260924_agent_policy_rule_kind.py — apply the 16 individual
`rule_kind` rulings for ateles#1245.

WHAT THIS IS:

  16 `agent_policy` rows carried a `rule_kind` outside {mandatory, advisory}
  (`recommended` x6, `operating_discipline` x5, absent x3, `allow` x1,
  `issue_spec_contribution` x1) — see ateles#1245. Each was read on its own
  merits (rule text, scope, and what it actually constrains) and ruled
  `mandatory` or `advisory` — never a mechanical `recommended -> advisory` or
  similar map, which is exactly the shortcut the issue's scope rules out.

  RULINGS is that record: one row per entity, its PRIOR `rule_kind`, the
  RULING, and a one-line RATIONALE. This is the source of truth the PR body's
  16-row table is generated from, and this script's own test
  (test_2026_09_24_agent_policy_rule_kind_remediation.py) asserts the ruling
  table matches expectations, so a future edit that turns this into a
  mechanical remap breaks the test rather than silently reintroducing the
  defect this issue fixes.

WHY 16 INDIVIDUAL correct() CALLS, NOT ONE BULK WRITE:

  Each row's `rule_kind` is a distinct ruling, not a batch transform. Writing
  them as 16 separate `correct()` calls, each carrying its own row's
  rationale in the request, keeps that a reviewable fact about the change
  rather than an opaque `UPDATE ... WHERE rule_kind NOT IN (...)`.

IDEMPOTENCY:

  Each row's idempotency_key is `agent-policy-rule-kind-remediation-{entity_id}`
  — stable across re-runs. `--dry-run` prints the diff without writing.
  Running this twice against an already-remediated row set is a no-op: the
  script reads each row's CURRENT rule_kind first and skips any row already
  equal to its ruling, rather than depending solely on server-side
  idempotency-key dedup (the read-back is what proves the RIGHT row was
  corrected, not just that A write with that key happened once).

USAGE:

  python3 scripts/migrations/remediation_20260924_agent_policy_rule_kind.py --dry-run
  NEOTOMA_BEARER_TOKEN=... NEOTOMA_BASE_URL=... \\
    python3 scripts/migrations/remediation_20260924_agent_policy_rule_kind.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

# entity_id -> (prior_rule_kind, ruling, rationale)
RULINGS: dict[str, tuple[str | None, str, str]] = {
    "ent_00fae21cb9a74370c2fdd66d": (
        "recommended",
        "mandatory",
        "Retired twin of 'Agent Background Execution Rules'; content is hard safety "
        "constraints (never delete without confirmation, never deploy without "
        "approval, never modify git history) — the 2026-09-18 ruling (#1114/PR "
        "#1116) kept the mandatory twin and retired this one; its value must "
        "reflect what it WAS, not invert that ruling.",
    ),
    "ent_06550fd3f9956dab85dadc2e": (
        "operating_discipline",
        "advisory",
        "Operating know-how about publish_rendered_page's update semantics "
        "(verify writes by diffing bytes) — improves practice, not a hard "
        "constraint whose violation is unsafe.",
    ),
    "ent_1268eaa4229508046a68455b": (
        "operating_discipline",
        "advisory",
        "Research-quality guidance (check primary sources before asserting "
        "absence) — improves output accuracy, doesn't prevent harm if skipped.",
    ),
    "ent_2f9bac75ca47814bd2af40a9": (
        None,
        "advisory",
        "Cadence/prioritization guidance for Corvus's release radar — a "
        "scheduling preference, not a safety-bearing constraint.",
    ),
    "ent_4d34c6f96312be686f572add": (
        "operating_discipline",
        "mandatory",
        "States 'Never widen a convention... Never encode your invented "
        "behaviour into a test' — imperative prohibitions preventing silent "
        "design drift and false test coverage; matches the safety bar.",
    ),
    "ent_663888501a290e9aaf60270c": (
        "allow",
        "mandatory",
        "Defines the hard boundary between autonomous-OK and consent-required "
        "actions (credential storage location, financial/account APIs) — "
        "getting this wrong has real operational/financial consequences.",
    ),
    "ent_6d91e99c8574a56a97ffff85": (
        "recommended",
        "advisory",
        "Retired 'Human Review Checklist' — a reviewer checklist (structural "
        "cleanliness, determinism) for humans, not an agent-binding safety rule.",
    ),
    "ent_a3dfefaa7b0bc7474c2332c5": (
        "recommended",
        "advisory",
        "'Risk Classification for Neotoma Changes [strategy_governance]' — "
        "risk-tiering/reviewer-count guidance that informs judgment, not "
        "itself a prohibition.",
    ),
    "ent_a8eabeafc9a3ae6eaa8ff86c": (
        "operating_discipline",
        "mandatory",
        "'do NOT write ad-hoc transfer scripts inline' routing payments to "
        "Monedula — a financial-domain boundary, matching the allow-row bar.",
    ),
    "ent_aa8e75e45c578b766f208dea": (
        "recommended",
        "advisory",
        "Active 'Human Review Checklist for Neotoma Changes' — same reviewer- "
        "checklist content and reasoning as the retired twin above.",
    ),
    "ent_acb658f262fd46654510c954": (
        None,
        "advisory",
        "Corvus voice guide — stylistic guidance for content generation, not a "
        "safety constraint.",
    ),
    "ent_aec7f8342e76be908d4a161f": (
        None,
        "mandatory",
        "'No SaaS analytics vendors that store usage data outside Neotoma' is "
        "an absolute prohibition protecting the data-locality/auditability "
        "guarantee the product is built on.",
    ),
    "ent_bbcb6e308090577bece15ed7": (
        "issue_spec_contribution",
        "mandatory",
        "'MUST contribute... NEVER post your section as a comment' — a hard "
        "pipeline-integrity rule; violating it breaks the swarm's spec-assembly "
        "mechanism, not merely suboptimal.",
    ),
    "ent_ca9affa6c513a40034133203": (
        "operating_discipline",
        "advisory",
        "'search before claiming absence' — improves accuracy, not a "
        "prevent-harm constraint.",
    ),
    "ent_d8bcace6c7f5f1eec618cf55": (
        "recommended",
        "mandatory",
        "Retired 'Global Agent Rules for Neotoma' — absolute architectural "
        "constraints (MUST NOT introduce strategy/execution logic into "
        "Neotoma); matches the mandatory bar, twin of ent_00fae21c...'s ruling.",
    ),
    "ent_fdc79e7b4bf151e20693b27a": (
        "recommended",
        "advisory",
        "'Risk Classification for Neotoma Changes [governance]' — same "
        "risk-classification content as the strategy_governance variant above.",
    ),
}

ENTITY_TYPE = "agent_policy"


def _idempotency_key(entity_id: str) -> str:
    return f"agent-policy-rule-kind-remediation-{entity_id}"


def _neotoma_request(
    method: str, path: str, token: str, base: str, body: dict | None = None
) -> dict:
    url = f"{base.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "ateles-lint/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


class EntityNotFound(RuntimeError):
    """The row a ruling targets no longer exists — never proceed to correct()
    an entity_id that vanished between snapshot capture and this run."""


def _current_rule_kind(entity_id: str, token: str, base: str) -> str | None:
    payload = _neotoma_request("GET", f"/entities/{entity_id}", token, base)
    if "snapshot" not in payload:
        raise EntityNotFound(entity_id)
    snap = payload.get("snapshot") or {}
    return snap.get("rule_kind")


def run(dry_run: bool) -> int:
    token = os.environ.get("NEOTOMA_BEARER_TOKEN")
    base = os.environ.get("NEOTOMA_BASE_URL")
    if not dry_run and (not token or not base):
        missing = [
            n
            for n, v in (("NEOTOMA_BEARER_TOKEN", token), ("NEOTOMA_BASE_URL", base))
            if not v
        ]
        print(
            f"remediation: needs {' and '.join(missing)} (or pass --dry-run)",
            file=sys.stderr,
        )
        return 2

    applied, skipped = 0, 0
    for entity_id, (prior, ruling, rationale) in sorted(RULINGS.items()):
        if dry_run:
            print(f"{entity_id}: {prior!r} -> {ruling!r}  ({rationale})")
            continue

        try:
            current = _current_rule_kind(entity_id, token, base)
        except EntityNotFound:
            print(
                f"remediation: entity_id={entity_id} not found — skipping rather "
                "than correct()-ing a row that no longer exists",
                file=sys.stderr,
            )
            return 1
        if current == ruling:
            print(f"{entity_id}: already {ruling!r}, skipping (idempotent no-op)")
            skipped += 1
            continue

        _neotoma_request(
            "POST",
            "/correct",
            token,
            base,
            {
                "entity_id": entity_id,
                "entity_type": ENTITY_TYPE,
                "field": "rule_kind",
                "value": ruling,
                "idempotency_key": _idempotency_key(entity_id),
            },
        )
        # Read back — a write that reports success has not necessarily
        # happened (CLAUDE.md verification discipline).
        after = _current_rule_kind(entity_id, token, base)
        if after != ruling:
            print(
                f"remediation: entity_id={entity_id} read-back shows "
                f"rule_kind={after!r}, expected {ruling!r}",
                file=sys.stderr,
            )
            return 1
        print(f"{entity_id}: {prior!r} -> {ruling!r} (confirmed by read-back)")
        applied += 1

    if not dry_run:
        print(f"\n{applied} applied, {skipped} already-correct (idempotent no-op).")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planned rulings without writing",
    )
    args = parser.parse_args(argv)
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
