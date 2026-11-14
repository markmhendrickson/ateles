"""Post-payment task updates must land in Neotoma (and be read back).

Regression: the handlers shelled out to `neotoma entities update`, a command the
installed CLI does not have, so every update failed with a warning and a settled
payment's task stayed awaiting_approval with its due date unrolled.
"""

from __future__ import annotations

import logging
import subprocess

import pytest
from fake_neotoma import FakeNeotoma
from handlers import btc_transfer, task_writes, wise_transfer
from handlers.payment_profile import PaymentProfile

TASK = "ent_task_1"
NEXT_DUE = "2026-12-01"


def _profile(**kw) -> PaymentProfile:
    return PaymentProfile(
        prefix="THERAPY",
        label="Therapy",
        calendar_keywords=["therapy"],
        payment_type="wise",
        amount_eur=60,
        neotoma_task_id=TASK,
        **kw,
    )


@pytest.fixture
def no_subprocess(monkeypatch):
    def boom(*a, **k):
        raise AssertionError(f"task update must not shell out: {a}")

    monkeypatch.setattr(subprocess, "run", boom)


def _wire(monkeypatch, fake: FakeNeotoma, module) -> None:
    monkeypatch.setattr(task_writes, "make_client", fake.connection)
    monkeypatch.setattr(module, "_find_next_event_due_date", lambda _p: NEXT_DUE)


def test_wise_update_task_writes_notes_and_due_date(monkeypatch, no_subprocess) -> None:
    fake = FakeNeotoma({TASK: {"status": "awaiting_approval", "notes": "old note"}})
    _wire(monkeypatch, fake, wise_transfer)

    wise_transfer._update_task(_profile(), {"status": "sent", "transfer_id": "tr_9"})

    notes = fake.entities[TASK]["notes"]
    assert notes.startswith("old note\n") and "transfer_id=tr_9" in notes
    assert fake.entities[TASK]["due_date"] == NEXT_DUE
    # therapy/yoga tasks are never marked completed: status untouched
    assert fake.entities[TASK]["status"] == "awaiting_approval"
    assert all(p["entity_type"] == "task" for p in fake.posts)
    assert {p["field"] for p in fake.posts} == {"notes", "due_date"}


def test_btc_update_task_writes_notes_and_due_date(monkeypatch, no_subprocess) -> None:
    fake = FakeNeotoma({TASK: {"notes": "prior"}})
    _wire(monkeypatch, fake, btc_transfer)

    btc_transfer._update_task(_profile(), "abc123")

    assert fake.entities[TASK]["notes"].startswith("prior\n")
    assert "txid=abc123" in fake.entities[TASK]["notes"]
    assert fake.entities[TASK]["due_date"] == NEXT_DUE
    assert "status" not in fake.entities[TASK]


def test_notes_are_merged_not_overwritten(monkeypatch) -> None:
    fake = FakeNeotoma({TASK: {"notes": "keep me"}})
    client, base = fake.connection()
    assert task_writes.append_note(client, base, TASK, "new line")
    assert fake.entities[TASK]["notes"] == "keep me\nnew line"


def test_failed_notes_read_does_not_overwrite(monkeypatch, caplog) -> None:
    fake = FakeNeotoma({TASK: {"notes": "keep me"}}, fail_get=True)
    client, base = fake.connection()
    with caplog.at_level(logging.ERROR):
        assert not task_writes.append_note(client, base, TASK, "new line")
    assert fake.posts == []
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_write_failure_logs_error_not_warning(monkeypatch, caplog) -> None:
    fake = FakeNeotoma({TASK: {}}, fail_correct=True)
    _wire(monkeypatch, fake, wise_transfer)
    with caplog.at_level(logging.WARNING):
        wise_transfer._update_task(_profile(), {"status": "sent", "transfer_id": "t"})
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("notes NOT updated" in r.getMessage() for r in errors)
    assert any("due_date NOT updated" in r.getMessage() for r in errors)
    assert not [r for r in caplog.records if r.levelno == logging.WARNING
                and "update failed" in r.getMessage()]


def test_readback_mismatch_is_an_error(monkeypatch, caplog) -> None:
    fake = FakeNeotoma({TASK: {}}, drop_writes=True)
    _wire(monkeypatch, fake, btc_transfer)
    with caplog.at_level(logging.ERROR):
        btc_transfer._update_task(_profile(), "tx1")
    assert any("readback MISMATCH" in r.getMessage() for r in caplog.records)
    assert any("due_date NOT updated" in r.getMessage() for r in caplog.records)


def test_missing_credentials_log_error(monkeypatch, caplog) -> None:
    monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    with caplog.at_level(logging.ERROR):
        assert task_writes.make_client() is None
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_make_client_never_logs_token(monkeypatch, caplog) -> None:
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.test/")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "sekrit-token-value")
    with caplog.at_level(logging.DEBUG):
        client, base = task_writes.make_client()
    client.close()
    assert base == "https://neotoma.test"
    assert "sekrit-token-value" not in caplog.text


