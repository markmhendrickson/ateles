"""Spend is recorded BEFORE the vendor is asked, and stays counted when the
outcome is unknown. These are the repros from the PR #1354 review round."""

import json
import os
import stat
from pathlib import Path

import pytest

from lib.capabilities import slots, spend
from lib.capabilities.errors import (
    ARTIFACT_UNSAVED,
    CAP_EXHAUSTED,
    CAP_UNREADABLE,
    EMPTY_RESULT,
    VENDOR_ERROR,
    GenerationRefused,
    VendorFailure,
)
from lib.capabilities.vendors import GoogleImageAdapter, HttpResponse, StubVendor, VeoAdapter

from .conftest import binding_row
from .test_vendors import KEY, VIDEO_URI, Fake, _done, ok

SLOT = slots.IMAGE_GENERATION
VIDEO = slots.VIDEO_GENERATION


def _ledger_rows(ledger, slot=SLOT):
    path = ledger.root / slot / f"{ledger.month()}.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


# --- the pending row precedes the vendor request -----------------------------


def test_pending_row_at_the_estimate_is_on_the_ledger_before_the_vendor_is_called(make_client, ledger):
    seen = []

    def spy():
        seen.append([(r["state"], r["cost_usd"]) for r in _ledger_rows(ledger)])

    stub = StubVendor(cost_usd=0.75, on_call=spy)
    client = make_client(
        [binding_row(SLOT, constraints={"model_tier": "m", "monthly_cap_usd": 10})],
        adapters={"stub": stub},
    )
    client.generate(SLOT, "p")
    assert seen == [[("pending", 0.75)]]


def test_a_crash_between_request_and_finalize_leaves_the_estimate_counted(make_client, ledger):
    class Crash(BaseException):
        pass

    stub = StubVendor(cost_usd=1.0, fail_with=Crash())
    client = make_client(
        [binding_row(SLOT, constraints={"model_tier": "m", "monthly_cap_usd": 1.5})],
        adapters={"stub": stub},
    )
    with pytest.raises(Crash):
        client.generate(SLOT, "p")
    # a fresh process sees the pending row and refuses the next call
    stub2 = StubVendor(cost_usd=1.0)
    client2 = make_client(
        [binding_row(SLOT, constraints={"model_tier": "m", "monthly_cap_usd": 1.5})],
        adapters={"stub": stub2},
    )
    with pytest.raises(GenerationRefused) as exc:
        client2.generate(SLOT, "p")
    assert exc.value.code == CAP_EXHAUSTED and stub2.calls == 0


def test_pending_write_failure_refuses_before_any_vendor_request(make_client, monkeypatch):
    stub = StubVendor()
    client = make_client(
        [binding_row(SLOT, constraints={"model_tier": "m", "monthly_cap_usd": 10})],
        adapters={"stub": stub},
    )

    def broken(self, row, billing_slot, month=None):
        raise spend.LedgerWriteError("OSError")

    monkeypatch.setattr(spend.LockedLedger, "record", broken)
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == CAP_UNREADABLE and "no vendor request was made" in exc.value.message
    assert stub.calls == 0


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), -1.0, True, "1.0"])
def test_a_non_finite_or_negative_estimate_is_refused_not_a_raw_exception(make_client, bad):
    class Bad(StubVendor):
        def estimate_cost(self, model, opts):
            return bad

    stub = Bad()
    client = make_client(
        [binding_row(SLOT, constraints={"model_tier": "m", "monthly_cap_usd": 10})],
        adapters={"stub": stub},
    )
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == CAP_UNREADABLE and stub.calls == 0


# --- (a) Veo accepted, then failed: the five-call repro ------------------------

VEO_MODEL = "veo-3.1-fast-generate-preview"
VEO_CONSTRAINTS = {"model_tier": VEO_MODEL, "monthly_cap_usd": 2.0, "credential_env_file": None}


