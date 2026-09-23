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
from url_policy import local_asset_url, public_href  # noqa: E402

SCHEMA_ENTITY_ID = "ent_73da44b2d434cbafe5d8ecb9"
DEFAULT_GUIDELINE_ENTITY_IDS = {
    "ateles": "ent_bada69d5cbb1f27bf82bb86b",
    "neotoma": "ent_c5f3ebd1800a887a3b405e53",
}
KNOWN_PRODUCTS = tuple(DEFAULT_GUIDELINE_ENTITY_IDS)
ALLOWED_STATUSES = {"approved", "provisional", "missing", "retired"}
CONCEPT_DIRECTION_STATUSES = {"concept", "selected", "rejected"}
CONCEPT_COMPLETENESS_STATUSES = ALLOWED_STATUSES | {"selected"}
ADVANCING_LOGO_STATUSES = {"provisional", "approved"}
PLACEHOLDER_SVG_MARKERS = ("TODO", "FIXME", "placeholder", "<svg></svg>")
PRODUCT_FORBIDDEN_TROPES = {
    "ateles": (
        "Generic orchestration",
        "Central controller",
        "Anonymous neural mesh",
        "Literal hive or insects",
        "Seal as brand",
    ),
    "neotoma": (
        "Memory chatbot",
        "Retrieval cache",
        "Agent directory",
        "Database dashboard",
        "Destructive overwrite",
    ),
}
TAUTOLOGY_MARKERS = (
    "unique and memorable",
    "stands out",
    "different from others",
    "ownable mark",
    "distinctive logo",
)
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
GENERATION_GATE_PREDICATES = {
    "brand_intent",
    "category_language",
    "phrases_terminology_voice",
    "symbol_and_logo_for_film",
    "palette_materiality_typography",
    "motion_cinematography",
    "accessibility_static_equivalence",
    "market_learning_and_differentiation",
    "section_copy_to_visual_matrix",
    "brand_brief_consistency",
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
    "mark_concept_board",
    "concept_selection",
    "updated_at",
)


class BrandSystemError(ValueError):
    """A canonical snapshot cannot safely become a public brand contract."""

    def __init__(
        self,
        message: str,
        *,
        field_path: str | None = None,
        reason: str | None = None,
        hint: str | None = None,
    ) -> None:
        self.field_path = field_path
        self.reason = reason
        self.hint = hint
        if field_path and reason and hint and " — hint: " not in message:
            message = f"{field_path}: {reason} — hint: {hint}"
        super().__init__(message)


