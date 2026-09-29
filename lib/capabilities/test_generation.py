import json
import os
from pathlib import Path

import pytest

from lib.capabilities import generation as gen_mod
from lib.capabilities import slots
from lib.capabilities.credential_names import AGENT_CHILD_MARKER_ENV
from lib.capabilities.errors import (
    BINDING_MISSING,
    CAP_UNREADABLE,
    CREDENTIAL_UNRESOLVED,
    EMPTY_RESULT,
    FALLBACK_EXHAUSTED,
    VENDOR_ERROR,
    EmptyArtifact,
    GenerationRefused,
    VendorFailure,
)
from lib.capabilities.generation import NeotomaRecordSink
from lib.capabilities.vendors import StubVendor

from .conftest import binding_row

SLOT = slots.IMAGE_GENERATION
CAP = {"model_tier": "stub-model", "monthly_cap_usd": 10}


def test_stub_generate_persists_prompt_and_cost(make_client, sink, ledger):
    stub = StubVendor(cost_usd=0.75)
    client = make_client([binding_row(SLOT, constraints=CAP)], adapters={"stub": stub})
    prior = 0.0
    result = client.generate(SLOT, "a red circle")

    assert result.slot == SLOT
    assert result.vendor == "stub" and result.model_tier == "stub-model"
    assert result.cost_usd == 0.75
    assert result.remaining_cap_usd == 10 - (prior + 0.75)
    assert Path(result.artifact_ref).read_bytes() == stub.data

    # ledger row
    line = (ledger.root / SLOT / f"{ledger.month()}.jsonl").read_text().strip()
    row = json.loads(line)
    assert row["generation_id"] == result.generation_id
    assert row["cost_usd"] == 0.75 and row["vendor"] == "stub"

    # generation_record: each field individually
    (record, key), = sink.stored
    assert record["entity_type"] == "generation_record"
    assert record["slot"] == SLOT
    assert record["prompt"] == "a red circle"
    assert record["vendor"] == "stub"
    assert record["model_tier"] == "stub-model"
    assert record["cost_usd"] == 0.75
    assert record["artifact_ref"] == result.artifact_ref
    assert record["generation_id"] == result.generation_id
    assert record["binding_entity_id"] == f"ent_{SLOT}"
    assert record["created_at"]
    assert record["visibility"] == "private"
    assert key == f"generation-{result.generation_id}"
    assert result.record_persisted and result.record_entity_id == "ent_rec_1"


def test_record_carries_no_secret_field(make_client, sink):
    client = make_client(
        [binding_row(SLOT, constraints=CAP)],
        adapters={"stub": StubVendor(credential_names=("STUB_KEY",))},
    )
    result = client.generate(SLOT, "p")
    (record, _), = sink.stored
    blob = json.dumps(record) + repr(result)
    assert "stub-secret-value-123" not in blob


def test_generation_id_precedes_vendor_call(make_client):
    order = []
    stub = StubVendor(on_call=lambda: order.append("vendor"))

    def new_id():
        order.append("id")
        return "gen_fixed"

    client = make_client(
        [binding_row(SLOT, constraints=CAP)], adapters={"stub": stub}, new_id=new_id
    )
    result = client.generate(SLOT, "p")
    assert order == ["id", "vendor"]
    assert result.generation_id == "gen_fixed"


def test_fallback_vendor_named_in_result(make_client):
    primary = StubVendor("primary", fail_with=VendorFailure("boom", http_status=503))
    backup = StubVendor("backup", default_slot=slots.VIDEO_GENERATION, cost_usd=1.0)
    rows = [
        binding_row(SLOT, "primary", constraints=CAP, fallback="backup"),
        binding_row(slots.VIDEO_GENERATION, "backup", constraints={"model_tier": "backup-model", "monthly_cap_usd": 10}),
    ]
    client = make_client(rows, adapters={"primary": primary, "backup": backup})
    result = client.generate(SLOT, "p")
    assert result.vendor == "backup" and result.model_tier == "backup-model"
    assert result.requested_vendor == "primary" and result.fallback_used
    assert primary.calls == 1 and backup.calls == 1
    assert result.slot == SLOT


