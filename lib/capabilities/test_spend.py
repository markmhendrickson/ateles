"""Fail-closed spend: every refusal happens BEFORE the vendor is called."""

import json
import threading

import pytest

from lib.capabilities import slots
from lib.capabilities.errors import (
    CAP_EXHAUSTED,
    CAP_UNREADABLE,
    CAP_UNSET,
    GenerationRefused,
)
from lib.capabilities.spend import parse_cap_policy
from lib.capabilities.vendor_binding import binding_from_row

from .conftest import binding_row

SLOT = slots.IMAGE_GENERATION


def _client(make_client, constraints, cost=1.0):
    from lib.capabilities.vendors import StubVendor

    stub = StubVendor(cost_usd=cost)
    return make_client([binding_row(SLOT, constraints=constraints)], adapters={"stub": stub}), stub


def _raise_code(client, code):
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == code
    return exc.value


def test_cap_unset_refuses_without_vendor_call(make_client):
    for constraints in ({"model_tier": "stub-model"}, None, ""):
        client, stub = _client(make_client, constraints)
        _raise_code(client, CAP_UNSET)
        assert stub.calls == 0


def test_cap_unreadable_malformed_constraints_refuses_without_vendor_call(make_client):
    client, stub = _client(make_client, "{not json")
    _raise_code(client, CAP_UNREADABLE)
    assert stub.calls == 0


@pytest.mark.parametrize("bad", ["ten", None, True, -5, float("inf"), [10]])
def test_cap_value_not_a_number_is_unreadable_not_zero(make_client, bad):
    client, stub = _client(make_client, json.dumps({"monthly_cap_usd": bad}))
    _raise_code(client, CAP_UNREADABLE)
    assert stub.calls == 0


def test_cap_unreadable_ledger_io_error_refuses_without_vendor_call(make_client, ledger):
    client, stub = _client(make_client, {"monthly_cap_usd": 10})
    slot_dir = ledger.root / SLOT
    slot_dir.mkdir(parents=True)
    # A directory where the month file should be: reading it raises OSError.
    (slot_dir / f"{ledger.month()}.jsonl").mkdir()
    _raise_code(client, CAP_UNREADABLE)
    assert stub.calls == 0


def test_cap_unreadable_corrupt_ledger_row_refuses(make_client, ledger):
    client, stub = _client(make_client, {"monthly_cap_usd": 10})
    slot_dir = ledger.root / SLOT
    slot_dir.mkdir(parents=True)
    (slot_dir / f"{ledger.month()}.jsonl").write_text("{garbage\n")
    _raise_code(client, CAP_UNREADABLE)
    assert stub.calls == 0


def test_world_writable_ledger_is_treated_as_tampered(make_client, ledger):
    client, stub = _client(make_client, {"monthly_cap_usd": 10})
    slot_dir = ledger.root / SLOT
    slot_dir.mkdir(parents=True)
    path = slot_dir / f"{ledger.month()}.jsonl"
    path.write_text("")
    path.chmod(0o666)
    _raise_code(client, CAP_UNREADABLE)
    assert stub.calls == 0


def test_cap_exhausted_refuses_without_vendor_call(make_client):
    client, stub = _client(make_client, {"monthly_cap_usd": 3}, cost=1.0)
    for _ in range(3):
        client.generate(SLOT, "p")
    assert stub.calls == 3
    err = _raise_code(client, CAP_EXHAUSTED)
    assert stub.calls == 3  # the refused call never reached the vendor
    assert (err.spent_usd, err.cap_usd, err.remaining_usd) == (3.0, 3.0, 0.0)
    assert err.hint


def test_estimate_larger_than_remaining_reports_arithmetic(make_client):
    client, stub = _client(make_client, {"monthly_cap_usd": 2.5}, cost=1.0)
    client.generate(SLOT, "p")
    client.generate(SLOT, "p")  # spent 2.0, remaining 0.5, next costs 1.0
    err = _raise_code(client, CAP_EXHAUSTED)
    assert err.spent_usd == 2.0 and err.cap_usd == 2.5 and err.remaining_usd == 0.5
    assert stub.calls == 2


def test_cap_exactly_at_boundary_authorizes(make_client):
    client, stub = _client(make_client, {"monthly_cap_usd": 2.0}, cost=1.0)
    client.generate(SLOT, "p")
    result = client.generate(SLOT, "p")  # spent + estimate == cap: allowed
    assert result.remaining_cap_usd == 0.0
    assert stub.calls == 2
    _raise_code(client, CAP_EXHAUSTED)  # and the next is refused


def test_boundary_is_not_decided_by_float_noise(make_client):
    # 0.1 + 0.2 != 0.3 in floats; in micro-dollars it must still fit exactly.
    client, stub = _client(make_client, {"monthly_cap_usd": 0.3}, cost=0.1)
    client.generate(SLOT, "p")
    client.generate(SLOT, "p")
    client.generate(SLOT, "p")
    assert stub.calls == 3
    _raise_code(client, CAP_EXHAUSTED)


def test_zero_cap_refuses_everything(make_client):
    client, stub = _client(make_client, {"monthly_cap_usd": 0})
    _raise_code(client, CAP_EXHAUSTED)
    assert stub.calls == 0


def test_second_call_sees_first_calls_recorded_spend(make_client):
    """The ledger read for call 2 happens after call 1's write (sequential)."""
    client, stub = _client(make_client, {"monthly_cap_usd": 1.5}, cost=1.0)
    client.generate(SLOT, "p")
    _raise_code(client, CAP_EXHAUSTED)
    assert stub.calls == 1