def raise_brand_error(field_path: str, reason: str, hint: str) -> None:
    raise BrandSystemError(
        f"{field_path}: {reason} — hint: {hint}",
        field_path=field_path,
        reason=reason,
        hint=hint,
    )


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

    territory = styles.get("aesthetic_territory") or {}
    required_territory = {
        "status",
        "name",
        "scope_statement",
        "summary",
        "product_truth",
        "identity",
        "palette_material_light",
        "typography_layout",
        "camera_motion",
        "rejected_alternatives",
        "convergence_tests",
        "originality_basis",
        "source_artifact",
    }
    if required_territory - set(territory):
        raise BrandSystemError("original aesthetic territory is incomplete")
    if territory.get("status") not in ALLOWED_STATUSES:
        raise BrandSystemError("original aesthetic territory has an invalid status")
    if not territory.get("rejected_alternatives") or not territory.get(
        "convergence_tests"
    ):
        raise BrandSystemError(
            "original aesthetic territory needs rejected alternatives and convergence tests"
        )

    accessibility = (data.get("production_specs") or {}).get("accessibility") or {}
    if ACCESSIBILITY_CHECKS - set(accessibility):
        raise BrandSystemError("accessibility requirements are incomplete")
    for name, check in accessibility.items():
        if check.get("status") not in ALLOWED_STATUSES or not check.get("requirement"):
            raise BrandSystemError(f"accessibility check {name!r} is incomplete")
    generation_gate = (data.get("production_specs") or {}).get("generation_gate") or {}
    predicates = generation_gate.get("predicates") or []
    if {item.get("name") for item in predicates} != GENERATION_GATE_PREDICATES:
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
    regeneration_chain = provenance.get("regeneration_chain") or {}
    required_chain_stages = {
        "operator_inputs_and_settled_decisions",
        "define_category",
        "frame_product_argument",
        "competitive_research",
        "aesthetic_ui_benchmark",
        "original_aesthetic_territory",
        "brand_system_draft",
        "operator_brand_approval",
    }
    if {
        item.get("name") for item in regeneration_chain.get("stages") or []
    } != required_chain_stages:
        raise BrandSystemError("brand regeneration chain is incomplete")
    if not regeneration_chain.get("sampling_rationale") or not regeneration_chain.get(
        "source_artifacts"
    ):
        raise BrandSystemError("brand regeneration chain lacks rationale or evidence")
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
    for source in research.get("sources") or []:
        if not public_href(source.get("url")):
            raise BrandSystemError(
                f"research source {source.get('label')!r} has an unsafe URL"
            )
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
        evidence_source = str(evidence.get("source") or "")
        if evidence_source.startswith(
            ("http:", "https:", "javascript:")
        ) and not public_href(evidence_source):
            raise BrandSystemError(
                f"market reference {name!r} has an unsafe public evidence URL"
            )

    matrix = provenance.get("differentiation_matrix") or {}
    rows = matrix.get("axes") or []
    if {row.get("axis") for row in rows} != DIFFERENTIATION_AXES:
        raise BrandSystemError("differentiation matrix must cover all seven axes")
    territory_key = f"{data['slug']}_brand_rule_class"
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
            name = item.get("name") or item.get("consumer")
            status = item.get("status")
            if name == "mark_concept_selection":
                if status not in CONCEPT_COMPLETENESS_STATUSES:
                    raise BrandSystemError(
                        f"unrecognized item status for {name!r}"
                    )
                continue
            if status not in ALLOWED_STATUSES:
                raise BrandSystemError(
                    f"unrecognized item status for {name!r}"
                )
    for asset in data.get("asset_inventory") or []:
        public_path = asset.get("public_path")
        if public_path and not (
            local_asset_url(public_path) or public_href(public_path)
        ):
            raise BrandSystemError(
                f"brand asset {asset.get('name')!r} has an unsafe public path"
            )

    flattened = json.dumps(data, sort_keys=True).casefold()
    if data["slug"] == "ateles":
        if (
            "the distributed-authority operating layer for governed initiative"
            in flattened
        ):
            retired = [
                item
                for item in data.get("phrases", [])
                if item.get("name", "").casefold()
                == "the distributed-authority operating layer for governed initiative."
            ]
            if not retired or retired[0].get("status") != "retired":
                raise BrandSystemError("superseded Ateles category is not retired")
        symbol = json.dumps(
            (data.get("visual_styles") or {}).get("symbol") or {}
        ).casefold()
        if "swarm" not in symbol or "no central hub or permanent edges" not in symbol:
            raise BrandSystemError("Ateles symbol must be an edge-free swarm")
    else:
        symbol_data = (data.get("visual_styles") or {}).get("symbol") or {}
        symbol = json.dumps(symbol_data).casefold()
        guidance = str(symbol_data.get("guidance") or "").casefold()
        if "persistent record graph" not in symbol or not all(
            concept in guidance
            for concept in (
                "records",
                "relationships",
                "prior versions",
                "agent operations",
            )
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

    validate_mark_concept_selection(data)


def _repo_file(path_value: object) -> Path | None:
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    path = Path(path_value)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def _svg_is_placeholder(path: Path) -> str | None:
    if not path.is_file():
        return "missing asset path"
    text = path.read_text(encoding="utf-8", errors="replace")
    lowered = text.casefold()
    if any(marker.casefold() in lowered for marker in PLACEHOLDER_SVG_MARKERS):
        return "placeholder asset"
    if "<svg" not in lowered:
        return "placeholder asset"
    # Prefer filled silhouettes: reject stroke-only marks with no fill geometry.
    has_fill_geom = any(
        token in lowered
        for token in (
            "<circle",
            "<rect",
            "<path",
            "<ellipse",
            "<polygon",
            'fill="currentcolor"',
            "fill='currentcolor'",
            'fill="#',
        )
    )
    stroke_only = "stroke=" in lowered and "fill=\"none\"" in lowered and not has_fill_geom
    if stroke_only or (not has_fill_geom and "stroke=" in lowered):
        return "placeholder asset"
    return None


def _is_tautology(text: str) -> bool:
    lowered = text.casefold().strip()
    if len(lowered) < 24:
        return True
    return any(marker in lowered for marker in TAUTOLOGY_MARKERS) and len(lowered) < 80


def concept_selection_complete(selection: dict, directions: list[dict]) -> bool:
    selected = selection.get("selected_concept_id")
    accepted = selection.get("operator_accepted_at")
    if not selected or not accepted:
        return False
    return any(item.get("concept_id") == selected for item in directions)


def validate_mark_concept_selection(data: dict) -> None:
    """Fail closed on concept authorship and family-advance gating."""
    slug = data.get("slug")
    board = data.get("mark_concept_board")
    selection = data.get("concept_selection")
    if not isinstance(board, dict):
        raise_brand_error(
            "mark_concept_board",
            "missing mark_concept_board",
            "Author 3–5 concept directions under mark_concept_board.directions",
        )
    if not isinstance(selection, dict):
        raise_brand_error(
            "concept_selection",
            "missing concept_selection",
            "Add concept_selection with selected_concept_id and operator_accepted_at",
        )

    directions = board.get("directions")
    if not isinstance(directions, list):
        raise_brand_error(
            "mark_concept_board.directions",
            "directions must be a list",
            "Add/remove until count ∈ [3,5]",
        )
    if not 3 <= len(directions) <= 5:
        raise_brand_error(
            "mark_concept_board.directions",
            f"need 3–5 directions, found {len(directions)}",
            "Add/remove until count ∈ [3,5]",
        )

    seen_ids: set[str] = set()
    required_fields = (
        "concept_id",
        "name",
        "status",
        "compressed_idea",
        "silhouette",
        "form_notes",
        "wordmark_relationship",
        "motion_premise",
        "competitive_distance",
        "memorability",
        "forbidden_perception_checks",
        "symbol_asset",
        "favicon_asset",
    )
    forbidden_required = list(PRODUCT_FORBIDDEN_TROPES.get(slug, ()))
    for index, direction in enumerate(directions):
        prefix = f"mark_concept_board.directions[{index}]"
        if not isinstance(direction, dict):
            raise_brand_error(prefix, "direction must be an object", "Supply a concept direction object")
        for field in required_fields:
            value = direction.get(field)
            if value is None or value == "" or value == []:
                raise_brand_error(
                    f"{prefix}.{field}",
                    f"missing field {field}",
                    f"Supply non-empty value meeting rubric for {field}",
                )
        concept_id = direction["concept_id"]
        if concept_id in seen_ids:
            raise_brand_error(
                f"{prefix}.concept_id",
                f"duplicate concept_id {concept_id!r}",
                "Make each concept_id unique",
            )
        seen_ids.add(concept_id)
        status = direction.get("status")
        if status == "approved":
            raise_brand_error(
                f"{prefix}.status",
                "status: approved is forbidden at concept stage",
                "Use concept / selected / rejected",
            )
        if status not in CONCEPT_DIRECTION_STATUSES:
            raise_brand_error(
                f"{prefix}.status",
                f"unrecognized concept status {status!r}",
                "Use concept / selected / rejected",
            )
        if _is_tautology(str(direction.get("competitive_distance") or "")):
            raise_brand_error(
                f"{prefix}.competitive_distance",
                "competitive_distance is empty or tautological",
                "Cite concrete adjacent brands / conventions",
            )
        if _is_tautology(str(direction.get("memorability") or "")):
            raise_brand_error(
                f"{prefix}.memorability",
                "memorability is empty or tautological",
                "Cite concrete ownable account, not mere semantic compliance",
            )
        checks = [str(item) for item in direction.get("forbidden_perception_checks") or []]
        missing_tropes = [trope for trope in forbidden_required if trope not in checks]
        if missing_tropes:
            raise_brand_error(
                f"{prefix}.forbidden_perception_checks",
                f"forbidden tropes uncovered: {', '.join(missing_tropes)}",
                "Cover full Ateles/Neotoma forbidden list in forbidden_perception_checks",
            )
        for asset_field in ("symbol_asset", "favicon_asset"):
            asset_path = str(direction.get(asset_field) or "")
            if "marks/historical/" in asset_path.replace("\\", "/"):
                raise_brand_error(
                    f"{prefix}.{asset_field}",
                    f"historical as candidate: {asset_path}",
                    "use marks/concepts/ for candidates; keep historical retired/non-candidate",
                )
            resolved = _repo_file(asset_path)
            if resolved is None:
                raise_brand_error(
                    f"{prefix}.{asset_field}",
                    "missing asset path",
                    "Create filled SVG at that path",
                )
            problem = _svg_is_placeholder(resolved)
            if problem == "missing asset path":
                raise_brand_error(
                    f"{prefix}.{asset_field}",
                    f"missing asset path: {asset_path}",
                    "Create filled SVG at that path",
                )
            if problem == "placeholder asset":
                raise_brand_error(
                    f"{prefix}.{asset_field}",
                    f"placeholder asset: {asset_path}",
                    "Replace TODO/empty/stroke-only with filled silhouette",
                )

    selected_id = selection.get("selected_concept_id")
    accepted_at = selection.get("operator_accepted_at")
    if selected_id and not accepted_at:
        raise_brand_error(
            "concept_selection.operator_accepted_at",
            "selected_concept_id set but operator_accepted_at null",
            "Record operator acceptance timestamp (and actor)",
        )
    if selected_id and selected_id not in seen_ids:
        raise_brand_error(
            "concept_selection.selected_concept_id",
            "selected_concept_id not in directions",
            "Use an existing concept_id or add the direction first",
        )
    if accepted_at and not selected_id:
        raise_brand_error(
            "concept_selection.selected_concept_id",
            "operator_accepted_at set without selected_concept_id",
            "Set selected_concept_id to an existing concept_id",
        )

    selection_ok = concept_selection_complete(selection, directions)
    if selection.get("blocking_family_until_selection") is not True and not selection_ok:
        raise_brand_error(
            "concept_selection.blocking_family_until_selection",
            "blocking_family_until_selection must be true until selection is complete",
            "Keep blocking_family_until_selection true until operator acceptance",
        )

    styles = data.get("visual_styles") or {}
    logo = styles.get("logo_system") or {}
    variants = logo.get("variants") or {}
    if not selection_ok:
        for key, variant in variants.items():
            status = (variant or {}).get("status")
            source_asset = str((variant or {}).get("source_asset") or "").replace("\\", "/")
            historical_only = bool(source_asset) and "marks/historical/" in source_asset
            if status in ADVANCING_LOGO_STATUSES and not historical_only:
                raise_brand_error(
                    f"visual_styles.logo_system.variants.{key}.status",
                    "blocked: logo_system.variants provisional/approved while concept_selection incomplete",
                    "Set selected_concept_id + operator_accepted_at, or keep variants missing/retired / historical-only",
                )
            if source_asset and "marks/historical/" in source_asset and status in ADVANCING_LOGO_STATUSES:
                raise_brand_error(
                    f"visual_styles.logo_system.variants.{key}.source_asset",
                    f"historical as candidate: {source_asset}",
                    "use marks/concepts/ for candidates; keep historical retired/non-candidate",
                )

    dims = (data.get("completeness") or {}).get("dimensions") or []
    concept_dim = next((item for item in dims if item.get("name") == "mark_concept_selection"), None)
    if concept_dim is None:
        raise_brand_error(
            "completeness.dimensions",
            "missing mark_concept_selection dimension",
            "Add completeness dimension name mark_concept_selection",
        )
    dim_status = concept_dim.get("status")
    if dim_status not in CONCEPT_COMPLETENESS_STATUSES:
        raise_brand_error(
            "completeness.dimensions[mark_concept_selection].status",
            f"unrecognized completeness status {dim_status!r}",
            "Use missing / provisional / selected / approved / retired",
        )
    if selection_ok and dim_status not in {"selected", "approved", "provisional"}:
        raise_brand_error(
            "completeness.dimensions[mark_concept_selection].status",
            "selection complete but mark_concept_selection not marked selected",
            "Set mark_concept_selection status to selected after operator acceptance",
        )
    if not selection_ok and dim_status == "selected":
        raise_brand_error(
            "completeness.dimensions[mark_concept_selection].status",
            "mark_concept_selection selected while concept_selection incomplete",
            "Keep status missing/provisional until selected_concept_id + operator_accepted_at",
        )


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
            f"### Recommended aesthetic territory · {_status(styles['aesthetic_territory']['status'])}",
            "",
            f"**{styles['aesthetic_territory']['name']}** — {styles['aesthetic_territory']['summary']}",
            "",
            f"- **Scope:** {styles['aesthetic_territory']['scope_statement']}",
            f"- **Product truth:** {styles['aesthetic_territory']['product_truth']}",
            f"- **Identity:** {styles['aesthetic_territory']['identity']}",
            f"- **Palette, material, and light:** {styles['aesthetic_territory']['palette_material_light']}",
            f"- **Typography and layout:** {styles['aesthetic_territory']['typography_layout']}",
            f"- **Camera and motion:** {styles['aesthetic_territory']['camera_motion']}",
            f"- **Originality basis:** {styles['aesthetic_territory']['originality_basis']}",
            "",
            "#### Rejected alternatives",
            "",
            *_bullets(
                [
                    f"{item['name']} — {item['reason']}"
                    for item in styles["aesthetic_territory"]["rejected_alternatives"]
                ]
            ),
            "",
            "#### Convergence tests",
            "",
            *_bullets(
                [
                    f"{item['reference']} — {item['test']}"
                    for item in styles["aesthetic_territory"]["convergence_tests"]
                ]
            ),
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
    board = contract.get("mark_concept_board") or {}
    selection = contract.get("concept_selection") or {}
    lines.extend(
        [
            "",
            "## Mark concept board",
            "",
            f"- **Selection:** {selection.get('selected_concept_id') or 'none'} · accepted_at={selection.get('operator_accepted_at') or 'null'} · blocking_family={selection.get('blocking_family_until_selection')}",
            f"- **Operator actor:** {selection.get('operator_actor') or 'unset'}",
            "",
            "### Directions",
            "",
        ]
    )
    directions = board.get("directions") or []
    if not directions:
        lines.append("- [COPY: no concepts yet — author 3–5 directions]")
    for direction in directions:
        lines.extend(
            [
                f"#### {direction.get('name')} (`{direction.get('concept_id')}`) · {_status(direction.get('status'))}",
                "",
                f"- **Idea:** {direction.get('compressed_idea')}",
                f"- **Silhouette:** {direction.get('silhouette')}",
                f"- **Form notes:** {direction.get('form_notes')}",
                f"- **Wordmark relationship:** {direction.get('wordmark_relationship')}",
                f"- **Motion premise:** {direction.get('motion_premise')}",
                f"- **Competitive distance:** {direction.get('competitive_distance')}",
                f"- **Memorability:** {direction.get('memorability')}",
                f"- **Forbidden perception checks:** {'; '.join(direction.get('forbidden_perception_checks') or [])}",
                f"- **Symbol asset:** `{direction.get('symbol_asset')}`",
                f"- **Favicon asset:** `{direction.get('favicon_asset')}`",
                "",
            ]
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
            f"- **Sampling rationale:** {provenance['regeneration_chain']['sampling_rationale']}",
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
                f"- **State:** {_status(item['status'])}; {item['confidence']} confidence; {item['brand_rule_class'].replace('_', ' ')}; {item['visibility']}",
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
                f"- **Ateles · {item['ateles_brand_rule_class'].replace('_', ' ')}:** {item['ateles']}",
                f"- **Neotoma · {item['neotoma_brand_rule_class'].replace('_', ' ')}:** {item['neotoma']}",
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
    lines.extend(
        ["", "### Missing", "", *_bullets(completeness.get("missing_items") or []), ""]
    )
    return "\n".join(lines)


def _schema_document(snapshot: dict) -> dict:
    schema = _as_json_object(snapshot.get("content"), "schema content")
    if schema.get("$id") != "urn:ateles:brand-system:1.3.0":
        raise BrandSystemError("unexpected brand schema identifier")
    return schema


def load_local_mirrors() -> tuple[dict, dict[str, dict]]:
    schema = json.loads((OUT_DIR / "schema.v1.json").read_text())
    if schema.get("$id") != "urn:ateles:brand-system:1.3.0":
        raise BrandSystemError("unexpected brand schema identifier")
    contracts: dict[str, dict] = {}
    for product in KNOWN_PRODUCTS:
        contracts[product] = json.loads((OUT_DIR / f"{product}.json").read_text())
    return schema, contracts


def check_local(schema: dict, contracts: dict[str, dict]) -> bool:
    ok = True
    for product, contract in contracts.items():
        try:
            validate_brand_system(contract, schema)
            expected_doc = render_markdown(contract)
            doc_path = DOC_DIR / f"{product}.md"
            if doc_path.exists() and doc_path.read_text() != expected_doc:
                print(
                    f"DRIFT: docs/brand/{product}.md differs from local contract render"
                )
                ok = False
            print(f"OK local validate {product}")
        except BrandSystemError as exc:
            # Print the full BrandSystemError message (field_path + reason + hint).
            # Preferring .hint alone drops locating context on the offline --local path.
            print(f"FAIL {product}: {exc}")
            ok = False
    return ok


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
        if not json_path.exists() or _normalized(
            json.loads(json_path.read_text())
        ) != _normalized(contract):
            print(f"DRIFT: brand_systems/{product}.json differs from Neotoma")
            ok = False
        if not doc_path.exists() or doc_path.read_text() != render_markdown(contract):
            print(f"DRIFT: docs/brand/{product}.md differs from Neotoma")
            ok = False
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--local",
        action="store_true",
        help="Validate checked-in disk mirrors without Neotoma credentials",
    )
    args = parser.parse_args()
    if args.local:
        schema, contracts = load_local_mirrors()
        if check_local(schema, contracts):
            print("brand system local check OK — disk mirrors validate")
            return 0
        return 1
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
