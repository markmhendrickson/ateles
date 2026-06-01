#!/usr/bin/env python3
"""
Build Neotoma store payload: financial_account observation entities from gws JSON export
(Assets "CSV" that is actually Sheets values JSON).

Reads:
  - Assets JSON path (argv[1])
  - Neotoma entities JSON from retrieve_entities (stdin or argv[2]) for registry_id -> canonical_name

Prints JSON array suitable for neotoma store --file (entities only; use shell for idempotency).

Does not print secrets.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
from collections import defaultdict
from pathlib import Path

# Sheet "Description 2" (custody label) -> registry_id (§2 registry)
SHEET_TO_REGISTRY: dict[str, str] = {
    "binance": "custody_binance",
    "coinbase": "custody_coinbase",
    "okx": "custody_okx",
    "ethereum hot 1": "custody_ethereum_hot_1",
    "ledger nano x": "custody_ledger_nano_x",
    "nano s 1": "custody_nano_s_1",
    "nano s 0": "custody_nano_s_0",
    "stacks hot 2": "custody_stacks_hot_2",
    "stacks hot 5": "custody_stacks_hot_5",
    "stacks hot 1": "custody_stacks_hot_1",
    "stacks hot 6": "custody_stacks_hot_6",
    "wallet 18": "custody_wallet_18",
    "wallet 36: bitcoin hot 10": "custody_wallet_36_btc_hot_10",
    "multi hot 1": "custody_multi_hot_1",
    "solana hot 1": "custody_solana_hot_1",
    "monero hot": "custody_monero_hot",
    "rsk hot 1": "custody_rsk_hot_1",
    "casa": "custody_casa",
    "12": "custody_wallet_12",
    "wallet 51: lava": "custody_wallet_51_lava",
    "wallet_0 (unlabeled rows)": "custody_wallet_0_misc",
    "wallet_0": "custody_wallet_0_misc",
    "btc (legacy stacking rewards)": "custody_legacy_btc",
    "btc (orphan description)": "custody_orphan_btc_label",
}


def norm(s: str) -> str:
    return " ".join(s.strip().lower().split())


def row_to_obj(header: list[str], row: list[str]) -> dict:
    out: dict = {}
    for i, key in enumerate(header):
        key = key.strip() or f"col_{i}"
        val = row[i] if i < len(row) else ""
        out[key] = val
    return out


def load_values_json(path: Path) -> tuple[list[str], list[list[str]]]:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    vals = data.get("values") or []
    if not vals:
        return [], []
    header = [str(x).strip() for x in vals[0]]
    return header, vals[1:]


def load_registry_map(neotoma_path: Path) -> dict[str, str]:
    """registry_id -> canonical_name"""
    data = json.loads(neotoma_path.read_text(encoding="utf-8"))
    m: dict[str, str] = {}
    for e in data.get("entities", []):
        snap = e.get("snapshot") or {}
        rid = snap.get("registry_id")
        if not rid:
            continue
        cn = e.get("canonical_name") or snap.get("canonical_name")
        if cn:
            m[str(rid)] = str(cn)
    return m


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "Usage: build_assets_observation_entities.py <Assets.json> <retrieve_entities.json>",
            file=sys.stderr,
        )
        return 2
    assets_path = Path(sys.argv[1])
    neo_path = Path(sys.argv[2])
    as_of = assets_path.stem.split("_")[0] if "_" in assets_path.stem else "unknown"

    body = assets_path.read_bytes()
    sha = hashlib.sha256(body).hexdigest()[:16]

    header, rows = load_values_json(assets_path)
    if not header:
        print("[]", file=sys.stdout)
        return 0

    reg_map = load_registry_map(neo_path)
    # Find column indices
    hlower = [norm(x) for x in header]
    try:
        i_type = next(i for i, x in enumerate(hlower) if x == "type")
        i_desc2 = next(i for i, x in enumerate(hlower) if x == "description 2")
    except StopIteration:
        print("error: could not find Type / Description 2 columns", file=sys.stderr)
        return 1

    grouped: dict[str, list[dict]] = defaultdict(list)
    unmapped: list[dict] = []

    for row in rows:
        while len(row) < len(header):
            row.append("")
        t = (row[i_type] if i_type < len(row) else "").strip()
        if t.lower() != "crypto":
            continue
        d2 = (row[i_desc2] if i_desc2 < len(row) else "").strip()
        if not d2:
            unmapped.append(row_to_obj(header, row))
            continue
        key = SHEET_TO_REGISTRY.get(norm(d2))
        if not key:
            unmapped.append(row_to_obj(header, row))
            continue
        grouped[key].append(row_to_obj(header, row))

    entities: list[dict] = []

    for registry_id, obs_rows in sorted(grouped.items()):
        canonical = reg_map.get(registry_id)
        if not canonical:
            continue
        entity: dict = {
            "entity_type": "financial_account",
            "canonical_name": canonical,
            "registry_id": registry_id,
            "assets_sheet_as_of_date": as_of,
            "assets_sheet_import_sha256": sha,
            "assets_sheet_source_file": assets_path.name,
            "observation_kind": "assets_sheet_rows",
            "rows": obs_rows,
            "balance_date": as_of,
            "denomination_category": "crypto",
        }
        entities.append(entity)

    # Savings tab: single note with table as CSV string (compact)
    if len(sys.argv) >= 4:
        sav_path = Path(sys.argv[3])
        sbody = sav_path.read_bytes()
        ssha = hashlib.sha256(sbody).hexdigest()[:16]
        sh, srows = load_values_json(sav_path)
        buf = io.StringIO()
        w = csv.writer(buf)
        if sh:
            w.writerow(sh)
            for r in srows:
                w.writerow(r[: len(sh)])
        entities.append(
            {
                "entity_type": "note",
                "canonical_name": f"finances savings accounts snapshot {as_of}",
                "title": f"Savings accounts snapshot {as_of}",
                "savings_sheet_as_of_date": as_of,
                "savings_sheet_import_sha256": ssha,
                "savings_sheet_source_file": sav_path.name,
                "observation_kind": "savings_accounts_csv",
                "sheet_csv": buf.getvalue(),
            }
        )

    if unmapped:
        entities.append(
            {
                "entity_type": "note",
                "canonical_name": f"finances assets unmapped crypto rows {as_of}",
                "title": f"Assets import unmapped crypto rows {as_of}",
                "assets_sheet_as_of_date": as_of,
                "observation_kind": "assets_sheet_unmapped_crypto",
                "row_count": len(unmapped),
                "rows": unmapped[:500],
            }
        )

    json.dump(entities, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