def test_concurrent_calls_do_not_double_authorize_over_cap(make_client):
    """Two threads race for a balance that fits only one call."""
    client, stub = _client(make_client, {"monthly_cap_usd": 1.5}, cost=1.0)
    outcomes = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        try:
            client.generate(SLOT, "p")
            outcomes.append("ok")
        except GenerationRefused as refused:
            outcomes.append(refused.code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(outcomes) == [CAP_EXHAUSTED, "ok"]
    assert stub.calls == 1


def test_replayed_ledger_row_is_not_double_counted(make_client, ledger):
    client, stub = _client(make_client, {"monthly_cap_usd": 2.0}, cost=1.0)
    result = client.generate(SLOT, "p")
    # Simulate an at-least-once replay of the same row.
    path = ledger.root / SLOT / f"{ledger.month()}.jsonl"
    line = path.read_text()
    path.write_text(line + line)
    # Only 1.0 is counted, so one more 1.0 call still fits (boundary), not two.
    second = client.generate(SLOT, "p")
    assert second.remaining_cap_usd == 0.0
    assert result.generation_id != second.generation_id


def test_ledger_files_are_owner_only(make_client, ledger):
    client, _ = _client(make_client, {"monthly_cap_usd": 5})
    client.generate(SLOT, "p")
    path = ledger.root / SLOT / f"{ledger.month()}.jsonl"
    assert path.stat().st_mode & 0o077 == 0
    assert ledger.root.stat().st_mode & 0o077 == 0


def test_new_utc_month_starts_a_fresh_budget(tmp_path, make_client):
    from datetime import datetime, timezone

    from lib.capabilities.spend import SpendLedger

    clock = {"t": datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc)}
    led = SpendLedger(tmp_path / "s2", clock=lambda: clock["t"], lock_timeout_s=2)
    client, stub = _client(make_client, {"monthly_cap_usd": 1.0}, cost=1.0)
    client._ledger = led
    client.generate(SLOT, "p")
    _raise_code(client, CAP_EXHAUSTED)
    clock["t"] = datetime(2026, 10, 1, 0, 1, tzinfo=timezone.utc)
    client.generate(SLOT, "p")
    assert stub.calls == 2


# --- cap groups: USD 50 combined across two slots ---------------------------

GROUP = {"monthly_cap_usd": 50, "cap_group": "google_generation", "cap_group_total_usd": 50}


def _grouped_client(make_client, cost_image=20.0, cost_video=20.0):
    from lib.capabilities.vendors import StubVendor

    img = StubVendor("img", default_slot=slots.IMAGE_GENERATION, cost_usd=cost_image)
    vid = StubVendor("vid", default_slot=slots.VIDEO_GENERATION, cost_usd=cost_video)
    rows = [
        binding_row(slots.IMAGE_GENERATION, "img", constraints=GROUP),
        binding_row(slots.VIDEO_GENERATION, "vid", constraints=GROUP),
    ]
    return make_client(rows, adapters={"img": img, "vid": vid}), img, vid


def test_cap_group_is_enforced_across_slots(make_client):
    client, img, vid = _grouped_client(make_client)
    client.generate(slots.IMAGE_GENERATION, "p")   # 20
    client.generate(slots.VIDEO_GENERATION, "p")   # 40 combined
    # Each slot alone (20 of 50) has room, but the group has 10 left.
    with pytest.raises(GenerationRefused) as exc:
        client.generate(slots.IMAGE_GENERATION, "p")
    err = exc.value
    assert err.code == CAP_EXHAUSTED
    assert (err.spent_usd, err.cap_usd, err.remaining_usd) == (40.0, 50.0, 10.0)
    assert img.calls == 1 and vid.calls == 1  # refused call never reached a vendor


def test_cap_group_remaining_reflects_combined_spend(make_client):
    client, _, _ = _grouped_client(make_client, cost_image=10.0, cost_video=5.0)
    first = client.generate(slots.IMAGE_GENERATION, "p")
    second = client.generate(slots.VIDEO_GENERATION, "p")
    assert first.remaining_cap_usd == 40.0
    assert second.remaining_cap_usd == 35.0  # group remaining, not the slot's 45


def test_cap_group_without_total_is_unset(make_client):
    client, stub = _client(
        make_client, {"monthly_cap_usd": 10, "cap_group": "g"}, cost=1.0
    )
    _raise_code(client, CAP_UNSET)
    assert stub.calls == 0


def test_cap_group_bad_total_is_unreadable(make_client):
    client, stub = _client(
        make_client, {"monthly_cap_usd": 10, "cap_group": "g", "cap_group_total_usd": "lots"}
    )
    _raise_code(client, CAP_UNREADABLE)
    assert stub.calls == 0


def test_parse_cap_policy_reads_real_shaped_google_binding():
    constraints = {
        "model_tier": "gemini-3-pro-image",
        "monthly_cap_usd": 50,
        "cap_group": "google_generation",
        "cap_group_total_usd": 50,
        "credential_env_file": "~/.config/ateles/generation.env",
    }
    policy = parse_cap_policy(binding_from_row(binding_row(SLOT, "google_image", constraints=constraints)))
    assert (policy.cap_usd, policy.group, policy.group_total_usd) == (50.0, "google_generation", 50.0)
