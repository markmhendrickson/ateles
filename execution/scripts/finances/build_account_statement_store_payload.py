#!/usr/bin/env python3
"""
Build a Neotoma store_structured payload for one dated account statement snapshot.

This keeps long-lived account identity on `financial_account` and writes statement-specific
evidence to a related `account_statement` entity.

Usage:
  python3 execution/scripts/finances/build_account_statement_store_payload.py statement.json

Input JSON shape:
{
  "account": {
    "registry_id": "fidelity_lyft_shares",
    "canonical_name": "Fidelity Lyft shares",
    "institution": "Fidelity",
    "jurisdiction": "USA",
    "currency": "USD"
  },
  "statement": {
    "title": "Fidelity LYFT investment report 2025-10-01 to 2025-12-31",
    "statement_as_of_date": "2025-12-31",
    "statement_period_start": "2025-10-01",
    "statement_period_end": "2025-12-31",
    "statement_source_kind": "fidelity_investment_report_pdf",
    "statement_pdf_path": "/abs/path/to/file.pdf",
    "ending_account_value_usd": 1011.09
  }
}

Output JSON shape:
{
  "entities": [
    { "entity_type": "financial_account", ... },
    { "entity_type": "account_statement", ... }
  ],
  "relationships": [
    { "relationship_type": "REFERS_TO", "source_index": 1, "target_index": 0 }
  ]
}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def load_input(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and value.strip() == "":
            continue
        return value
    return None


def derive_statement_value(
    statement: dict[str, Any], account: dict[str, Any]
) -> tuple[Any, Any]:
    account_value = statement.get("account_value")
    account_value_currency = first_non_empty(
        statement.get("account_value_currency"),
        account.get("account_value_currency"),
        account.get("currency"),
    )
    if isinstance(account_value, int | float):
        return account_value, account_value_currency

    ending_value = statement.get("ending_account_value")
    ending_currency = first_non_empty(
        statement.get("ending_account_value_currency"),
        account.get("currency"),
    )
    if isinstance(ending_value, int | float):
        return ending_value, ending_currency

    ending_usd = statement.get("ending_account_value_usd")
    if isinstance(ending_usd, int | float):
        return ending_usd, "USD"

    ending_eur = statement.get("ending_account_value_eur")
    if isinstance(ending_eur, int | float):
        return ending_eur, "EUR"

    return None, None


def normalize_filing_tags(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    s = str(raw).strip()
    if not s:
        return None
    return [t.strip() for t in s.split(",") if t.strip()]


def build_account_entity(
    account: dict[str, Any], statement: dict[str, Any]
) -> dict[str, Any]:
    registry_id = account.get("registry_id")
    if not registry_id:
        raise ValueError("account.registry_id is required")

    account_value, account_value_currency = derive_statement_value(statement, account)
    last_statement_date = first_non_empty(
        statement.get("statement_as_of_date"),
        statement.get("statement_period_end"),
    )

    out: dict[str, Any] = {
        "entity_type": "financial_account",
        "registry_id": registry_id,
    }
    for key in [
        "canonical_name",
        "institution",
        "jurisdiction",
        "postal_address",
        "currency",
        "modelo_bien",
        "modelo_tipo",
        "filing_model",
        "bic",
        "account_mask_last4",
        "iban_suffix",
        "display_name_en",
        "display_name_es",
        "account_purpose",
        "opened_at",
        "closed_at",
        "notes",
        "strategy_bucket",
        "liquidity_tier",
        "tax_persona",
        "concentration_watch",
        "tax_treatment_hint",
        "quarterly_review_id",
    ]:
        value = account.get(key)
        if value is not None:
            out[key] = value

    filing_tags = normalize_filing_tags(account.get("filing_tags"))
    if filing_tags is not None:
        out["filing_tags"] = filing_tags

    if account_value is not None:
        out["account_value"] = account_value
        out["balance_value"] = account_value
    if account_value_currency is not None:
        out["account_value_currency"] = account_value_currency
        out["balance_currency"] = account_value_currency
    if last_statement_date is not None:
        out["last_statement_date"] = last_statement_date
        out["balance_date"] = last_statement_date

    institution = account.get("institution")
    if institution and str(institution).strip():
        out["institution_name"] = str(institution).strip()

    return out


def build_statement_entity(
    account: dict[str, Any], statement: dict[str, Any]
) -> dict[str, Any]:
    registry_id = account.get("registry_id")
    if not registry_id:
        raise ValueError("account.registry_id is required")

    account_value, account_value_currency = derive_statement_value(statement, account)
    title = first_non_empty(
        statement.get("title"),
        statement.get("canonical_name"),
        f"{first_non_empty(account.get('canonical_name'), registry_id)} statement {first_non_empty(statement.get('statement_as_of_date'), statement.get('statement_period_end'), 'undated')}",
    )

    out = {
        "entity_type": "account_statement",
        "account_registry_id": registry_id,
        "canonical_name": title,
    }

    for key, value in statement.items():
        if key in {"title", "canonical_name"}:
            continue
        out[key] = value

    if account_value is not None and "account_value" not in out:
        out["account_value"] = account_value
    if account_value_currency is not None and "account_value_currency" not in out:
        out["account_value_currency"] = account_value_currency

    return out


def main() -> int:
    if len(sys.argv) != 2:
        print(
            "Usage: build_account_statement_store_payload.py <statement-input.json>",
            file=sys.stderr,
        )
        return 2

    payload = load_input(Path(sys.argv[1]))
    account = payload.get("account")
    statement = payload.get("statement")
    if not isinstance(account, dict):
        raise ValueError("input must include object field `account`")
    if not isinstance(statement, dict):
        raise ValueError("input must include object field `statement`")

    out = {
        "entities": [
            build_account_entity(account, statement),
            build_statement_entity(account, statement),
        ],
        "relationships": [
            {
                "relationship_type": "REFERS_TO",
                "source_index": 1,
                "target_index": 0,
            }
        ],
    }
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