def _veo_client(make_client, cred_file, responses_per_call, **kw):
    """Client with the REAL Veo adapter over a scripted transport."""
    path = cred_file("GEMINI_API_KEY", KEY.reveal())
    transports = []

    class Script:
        def __init__(self):
            self.starts = 0
            self.queue = []

        def __call__(self, method, url, *, headers, body, timeout):
            if url.endswith(":predictLongRunning"):
                self.starts += 1
                self.queue = list(responses_per_call())
                return self.queue.pop(0)
            return self.queue.pop(0)

    t = Script()
    adapter = VeoAdapter(t, sleep=lambda s: None, ffmpeg="/nonexistent", **kw)
    rows = [binding_row(VIDEO, "veo", constraints={**VEO_CONSTRAINTS, "credential_env_file": path},
                        credential_location="GEMINI_API_KEY", fallback="None by design")]
    return make_client(rows, adapters={"veo": adapter}), t


def _accepted_then(*rest):
    return lambda: [ok({"name": "operations/o"}), *rest]


@pytest.mark.parametrize(
    "label, script",
    [
        ("download 500", _accepted_then(_done(), HttpResponse(500, b"nope"))),
        ("3xx with a body", _accepted_then(_done(), HttpResponse(302, b"<html>moved</html>"))),
        ("host check refuses the URI", _accepted_then(_done("https://evil.example.com/x.mp4"))),
        ("missing sample", _accepted_then(ok({"done": True, "response": {}}))),
        ("poll 404", _accepted_then(HttpResponse(404, b"{}"))),
        ("operation error", _accepted_then(ok({"done": True, "error": {"message": "internal"}}))),
    ],
)
def test_veo_accepted_then_failed_five_call_repro(make_client, cred_file, ledger, label, script):
    """cap 2.00, each call estimated 0.96: without the fix all five calls reach
    the vendor and the ledger stays empty. With it, two are held and the third
    is CAP_EXHAUSTED."""
    client, t = _veo_client(make_client, cred_file, script)
    codes = []
    for _ in range(5):
        with pytest.raises(GenerationRefused) as exc:
            client.generate(VIDEO, "a desk")
        codes.append(exc.value.code)
    assert codes[0] == codes[1] and codes[0] in (VENDOR_ERROR, EMPTY_RESULT), label
    assert codes[2:] == [CAP_EXHAUSTED] * 3, label
    assert t.starts == 2                                   # the cap stopped calls 3 to 5
    rows = _ledger_rows(ledger, VIDEO)
    assert rows and {r["state"] for r in rows} == {"pending", "accepted_unfinished"}


def test_veo_poll_timeout_is_held_and_not_retryable(make_client, cred_file, ledger):
    ticks = iter(range(0, 100_000, 400))
    client, t = _veo_client(
        make_client, cred_file,
        _accepted_then(ok({"done": False}), ok({"done": False}), ok({"done": False})),
        clock=lambda: next(ticks), timeout_s=600,
    )
    with pytest.raises(GenerationRefused) as exc:
        client.generate(VIDEO, "a desk")
    err = exc.value
    assert err.code == VENDOR_ERROR and err.retryable is False
    assert "Retryable" not in err.hint and "Do not retry" in err.hint
    assert err.spent_usd == pytest.approx(0.96) and err.generation_id


def test_fallback_is_not_invoked_after_accepted_then_failed(make_client, cred_file, ledger):
    """The primary took the job and failed: the fallback must not pay again."""
    path = cred_file("GEMINI_API_KEY", KEY.reveal())
    down = Fake(ok({"name": "operations/o"}), HttpResponse(500, b"{}"))
    veo = VeoAdapter(down, sleep=lambda s: None, ffmpeg="/nonexistent")
    backup = StubVendor("backup", default_slot=SLOT, cost_usd=1.0)
    rows = [
        binding_row(VIDEO, "veo", constraints={**VEO_CONSTRAINTS, "monthly_cap_usd": 50, "credential_env_file": path},
                    credential_location="GEMINI_API_KEY", fallback="backup"),
        binding_row(SLOT, "backup", constraints={"model_tier": "m", "monthly_cap_usd": 50}),
    ]
    client = make_client(rows, adapters={"veo": veo, "backup": backup})
    with pytest.raises(GenerationRefused) as exc:
        client.generate(VIDEO, "a desk")
    assert exc.value.code == VENDOR_ERROR and exc.value.retryable is False
    assert backup.calls == 0 and not (ledger.root / SLOT).exists()


