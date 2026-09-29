import json
import os
from pathlib import Path

import pytest

from lib.capabilities import generation as gen_mod
from lib.capabilities import slots
from lib.credential_scrub import AGENT_CHILD_MARKER_ENV
from lib.capabilities.errors import (
    SPENT_UNRECORDED,
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


def _rows(ledger):
    path = ledger.root / SLOT / f"{ledger.month()}.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _spent(ledger):
    """Spend as the ledger counts it: last row per id, voided counts zero."""
    last = {}
    for r in _rows(ledger):
        last[r["generation_id"]] = r
    return sum(0.0 if r["state"] == "voided" else r["cost_usd"] for r in last.values())


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

    # ledger: a PENDING row at the estimate, then the completed row
    lines = (ledger.root / SLOT / f"{ledger.month()}.jsonl").read_text().strip().splitlines()
    rows = [json.loads(l) for l in lines]
    assert [r["state"] for r in rows] == ["pending", "completed"]
    assert all(r["generation_id"] == result.generation_id for r in rows)
    assert rows[-1]["cost_usd"] == 0.75 and rows[-1]["vendor"] == "stub"

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
    assert record["requested_vendor"] == "stub" and record["fallback_used"] is False
    assert record["billing_slot"] == SLOT and record["cap_group"] == ""
    assert record["remaining_cap_usd"] == result.remaining_cap_usd == 9.25
    assert key == f"generation-{result.generation_id}"
    assert result.record_persisted and result.record_entity_id == "ent_rec_1"


def test_record_carries_no_secret_field(make_client, sink, cred_file):
    secret = "stub-" + "secret-value-123"
    path = cred_file("STUB_KEY", secret)
    client = make_client(
        [binding_row(SLOT, constraints={**CAP, "credential_env_file": path})],
        adapters={"stub": StubVendor(credential_names=("STUB_KEY",))},
    )
    result = client.generate(SLOT, "p")
    (record, _), = sink.stored
    blob = json.dumps(record) + repr(result)
    assert secret not in blob


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
    primary = StubVendor("primary", fail_with=VendorFailure("boom", http_status=429))
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
    primary = StubVendor("primary", fail_with=VendorFailure("boom", http_status=429))
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
    primary = StubVendor("primary", fail_with=VendorFailure("boom", http_status=429))
    backup = StubVendor("backup", default_slot=slots.VIDEO_GENERATION, fail_with=VendorFailure("boom2", http_status=429))
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
    lone = StubVendor("primary", fail_with=VendorFailure("boom", http_status=429))
    client = make_client(
        [binding_row(SLOT, "primary", constraints=CAP, fallback="None by design")],
        adapters={"primary": lone},
    )
    with pytest.raises(GenerationRefused) as one:
        client.generate(SLOT, "p")
    assert one.value.code == VENDOR_ERROR
    assert one.value.vendors_tried == ("primary",)


def test_fallback_configured_but_unbound_is_fallback_exhausted(make_client):
    primary = StubVendor("primary", fail_with=VendorFailure("boom", http_status=429))
    client = make_client(
        [binding_row(SLOT, "primary", constraints=CAP, fallback="ghost")],
        adapters={"primary": primary},
    )
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == FALLBACK_EXHAUSTED


def test_fallback_does_not_bypass_its_own_cap(make_client):
    primary = StubVendor("primary", fail_with=VendorFailure("boom", http_status=429))
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


def test_empty_artifact_is_empty_result_and_the_estimate_stays_held(make_client, sink, ledger):
    """A 200 with no bytes may still have been billed: EMPTY_RESULT, not
    retryable, and the estimate keeps counting against the cap."""
    stub = StubVendor(data=b"", cost_usd=1.0)
    client = make_client([binding_row(SLOT, constraints=CAP)], adapters={"stub": stub})
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == EMPTY_RESULT and exc.value.retryable is False
    assert exc.value.spent_usd == 1.0 and "held" in exc.value.message
    assert sink.stored == []
    assert [r["state"] for r in _rows(ledger)] == ["pending", "accepted_unfinished"]


def test_adapter_reported_empty_is_held_not_free(make_client, sink, ledger):
    stub = StubVendor(fail_with=EmptyArtifact("nothing"), cost_usd=1.0)
    client = make_client([binding_row(SLOT, constraints=CAP)], adapters={"stub": stub})
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == EMPTY_RESULT and exc.value.retryable is False
    assert sink.stored == []
    assert _spent(ledger) == 1.0


def test_non_svg_payload_for_svg_media_type_is_empty_result(make_client, sink):
    stub = StubVendor(data=b"<html>nope</html>", media_type="image/svg+xml")
    client = make_client([binding_row(SLOT, constraints=CAP)], adapters={"stub": stub})
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == EMPTY_RESULT and sink.stored == []


@pytest.mark.parametrize(
    "status, no_charge, retryable",
    [(400, True, False), (403, True, False), (429, True, True), (None, False, False),
     (500, False, False), (503, False, False), (408, False, False)],
)
def test_vendor_error_includes_classification(make_client, ledger, status, no_charge, retryable):
    stub = StubVendor(fail_with=VendorFailure("upstream said no", http_status=status), cost_usd=1.0)
    client = make_client([binding_row(SLOT, constraints=CAP)], adapters={"stub": stub})
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    err = exc.value
    assert err.code == VENDOR_ERROR and err.retryable is retryable
    if no_charge:
        assert _spent(ledger) == 0.0                    # voided: definitive no charge
        assert ("Retryable" in err.hint) is retryable
        assert ("Operator-fix" in err.hint) is (not retryable)
    else:
        assert _spent(ledger) == 1.0                    # possibly billed: held
        assert "Do not retry" in err.hint and "held" in err.message
        assert "Retryable" not in err.hint
    assert ("HTTP" in err.message) is (status is not None)


def test_definitive_no_charge_failure_voids_the_row(make_client, ledger, sink):
    stub = StubVendor(fail_with=VendorFailure("bad request", http_status=400), cost_usd=1.0)
    client = make_client([binding_row(SLOT, constraints={"model_tier": "m", "monthly_cap_usd": 1.0})],
                         adapters={"stub": stub})
    for _ in range(3):  # never accumulates, so never exhausts
        with pytest.raises(GenerationRefused) as exc:
            client.generate(SLOT, "p")
        assert exc.value.code == VENDOR_ERROR
    assert stub.calls == 3 and sink.stored == []
    assert [r["state"] for r in _rows(ledger)] == ["pending", "voided"] * 3


def test_credential_unresolved_refuses(make_client, cred_file):
    stub = StubVendor(credential_names=("STUB_KEY",))
    good = cred_file("OTHER_NAME", "x" * 20)                    # file lacks STUB_KEY
    for constraints in (
        {**CAP, "credential_env_file": good},
        {**CAP, "credential_env_file": good + ".missing"},
        CAP,                                                     # no credential_env_file at all
    ):
        client = make_client([binding_row(SLOT, constraints=constraints)], adapters={"stub": stub})
        with pytest.raises(GenerationRefused) as exc:
            client.generate(SLOT, "p")
        assert exc.value.code == CREDENTIAL_UNRESOLVED
        text = exc.value.hint.lower()
        assert "export" not in text
        assert "agent" in text or "credential_env_file" in text
    assert stub.calls == 0


def test_credential_reaches_only_the_adapter_and_never_the_result(make_client, cred_file):
    secret = "stub-" + "secret-value-123"
    path = cred_file("STUB_KEY", secret)
    stub = StubVendor(credential_names=("STUB_KEY",))
    client = make_client([binding_row(SLOT, constraints={**CAP, "credential_env_file": path})],
                         adapters={"stub": stub})
    result = client.generate(SLOT, "p")
    assert stub.last_credential.reveal() == secret
    assert secret not in repr(stub.last_credential)
    assert secret not in repr(result)
    import os
    assert "STUB_KEY" not in os.environ


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


def test_final_ledger_write_failure_is_spent_unrecorded_and_still_counts(make_client, ledger, monkeypatch):
    """A paid call whose final ledger row cannot be written: SPENT_UNRECORDED,
    non-retryable, names the artifact and generation_id; the PENDING row keeps
    the estimate counted so the cap still advances."""
    stub = StubVendor(cost_usd=1.0)
    client = make_client([binding_row(SLOT, constraints={"model_tier": "m", "monthly_cap_usd": 1.5})],
                         adapters={"stub": stub})
    from lib.capabilities import spend

    real = spend.LockedLedger.record

    def fail_on_completed(self, row, billing_slot, month=None):
        if row["state"] == "completed":
            raise spend.LedgerWriteError("OSError")
        return real(self, row, billing_slot, month)

    monkeypatch.setattr(spend.LockedLedger, "record", fail_on_completed)
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    err = exc.value
    assert err.code == SPENT_UNRECORDED and err.retryable is False
    assert err.generation_id and Path(err.artifact_ref).exists()
    assert err.generation_id in err.hint and "Do NOT retry" in err.hint
    monkeypatch.setattr(spend.LockedLedger, "record", real)
    with pytest.raises(GenerationRefused) as again:
        client.generate(SLOT, "p")
    assert again.value.code == "CAP_EXHAUSTED"     # the estimate is still counted
    assert stub.calls == 1


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
        created_at="2026-09-15T12:00:00+00:00", requested_vendor="v", fallback_used=False,
        billing_slot=SLOT, cap_group="", remaining_cap_usd=9.5,
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