def test_fallback_not_attempted_when_disabled(make_client):
    primary = StubVendor("primary", fail_with=VendorFailure("boom", http_status=503))
    backup = StubVendor("backup", default_slot=slots.VIDEO_GENERATION)
    rows = [
        binding_row(SLOT, "primary", constraints=CAP, fallback="backup"),
        binding_row(slots.VIDEO_GENERATION, "backup", constraints=CAP),
    ]
    client = make_client(rows, adapters={"primary": primary, "backup": backup})
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p", allow_fallback=False)
    assert exc.value.code == VENDOR_ERROR
    assert backup.calls == 0


def test_fallback_exhausted_vs_vendor_error_are_distinguished(make_client):
    # both fail -> FALLBACK_EXHAUSTED
    primary = StubVendor("primary", fail_with=VendorFailure("boom", http_status=500))
    backup = StubVendor("backup", default_slot=slots.VIDEO_GENERATION, fail_with=VendorFailure("boom2", http_status=500))
    rows = [
        binding_row(SLOT, "primary", constraints=CAP, fallback="backup"),
        binding_row(slots.VIDEO_GENERATION, "backup", constraints=CAP),
    ]
    client = make_client(rows, adapters={"primary": primary, "backup": backup})
    with pytest.raises(GenerationRefused) as both:
        client.generate(SLOT, "p")
    assert both.value.code == FALLBACK_EXHAUSTED
    assert both.value.vendors_tried == ("primary", "backup")

    # no fallback configured -> VENDOR_ERROR
    lone = StubVendor("primary", fail_with=VendorFailure("boom", http_status=500))
    client = make_client(
        [binding_row(SLOT, "primary", constraints=CAP, fallback="None by design")],
        adapters={"primary": lone},
    )
    with pytest.raises(GenerationRefused) as one:
        client.generate(SLOT, "p")
    assert one.value.code == VENDOR_ERROR
    assert one.value.vendors_tried == ("primary",)


def test_fallback_configured_but_unbound_is_fallback_exhausted(make_client):
    primary = StubVendor("primary", fail_with=VendorFailure("boom", http_status=500))
    client = make_client(
        [binding_row(SLOT, "primary", constraints=CAP, fallback="ghost")],
        adapters={"primary": primary},
    )
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == FALLBACK_EXHAUSTED


def test_fallback_does_not_bypass_its_own_cap(make_client):
    primary = StubVendor("primary", fail_with=VendorFailure("boom", http_status=500))
    backup = StubVendor("backup", default_slot=slots.VIDEO_GENERATION, cost_usd=5.0)
    rows = [
        binding_row(SLOT, "primary", constraints=CAP, fallback="backup"),
        binding_row(slots.VIDEO_GENERATION, "backup", constraints={"model_tier": "m", "monthly_cap_usd": 1}),
    ]
    client = make_client(rows, adapters={"primary": primary, "backup": backup})
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == FALLBACK_EXHAUSTED
    assert backup.calls == 0


def test_cap_refusal_on_primary_never_falls_back_to_another_paid_vendor(make_client):
    primary = StubVendor("primary")
    backup = StubVendor("backup", default_slot=slots.VIDEO_GENERATION)
    rows = [
        binding_row(SLOT, "primary", constraints={"model_tier": "m"}, fallback="backup"),  # no cap
        binding_row(slots.VIDEO_GENERATION, "backup", constraints=CAP),
    ]
    client = make_client(rows, adapters={"primary": primary, "backup": backup})
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == "CAP_UNSET"
    assert primary.calls == 0 and backup.calls == 0


def test_empty_artifact_is_empty_result(make_client, sink, ledger):
    stub = StubVendor(data=b"")
    client = make_client([binding_row(SLOT, constraints=CAP)], adapters={"stub": stub})
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == EMPTY_RESULT
    assert sink.stored == []
    assert not (ledger.root / SLOT).exists()  # no spend recorded


def test_adapter_reported_empty_is_empty_result_and_records_nothing(make_client, sink, ledger):
    stub = StubVendor(fail_with=EmptyArtifact("nothing"))
    client = make_client([binding_row(SLOT, constraints=CAP)], adapters={"stub": stub})
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == EMPTY_RESULT
    assert sink.stored == [] and not (ledger.root / SLOT).exists()


def test_non_svg_payload_for_svg_media_type_is_empty_result(make_client, sink):
    stub = StubVendor(data=b"<html>nope</html>", media_type="image/svg+xml")
    client = make_client([binding_row(SLOT, constraints=CAP)], adapters={"stub": stub})
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == EMPTY_RESULT and sink.stored == []


