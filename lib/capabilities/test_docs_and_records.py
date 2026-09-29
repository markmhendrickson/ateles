"""Docstring examples the UX lens requires, and the record contract."""

import re
from pathlib import Path

import pytest

from lib.capabilities import critique, errors, generation, records, slots


def test_generation_docstring_has_one_generate_example_per_slot():
    doc = generation.__doc__
    for slot in slots.SLOTS:
        assert re.search(rf'generate\(\s*"{slot}"', doc), slot


def test_generation_docstring_shows_catching_a_refusal():
    doc = generation.__doc__
    assert "except GenerationRefused" in doc
    for needle in ("CAP_EXHAUSTED", "CAP_UNSET", "spent_usd", "cap_usd", "remaining_usd", "hint"):
        assert needle in doc, needle


def test_critique_docstring_shows_the_round_limit_path():
    doc = critique.__doc__
    assert "round_limit" in doc and "ready_for_operator_selection is False" in doc
    assert "CRITIQUE_ROUND_LIMIT" in doc


def test_success_fields_are_all_present():
    fields = generation.GenerationResult.__dataclass_fields__
    for name in ("generation_id", "artifact_ref", "vendor", "model_tier", "cost_usd", "remaining_cap_usd", "slot"):
        assert name in fields


def test_error_codes_match_the_ux_table_exactly():
    assert set(errors.CODES) == {
        "BINDING_MISSING", "CAP_UNSET", "CAP_UNREADABLE", "CAP_EXHAUSTED",
        "CREDENTIAL_UNRESOLVED", "VENDOR_ERROR", "FALLBACK_EXHAUSTED",
        "EMPTY_RESULT", "CRITIQUE_ROUND_LIMIT",
        # raised AFTER a paid call; never retry blindly
        "SPENT_UNRECORDED", "ARTIFACT_UNSAVED",
    }
    assert errors.POST_SPEND_CODES == ("SPENT_UNRECORDED", "ARTIFACT_UNSAVED")
    # the "before any vendor request" group must never contain a post-spend code
    assert not set(errors.REFUSE_BEFORE_VENDOR) & set(errors.POST_SPEND_CODES)
    with pytest.raises(ValueError):
        errors.GenerationRefused("MADE_UP", "s", "m", "h")


def test_refusal_is_a_single_library_exception_with_structured_fields():
    e = errors.GenerationRefused("CAP_EXHAUSTED", "image_generation", "m", "h", spent_usd=1, cap_usd=2, remaining_usd=1)
    assert isinstance(e, errors.CapabilityError)
    assert (e.code, e.slot, e.message, e.hint) == ("CAP_EXHAUSTED", "image_generation", "m", "h")
    assert "remaining_usd=1" in str(e)


def test_no_credential_refusal_hint_advises_exporting_a_key(tmp_path, monkeypatch):
    """Drive every CREDENTIAL_UNRESOLVED path and read the hint text."""
    from lib.capabilities import credentials

    monkeypatch.setenv(credentials.CREDENTIAL_DIR_ENV, str(tmp_path))
    loose = tmp_path / "loose.env"
    loose.write_text("GEMINI_API_KEY" + "=" + "fake" + "-value-" + "0123456789\n")
    loose.chmod(0o644)
    cases = [
        dict(credential_location="oauth:x", credential_file=None),
        dict(credential_location="GEMINI_API_KEY", credential_file=None),
        dict(credential_location="OPENAI_API_KEY", credential_file=None),
        dict(credential_location="not a name", credential_file=None),
        dict(credential_location="GEMINI_API_KEY", credential_file=str(tmp_path / "missing.env")),
        dict(credential_location="GEMINI_API_KEY", credential_file=str(loose)),
    ]
    for case in cases:
        with pytest.raises(errors.GenerationRefused) as exc:
            credentials.resolve_credential(
                "image_generation", allowed_names=("GEMINI_API_KEY",), **case
            )
        text = (exc.value.hint + exc.value.message).lower()
        assert "export" not in text, case


def test_record_fields_are_the_spec_fields_and_carry_no_secret():
    assert set(records.GENERATION_RECORD_FIELDS) == {
        "generation_id", "slot", "prompt", "vendor", "model_tier", "cost_usd",
        "artifact_ref", "binding_entity_id", "created_at", "visibility",
        "requested_vendor", "fallback_used", "billing_slot", "cap_group", "remaining_cap_usd",
    }
    assert not any("key" in f or "secret" in f or "token" in f for f in records.GENERATION_RECORD_FIELDS)


def test_build_record_rejects_undeclared_fields_and_forces_private():
    base = dict(generation_id="g", slot="s", prompt="p", vendor="v", model_tier="m",
                cost_usd=1.0, artifact_ref="a", binding_entity_id="b", created_at="t",
                requested_vendor="v", fallback_used=False, billing_slot="s", cap_group="",
                remaining_cap_usd=1.0)
    assert records.build_generation_record(**base, visibility="public")["visibility"] == "private"
    with pytest.raises(ValueError):
        records.build_generation_record(**base, api_key="nope")
    with pytest.raises(ValueError):
        records.build_generation_record(generation_id="g")