def test_fallback_still_runs_when_the_primary_definitively_did_not_bill(make_client, ledger, sink):
    primary = StubVendor("primary", fail_with=VendorFailure("bad key", http_status=401), cost_usd=1.0)
    backup = StubVendor("backup", default_slot=VIDEO, cost_usd=1.0)
    rows = [
        binding_row(SLOT, "primary", constraints={"model_tier": "m", "monthly_cap_usd": 50}, fallback="backup"),
        binding_row(VIDEO, "backup", constraints={"model_tier": "m2", "monthly_cap_usd": 50}),
    ]
    client = make_client(rows, adapters={"primary": primary, "backup": backup})
    result = client.generate(SLOT, "p")
    assert result.vendor == "backup" and result.fallback_used
    assert [r["state"] for r in _ledger_rows(ledger, SLOT)] == ["pending", "voided"]   # primary voided
    assert [r["state"] for r in _ledger_rows(ledger, VIDEO)] == ["pending", "completed"]
    (record, _), = sink.stored
    assert record["vendor"] == "backup" and record["requested_vendor"] == "primary"
    assert record["fallback_used"] is True and record["billing_slot"] == VIDEO


# --- (b) the artifact cannot be written after a paid call -----------------------


def test_unwritable_artifact_root_records_the_spend_five_call_repro(make_client, ledger, tmp_path):
    root = tmp_path / "ro-artifacts"
    root.mkdir()
    root.chmod(0o500)
    try:
        stub = StubVendor(cost_usd=1.0)
        client = make_client(
            [binding_row(SLOT, constraints={"model_tier": "m", "monthly_cap_usd": 2.0})],
            adapters={"stub": stub},
            artifact_root=root,
        )
        codes = []
        for _ in range(5):
            with pytest.raises(GenerationRefused) as exc:
                client.generate(SLOT, "p")
            codes.append(exc.value.code)
    finally:
        root.chmod(0o700)
    assert codes == [ARTIFACT_UNSAVED, ARTIFACT_UNSAVED, CAP_EXHAUSTED, CAP_EXHAUSTED, CAP_EXHAUSTED]
    assert stub.calls == 2
    states = [r["state"] for r in _ledger_rows(ledger)]
    assert states.count("completed") == 2                   # spend recorded despite the lost bytes


def test_artifact_unsaved_says_do_not_retry_and_names_the_generation(make_client, tmp_path):
    root = tmp_path / "ro"
    root.mkdir()
    root.chmod(0o500)
    try:
        client = make_client(
            [binding_row(SLOT, constraints={"model_tier": "m", "monthly_cap_usd": 5})],
            adapters={"stub": StubVendor(cost_usd=1.0)},
            artifact_root=root,
        )
        with pytest.raises(GenerationRefused) as exc:
            client.generate(SLOT, "p")
    finally:
        root.chmod(0o700)
    err = exc.value
    assert err.code == ARTIFACT_UNSAVED and err.retryable is False
    assert err.generation_id in err.message and "Do NOT retry" in err.hint
    assert err.spent_usd == 1.0


# --- ledger integrity ------------------------------------------------------------


def _one_call_client(make_client):
    return make_client(
        [binding_row(SLOT, constraints={"model_tier": "m", "monthly_cap_usd": 5})],
        adapters={"stub": StubVendor(cost_usd=1.0)},
    )