@pytest.mark.parametrize(
    "status, retryable",
    [(400, False), (403, False), (429, True), (503, True), (None, True)],
)
def test_vendor_error_includes_classification(make_client, status, retryable):
    stub = StubVendor(fail_with=VendorFailure("upstream said no", http_status=status))
    client = make_client([binding_row(SLOT, constraints=CAP)], adapters={"stub": stub})
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    err = exc.value
    assert err.code == VENDOR_ERROR and err.retryable is retryable
    assert ("Retryable" in err.hint) is retryable
    assert ("Operator-fix" in err.hint) is (not retryable)
    assert ("HTTP" in err.message) is (status is not None)


def test_vendor_error_records_no_spend(make_client, ledger, sink):
    stub = StubVendor(fail_with=VendorFailure("x", http_status=500))
    client = make_client([binding_row(SLOT, constraints=CAP)], adapters={"stub": stub})
    with pytest.raises(GenerationRefused):
        client.generate(SLOT, "p")
    assert not (ledger.root / SLOT).exists() and sink.stored == []


def test_credential_unresolved_refuses(make_client):
    stub = StubVendor(credential_names=("STUB_KEY",))
    for values in ({}, {"STUB_KEY": ""}, {"STUB_KEY": "   "}):
        client = make_client(
            [binding_row(SLOT, constraints=CAP)],
            adapters={"stub": stub},
            process_values=values,
        )
        with pytest.raises(GenerationRefused) as exc:
            client.generate(SLOT, "p")
        assert exc.value.code == CREDENTIAL_UNRESOLVED
        text = exc.value.hint.lower()
        assert "export" not in text
        assert "agent" in text  # it names the client, not the agent env, as the home
    assert stub.calls == 0


def test_credential_reaches_only_the_adapter_and_never_the_result(make_client):
    stub = StubVendor(credential_names=("STUB_KEY",))
    client = make_client([binding_row(SLOT, constraints=CAP)], adapters={"stub": stub})
    result = client.generate(SLOT, "p")
    assert stub.last_credential.reveal() == "stub-secret-value-123"
    assert "stub-secret-value-123" not in repr(stub.last_credential)
    assert "stub-secret-value-123" not in repr(result)


def test_unknown_vendor_id_refuses_before_any_call(make_client):
    stub = StubVendor()
    client = make_client(
        [binding_row(SLOT, "mystery", constraints=CAP)], adapters={"stub": stub}
    )
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == BINDING_MISSING and stub.calls == 0


def test_unpriced_model_refuses_rather_than_treating_it_as_free(make_client):
    from lib.capabilities.vendors import GoogleImageAdapter

    calls = []
    adapter = GoogleImageAdapter(lambda *a, **k: calls.append(1))
    rows = [binding_row(SLOT, "google_image", constraints={"model_tier": "gemini-9-mystery", "monthly_cap_usd": 50}, credential_location="GEMINI_API_KEY")]
    client = make_client(rows, adapters={"google_image": adapter}, process_values={"GEMINI_API_KEY": "k" * 20})
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == CAP_UNREADABLE and calls == []


def test_unknown_tier_refuses(make_client):
    client = make_client([binding_row(SLOT, constraints=CAP)])
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p", tier="draft")
    assert exc.value.code == BINDING_MISSING


def test_tier_selects_the_bindings_named_model(make_client):
    stub = StubVendor()
    constraints = {**CAP, "draft_model_tier": "cheap-model"}
    client = make_client([binding_row(SLOT, constraints=constraints)], adapters={"stub": stub})
    assert client.generate(SLOT, "p", tier="draft").model_tier == "cheap-model"


def test_empty_prompt_refuses_before_anything(make_client):
    stub = StubVendor()
    client = make_client([binding_row(SLOT, constraints=CAP)], adapters={"stub": stub})
    with pytest.raises(GenerationRefused):
        client.generate(SLOT, "   ")
    assert stub.calls == 0


def test_refuses_inside_a_dispatched_agent_child(make_client):
    stub = StubVendor()
    client = make_client(
        [binding_row(SLOT, constraints=CAP)],
        adapters={"stub": stub},
        process_values={AGENT_CHILD_MARKER_ENV: "1", "STUB_KEY": "stub-secret-value-123"},
    )
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == CREDENTIAL_UNRESOLVED and stub.calls == 0


