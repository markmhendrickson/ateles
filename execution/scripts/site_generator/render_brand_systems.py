#!/usr/bin/env python3
"""Render canonical Neotoma brand systems into repository mirrors.

Neotoma ``brand_guideline`` entities own brand expression. This renderer
creates two reviewable derivatives from the same snapshot: a machine contract
for the site generator and a human guide under ``docs/brand``. Neither output
is an independent source of truth and both are checked for drift with
``--check``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
GEN_DIR = Path(__file__).resolve().parent
OUT_DIR = GEN_DIR / "brand_systems"
DOC_DIR = REPO_ROOT / "docs" / "brand"

sys.path.insert(0, str(REPO_ROOT / "execution" / "scripts"))
from neotoma_mirror_lib import load_env, request, unwrap_snapshot  # noqa: E402

SCHEMA_ENTITY_ID = "ent_73da44b2d434cbafe5d8ecb9"
DEFAULT_GUIDELINE_ENTITY_IDS = {
    "ateles": "ent_bada69d5cbb1f27bf82bb86b",
    "neotoma": "ent_c5f3ebd1800a887a3b405e53",
}
KNOWN_PRODUCTS = tuple(DEFAULT_GUIDELINE_ENTITY_IDS)
ALLOWED_STATUSES = {"approved", "provisional", "missing", "retired"}
REQUIRED_SECTIONS = (
    "name",
    "product",
    "slug",
    "schema_version",
    "status",
    "visibility",
    "ownership",
    "scope",
    "positioning",
    "phrases",
    "terminology",
    "voice",
    "visual_styles",
    "visual_concepts",
    "asset_inventory",
    "production_specs",
    "provenance",
    "downstream_contracts",
    "completeness",
    "updated_at",
)


class BrandSystemError(ValueError):
    """A canonical snapshot cannot safely become a public brand contract."""


def _as_json_object(value: object, label: str) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise BrandSystemError(f"{label} is not valid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise BrandSystemError(f"{label} must be a JSON object")
    return value


def validate_brand_system(data: dict, schema: dict | None = None) -> None:
    """Fail closed on the fields that carry brand and publication meaning.

    This intentionally validates the contract's safety-bearing subset using
    stdlib only. The complete JSON Schema is still emitted for downstream
    validators that support draft 2020-12.
    """
    required = tuple((schema or {}).get("required") or REQUIRED_SECTIONS)
    missing = [field for field in required if field not in data]
    if missing:
        raise BrandSystemError(f"missing required brand sections: {', '.join(missing)}")
    if data.get("schema_version") != "1.0":
        raise BrandSystemError("brand entity schema_version must be 1.0")
    if data.get("status") not in ALLOWED_STATUSES:
        raise BrandSystemError(f"unrecognized brand status: {data.get('status')!r}")
    if data.get("visibility") != "private_source":
        raise BrandSystemError("canonical brand guideline must remain private_source")
    if data.get("slug") not in KNOWN_PRODUCTS:
        raise BrandSystemError(f"unsupported product slug: {data.get('slug')!r}")

    positioning = data.get("positioning") or {}
    expected_category = {
        "ateles": "The operating system for agentic organizations.",
        "neotoma": "The system of record for AI agents.",
    }[data["slug"]]
    if positioning.get("category") != expected_category:
        raise BrandSystemError(
            f"{data['slug']} category drift: expected {expected_category!r}"
        )
    if positioning.get("hero_headline") != expected_category:
        raise BrandSystemError(
            f"{data['slug']} hero headline must equal the settled category"
        )

    boundary = str((data.get("provenance") or {}).get("boundary") or "")
    for phrase in ("This entity owns expression", "generated mirrors", "viewer"):
        if phrase.casefold() not in boundary.casefold():
            raise BrandSystemError(f"source boundary is missing {phrase!r}")

    status_lists = (
        data.get("phrases") or [],
        data.get("terminology") or [],
        data.get("visual_concepts") or [],
        data.get("asset_inventory") or [],
        data.get("downstream_contracts") or [],
        (data.get("completeness") or {}).get("dimensions") or [],
    )
    for items in status_lists:
        for item in items:
            if item.get("status") not in ALLOWED_STATUSES:
                raise BrandSystemError(
                    f"unrecognized item status for {item.get('name') or item.get('consumer')!r}"
                )

    flattened = json.dumps(data, sort_keys=True).casefold()
    if data["slug"] == "ateles":
        if "the distributed-authority operating layer for governed initiative" in flattened:
            retired = [
                item
                for item in data.get("phrases", [])
                if item.get("name", "").casefold()
                == "the distributed-authority operating layer for governed initiative."
            ]
            if not retired or retired[0].get("status") != "retired":
                raise BrandSystemError("superseded Ateles category is not retired")
        symbol = json.dumps((data.get("visual_styles") or {}).get("symbol") or {}).casefold()
        if "swarm" not in symbol or "no central hub or permanent edges" not in symbol:
            raise BrandSystemError("Ateles symbol must be an edge-free swarm")
    else:
        symbol_data = (data.get("visual_styles") or {}).get("symbol") or {}
        symbol = json.dumps(symbol_data).casefold()
        guidance = str(symbol_data.get("guidance") or "").casefold()
        if "persistent record graph" not in symbol or not all(
            concept in guidance
            for concept in ("records", "relationships", "prior versions", "agent operations")
        ):
            raise BrandSystemError("Neotoma symbol must be a persistent record graph")
        if any(
            phrase in flattened
            for phrase in ("your agents forget", "neotoma makes them remember")
        ):
            active = [
                item
                for item in data.get("phrases", [])
                if "forget" in item.get("name", "").casefold()
                and item.get("status") != "retired"
            ]
            if active:
                raise BrandSystemError("retired Neotoma memory framing is still active")


def render_contract(
    product: str,
    entity_id: str,
    snapshot: dict,
    provenance: dict,
    *,
    fetched_at: str,
    entity_schema_version: str = "1.0",
) -> dict:
    data = {field: snapshot.get(field) for field in REQUIRED_SECTIONS}
    # The entity API may serialize the entity-schema version as numeric 1.0;
    # the brand contract keeps it as the schema's declared string value.
    data["schema_version"] = str(entity_schema_version)
    doc = {
        "_source": {
            "entity_id": entity_id,
            "entity_type": "brand_guideline",
            "schema_entity_id": SCHEMA_ENTITY_ID,
            "product": product,
            "fetched_at": fetched_at,
        },
        "_observation_ids": {
            field: provenance.get(field) or "unknown" for field in REQUIRED_SECTIONS
        },
        **data,
    }
    return doc


def _status(value: object) -> str:
    return str(value or "missing").upper()


def _bullets(items: list[object]) -> list[str]:
    return [f"- {item}" for item in items] or ["- None declared."]


def render_markdown(contract: dict) -> str:
    """Human mirror with complete, reviewable guidance and source stamps."""
    source = contract["_source"]
    positioning = contract["positioning"]
    voice = contract["voice"]
    styles = contract["visual_styles"]
    production = contract["production_specs"]
    completeness = contract["completeness"]
    lines = [
        "<!-- GENERATED by execution/scripts/site_generator/render_brand_systems.py; DO NOT EDIT. -->",
        "---",
        f"product: {contract['product']}",
        f"schema_version: {contract['schema_version']}",
        f"status: {contract['status']}",
        f"source_entity_id: {source['entity_id']}",
        f"schema_entity_id: {source['schema_entity_id']}",
        "observation_ids:",
    ]
    lines.extend(
        f"  {field}: {observation_id}"
        for field, observation_id in sorted(contract["_observation_ids"].items())
    )
    lines.extend(
        [
            "---",
            "",
            f"# {contract['product']} brand system",
            "",
            "> Neotoma is canonical. This document is a generated human mirror; correct the source entity and rerun the renderer rather than editing this file.",
            "",
            "## Positioning",
            "",
            f"- **Category:** {positioning['category']}",
            f"- **Hero support:** {positioning['hero_support']}",
            f"- **Product promise:** {positioning['product_promise']}",
            f"- **Audience:** {positioning['audience']}",
            "",
            "## Voice and copy",
            "",
            "### Attributes",
            "",
            *_bullets(voice.get("attributes") or []),
            "",
            "### Rules",
            "",
            *_bullets(voice.get("rules") or []),
            "",
            "### Grammar",
            "",
            *_bullets(voice.get("grammar") or []),
            "",
            "### Avoid",
            "",
            *_bullets(voice.get("avoid") or []),
            "",
            "## Phrases",
            "",
        ]
    )
    for item in contract["phrases"]:
        lines.append(
            f"- **{_status(item['status'])}:** {item['name']} — {item['guidance']} (source: {item['source']})"
        )
    lines.extend(["", "## Terms", ""])
    for item in contract["terminology"]:
        lines.append(
            f"- **{_status(item['status'])} · {item['name']}:** {item['guidance']} (source: {item['source']})"
        )
    lines.extend(
        [
            "",
            "## Visual system",
            "",
            f"- **Primary symbol:** {styles['symbol']['name']} — {styles['symbol']['guidance']}",
            f"- **Secondary symbol:** {styles['symbol']['secondary']}",
            f"- **Material:** {styles['material']}",
            f"- **Light:** {styles['light']}",
            f"- **Camera:** {styles['camera']}",
            f"- **Motion:** {styles['motion']}",
            f"- **Composition:** {styles['composition']}",
            "",
            "### Anti-patterns",
            "",
            *_bullets(styles.get("anti_patterns") or []),
            "",
            "### Visual concepts",
            "",
        ]
    )
    for item in contract["visual_concepts"]:
        lines.append(
            f"- **{_status(item['status'])} · {item['name']}:** {item['guidance']}"
        )
    lines.extend(["", "## Asset inventory", ""])
    for item in contract["asset_inventory"]:
        repository = item.get("repository_path") or "not produced"
        lines.append(
            f"- **{_status(item['status'])} · {item['name']}** ({item['kind']}): {item['use']} — `{repository}`"
        )
    lines.extend(
        [
            "",
            "## Production contract",
            "",
            f"- **Cinematic semantics:** {production['cinematic']['semantics']}",
            f"- **Palette and material:** {production['cinematic']['palette_and_material']}",
            f"- **Hero delivery:** {production['delivery']['hero']}",
            f"- **Responsive:** {production['delivery']['responsive']}",
            f"- **Reduced motion:** {production['still_and_reduced_motion']['requirement']}",
            "",
            "### Cinematic prohibitions",
            "",
            *_bullets(production["cinematic"].get("prohibitions") or []),
            "",
            "## Provenance and downstream use",
            "",
            f"{contract['provenance']['boundary']}",
            "",
        ]
    )
    for item in contract["downstream_contracts"]:
        lines.append(
            f"- **{_status(item['status'])} · {item['consumer']}:** {item['contract']}"
        )
    lines.extend(
        [
            "",
            "## Completeness",
            "",
            f"Overall: **{_status(completeness['overall_status'])}**",
            "",
        ]
    )
    for item in completeness.get("dimensions") or []:
        lines.append(f"- {_status(item['status'])} · {item['name']}")
    lines.extend(["", "### Missing", "", *_bullets(completeness.get("missing_items") or []), ""])
    return "\n".join(lines)


def _schema_document(snapshot: dict) -> dict:
    schema = _as_json_object(snapshot.get("content"), "schema content")
    if schema.get("$id") != "urn:ateles:brand-system:1.0.0":
        raise BrandSystemError("unexpected brand schema identifier")
    return schema


def fetch_all(base_url: str, token: str) -> tuple[dict, dict[str, dict]]:
    schema_payload = request(f"{base_url}/entities/{SCHEMA_ENTITY_ID}", token)
    schema_snapshot, _ = unwrap_snapshot(schema_payload)
    schema = _schema_document(schema_snapshot)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    contracts: dict[str, dict] = {}
    for product, default_id in DEFAULT_GUIDELINE_ENTITY_IDS.items():
        entity_id = os.environ.get(
            f"ATELES_{product.upper()}_BRAND_GUIDELINE_ENTITY_ID", default_id
        )
        payload = request(f"{base_url}/entities/{entity_id}", token)
        snapshot, provenance = unwrap_snapshot(payload)
        contract = render_contract(
            product,
            entity_id,
            snapshot,
            provenance,
            fetched_at=fetched_at,
            entity_schema_version=str(payload.get("schema_version") or "1.0"),
        )
        validate_brand_system(contract, schema)
        contracts[product] = contract
    return schema, contracts


def _normalized(document: dict) -> dict:
    document = json.loads(json.dumps(document))
    if "_source" in document:
        document["_source"]["fetched_at"] = None
    return document


def write_all(schema: dict, contracts: dict[str, dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DOC_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "schema.v1.json").write_text(
        json.dumps(schema, indent=2, sort_keys=True) + "\n"
    )
    for product, contract in contracts.items():
        (OUT_DIR / f"{product}.json").write_text(
            json.dumps(contract, indent=2, sort_keys=True) + "\n"
        )
        (DOC_DIR / f"{product}.md").write_text(render_markdown(contract))
        print(f"wrote brand mirrors for {product}")


def check_all(schema: dict, contracts: dict[str, dict]) -> bool:
    ok = True
    schema_path = OUT_DIR / "schema.v1.json"
    if not schema_path.exists() or json.loads(schema_path.read_text()) != schema:
        print("DRIFT: brand_systems/schema.v1.json differs from Neotoma")
        ok = False
    for product, contract in contracts.items():
        json_path = OUT_DIR / f"{product}.json"
        doc_path = DOC_DIR / f"{product}.md"
        if not json_path.exists() or _normalized(json.loads(json_path.read_text())) != _normalized(contract):
            print(f"DRIFT: brand_systems/{product}.json differs from Neotoma")
            ok = False
        if not doc_path.exists() or doc_path.read_text() != render_markdown(contract):
            print(f"DRIFT: docs/brand/{product}.md differs from Neotoma")
            ok = False
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    base_url, token = load_env()
    schema, contracts = fetch_all(base_url, token)
    if args.check:
        if check_all(schema, contracts):
            print("brand system mirror check OK — disk matches Neotoma")
            return 0
        return 1
    write_all(schema, contracts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
