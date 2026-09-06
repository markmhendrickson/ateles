#!/usr/bin/env python3
"""
check_gate_lens_grants.py — verify the gate-lens agent_grant fixture against
live Neotoma.

`agent_grant` entities in Neotoma are the admission surface: AAuth consults
them, not the repo. The checked-in fixture
(execution/fixtures/agent_grants/gate_lens_capabilities.json) lets
test_review_panel.py assert the invariant with no network call in the test
path — but a checked-in snapshot can silently drift from the live grants it
claims to describe, which is the exact failure class ateles#762 is about: a
surface that *claims* the write is admitted while the live grant denies it.

This script closes that loop. It fetches every `agent_grant` from Neotoma and
compares, per gate-owning lens derived from the LENSES registry:

    * the grant exists at all (a missing grant is the case that slipped
      through review on ateles#769)
    * fixture entity_id == live entity_id
    * fixture capabilities == live capabilities, op by op
    * the grant is `active` (a suspended/revoked grant denies the write just
      as effectively as a missing one)
    * `issue` is admitted on retrieve AND correct

Usage:
    check_gate_lens_grants.py           # report drift, exit 1 on mismatch
    check_gate_lens_grants.py --json    # machine-readable report

Env: NEOTOMA_BASE_URL, NEOTOMA_BEARER_TOKEN (falls back to
~/.config/neotoma/.env), same as render_agent_docs.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = (
    REPO_ROOT / "execution" / "fixtures" / "agent_grants" / "gate_lens_capabilities.json"
)

# Import LENSES so the checked list is derived from the registry, never from a
# doc table. ateles#769: the issue's own table named four lenses and missed a
# fifth (buteo/legal), which is why deriving is the whole point.
sys.path.insert(0, str(REPO_ROOT / "execution" / "daemons" / "apis"))
from review_panel import LENSES  # noqa: E402


def _load_env() -> tuple[str, str]:
    base_url = os.environ.get("NEOTOMA_BASE_URL", "")
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    env_path = Path.home() / ".config" / "neotoma" / ".env"
    if (not base_url or not token) and env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            if key == "NEOTOMA_BASE_URL" and not base_url:
                base_url = value
            elif key == "NEOTOMA_BEARER_TOKEN" and not token:
                token = value
    if not base_url:
        sys.exit("NEOTOMA_BASE_URL not set (env or ~/.config/neotoma/.env)")
    return base_url.rstrip("/"), token


def _request(url: str, token: str, payload: dict | None = None, retries: int = 5) -> dict:
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "ateles-neotoma-sync/1.0")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            if payload is not None:
                req.add_header("Content-Type", "application/json")
                req.data = json.dumps(payload).encode()
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, ConnectionError) as exc:
            last = exc
            time.sleep(2)
    raise SystemExit(f"Neotoma unreachable after {retries} tries: {last}")


def _unwrap(entity: dict) -> dict:
    s = entity.get("snapshot", entity)
    if isinstance(s.get("snapshot"), dict):
        s = s["snapshot"]
    return s


def _capabilities(snapshot: dict) -> dict[str, list[str]]:
    """Normalize `capabilities` to {op: sorted(entity_types)}.

    Stored shape varies: some grants hold a real list, others a JSON-encoded
    string (see ent_1125bd72c60eaf298e0d6df0 and ent_b1ad492285fc45733a0c1244,
    which store the array stringified). Both must normalize identically or the
    comparison reports phantom drift.
    """
    caps = snapshot.get("capabilities")
    if isinstance(caps, str):
        try:
            caps = json.loads(caps)
        except (json.JSONDecodeError, ValueError):
            return {}
    if not isinstance(caps, list):
        return {}
    out: dict[str, list[str]] = {}
    for entry in caps:
        if not isinstance(entry, dict):
            continue
        op = entry.get("op")
        if not op:
            continue
        out[str(op)] = sorted(str(t) for t in (entry.get("entity_types") or []))
    return out


def fetch_grants(base_url: str, token: str) -> dict[str, dict]:
    """Live agent_grant rows keyed by match_sub (e.g. 'waxwing@ateles-swarm')."""
    data = _request(
        f"{base_url}/entities/query", token, {"entity_type": "agent_grant", "limit": 500}
    )
    ents = data.get("entities") or data.get("results") or []
    by_sub: dict[str, dict] = {}
    for e in ents:
        s = _unwrap(e)
        sub = s.get("match_sub")
        if not sub:
            continue
        by_sub[str(sub)] = {
            "entity_id": e.get("entity_id") or s.get("entity_id", ""),
            "status": s.get("status", ""),
            "capabilities": _capabilities(s),
        }
    return by_sub


def check(live: dict[str, dict]) -> tuple[list[str], list[dict]]:
    fixture = json.loads(FIXTURE_PATH.read_text())
    by_agent = {entry["agent"]: entry for entry in fixture["lenses"]}

    problems: list[str] = []
    report: list[dict] = []

    for lens in LENSES:
        if not lens.gate:
            continue  # a non-gating lens signs nothing on the issue

        entry = by_agent.get(lens.agent)
        if entry is None:
            problems.append(
                f"{lens.agent} owns the {lens.gate!r} gate but is absent from "
                f"{FIXTURE_PATH.name} — add its agent_grant row"
            )
            continue

        sub = entry.get("match_sub") or f"{lens.agent}@ateles-swarm"
        row = live.get(sub)
        if row is None:
            problems.append(
                f"{lens.agent} ({lens.gate} gate): NO LIVE agent_grant for "
                f"sub {sub!r} — AAuth will deny the gate writeback. The "
                f"fixture claims {entry.get('entity_id')}."
            )
            report.append({"agent": lens.agent, "gate": lens.gate, "state": "missing"})
            continue

        if row["status"] != "active":
            problems.append(
                f"{lens.agent} ({lens.gate} gate): live grant "
                f"{row['entity_id']} status is {row['status']!r}, not "
                f"'active' — the writeback is denied."
            )

        if entry.get("entity_id") and entry["entity_id"] != row["entity_id"]:
            problems.append(
                f"{lens.agent}: fixture entity_id {entry['entity_id']} != live "
                f"{row['entity_id']}"
            )

        fixture_caps = {
            str(c["op"]): sorted(str(t) for t in (c.get("entity_types") or []))
            for c in entry["capabilities"]
        }
        if fixture_caps != row["capabilities"]:
            problems.append(
                f"{lens.agent}: fixture capabilities differ from live grant "
                f"{row['entity_id']}.\n"
                f"    fixture: {json.dumps(fixture_caps, sort_keys=True)}\n"
                f"    live   : {json.dumps(row['capabilities'], sort_keys=True)}"
            )

        for op in ("retrieve", "correct"):
            admitted = row["capabilities"].get(op, [])
            if "issue" not in admitted and "*" not in admitted:
                problems.append(
                    f"{lens.agent} ({lens.gate} gate): live grant "
                    f"{row['entity_id']} does not admit {op} on 'issue' "
                    f"(got {admitted}) — the gate writeback will be denied."
                )

        report.append(
            {
                "agent": lens.agent,
                "gate": lens.gate,
                "entity_id": row["entity_id"],
                "status": row["status"],
                "state": "ok",
                "capabilities": row["capabilities"],
            }
        )

    return problems, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args()

    base_url, token = _load_env()
    problems, report = check(fetch_grants(base_url, token))

    if args.json:
        print(json.dumps({"ok": not problems, "problems": problems, "grants": report}, indent=2))
        return 1 if problems else 0

    if problems:
        print("gate-lens agent_grant check FAILED\n")
        for p in problems:
            print(f"  ✗ {p}")
        print(
            f"\n{len(problems)} problem(s). The fixture and the live grants "
            f"disagree, or a gate owner cannot write the issue it signs."
        )
        return 1

    gates = ", ".join(f"{r['agent']}/{r['gate']}" for r in report)
    print(
        f"gate-lens agent_grant check OK — {len(report)} gate-owning lenses "
        f"match live Neotoma ({gates})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
