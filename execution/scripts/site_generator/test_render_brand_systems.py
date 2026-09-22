"""Binding tests for the canonical brand-system projection."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

GEN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(GEN_DIR))

import render_brand_systems as renderer  # noqa: E402


@pytest.fixture()
def schema() -> dict:
    return json.loads((GEN_DIR / "brand_systems" / "schema.v1.json").read_text())


@pytest.fixture(params=("ateles", "neotoma"))
def contract(request) -> dict:
    return json.loads((GEN_DIR / "brand_systems" / f"{request.param}.json").read_text())


def test_checked_in_contracts_validate_and_render_complete_human_mirrors(
    schema, contract
):
    renderer.validate_brand_system(contract, schema)
    document = renderer.render_markdown(contract)
    for heading in (
        "## Review status",
        "## Positioning",
        "## Brand intent",
        "## Voice and copy",
        "## Visual system",
        "### Recommended aesthetic territory",
        "## Logo system",
        "## Typography system",
        "## Asset inventory",
        "## Production contract",
        "### Cinematic generation gate",
        "## Accessibility",
        "## Research provenance and review",
        "## Market-reference learning ledger",
        "## Cross-product differentiation matrix",
        "## Provenance and downstream use",
        "## Completeness",
    ):
        assert heading in document
    assert "Neotoma is canonical" in document
    assert "not an approved brand baseline" in document
    assert "/Users/" not in document


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.pop("voice"), "missing required brand sections"),
        (lambda value: value.update(status="maybe"), "unrecognized brand status"),
    ],
)
def test_validator_rejects_known_positive_schema_errors(
    schema, contract, mutation, message
):
    broken = copy.deepcopy(contract)
    mutation(broken)
    with pytest.raises(renderer.BrandSystemError, match=message):
        renderer.validate_brand_system(broken, schema)


def test_validator_rejects_known_positive_ateles_category_and_symbol_drift(schema):
    contract = json.loads((GEN_DIR / "brand_systems" / "ateles.json").read_text())
    stale = copy.deepcopy(contract)
    stale["positioning"]["category"] = (
        "The distributed-authority operating layer for governed initiative."
    )
    with pytest.raises(renderer.BrandSystemError, match="category drift"):
        renderer.validate_brand_system(stale, schema)

    hub = copy.deepcopy(contract)
    hub["visual_styles"]["symbol"]["guidance"] = (
        "A swarm connected permanently to one central hub."
    )
    with pytest.raises(renderer.BrandSystemError, match="edge-free swarm"):
        renderer.validate_brand_system(hub, schema)


def test_validator_rejects_known_positive_neotoma_symbol_and_copy_drift(schema):
    contract = json.loads((GEN_DIR / "brand_systems" / "neotoma.json").read_text())
    memory = copy.deepcopy(contract)
    memory["phrases"].append(
        {
            "name": "Your agents forget.",
            "status": "approved",
            "guidance": "Use in the hero.",
            "source": "fixture",
        }
    )
    with pytest.raises(renderer.BrandSystemError, match="memory framing"):
        renderer.validate_brand_system(memory, schema)

    transient = copy.deepcopy(contract)
    transient["visual_styles"]["symbol"]["guidance"] = (
        "Transient cards with no durable relationships."
    )
    with pytest.raises(renderer.BrandSystemError, match="persistent record graph"):
        renderer.validate_brand_system(transient, schema)


def test_validator_rejects_missing_brand_intent(schema, contract):
    broken = copy.deepcopy(contract)
    broken["positioning"].pop("intent")
    with pytest.raises(renderer.BrandSystemError, match="brand intent"):
        renderer.validate_brand_system(broken, schema)


def test_validator_rejects_missing_logo_variant_and_rule(schema, contract):
    broken = copy.deepcopy(contract)
    broken["visual_styles"]["logo_system"]["variants"].pop("favicon")
    with pytest.raises(renderer.BrandSystemError, match="logo variants"):
        renderer.validate_brand_system(broken, schema)

    broken = copy.deepcopy(contract)
    broken["visual_styles"]["logo_system"].pop("clear_space")
    with pytest.raises(renderer.BrandSystemError, match="logo rules"):
        renderer.validate_brand_system(broken, schema)


def test_validator_rejects_invented_logo_approval(schema, contract):
    broken = copy.deepcopy(contract)
    broken["visual_styles"]["logo_system"]["variants"]["favicon"].update(
        status="approved", source_asset=None
    )
    with pytest.raises(renderer.BrandSystemError, match="approved logo variant"):
        renderer.validate_brand_system(broken, schema)


def test_validator_rejects_typography_role_gap(schema, contract):
    broken = copy.deepcopy(contract)
    broken["visual_styles"]["typography_system"]["roles"].pop("productive")
    with pytest.raises(renderer.BrandSystemError, match="typography roles"):
        renderer.validate_brand_system(broken, schema)


def test_validator_rejects_missing_aesthetic_territory_and_convergence_tests(
    schema, contract
):
    broken = copy.deepcopy(contract)
    broken["visual_styles"].pop("aesthetic_territory")
    with pytest.raises(renderer.BrandSystemError, match="aesthetic territory"):
        renderer.validate_brand_system(broken, schema)

    broken = copy.deepcopy(contract)
    broken["visual_styles"]["aesthetic_territory"]["convergence_tests"] = []
    with pytest.raises(renderer.BrandSystemError, match="convergence tests"):
        renderer.validate_brand_system(broken, schema)


def test_validator_rejects_incomplete_regeneration_chain(schema, contract):
    broken = copy.deepcopy(contract)
    broken["provenance"]["regeneration_chain"]["stages"] = broken["provenance"][
        "regeneration_chain"
    ]["stages"][:-1]
    with pytest.raises(renderer.BrandSystemError, match="regeneration chain"):
        renderer.validate_brand_system(broken, schema)


def test_validator_rejects_accessibility_gap(schema, contract):
    broken = copy.deepcopy(contract)
    broken["production_specs"]["accessibility"].pop("images_of_text")
    with pytest.raises(renderer.BrandSystemError, match="accessibility"):
        renderer.validate_brand_system(broken, schema)


def test_validator_rejects_fail_open_cinematic_generation_gate(schema, contract):
    broken = copy.deepcopy(contract)
    broken["production_specs"]["generation_gate"]["generation_allowed"] = True
    with pytest.raises(renderer.BrandSystemError, match="cannot fail open"):
        renderer.validate_brand_system(broken, schema)

    broken = copy.deepcopy(contract)
    broken["production_specs"]["generation_gate"]["predicates"] = broken[
        "production_specs"
    ]["generation_gate"]["predicates"][:-1]
    with pytest.raises(renderer.BrandSystemError, match="all predicates"):
        renderer.validate_brand_system(broken, schema)


def test_validator_rejects_missing_or_misrepresented_regeneration_gate(
    schema, contract
):
    broken = copy.deepcopy(contract)
    broken["provenance"].pop("regeneration_gate")
    with pytest.raises(renderer.BrandSystemError, match="regeneration gate"):
        renderer.validate_brand_system(broken, schema)

    broken = copy.deepcopy(contract)
    broken["status"] = "approved"
    with pytest.raises(renderer.BrandSystemError, match="approved or generation-ready"):
        renderer.validate_brand_system(broken, schema)

    broken = copy.deepcopy(contract)
    broken["completeness"]["overall_status"] = "approved"
    with pytest.raises(renderer.BrandSystemError, match="approved or generation-ready"):
        renderer.validate_brand_system(broken, schema)

    broken = copy.deepcopy(contract)
    next(
        item
        for item in broken["completeness"]["dimensions"]
        if item["name"] == "positioning"
    )["status"] = "approved"
    with pytest.raises(renderer.BrandSystemError, match="positioning and phrases"):
        renderer.validate_brand_system(broken, schema)

    broken = copy.deepcopy(contract)
    category = broken["positioning"]["category"]
    next(item for item in broken["phrases"] if item["name"] == category)["status"] = (
        "approved"
    )
    with pytest.raises(renderer.BrandSystemError, match="category phrase provisional"):
        renderer.validate_brand_system(broken, schema)


def test_validator_rejects_market_reference_evidence_and_inference_errors(
    schema, contract
):
    broken = copy.deepcopy(contract)
    broken["provenance"]["market_reference_ledger"][0]["evidence"]["source"] = ""
    with pytest.raises(renderer.BrandSystemError, match="evidence source"):
        renderer.validate_brand_system(broken, schema)

    broken = copy.deepcopy(contract)
    item = broken["provenance"]["market_reference_ledger"][0]
    item["derived_learning"] = item["observed_fact"]
    with pytest.raises(renderer.BrandSystemError, match="observation and inference"):
        renderer.validate_brand_system(broken, schema)

    broken = copy.deepcopy(contract)
    item = broken["provenance"]["market_reference_ledger"][0]
    item["evidence"]["observed_at"] = None
    item["status"] = "approved"
    with pytest.raises(renderer.BrandSystemError, match="undated"):
        renderer.validate_brand_system(broken, schema)

    broken = copy.deepcopy(contract)
    item = broken["provenance"]["market_reference_ledger"][0]
    item["support"] = "gap"
    item["status"] = "approved"
    with pytest.raises(renderer.BrandSystemError, match="unsupported"):
        renderer.validate_brand_system(broken, schema)


def test_every_market_reference_has_learning_and_brands_remain_distinct(
    schema, contract
):
    renderer.validate_brand_system(contract, schema)
    for item in contract["provenance"]["market_reference_ledger"]:
        assert item["observed_fact"]
        assert item["derived_learning"].startswith("Inference:")
        assert item["best_practices_to_adopt"]
        assert item["bad_practices_to_avoid"]
        assert item["differentiation_implication"]
    matrix = contract["provenance"]["differentiation_matrix"]
    assert len(matrix["axes"]) == 7
    distinctive = sum(
        row[f"{contract['slug']}_brand_rule_class"] == "distinctive_brand_territory"
        for row in matrix["axes"]
    )
    assert distinctive >= 4


def test_renderer_check_is_deterministic_except_fetch_timestamp(schema, contract):
    later = copy.deepcopy(contract)
    later["_source"]["fetched_at"] = "2099-01-01T00:00:00+00:00"
    assert renderer._normalized(contract) == renderer._normalized(later)
    changed = copy.deepcopy(contract)
    changed["positioning"]["product_promise"] = "stale"
    assert renderer._normalized(contract) != renderer._normalized(changed)
