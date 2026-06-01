#!/usr/bin/env python3
"""
Migrate financial_account entities to use canonical balance/metadata fields.

Reads all financial_account entities from Neotoma, computes canonical fields
(balance_value, balance_currency, balance_date, institution_name,
denomination_category, display_sign, and normalized filing_tags), then stores
an observation with the canonical fields for each entity.

Usage:
  python3 execution/scripts/finances/migrate_financial_accounts_canonical_fields.py [--dry-run] [--api-only]

Flags:
  --dry-run     Print what would be stored without writing
  --api-only    Pass --api-only to neotoma CLI
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from typing import Any

CRYPTO_CURRENCIES = {
    "BTC",
    "ETH",
    "SOL",
    "STX",
    "USDT",
    "USDC",
    "DAI",
    "ADA",
    "DOT",
    "AVAX",
    "MATIC",
    "POL",
    "LINK",
    "UNI",
    "ATOM",
    "XRP",
    "LTC",
    "BCH",
    "DOGE",
    "TRX",
    "TON",
    "NEAR",
    "APT",
    "SUI",
    "ARB",
    "OP",
    "STRK",
    "WBTC",
    "WETH",
}

CRYPTO_INSTITUTION_RE = re.compile(
    r"\b(coinbase|kraken|binance|ledger|trezor|metamask|crypto\.com|gemini|okx|bybit|kucoin|nexo|electrum|exodus|phantom|rainbow|uniswap|aave|lido|stacks)\b",
    re.IGNORECASE,
)

CASH_LIKE_RE = re.compile(
    r"\b(checking|savings|cash\s*management|money\s*market|high\s*yield|deposit\s*account|current\s*account|giro|iban|banking)\b",
    re.IGNORECASE,
)

INVESTMENT_RE = re.compile(
    r"\b(ira|sep|401k|401\(k\)|brokerage|broker|investment|mutual\s*fund|etf|retirement|pension\s*plan|portfolio\s*account|equity|equities)\b",
    re.IGNORECASE,
)

INSTITUTION_INFER = [
    ("schwab", "Charles Schwab"),
    ("fidelity", "Fidelity"),
    ("american express", "American Express"),
    ("amex", "American Express"),
    ("capital one", "Capital One"),
    ("coinbase", "Coinbase"),
    ("wise", "Wise"),
    ("chase", "Chase"),
    ("bank of america", "Bank of America"),
    ("vanguard", "Vanguard"),
    ("interactive brokers", "Interactive Brokers"),
]

YEAR_FIELDS = [
    "tax_year_context",
    "filing_year",
    "reporting_year",
    "calendar_year",
    "statement_year",
    "modelo_year",
    "tax_year",
]


def snap_field(snap: dict[str, Any], key: str) -> Any:
    return snap.get(key)


def normalize_filing_tags(snap: dict[str, Any]) -> list[str]:
    raw = snap_field(snap, "filing_tags")
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    s = str(raw).strip()
    if not s:
        return []
    return [t.strip() for t in s.split(",") if t.strip()]


def get_year_integers(snap: dict[str, Any]) -> set[int]:
    years: set[int] = set()
    for k in YEAR_FIELDS:
        v = snap.get(k)
        if isinstance(v, int | float) and math.isfinite(v):
            years.add(round(v))
        elif isinstance(v, str) and re.match(r"^\s*\d{4}\s*$", v):
            years.add(int(v.strip()))
    return years


def is_year_misimport(eur: float, snap: dict[str, Any]) -> bool:
    r = round(eur)
    if not math.isfinite(eur) or abs(eur - r) > 1e-6:
        return False
    return r in get_year_integers(snap)


def derive_balance(snap: dict[str, Any]) -> tuple[float | None, str | None, str | None]:
    """Derive canonical balance_value, balance_currency, balance_date from snapshot."""
    value: float | None = None
    currency: str | None = None
    date: str | None = None

    av = snap.get("account_value")
    if isinstance(av, int | float) and math.isfinite(av):
        value = float(av)
        currency = str(
            snap.get("account_value_currency") or snap.get("currency") or "EUR"
        ).upper()

    if value is None:
        for eur_key in ["account_value_eur", "balance_eur"]:
            v = snap.get(eur_key)
            if isinstance(v, int | float) and math.isfinite(v) and abs(v) > 1e-9:
                if not is_year_misimport(v, snap):
                    value = float(v)
                    currency = "EUR"
                    break

    if value is None:
        for usd_key in ["account_value_usd", "balance_usd", "ending_account_value_usd"]:
            v = snap.get(usd_key)
            if isinstance(v, int | float) and math.isfinite(v) and abs(v) > 1e-9:
                value = float(v)
                currency = "USD"
                break

    if value is None:
        ev = snap.get("ending_account_value")
        if isinstance(ev, int | float) and math.isfinite(ev):
            value = float(ev)
            ec = str(
                snap.get("ending_account_value_currency")
                or snap.get("currency")
                or "EUR"
            ).upper()
            currency = ec

    if value is None:
        for k in ["outstanding_principal_eur", "outstanding_principal"]:
            v = snap.get(k)
            if isinstance(v, int | float) and math.isfinite(v) and abs(v) > 1e-9:
                value = float(v)
                currency = (
                    "EUR"
                    if k.endswith("_eur")
                    else str(snap.get("currency") or "EUR").upper()
                )
                break

    if value is None:
        for k in ["amount_eur", "amount"]:
            v = snap.get(k)
            if isinstance(v, int | float) and math.isfinite(v) and abs(v) > 1e-9:
                value = float(v)
                if k.endswith("_eur"):
                    currency = "EUR"
                else:
                    currency = str(snap.get("currency") or "EUR").upper()
                break

    if value is None:
        bal = snap.get("balance")
        if isinstance(bal, int | float) and math.isfinite(bal):
            value = float(bal)
            currency = str(snap.get("currency") or "EUR").upper()

    for dk in [
        "last_statement_date",
        "statement_as_of_date",
        "statement_period_end",
        "assets_sheet_as_of_date",
    ]:
        dv = snap.get(dk)
        if isinstance(dv, str) and re.match(r"\d{4}-\d{2}-\d{2}", dv.strip()):
            date = dv.strip()[:10]
            break

    return value, currency, date


def derive_institution_name(snap: dict[str, Any], canonical_name: str) -> str | None:
    inst = snap.get("institution")
    if (
        isinstance(inst, str)
        and inst.strip()
        and not re.match(r"^[\s\u2014\u2013-]+$", inst.strip())
    ):
        return inst.strip()

    haystack = " ".join(
        [
            str(snap.get("canonical_name") or ""),
            canonical_name or "",
            str(snap.get("account_name") or ""),
            str(snap.get("registry_id") or ""),
        ]
    ).lower()

    for needle, label in INSTITUTION_INFER:
        if needle in haystack:
            return label
    return None


def derive_denomination_category(snap: dict[str, Any], canonical_name: str) -> str:
    tags = normalize_filing_tags(snap)
    account_type = str(snap.get("account_type") or "").lower()
    institution = str(snap.get("institution") or "")
    registry = str(snap.get("registry_id") or "")
    haystack = f"{institution} {canonical_name} {registry} {account_type}".lower()

    ccys = []
    for k in ["currency", "account_value_currency"]:
        c = str(snap.get(k) or "").upper().strip()
        if c and re.match(r"^[A-Z]{2,10}$", c):
            ccys.append(c)

    is_custody = "721" in tags or "custod" in account_type
    ccy_crypto = any(c in CRYPTO_CURRENCIES for c in ccys)
    inst_crypto = bool(CRYPTO_INSTITUTION_RE.search(haystack))

    if is_custody or ccy_crypto or inst_crypto:
        return "crypto"
    if CASH_LIKE_RE.search(haystack):
        return "fiat_cash"
    if INVESTMENT_RE.search(haystack):
        return "investments"
    if ccys and all(c not in CRYPTO_CURRENCIES for c in ccys):
        return "fiat_cash"
    return "other"


def derive_display_sign(snap: dict[str, Any], canonical_name: str) -> int:
    is_liability_raw = snap.get("is_liability")
    if is_liability_raw is True or (
        isinstance(is_liability_raw, str)
        and is_liability_raw.strip().lower() in ("true", "1", "yes")
    ):
        lt = str(snap.get("liability_type") or "").lower()
        if not lt or re.search(
            r"credit\s*card|creditcard|charge\s*card|revolving|line\s*of\s*credit|\bcc\b",
            lt,
        ):
            return -1

    for field in ["liability_type", "account_type"]:
        val = str(snap.get(field) or "").lower().strip()
        if re.search(
            r"credit\s*card|creditcard|charge\s*card|revolving|line\s*of\s*credit|\bcc\b",
            val,
        ):
            return -1

    name = str(
        snap.get("canonical_name") or snap.get("account_name") or canonical_name or ""
    ).lower()
    if re.search(r"credit\s*card|charge\s*card", name):
        return -1

    return 1


def build_canonical_observation(entity: dict[str, Any]) -> dict[str, Any] | None:
    snap = entity.get("snapshot") or {}
    canonical_name = entity.get("canonical_name") or ""
    registry_id = snap.get("registry_id") or entity.get("entity_id")

    balance_value, balance_currency, balance_date = derive_balance(snap)
    institution_name = derive_institution_name(snap, canonical_name)
    denomination_category = derive_denomination_category(snap, canonical_name)
    display_sign = derive_display_sign(snap, canonical_name)
    filing_tags = normalize_filing_tags(snap)

    obs: dict[str, Any] = {
        "entity_type": "financial_account",
        "registry_id": registry_id,
    }

    if balance_value is not None:
        obs["balance_value"] = balance_value
    if balance_currency:
        obs["balance_currency"] = balance_currency
    if balance_date:
        obs["balance_date"] = balance_date
    if institution_name:
        obs["institution_name"] = institution_name
    obs["denomination_category"] = denomination_category
    obs["display_sign"] = display_sign
    if filing_tags:
        obs["filing_tags"] = filing_tags

    return obs


def fetch_all_accounts(api_only: bool) -> list[dict[str, Any]]:
    cmd = [
        "neotoma",
        "entities",
        "list",
        "--type",
        "financial_account",
        "--limit",
        "500",
    ]
    if api_only:
        cmd.append("--api-only")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error fetching entities: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(result.stdout or "{}")
    return data.get("entities", data.get("data", []))


def store_entities(
    entities: list[dict[str, Any]], api_only: bool, dry_run: bool
) -> None:
    if dry_run:
        print(json.dumps(entities, indent=2))
        print(
            f"\n--- DRY RUN: {len(entities)} entities would be stored ---",
            file=sys.stderr,
        )
        return

    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(entities, f, indent=2)
        f.flush()
        cmd = [
            "neotoma",
            "store",
            "--file",
            f.name,
            "--idempotency-key",
            "migrate-canonical-fields-v1",
        ]
        if api_only:
            cmd.append("--api-only")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Store failed: {result.stderr}", file=sys.stderr)
            sys.exit(1)
        print(f"Stored {len(entities)} entities", file=sys.stderr)
        if result.stdout.strip():
            print(result.stdout)


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    api_only = "--api-only" in sys.argv

    print("Fetching all financial_account entities...", file=sys.stderr)
    accounts = fetch_all_accounts(api_only)
    print(f"Found {len(accounts)} entities", file=sys.stderr)

    observations: list[dict[str, Any]] = []
    for entity in accounts:
        obs = build_canonical_observation(entity)
        if obs:
            observations.append(obs)

    print(f"Built {len(observations)} canonical observations", file=sys.stderr)

    if not observations:
        print("Nothing to migrate", file=sys.stderr)
        return 0

    batch_size = 50
    for i in range(0, len(observations), batch_size):
        batch = observations[i : i + batch_size]
        print(
            f"Storing batch {i // batch_size + 1} ({len(batch)} entities)...",
            file=sys.stderr,
        )
        store_entities(batch, api_only, dry_run)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
