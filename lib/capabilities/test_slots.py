import pytest

from lib.capabilities import slots
from lib.capabilities.errors import BINDING_MISSING, GenerationRefused
from lib.capabilities.vendor_binding import resolve_vendor_binding

from .conftest import Spy, binding_row


@pytest.mark.parametrize("slot", slots.SLOTS)
def test_exact_slot_strings_resolve(slot):
    binding = resolve_vendor_binding(slot, fetch=Spy([binding_row(slot)]))
    assert binding.capability == slot


@pytest.mark.parametrize(
    "alias", ["svg_gen", "veo", "image_gen", "mark_generation", "Image_Generation", " image_generation", ""]
)
def test_alias_slots_rejected(alias):
    # Even when a binding for the real slot exists, an alias must not resolve.
    spy = Spy([binding_row(s) for s in slots.SLOTS])
    with pytest.raises(GenerationRefused) as exc:
        resolve_vendor_binding(alias, fetch=spy)
    assert exc.value.code == BINDING_MISSING
    assert spy.calls == 0  # rejected before any lookup


def test_non_string_slot_rejected():
    with pytest.raises(GenerationRefused) as exc:
        resolve_vendor_binding(None, fetch=Spy([]))  # type: ignore[arg-type]
    assert exc.value.code == BINDING_MISSING
