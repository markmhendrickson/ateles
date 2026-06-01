#!/usr/bin/env python3
"""
Create Neotoma `crypto_wallet_address` records from the Assets sheet and link each to
`financial_account` via PART_OF (source = address entity, target = account entity).

Resolves parent accounts in order:
  1) custody_* registry from Description 2 (same map as build_assets_observation_entities.py)
  2) modelo_workbook_{year}_{slug} from the Description column for each --try-year

When a row maps to custody, also links the same address to the matching modelo_workbook line
for each try-year where that entity exists (separate address entity per parent; address_key is
scoped by parent_registry_id).

Custodial rows with no on-chain address but a Koinly wallet URL use chain_network=custodial_koinly.

After sheet rows, adds one placeholder `crypto_wallet_address` per crypto `financial_account`
that still has no related address (incoming PART_OF from a crypto_wallet_address), so every
crypto account has at least one address record.

Usage:
  python3 execution/scripts/finances/link_crypto_wallet_addresses.py \\
    --csv "$DATA_DIR/imports/google sheets finances/Assets-Table 1.csv" \\
    --base-url http://localhost:3180 \\
    --dry-run

  # write payloads and run Neotoma CLI (requires neotoma on PATH, correct --env / data dir):
  python3 ... --execute

Does not print full on-chain addresses in dry-run summary beyond length/prefix (logs counts only).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Sheet "Description 2" -> custody registry_id (must match build_assets_observation_entities.py)
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


def norm_ws(s: str) -> str:
    return " ".join(s.strip().lower().split())


def norm_key(s: str) -> str:
    return " ".join(s.strip().split())


def slug_modelo_suffix(description: str) -> str:
    t = norm_key(description).lower()
    t = re.sub(r"[^a-z0-9]+", "_", t).strip("_")
    return t[:80] if t else "row"


def infer_chain_network(address: str, asset: str) -> str:
    a = (address or "").strip()
    ast = (asset or "").strip().upper()
    if not a:
        return "custodial"
    if a.lower().startswith("0x") and len(a) >= 40:
        return "ethereum"
    if a.startswith("bc1") or a.startswith("1") or a.startswith("3"):
        return "bitcoin"
    if a.startswith("SP") or a.startswith("SM") or (len(a) > 20 and ast == "STX"):
        return "stacks"
    if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,48}", a):
        if ast in ("SOL",):
            return "solana"
        return "solana"
    if len(a) in (95, 106) and re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]+", a):
        return "monero"
    if ast in ("XMR",):
        return "monero"
    return "other"


KOINLY_WALLET_RE = re.compile(r"/wallets/([A-Fa-f0-9]{32})", re.I)


def koinly_wallet_id(url: str) -> str | None:
    m = KOINLY_WALLET_RE.search(url or "")
    return m.group(1).upper() if m else None


def address_material(row: dict[str, str]) -> tuple[str, str, str]:
    """Returns (normalized_address, chain_network, human_hint)."""
    raw_addr = (row.get("Address") or "").strip()
    asset = (row.get("Asset") or "").strip()
    kurl = (row.get("Koinly URL") or "").strip()
    kid = koinly_wallet_id(kurl)
    if raw_addr:
        return raw_addr, infer_chain_network(raw_addr, asset), ""
    if kid:
        return f"koinly_wallet:{kid}", "custodial_koinly", kid
    return "", "custodial", ""


def ascii_safe_label(s: str, max_len: int = 500) -> str:
    """Neotoma observation ids can collide when canonical_name contains unicode dashes/quotes."""
    t = unicodedata.normalize("NFKD", s or "")
    t = t.encode("ascii", "ignore").decode("ascii")
    t = " ".join(t.split())
    return t[:max_len] if t else "wallet address"


def make_address_key(parent_registry_id: str, normalized_address: str) -> str:
    h = hashlib.sha256(
        f"{parent_registry_id}|{normalized_address}".encode()
    ).hexdigest()
    return f"addr_{h[:40]}"


def http_post_json(
    url: str, body: dict[str, Any], timeout: float = 120
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_json(url: str, timeout: float = 120) -> dict[str, Any]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_financial_accounts(base_url: str, limit: int = 4000) -> list[dict[str, Any]]:
    url = base_url.rstrip("/") + "/entities/query"
    res = http_post_json(
        url,
        {"entity_type": "financial_account", "limit": limit, "include_snapshots": True},
    )
    return list(res.get("entities") or [])


def fetch_crypto_wallet_addresses(
    base_url: str, limit: int = 8000
) -> list[dict[str, Any]]:
    url = base_url.rstrip("/") + "/entities/query"
    res = http_post_json(
        url,
        {
            "entity_type": "crypto_wallet_address",
            "limit": limit,
            "include_snapshots": True,
        },
    )
    return list(res.get("entities") or [])


def account_incoming_address_rel_ids(base_url: str, account_entity_id: str) -> set[str]:
    """Entity IDs of crypto_wallet_address that have PART_OF -> this account."""
    url = base_url.rstrip("/") + f"/entities/{account_entity_id}/relationships"
    try:
        res = http_get_json(url)
    except urllib.error.HTTPError:
        return set()
    rels = res.get("relationships") or res.get("incoming") or []
    out: set[str] = set()
    for r in rels:
        if (r.get("relationship_type") or "").upper() != "PART_OF":
            continue
        if r.get("target_entity_id") != account_entity_id:
            continue
        sid = r.get("source_entity_id")
        if sid:
            out.add(sid)
    return out


def is_crypto_financial_account(ent: dict[str, Any]) -> bool:
    snap = ent.get("snapshot") or {}
    tags = snap.get("filing_tags") or []
    if not isinstance(tags, list):
        tags = []
    rid = str(snap.get("registry_id") or "")
    if "real_estate" in rid.lower():
        return False
    if snap.get("denomination_category") == "crypto":
        return True
    if "721" in tags:
        return True
    if rid.startswith("custody_"):
        return True
    if "modelo_workbook_" in rid and "721" in tags:
        return True
    if "modelo_workbook_" in rid and snap.get("denomination_category") == "crypto":
        return True
    return False


def resolve_parent_registry_ids(
    desc: str,
    desc2: str,
    by_registry: dict[str, str],
    try_years: list[int],
) -> list[str]:
    """Return ordered unique registry_ids to attach the same logical address to."""
    seen: list[str] = []
    custody_key = SHEET_TO_REGISTRY.get(norm_ws(desc2))
    modelo_slug = slug_modelo_suffix(desc)

    if custody_key and custody_key in by_registry:
        seen.append(custody_key)

    for y in try_years:
        mb = f"modelo_workbook_{y}_{modelo_slug}"
        if mb in by_registry and mb not in seen:
            seen.append(mb)

    if not seen and custody_key:
        for y in try_years:
            alt = f"modelo_workbook_{y}_{slug_modelo_suffix(desc2 + ' ' + desc)}"
            if alt in by_registry and alt not in seen:
                seen.append(alt)

    return seen


def load_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    import csv

    with path.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        headers = r.fieldnames or []
        rows = [{k: (v or "").strip() for k, v in row.items()} for row in r]
    return list(headers), rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, required=True, help="Assets-Table 1.csv path")
    ap.add_argument(
        "--base-url", default="http://localhost:3180", help="Neotoma HTTP API base"
    )
    ap.add_argument(
        "--try-years",
        default="2025,2024,2023",
        help="Comma-separated tax years for modelo_workbook_*",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan only (default if --execute not set)",
    )
    ap.add_argument(
        "--execute",
        action="store_true",
        help="Run neotoma store and relationships create",
    )
    args = ap.parse_args()
    try_years = [
        int(x.strip()) for x in args.try_years.split(",") if x.strip().isdigit()
    ]

    if not args.csv.is_file():
        print(f"error: CSV not found: {args.csv}", file=sys.stderr)
        return 2

    _, rows = load_csv_rows(args.csv)
    accounts = fetch_financial_accounts(args.base_url)
    by_registry: dict[str, str] = {}
    for e in accounts:
        snap = e.get("snapshot") or {}
        rid = snap.get("registry_id")
        if rid:
            by_registry[str(rid)] = str(e.get("entity_id") or "")

    existing_addrs = fetch_crypto_wallet_addresses(args.base_url)
    existing_keys: set[str] = set()
    for e in existing_addrs:
        snap = e.get("snapshot") or {}
        k = snap.get("address_key")
        if k:
            existing_keys.add(str(k))

    # (address_key) -> entity payload
    to_store: dict[str, dict[str, Any]] = {}

    crypto_sheet_rows = 0
    skipped_no_parent = 0
    for row in rows:
        if norm_ws(row.get("Type") or "") != "crypto":
            continue
        crypto_sheet_rows += 1
        desc = row.get("Description") or ""
        desc2 = row.get("Description 2") or ""
        if not desc2.strip():
            skipped_no_parent += 1
            continue
        addr, chain, k_hint = address_material(row)
        if not addr:
            skipped_no_parent += 1
            continue

        parents = resolve_parent_registry_ids(desc, desc2, by_registry, try_years)
        if not parents:
            skipped_no_parent += 1
            continue

        asset = row.get("Asset") or ""
        label_bits = [asset, desc[:80] if desc else ""]
        cn_suffix = " · ".join(x for x in label_bits if x).strip(" ·") or "wallet"

        for parent_registry_id in parents:
            peid = by_registry.get(parent_registry_id)
            if not peid:
                continue
            akey = make_address_key(parent_registry_id, addr)
            if akey not in to_store:
                canonical = ascii_safe_label(f"{parent_registry_id} — {cn_suffix}")
                to_store[akey] = {
                    "entity_type": "crypto_wallet_address",
                    "canonical_name": canonical,
                    "address_key": akey,
                    "parent_registry_id": parent_registry_id,
                    "chain_network": chain,
                    "address": addr,
                    "source_labels": ascii_safe_label(
                        f"Assets sheet · {args.csv.name}", 300
                    ),
                    "purpose": "custodial_koinly"
                    if chain == "custodial_koinly"
                    else "on_chain_or_custody",
                }
                if k_hint:
                    to_store[akey]["koinly_wallet_id"] = k_hint

    # Placeholders for crypto accounts with no incoming address link
    crypto_entities = [e for e in accounts if is_crypto_financial_account(e)]

    for e in crypto_entities:
        eid = str(e.get("entity_id") or "")
        snap = e.get("snapshot") or {}
        rid = str(snap.get("registry_id") or "")
        if not eid or not rid:
            continue
        linked = account_incoming_address_rel_ids(args.base_url, eid)
        if linked:
            continue
        akey = make_address_key(rid, f"reporting_placeholder:{rid}")
        if akey not in to_store:
            to_store[akey] = {
                "entity_type": "crypto_wallet_address",
                "canonical_name": ascii_safe_label(f"Reporting anchor · {rid}"),
                "address_key": akey,
                "parent_registry_id": rid,
                "chain_network": "reporting_placeholder",
                "address": f"placeholder:{rid}",
                "source_labels": ascii_safe_label(
                    "link_crypto_wallet_addresses.py", 120
                ),
                "purpose": "reporting_anchor",
            }

    # Build relationship list: after store we have entity ids; for dry-run we only have keys
    planned_rels: list[tuple[str, str]] = []  # address_key, parent_registry_id
    for akey, payload in to_store.items():
        planned_rels.append((akey, str(payload["parent_registry_id"])))

    new_entities = [
        to_store[k]
        for k in sorted(to_store.keys())
        if to_store[k]["address_key"] not in existing_keys
    ]
    existing_overlap = len(to_store) - len(new_entities)

    print(
        json.dumps(
            {
                "csv_rows_crypto": crypto_sheet_rows,
                "financial_accounts_crypto": len(crypto_entities),
                "unique_address_entities_planned": len(to_store),
                "new_address_entities_to_store": len(new_entities),
                "already_existing_address_key_overlap": existing_overlap,
                "skipped_sheet_rows_no_parent_or_no_address": skipped_no_parent,
                "relationships_to_ensure": len(planned_rels),
            },
            indent=2,
        )
    )

    if not args.execute:
        print(
            "\nDry run only. Pass --execute to store + link (requires `neotoma` CLI).",
            file=sys.stderr,
        )
        return 0

    if not new_entities:
        print("No new crypto_wallet_address entities to store.", file=sys.stderr)
    else:
        neo_base = args.base_url.rstrip("/")
        neo = [
            "neotoma",
            "--base-url",
            neo_base,
            "--api-only",
            "--servers",
            "use-existing",
        ]
        # One entity per store call: multi-entity batches can hit observation id collisions in some Neotoma builds.
        tmp_one = Path("/tmp/neotoma_crypto_wallet_address_one.json")
        for ent in new_entities:
            tmp_one.write_text(json.dumps([ent], indent=2), encoding="utf-8")
            akey = str(ent.get("address_key") or "unknown")
            cmd = [
                *neo,
                "store",
                "--file",
                str(tmp_one),
                "--idempotency-key",
                f"crypto-wallet-address-{akey[:72]}",
            ]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                print(
                    f"store failed for {akey}: {r.stderr or r.stdout}", file=sys.stderr
                )
                continue

    # Resolve address entity ids from API by address_key
    refreshed = fetch_crypto_wallet_addresses(args.base_url)
    id_by_key_full: dict[str, str] = {}
    for ent in refreshed:
        snap = ent.get("snapshot") or {}
        ak = snap.get("address_key")
        if ak:
            id_by_key_full[str(ak)] = str(ent.get("entity_id") or "")

    created = 0
    for akey, parent_rid in planned_rels:
        aid = id_by_key_full.get(akey)
        pid = by_registry.get(parent_rid)
        if not aid or not pid:
            print(
                f"skip rel missing id: key={akey} address_id={aid} parent={parent_rid} pid={pid}",
                file=sys.stderr,
            )
            continue
        rc = subprocess.run(
            [
                "neotoma",
                "--base-url",
                args.base_url.rstrip("/"),
                "--api-only",
                "--servers",
                "use-existing",
                "relationships",
                "create",
                "--source-entity-id",
                aid,
                "--target-entity-id",
                pid,
                "--relationship-type",
                "PART_OF",
            ],
            capture_output=True,
            text=True,
        )
        if rc.returncode == 0:
            created += 1
        else:
            err = (rc.stderr or rc.stdout or "").lower()
            if "duplicate" in err or "already" in err or "exists" in err:
                created += 1
            else:
                print(rc.stderr or rc.stdout, file=sys.stderr)

    print(json.dumps({"relationships_created_or_idempotent_ok": created}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
