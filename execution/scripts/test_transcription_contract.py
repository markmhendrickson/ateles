"""Storage contract regressions using synthetic recordings and read-back data."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import transcribe_audio as ta


@pytest.fixture
def storage(tmp_path, monkeypatch):
    audio = tmp_path / "synthetic.wav"
    audio.write_bytes(b"RIFF-synthetic-audio")
    state = {"drop": None, "linked": True, "mismatch": False}
    monkeypatch.setattr(ta, "_neotoma_cli_available", lambda: True)
    monkeypatch.setattr(ta, "_neotoma_auth_preflight", lambda: (True, "ok"))
    monkeypatch.setattr(ta, "_neotoma_prod_cli_argv", lambda args: ["neotoma", *args])
    def run(cmd, **kwargs):
        entity = json.loads(Path(cmd[cmd.index("--file") + 1]).read_text())[0]
        state["entity"] = entity
        state["cmd"] = cmd
        return SimpleNamespace(returncode=1 if state["mismatch"] else 0,
            stdout="" if state["mismatch"] else json.dumps({"structured": {"entities": [{"entity_id": "ent_fixture"}]}, "unstructured": {"source_id": "src_fixture"}}),
            stderr="ERR_IDEMPOTENCY_MISMATCH ent_000000000000000000000001" if state["mismatch"] else "")
    monkeypatch.setattr(ta.subprocess, "run", run)
    def read(args):
        if args[0] == "entities":
            snapshot = {k: v for k, v in state["entity"].items() if k != state["drop"]}
            return {"entity": {"id": "ent_fixture", "snapshot": snapshot}}
        if args[0] == "observations":
            return {"observations": [{"entity_id": "ent_fixture", "source_id": "src_fixture"}] if state["linked"] else []}
        if args[0] == "sources":
            return {"source": {"id": "src_fixture", "content_hash": ta._audio_content_hash(audio)}}
        raise AssertionError(args)
    monkeypatch.setattr(ta, "_neotoma_cli_json", read)
    def save(**kwargs):
        return ta.save_transcription(audio, {"transcription_text": "Synthetic transcript.", "transcription_engine": "local_whisper_cpp"}, extra_entity_fields={"capture_method": "voice_memo", "consent_basis": "unknown"}, **kwargs)
    return state, save


@pytest.mark.parametrize("field", ["audio_content_sha256", "original_source_file", "capture_method", "transcription_engine", "consent_basis", "transcription_text", "file_size_bytes"])
def test_dropped_mandatory_field_is_partial_ingestion(storage, field, capsys):
    state, save = storage
    state["drop"] = field
    with pytest.raises(RuntimeError, match="incomplete"):
        save()
    assert "NEOTOMA_TRANSCRIPTION_ENTITY_ID=" not in capsys.readouterr().out


def test_missing_source_link_is_partial_ingestion(storage):
    state, save = storage
    state["linked"] = False
    with pytest.raises(RuntimeError, match="incomplete"):
        save()


def test_idempotency_mismatch_is_not_success(storage):
    state, save = storage
    state["mismatch"] = True
    with pytest.raises(RuntimeError, match="store failed"):
        save()


def test_combined_store_readback_success(storage, capsys):
    state, save = storage
    assert save()["entity_id"] == "ent_fixture"
    assert "NEOTOMA_TRANSCRIPTION_ENTITY_ID=ent_fixture" in capsys.readouterr().out
    assert "--interpretation-source-ref" in state["cmd"]
    assert state["entity"]["file_size_bytes"] == len(b"RIFF-synthetic-audio")


def test_repeat_verified_store_remains_success(storage):
    _, save = storage
    assert save()["entity_id"] == save()["entity_id"]


def test_metadata_only_is_explicit_and_still_verified(storage):
    state, save = storage
    assert save(attach_audio_file=False)["entity_id"] == "ent_fixture"
    assert "--file-path" not in state["cmd"]
    state["drop"] = "transcription_text"
    with pytest.raises(RuntimeError, match="incomplete"):
        save(attach_audio_file=False)


def test_attachment_limit_cannot_report_complete(storage, monkeypatch):
    _, save = storage
    monkeypatch.setenv("NEOTOMA_MAX_TRANSCRIPTION_WAV_BYTES", "1")
    with pytest.raises(RuntimeError, match="incomplete"):
        save()


def test_source_read_failure_cannot_report_complete(storage, monkeypatch):
    _, save = storage
    read = ta._neotoma_cli_json
    monkeypatch.setattr(ta, "_neotoma_cli_json", lambda args: {"error_code": "UNAUTHORIZED"} if args[0] == "sources" else read(args))
    with pytest.raises(RuntimeError, match="incomplete"):
        save()