# ── Round 2: snapshot shapes, empty reads, keys, dates, guards ───────────────


@pytest.mark.parametrize("shape", ["plain", "entity", "nested"])
def test_notes_merge_accepts_every_snapshot_shape(shape) -> None:
    fake = FakeNeotoma({TASK: {"notes": "OPERATOR NOTE"}}, shape=shape)
    client, base = fake.connection()
    assert task_writes.append_note(client, base, TASK, "new line")
    assert fake.entities[TASK]["notes"] == "OPERATOR NOTE\nnew line"


def test_unrecognised_snapshot_shape_writes_nothing(caplog) -> None:
    fake = FakeNeotoma({TASK: {"notes": "OPERATOR NOTE"}}, shape="unknown")
    client, base = fake.connection()
    with caplog.at_level(logging.ERROR):
        assert not task_writes.append_note(client, base, TASK, "new line")
    assert fake.posts == []
    assert fake.entities[TASK]["notes"] == "OPERATOR NOTE"
    assert any("unrecognised snapshot" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("shape", ["plain", "entity", "nested"])
def test_missing_task_empty_snapshot_writes_nothing(shape, caplog) -> None:
    fake = FakeNeotoma({}, shape=shape)
    client, base = fake.connection()
    with caplog.at_level(logging.ERROR):
        assert not task_writes.append_note(client, base, "ent_absent", "new line")
    assert fake.posts == []


def test_snapshot_without_identity_writes_nothing() -> None:
    import httpx

    posts = []

    def handler(req):
        if req.method == "POST":
            posts.append(req)
        return httpx.Response(200, json={"snapshot": {"notes": "OPERATOR NOTE"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert not task_writes.append_note(client, "https://neotoma.test", TASK, "n")
    assert posts == []


def test_non_text_notes_not_overwritten() -> None:
    fake = FakeNeotoma({TASK: {"notes": ["a", "b"]}})
    client, base = fake.connection()
    assert not task_writes.append_note(client, base, TASK, "new")
    assert fake.posts == []


def test_idempotency_key_depends_on_value() -> None:
    a = task_writes._idem(TASK, "due_date", "2026-12-01")
    b = task_writes._idem(TASK, "due_date", "2027-01-01")
    assert a != b
    assert a == task_writes._idem(TASK, "due_date", "2026-12-01")
    assert a != task_writes._idem(TASK, "notes", "2026-12-01")


def test_due_date_readback_compares_as_dates(monkeypatch, caplog) -> None:
    fake = FakeNeotoma({TASK: {"notes": "n"}}, normalise_due=True)
    client, base = fake.connection()
    with caplog.at_level(logging.ERROR):
        assert task_writes.write_field(client, base, TASK, "task", "due_date", NEXT_DUE)
    assert not caplog.records
    # a different date is still a mismatch
    fake.entities[TASK]["due_date"] = "2030-01-01"
    fake.drop_writes = True
    with caplog.at_level(logging.ERROR):
        assert not task_writes.write_field(client, base, TASK, "task", "due_date", NEXT_DUE)


def test_failed_notes_write_still_attempts_due_date(monkeypatch) -> None:
    fake = FakeNeotoma({TASK: {"notes": "x"}}, fail_get=True)
    _wire(monkeypatch, fake, wise_transfer)
    wise_transfer._update_task(_profile(), {"status": "sent", "transfer_id": "t"})
    assert [p["field"] for p in fake.posts] == ["due_date"]


def test_failure_logs_say_payment_settled_and_note_text(monkeypatch, caplog) -> None:
    fake = FakeNeotoma({TASK: {}}, fail_correct=True)
    _wire(monkeypatch, fake, btc_transfer)
    with caplog.at_level(logging.ERROR):
        btc_transfer._update_task(_profile(), "tx77")
    text = caplog.text
    assert "payment settled" in text
    assert "txid=tx77" in text  # lost note text is recoverable from the log


def test_mismatch_log_has_length_only_for_long_values(monkeypatch, caplog) -> None:
    fake = FakeNeotoma({TASK: {}}, drop_writes=True)
    client, base = fake.connection()
    long_note = "long-sensitive-looking-note-" + "x" * 100
    with caplog.at_level(logging.ERROR):
        assert not task_writes.write_field(client, base, TASK, "task", "notes", long_note)
    assert "long-sensitive-looking-note" not in caplog.text
    assert "len=" in caplog.text


@pytest.mark.parametrize("module,args", [
    (wise_transfer, ({"status": "sent", "transfer_id": "t"},)),
    (btc_transfer, ("tx1",)),
])
def test_update_task_never_raises_after_payment(monkeypatch, caplog, module, args) -> None:
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(task_writes, "make_client", boom)
    monkeypatch.setattr(module, "_find_task_id", boom, raising=False)
    with caplog.at_level(logging.ERROR):
        module._update_task(_profile(), *args)  # must not raise
    assert any("payment itself settled" in r.getMessage() for r in caplog.records)
