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
    return json.loads(
        (GEN_DIR / "brand_systems" / f"{request.param}.json").read_text()
    )


def test_checked_in_contracts_validate_and_render_complete_human_mirrors(
    schema, contract
):
    renderer.validate_brand_system(contract, schema)
    document = renderer.render_markdown(contract)
    for heading in (
        "## Positioning",
        "## Voice and copy",
        "## Visual system",
        "## Asset inventory",
        "## Production contract",
        "## Provenance and downstream use",
        "## Completeness",
    ):
        assert heading in document
    assert "Neotoma is canonical" in document
    assert "/Users/" not in document


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.pop("voice"), "missing required brand sections"),
        (lambda value: value.update(status="maybe"), "unrecognized brand status"),
    ],
)
def test_validator_rejects_known_positive_schema_errors(schema, contract, mutation, message):
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


def test_renderer_check_is_deterministic_except_fetch_timestamp(schema, contract):
    later = copy.deepcopy(contract)
    later["_source"]["fetched_at"] = "2099-01-01T00:00:00+00:00"
    assert renderer._normalized(contract) == renderer._normalized(later)
    changed = copy.deepcopy(contract)
    changed["positioning"]["product_promise"] = "stale"
    assert renderer._normalized(contract) != renderer._normalized(changed)