def test_ledger_write_failure_after_spend_is_loud_and_names_the_artifact(make_client, ledger, monkeypatch):
    stub = StubVendor()
    client = make_client([binding_row(SLOT, constraints=CAP)], adapters={"stub": stub})
    from lib.capabilities import spend

    def broken(self, row, billing_slot):
        raise GenerationRefused(CAP_UNREADABLE, SLOT, "disk full", "fix it")

    monkeypatch.setattr(spend.LockedLedger, "record", broken)
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == CAP_UNREADABLE
    assert "saved at" in exc.value.message


def test_record_store_failure_keeps_the_artifact_and_warns(make_client):
    class Exploding:
        def store(self, record, key):
            raise RuntimeError("neotoma down")

    client = make_client([binding_row(SLOT, constraints=CAP)], sink=Exploding())
    result = client.generate(SLOT, "p")
    assert not result.record_persisted
    assert any("generation_record was not stored" in w for w in result.warnings)
    assert Path(result.artifact_ref).exists()


def test_svg_generation_gets_a_raster_preview(make_client):
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><circle r='4'/></svg>"
    stub = StubVendor(data=svg, media_type="image/svg+xml")
    client = make_client(
        [binding_row(slots.VECTOR_MARK_GENERATION, constraints=CAP)],
        adapters={"stub": stub},
        renderer=lambda data, width: b"\x89PNG-preview",
    )
    result = client.generate(slots.VECTOR_MARK_GENERATION, "mark")
    assert Path(result.raster_preview_ref).read_bytes() == b"\x89PNG-preview"
    assert result.artifact_ref.endswith(".svg")


def test_svg_without_rasterizer_keeps_paid_artifact(make_client, monkeypatch):
    from lib.capabilities import rasterize
    from lib.capabilities.errors import RasterizerUnavailable

    def none_available(data, width):
        raise RasterizerUnavailable("no rasterizer")

    svg = b"<svg xmlns='http://www.w3.org/2000/svg'/>"
    stub = StubVendor(data=svg, media_type="image/svg+xml")
    client = make_client(
        [binding_row(slots.VECTOR_MARK_GENERATION, constraints=CAP)],
        adapters={"stub": stub},
        renderer=none_available,
    )
    result = client.generate(slots.VECTOR_MARK_GENERATION, "mark")
    assert result.raster_preview_ref is None
    assert any("raster_preview_unavailable" in w for w in result.warnings)


def test_artifacts_are_owner_only(make_client):
    client = make_client([binding_row(SLOT, constraints=CAP)])
    result = client.generate(SLOT, "p")
    assert Path(result.artifact_ref).stat().st_mode & 0o077 == 0


def test_module_level_generate_uses_the_supplied_client(make_client):
    client = make_client([binding_row(SLOT, constraints=CAP)])
    assert gen_mod.generate(SLOT, "p", client=client).vendor == "stub"


# --- the Neotoma record sink: a 2xx is not proof -----------------------------


def _record():
    from lib.capabilities import records

    return records.build_generation_record(
        generation_id="gen_1", slot=SLOT, prompt="p", vendor="v", model_tier="m",
        cost_usd=0.5, artifact_ref="/tmp/a", binding_entity_id="ent_b",
        created_at="2026-09-15T12:00:00+00:00",
    )


def _fake_neotoma(snapshot_override=None):
    record = _record()

    def request(method, path, body):
        if method == "POST":
            assert path == "/store"
            assert body["idempotency_key"] == "generation-gen_1"
            assert body["entities"][0]["visibility"] == "private"
            return {"entities": [{"entity_id": "ent_9", "entity_type": "generation_record"}]}
        snap = {**record}
        snap.update(snapshot_override or {})
        return {"snapshot": {"snapshot": snap}}

    return request, record


def test_sink_reads_back_and_confirms_private():
    request, record = _fake_neotoma()
    entity_id, warning = NeotomaRecordSink(request).store(record, "generation-gen_1")
    assert entity_id == "ent_9" and warning is None


@pytest.mark.parametrize(
    "override", [{"visibility": "public"}, {"prompt": "other"}, {"cost_usd": 9.0}]
)
def test_sink_does_not_trust_a_success_code(override):
    request, record = _fake_neotoma(override)
    entity_id, warning = NeotomaRecordSink(request).store(record, "generation-gen_1")
    assert entity_id is None and warning