def test_a_deleted_ledger_file_does_not_read_as_zero_spend(make_client, ledger):
    client = _one_call_client(make_client)
    client.generate(SLOT, "p")
    (ledger.root / SLOT / f"{ledger.month()}.jsonl").unlink()
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == CAP_UNREADABLE


def test_ledger_files_without_a_manifest_refuse(make_client, ledger):
    client = _one_call_client(make_client)
    client.generate(SLOT, "p")
    (ledger.root / spend.MANIFEST_NAME).unlink()
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == CAP_UNREADABLE


def test_a_fresh_install_with_no_ledger_is_zero_spend(make_client):
    assert _one_call_client(make_client).generate(SLOT, "p").remaining_cap_usd == 4.0


def test_a_corrupt_manifest_refuses(make_client, ledger):
    client = _one_call_client(make_client)
    client.generate(SLOT, "p")
    (ledger.root / spend.MANIFEST_NAME).write_text("{not json")
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == CAP_UNREADABLE


def test_a_symlinked_ledger_file_is_refused(make_client, ledger, tmp_path):
    client = _one_call_client(make_client)
    client.generate(SLOT, "p")
    real = ledger.root / SLOT / f"{ledger.month()}.jsonl"
    elsewhere = tmp_path / "elsewhere.jsonl"
    elsewhere.write_text(real.read_text())
    elsewhere.chmod(0o600)
    real.unlink()
    real.symlink_to(elsewhere)
    with pytest.raises(GenerationRefused) as exc:
        client.generate(SLOT, "p")
    assert exc.value.code == CAP_UNREADABLE


def test_a_symlinked_ledger_root_is_refused(make_client, ledger, tmp_path):
    target = tmp_path / "real-root"
    target.mkdir()
    ledger.root = tmp_path / "link-root"
    ledger.root.symlink_to(target)
    with pytest.raises(GenerationRefused) as exc:
        _one_call_client(make_client).generate(SLOT, "p")
    assert exc.value.code == CAP_UNREADABLE


def test_the_default_ledger_and_artifact_locations_are_state_not_cache(monkeypatch, tmp_path):
    from lib.capabilities import generation

    monkeypatch.delenv(spend.SPEND_PATH_ENV, raising=False)
    monkeypatch.delenv(generation.ARTIFACT_PATH_ENV, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert spend.default_root() == tmp_path / "state" / "ateles" / "generation_spend"
    assert generation._default_artifact_root() == tmp_path / "state" / "ateles" / "generation_artifacts"
    monkeypatch.delenv("XDG_STATE_HOME")
    assert ".cache" not in str(spend.default_root()) and ".local/state" in str(spend.default_root())


def test_a_short_write_is_completed(monkeypatch, tmp_path):
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real(fd, bytes(data)[:3]))
    path = tmp_path / "f"
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    spend.write_all(fd, b"0123456789")
    os.close(fd)
    monkeypatch.undo()
    assert path.read_bytes() == b"0123456789"


def test_ledger_and_artifact_files_are_owner_only(make_client, ledger):
    client = _one_call_client(make_client)
    r = client.generate(SLOT, "p")
    for p in (ledger.root / SLOT / f"{ledger.month()}.jsonl", ledger.root / spend.MANIFEST_NAME, Path(r.artifact_ref)):
        assert stat.S_IMODE(p.stat().st_mode) & 0o077 == 0, p


def test_ledger_reads_do_not_follow_symlinks_on_their_own(tmp_path):
    """Direct unit check: the read helper refuses a symlink even when the
    target is a perfectly good owner-only file."""
    target = tmp_path / "t.jsonl"
    target.write_text("{}\n")
    target.chmod(0o600)
    link = tmp_path / "l.jsonl"
    link.symlink_to(target)
    with pytest.raises(OSError):
        spend._read_nofollow(link)
    assert spend._read_nofollow(target) == "{}\n"
