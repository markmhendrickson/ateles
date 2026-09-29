import json
import re
from pathlib import Path

import pytest

from lib.capabilities import slots
from lib.capabilities.errors import BINDING_MISSING, GenerationRefused
from lib.capabilities.vendor_binding import binding_from_row, resolve_vendor_binding

from .conftest import Spy, binding_row


def test_binding_missing_refuses(make_client):
    client = make_client([])
    vendor = client._adapters["stub"]
    with pytest.raises(GenerationRefused) as exc:
        client.generate(slots.IMAGE_GENERATION, "p")
    assert exc.value.code == BINDING_MISSING
    assert vendor.calls == 0


def test_binding_resolve_scoped_to_capability_match(make_client):
    # A binding exists, but for a DIFFERENT slot: still BINDING_MISSING.
    client = make_client([binding_row(slots.VIDEO_GENERATION)])
    vendor = client._adapters["stub"]
    with pytest.raises(GenerationRefused) as exc:
        client.generate(slots.IMAGE_GENERATION, "p")
    assert exc.value.code == BINDING_MISSING
    assert vendor.calls == 0


def test_unreachable_binding_store_refuses_closed():
    def boom(_):
        raise ConnectionError("down")

    with pytest.raises(GenerationRefused) as exc:
        resolve_vendor_binding(slots.IMAGE_GENERATION, fetch=boom)
    assert exc.value.code == BINDING_MISSING
    assert "unreachable" in exc.value.hint


def test_duplicate_bindings_for_one_slot_refuse():
    rows = [
        binding_row(slots.IMAGE_GENERATION, entity_id="ent_a"),
        binding_row(slots.IMAGE_GENERATION, entity_id="ent_b"),
    ]
    with pytest.raises(GenerationRefused) as exc:
        resolve_vendor_binding(slots.IMAGE_GENERATION, fetch=Spy(rows))
    assert exc.value.code == BINDING_MISSING


def test_fallback_none_by_design_is_none():
    b = binding_from_row(binding_row(slots.IMAGE_GENERATION, fallback="None by design"))
    assert b.fallback is None
    b = binding_from_row(binding_row(slots.IMAGE_GENERATION, fallback="google_image"))
    assert b.fallback == "google_image"


def test_constraints_string_is_parsed_and_bad_json_is_flagged():
    good = binding_from_row(binding_row(slots.IMAGE_GENERATION, constraints={"monthly_cap_usd": 5}))
    assert good.constraints == {"monthly_cap_usd": 5}
    bad = binding_from_row(binding_row(slots.IMAGE_GENERATION, constraints="{not json"))
    assert bad.constraints is None
    unset = binding_from_row(binding_row(slots.IMAGE_GENERATION, constraints=None))
    assert unset.constraints_unset


def test_no_second_binding_type():
    """Every entity_type literal used in the package is vendor_binding or
    generation_record; a new type string fails the build."""
    pkg = Path(__file__).parent
    allowed = {"vendor_binding", "generation_record"}
    found = set()
    for path in pkg.glob("*.py"):
        if path.name.startswith("test_") or path.name == "conftest.py":
            continue
        text = path.read_text()
        found |= set(re.findall(r"""entity_type["']?\s*[:=]\s*["']([a-z_]+)["']""", text))
        found |= set(re.findall(r"""ENTITY_TYPE\s*=\s*["']([a-z_]+)["']""", text))
    assert found and found <= allowed, found
    # and the resolver only ever asks for vendor_binding
    seen = []
    resolve_vendor_binding(slots.IMAGE_GENERATION, fetch=lambda t: seen.append(t) or [binding_row(slots.IMAGE_GENERATION)])
    assert seen == ["vendor_binding"]


def test_no_caller_supplied_entity_id_parameter():
    import inspect

    params = inspect.signature(resolve_vendor_binding).parameters
    assert "entity_id" not in params
    from lib.capabilities.generation import CapabilityClient

    assert "entity_id" not in inspect.signature(CapabilityClient.generate).parameters
