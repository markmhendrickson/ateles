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
LOGO_VARIANTS = {
    "primary_mark",
    "wordmark",
    "lockup",
    "symbol_only",
    "horizontal",
    "stacked",
    "monochrome",
    "reversed",
    "small_scale",
    "favicon",
    "application",
}
LOGO_RULES = {
    "clear_space",
    "minimum_size",
    "background_rules",
    "colorway_rules",
    "co_branding",
    "source_formats",
    "export_formats",
    "misuse_rules",
}
ACCESSIBILITY_CHECKS = {
    "contrast",
    "images_of_text",
    "semantic_headings",
    "relative_sizing",
    "reduced_motion_static_equivalence",
}
DIFFERENTIATION_AXES = {
    "category_language",
    "symbol_metaphor",
    "palette_materiality",
    "typography",
    "motion_cinematography",
    "voice",
    "proof_style",
}
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
    current_category_projection = {
        "ateles": "The operating system for agentic organizations.",
        "neotoma": "The system of record for AI agents.",
    }[data["slug"]]
    if positioning.get("category") != current_category_projection:
        raise BrandSystemError(
            f"{data['slug']} category drift: expected current provisional projection "
            f"{current_category_projection!r}"
        )
    if positioning.get("hero_headline") != current_category_projection:
        raise BrandSystemError(
            f"{data['slug']} hero headline must equal the current provisional "
            "category projection"
        )
    intent = positioning.get("intent") or {}
    required_intent = {
        "status",
        "core_idea",
        "functional_truth",
        "emotional_outcome",
        "intended_perceptions",
        "forbidden_perceptions",
        "proof_cues",
        "sibling_distinction",
    }
    if required_intent - set(intent):
        raise BrandSystemError("brand intent is incomplete")

    styles = data.get("visual_styles") or {}
    logo = styles.get("logo_system") or {}
    variants = logo.get("variants") or {}
    if LOGO_VARIANTS - set(variants):
        raise BrandSystemError("logo variants are incomplete")
    if LOGO_RULES - set(logo):
        raise BrandSystemError("logo rules are incomplete")
    for key, variant in variants.items():
        status = variant.get("status")
        if status not in ALLOWED_STATUSES:
            raise BrandSystemError(f"unrecognized logo status for {key!r}")
        if status == "approved" and (
            not variant.get("source_asset") or not variant.get("export_formats")
        ):
            raise BrandSystemError(
                f"approved logo variant {key!r} needs a source asset and export"
            )

    typography = styles.get("typography_system") or {}
    roles = typography.get("roles") or {}
    if {"expressive", "productive", "technical"} - set(roles):
        raise BrandSystemError("typography roles are incomplete")
    if not typography.get("hierarchy") or not typography.get("weights_styles"):
        raise BrandSystemError("typography hierarchy and weights/styles are required")
    if {"sizing", "line_height", "measure", "casing"} - set(
        typography.get("responsive") or {}
    ):
        raise BrandSystemError("typography responsive rules are incomplete")

    accessibility = (data.get("production_specs") or {}).get("accessibility") or {}
    if ACCESSIBILITY_CHECKS - set(accessibility):
        raise BrandSystemError("accessibility requirements are incomplete")
    for name, check in accessibility.items():
        if check.get("status") not in ALLOWED_STATUSES or not check.get("requirement"):
            raise BrandSystemError(f"accessibility check {name!r} is incomplete")
    generation_gate = (data.get("production_specs") or {}).get("generation_gate") or {}
    predicates = generation_gate.get("predicates") or []
    if len(predicates) != 9:
        raise BrandSystemError("cinematic generation gate must define all predicates")
    if any(
        item.get("status") not in {"approved", "provisional", "missing", "blocked"}
        or not item.get("name")
        or not item.get("evidence")
        for item in predicates
    ):
        raise BrandSystemError("cinematic generation gate predicate is incomplete")
    if generation_gate.get("generation_allowed") and (
        generation_gate.get("status") != "approved"
        or any(item["status"] != "approved" for item in predicates)
        or generation_gate.get("unresolved")
    ):
        raise BrandSystemError("cinematic generation gate cannot fail open")

    provenance = data.get("provenance") or {}
    regeneration_gate = provenance.get("regeneration_gate") or {}
    required_regeneration_fields = {
        "status",
        "reason",
        "next_gate",
        "task_id",
        "skill_id",
        "zero_category_definitions_verified",
    }
    if required_regeneration_fields - set(regeneration_gate):
        raise BrandSystemError("brand regeneration gate is incomplete")
    if regeneration_gate.get("status") == "blocked" and (
        data.get("status") != "provisional"
        or (data.get("completeness") or {}).get("overall_status") != "provisional"
        or generation_gate.get("generation_allowed")
    ):
        raise BrandSystemError(
            "blocked brand regeneration cannot present an approved or generation-ready baseline"
        )
    if regeneration_gate.get("status") == "blocked":
        dimension_statuses = {
            item.get("name"): item.get("status")
            for item in (data.get("completeness") or {}).get("dimensions") or []
        }
        if any(
            dimension_statuses.get(name) != "provisional"
            for name in ("positioning", "phrases")
        ):
            raise BrandSystemError(
                "blocked brand regeneration must mark positioning and phrases provisional"
            )
        category_phrase = next(
            (
                item
                for item in data.get("phrases") or []
                if item.get("name") == current_category_projection
            ),
            None,
        )
        if not category_phrase or category_phrase.get("status") != "provisional":
            raise BrandSystemError(
                "blocked brand regeneration must keep the category phrase provisional"
            )
    research = provenance.get("research") or {}
    if not research.get("reviewed_at") or not research.get("sources"):
        raise BrandSystemError("research provenance is incomplete")
    cadence = research.get("review_cadence") or {}
    if not cadence.get("cadence") or cadence.get("status") not in ALLOWED_STATUSES:
        raise BrandSystemError("research review cadence is incomplete")

    market_references = provenance.get("market_reference_ledger") or []
    if not market_references:
        raise BrandSystemError("market-reference learning ledger is empty")
    for item in market_references:
        name = item.get("referenced_product") or "unnamed reference"
        evidence = item.get("evidence") or {}
        if not str(evidence.get("source") or "").strip():
            raise BrandSystemError(f"market reference {name!r} has no evidence source")
        observation = str(item.get("observed_fact") or "").strip()
        inference = str(item.get("derived_learning") or "").strip()
        if not observation or not inference or observation == inference:
            raise BrandSystemError(
                f"market reference {name!r} must separate observation and inference"
            )
        if not inference.startswith("Inference:"):
            raise BrandSystemError(
                f"market reference {name!r} must explicitly label its inference"
            )
        for key in (
            "best_practices_to_adopt",
            "bad_practices_to_avoid",
        ):
            if not item.get(key):
                raise BrandSystemError(f"market reference {name!r} lacks {key}")
        if not item.get("differentiation_implication"):
            raise BrandSystemError(
                f"market reference {name!r} lacks a distinctiveness test"
            )
        if item.get("status") == "approved" and not evidence.get("observed_at"):
            raise BrandSystemError(f"approved market reference {name!r} is undated")
        if item.get("status") == "approved" and item.get("support") == "gap":
            raise BrandSystemError(f"approved market reference {name!r} is unsupported")
        if item.get("visibility") not in {
            "public_safe",
            "internal_review",
            "confidential",
        }:
            raise BrandSystemError(f"market reference {name!r} lacks visibility")
        if item.get("visibility") == "public_safe" and str(
            evidence.get("source")
        ).startswith("ent_"):
            raise BrandSystemError(
                f"public market reference {name!r} exposes an internal identifier"
            )

    matrix = provenance.get("differentiation_matrix") or {}
    rows = matrix.get("axes") or []
    if {row.get("axis") for row in rows} != DIFFERENTIATION_AXES:
        raise BrandSystemError("differentiation matrix must cover all seven axes")
    territory_key = f"{data['slug']}_territory"
    distinctive = sum(
        row.get(territory_key) == "distinctive_brand_territory" for row in rows
    )
    if distinctive < 4:
        raise BrandSystemError(
            "brand needs several product-grounded distinctiveness choices"
        )

    boundary = str(provenance.get("boundary") or "")
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
    intent = positioning["intent"]
    logo = styles["logo_system"]
    typography = styles["typography_system"]
    provenance = contract["provenance"]
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
            "## Review status",
            "",
            f"- **State:** {_status(provenance['regeneration_gate']['status'])}",
            f"- **Why:** {provenance['regeneration_gate']['reason']}",
            f"- **Next gate:** {provenance['regeneration_gate']['next_gate']}",
            "- **Rule:** This provisional evidence is not an approved brand baseline. No page or film may treat it as final until category and brand approval are complete.",
            "",
            "## Positioning",
            "",
            f"- **Category:** {positioning['category']}",
            f"- **Hero support:** {positioning['hero_support']}",
            f"- **Product promise:** {positioning['product_promise']}",
            f"- **Audience:** {positioning['audience']}",
            "",
            "## Brand intent",
            "",
            f"- **Core idea:** {intent['core_idea']}",
            f"- **Functional truth:** {intent['functional_truth']}",
            f"- **Emotional outcome:** {intent['emotional_outcome']}",
            f"- **Sibling distinction:** {intent['sibling_distinction']}",
            "",
            "### Intended perceptions",
            "",
            *_bullets(intent.get("intended_perceptions") or []),
            "",
            "### Forbidden perceptions",
            "",
            *_bullets(intent.get("forbidden_perceptions") or []),
            "",
            "### Proof cues",
            "",
            *_bullets(intent.get("proof_cues") or []),
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
    lines.extend(["", "## Logo system", ""])
    for key, item in logo["variants"].items():
        source_asset = item.get("source_asset") or "not produced"
        exports = ", ".join(item.get("export_formats") or []) or "not produced"
        lines.append(
            f"- **{_status(item['status'])} · {key.replace('_', ' ')} — {item['name']}:** "
            f"{item['use']} (source: `{source_asset}`; exports: {exports})"
        )
    lines.extend(
        [
            "",
            f"- **Clear space · {_status(logo['clear_space']['status'])}:** {logo['clear_space']['guidance']}",
            f"- **Minimum size · {_status(logo['minimum_size']['status'])}:** {logo['minimum_size']['guidance']}",
            f"- **Backgrounds · {_status(logo['background_rules']['status'])}:** {logo['background_rules']['guidance']}",
            f"- **Colorways · {_status(logo['colorway_rules']['status'])}:** {logo['colorway_rules']['guidance']}",
            f"- **Co-branding · {_status(logo['co_branding']['status'])}:** {logo['co_branding']['guidance']}",
            "",
            "### Logo misuse",
            "",
            *_bullets(logo.get("misuse_rules") or []),
            "",
            "## Typography system",
            "",
        ]
    )
    for key, role in typography["roles"].items():
        lines.append(
            f"- **{_status(role['status'])} · {key}:** {role['name']} — {role['use']}"
        )
    lines.extend(["", "### Hierarchy and tokens", ""])
    for item in typography["hierarchy"]:
        lines.append(
            f"- **{_status(item['status'])} · {item['token']}:** {item['family']}; "
            f"{item['size']}; line-height {item['line_height']}; measure {item['measure']}; {item['casing']}"
        )
    lines.extend(
        [
            "",
            "### Responsive rules",
            "",
            f"- **Sizing:** {typography['responsive']['sizing']}",
            f"- **Line height:** {typography['responsive']['line_height']}",
            f"- **Measure:** {typography['responsive']['measure']}",
            f"- **Casing:** {typography['responsive']['casing']}",
            "",
            "### Forbidden typography",
            "",
            *_bullets(typography.get("forbidden_use") or []),
        ]
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
            "### Cinematic generation gate",
            "",
            f"- **State:** {_status(production['generation_gate']['status'])}",
            f"- **Generation allowed:** {str(production['generation_gate']['generation_allowed']).lower()}",
            f"- **Rule:** {production['generation_gate']['rule']}",
            "",
        ]
    )
    for item in production["generation_gate"]["predicates"]:
        lines.append(
            f"- **{_status(item['status'])} · {item['name'].replace('_', ' ')}:** {item['evidence']}"
        )
    lines.extend(
        [
            "",
            "### Cinematic prohibitions",
            "",
            *_bullets(production["cinematic"].get("prohibitions") or []),
            "",
            "## Accessibility",
            "",
        ]
    )
    for name, check in production["accessibility"].items():
        lines.append(
            f"- **{_status(check['status'])} · {name.replace('_', ' ')}:** {check['requirement']}"
        )
    lines.extend(
        [
            "",
            "## Research provenance and review",
            "",
            f"- **Reviewed:** {provenance['research']['reviewed_at']}",
            f"- **Cadence · {_status(provenance['research']['review_cadence']['status'])}:** {provenance['research']['review_cadence']['cadence']}",
            "",
        ]
    )
    for item in provenance["research"]["sources"]:
        lines.append(
            f"- [{item['label']}]({item['url']}) — {item['informs']} ({item['checked_at']})"
        )
    lines.extend(["", "## Market-reference learning ledger", ""])
    for item in provenance["market_reference_ledger"]:
        evidence = item["evidence"]
        source_value = evidence["source"]
        source_label = (
            f"[{source_value}]({source_value})"
            if source_value.startswith("https://")
            else f"`{source_value}`"
        )
        lines.extend(
            [
                f"### {item['referenced_product']} · {item['relationship']}",
                "",
                f"- **State:** {_status(item['status'])}; {item['confidence']} confidence; {item['territory'].replace('_', ' ')}; {item['visibility']}",
                f"- **Evidence:** {source_label} ({evidence['observed_at']})",
                f"- **Observed fact:** {item['observed_fact']}",
                f"- **Derived learning:** {item['derived_learning']}",
                f"- **Best practice to adopt:** {'; '.join(item['best_practices_to_adopt'])}",
                f"- **Bad practice to avoid:** {'; '.join(item['bad_practices_to_avoid'])}",
                f"- **Differentiation implication:** {item['differentiation_implication']}",
                "",
            ]
        )
    lines.extend(["## Cross-product differentiation matrix", ""])
    for item in provenance["differentiation_matrix"]["axes"]:
        lines.extend(
            [
                f"### {item['axis'].replace('_', ' ')}",
                "",
                f"- **Ateles · {item['ateles_territory'].replace('_', ' ')}:** {item['ateles']}",
                f"- **Neotoma · {item['neotoma_territory'].replace('_', ' ')}:** {item['neotoma']}",
                f"- **Convergence test:** {item['convergence_test']}",
                "",
            ]
        )
    lines.extend(
        [
            "## Provenance and downstream use",
            "",
            f"{provenance['boundary']}",
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
    if schema.get("$id") != "urn:ateles:brand-system:1.1.0":
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
