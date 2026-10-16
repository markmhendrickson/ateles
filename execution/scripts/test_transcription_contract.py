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
    # declared=None mirrors a schema that declares every field Tyto writes;
    # a set limits the snapshot to those fields, the way Neotoma routes an
    # undeclared field to raw_fragments instead of the snapshot.
    state = {"drop": None, "linked": True, "mismatch": False, "declared": None,
             "schema_lookup": True, "source_hash": None, "prior": None}
    monkeypatch.setattr(ta, "_neotoma_cli_available", lambda: True)
    monkeypatch.setattr(ta, "_neotoma_auth_preflight", lambda: (True, "ok"))
    monkeypatch.setattr(ta, "_neotoma_prod_cli_argv", lambda args: ["neotoma", *args])
    def run(cmd, **kwargs):
        # attach_audio_file=True (the default) now composes via `ingest
        # --entities <path> --source-file <audio>` (ateles#1083 — `store
        # --file-path` sent a path hosted Neotoma cannot resolve on its own
        # filesystem); attach_audio_file=False still uses plain `store
        # --file <path>`. Read the entities payload from whichever flag the
        # call under test actually used.
        entities_flag = "--entities" if "--entities" in cmd else "--file"
        entity = json.loads(Path(cmd[cmd.index(entities_flag) + 1]).read_text())[0]
        state["entity"] = entity
        state["cmd"] = cmd
        return SimpleNamespace(returncode=1 if state["mismatch"] else 0,
            stdout="" if state["mismatch"] else json.dumps({"structured": {"entities": [{"entity_id": "ent_fixture"}]}, "unstructured": {"source_id": "src_fixture"}}),
            stderr="ERR_IDEMPOTENCY_MISMATCH ent_000000000000000000000001" if state["mismatch"] else "")
    monkeypatch.setattr(ta.subprocess, "run", run)
    def read(args):
        if args[0] == "schemas":
            if not state["schema_lookup"]:
                return None
            fields = state["declared"] if state["declared"] is not None else set(state["entity"])
            return {"schema": {"entity_type": "transcription",
                               "fields": {f: {"type": "string"} for f in fields}}}
        if args[:2] == ["entities", "search"]:
            prior = state["prior"]
            return {"entities": [{"id": prior["id"]}] if prior else [], "total": int(bool(prior))}
        if args[:2] == ["entities", "get"] and state["prior"] and args[2] == state["prior"]["id"]:
            return {"entity": {"id": state["prior"]["id"], "snapshot": state["prior"]["snapshot"]}}
        if args[0] == "entities":
            snapshot = {k: v for k, v in state["entity"].items() if k != state["drop"]}
            if state["declared"] is not None:
                snapshot = {k: v for k, v in snapshot.items() if k in state["declared"]}
            return {"entity": {"id": "ent_fixture", "snapshot": snapshot}}
        if args[0] == "observations":
            return {"observations": [{"entity_id": "ent_fixture", "source_id": "src_fixture"}] if state["linked"] else []}
        content_hash = state["source_hash"] or ta._audio_content_hash(audio)
        if args[:2] == ["sources", "list"]:
            return {"sources": [{"id": "src_fixture", "content_hash": content_hash}]}
        if args[0] == "sources":
            return {"source": {"id": "src_fixture", "content_hash": content_hash}}
        raise AssertionError(args)
    monkeypatch.setattr(ta, "_neotoma_cli_json", read)
    def save(**kwargs):
        return ta.save_transcription(audio, {"transcription_text": "Synthetic transcript.", "transcription_engine": "local_whisper_cpp"}, extra_entity_fields={"capture_method": "voice_memo", "consent_basis": "unknown"}, **kwargs)
    state["audio"] = audio
    return state, save


@pytest.mark.parametrize("field", ["audio_content_sha256", "original_source_file", "capture_method", "transcription_engine", "consent_basis", "transcription_text", "file_size_bytes"])
def test_dropped_mandatory_field_is_partial_ingestion(storage, field, capsys):
    state, save = storage
    state["drop"] = field
    with pytest.raises(RuntimeError, match="incomplete"):
        save()
    assert "NEOTOMA_TRANSCRIPTION_ENTITY_ID=" not in capsys.readouterr().out


def test_audio_source_without_an_observation_link_is_a_landed_store(storage, capsys):
    """`ingest --source-file` stores the audio as its own source and ties no
    observation from it to the entity — the entity's observations cite the
    JSON payload source. Live 2026-09-25: a landed memo's audio source held the
    right bytes and had zero observations. A check that demands the link can
    never pass on this write path, so the store must be judged on the source
    it actually wrote."""
    state, save = storage
    state["linked"] = False
    assert save()["entity_id"] == "ent_fixture"
    assert "NEOTOMA_TRANSCRIPTION_ENTITY_ID=ent_fixture" in capsys.readouterr().out


def test_audio_source_holding_other_bytes_is_partial_ingestion(storage):
    state, save = storage
    state["source_hash"] = "0" * 64
    with pytest.raises(RuntimeError, match="audio source hash mismatch"):
        save()


# ── Read-back verifies only what the schema declares (2026-09-25) ────────────
#
# transcription 1.3.0 declared none of the five provenance fields, so Neotoma
# kept them out of the snapshot and every memo's read-back failed although the
# store had landed; the daemon then re-stored it every five minutes.

_SCHEMA_1_3_0 = {
    "transcription_id", "audio_file_path", "audio_file_name", "source_directory",
    "language", "transcription_date", "audio_duration_seconds", "file_size_bytes",
    "transcription_text", "word_count", "recorded_at",
}


def test_undeclared_provenance_fields_do_not_fail_a_landed_store(storage, capsys):
    state, save = storage
    state["declared"] = set(_SCHEMA_1_3_0)
    assert save()["entity_id"] == "ent_fixture"
    captured = capsys.readouterr()
    assert "NEOTOMA_TRANSCRIPTION_ENTITY_ID=ent_fixture" in captured.out
    # The gap is reported, not hidden.
    for field in ("audio_content_sha256", "original_source_file", "capture_method",
                  "transcription_engine", "consent_basis"):
        assert field in captured.err


def test_declared_provenance_field_that_is_dropped_still_fails(storage):
    state, save = storage
    state["declared"] = set(_SCHEMA_1_3_0) | {"capture_method"}
    state["drop"] = "capture_method"
    with pytest.raises(RuntimeError, match="read-back mismatch for capture_method"):
        save()


def test_transcript_is_verified_even_when_the_schema_omits_it(storage):
    state, save = storage
    state["declared"] = set(_SCHEMA_1_3_0) - {"transcription_text"}
    with pytest.raises(RuntimeError, match="transcription_text"):
        save()


def test_failed_schema_lookup_verifies_every_field(storage):
    """No schema answer must not quietly narrow the check (fail closed)."""
    state, save = storage
    state["schema_lookup"] = False
    state["drop"] = "consent_basis"
    with pytest.raises(RuntimeError, match="consent_basis"):
        save()


# ── Audio too large for the hosted transport (2026-09-25) ────────────────────
#
# Neotoma's CLI refuses a remote upload over ~7.5 MB before any HTTP call, so a
# two-minute .qta memo could never be attached and was retried every five
# minutes. It is now stored without the audio, with the reason on the record.


def test_oversize_audio_is_stored_without_attachment_and_says_why(storage, monkeypatch, capsys):
    state, save = storage
    monkeypatch.setenv("NEOTOMA_PROD_BASE_URL", "https://neotoma.example.invalid")
    monkeypatch.setenv("NEOTOMA_REMOTE_UPLOAD_MAX_BYTES", "4")
    assert save()["entity_id"] == "ent_fixture"
    assert "ingest" not in state["cmd"] and "--source-file" not in state["cmd"]
    assert state["cmd"][:2] == ["neotoma", "store"]
    assert state["entity"]["audio_attachment_status"] == "omitted_exceeds_remote_upload_limit"
    assert "remote upload limit is 4 bytes" in state["entity"]["audio_attachment_note"]
    assert "without the audio attachment" in capsys.readouterr().err


def test_oversize_attachment_status_is_read_back(storage, monkeypatch):
    state, save = storage
    monkeypatch.setenv("NEOTOMA_PROD_BASE_URL", "https://neotoma.example.invalid")
    monkeypatch.setenv("NEOTOMA_REMOTE_UPLOAD_MAX_BYTES", "4")
    state["drop"] = "audio_attachment_status"
    with pytest.raises(RuntimeError, match="audio_attachment_status"):
        save()


def test_default_remote_limit_matches_the_neotoma_cli():
    assert ta._DEFAULT_REMOTE_UPLOAD_MAX_BYTES == 7549747


def test_audio_under_the_remote_limit_is_still_attached(storage, monkeypatch):
    state, save = storage
    monkeypatch.setenv("NEOTOMA_PROD_BASE_URL", "https://neotoma.example.invalid")
    save()
    assert "--source-file" in state["cmd"]
    assert "audio_attachment_status" not in state["entity"]


def test_localhost_target_attaches_regardless_of_remote_limit(storage, monkeypatch):
    state, save = storage
    monkeypatch.setenv("NEOTOMA_PROD_BASE_URL", "http://127.0.0.1:3080")
    monkeypatch.setenv("NEOTOMA_REMOTE_UPLOAD_MAX_BYTES", "4")
    save()
    assert "--source-file" in state["cmd"]
    assert "audio_attachment_status" not in state["entity"]


# ── ERR_IDEMPOTENCY_MISMATCH means the first write landed (2026-09-25) ───────
#
# The payload carries today's date, so a retry on a later day re-sends the
# same content key with different bytes and is rejected forever. The prior
# write is the durable record: resolve it and read it back.


def _prior(state, **overrides):
    snapshot = {
        "audio_file_path": str(state["audio"].resolve()),
        "file_size_bytes": state["audio"].stat().st_size,
        "transcription_text": "Transcript stored by an earlier attempt.",
    }
    snapshot.update(overrides)
    state["prior"] = {"id": "ent_prior", "snapshot": snapshot}


def test_idempotency_mismatch_resolves_and_verifies_the_prior_write(storage, capsys):
    state, save = storage
    state["mismatch"] = True
    _prior(state)
    assert save()["entity_id"] == "ent_prior"
    captured = capsys.readouterr()
    assert "NEOTOMA_TRANSCRIPTION_ENTITY_ID=ent_prior" in captured.out
    assert "Recovered prior transcription write ent_prior" in captured.err


def test_idempotency_mismatch_with_no_prior_entity_stays_a_named_failure(storage):
    state, save = storage
    state["mismatch"] = True
    with pytest.raises(RuntimeError, match="ERR_IDEMPOTENCY_MISMATCH"):
        save()


def test_idempotency_mismatch_prior_write_for_other_bytes_is_not_success(storage):
    state, save = storage
    state["mismatch"] = True
    _prior(state, file_size_bytes=1)
    with pytest.raises(RuntimeError, match="file_size_bytes"):
        save()


def test_idempotency_mismatch_prior_write_without_audio_is_not_success(storage):
    state, save = storage
    state["mismatch"] = True
    state["source_hash"] = "0" * 64
    _prior(state)
    with pytest.raises(RuntimeError, match="no stored audio source"):
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
    # Attaching audio composes via `ingest --source-file`, not a bare `store
    # --file-path` (ateles#1083); `ingest` has no --interpretation-source-ref.
    assert "ingest" in state["cmd"]
    assert "--source-file" in state["cmd"]
    assert "--file-path" not in state["cmd"]
    assert state["entity"]["file_size_bytes"] == len(b"RIFF-synthetic-audio")


def test_repeat_verified_store_remains_success(storage):
    _, save = storage
    assert save()["entity_id"] == save()["entity_id"]


def test_retry_payload_is_byte_stable_under_fixed_idempotency_key(storage):
    """Changing data_source under a content-keyed idempotency key makes retries reject."""
    state, save = storage
    save()
    first = dict(state["entity"])
    first_cmd = list(state["cmd"])
    save()
    assert state["cmd"][state["cmd"].index("--idempotency-key") + 1] == (
        first_cmd[first_cmd.index("--idempotency-key") + 1]
    )
    assert state["entity"] == first
    assert state["entity"]["data_source"] == first["data_source"]
    assert "transcribe_audio.py store" in state["entity"]["data_source"]


def test_metadata_only_is_explicit_and_still_verified(storage):
    state, save = storage
    assert save(attach_audio_file=False)["entity_id"] == "ent_fixture"
    assert "--file-path" not in state["cmd"]
    state["drop"] = "transcription_text"
    with pytest.raises(RuntimeError, match="incomplete"):
        save(attach_audio_file=False)


def test_metadata_only_missing_file_still_stores(tmp_path, monkeypatch):
    """Explicit metadata-only imports must not require the audio bytes on disk."""
    missing = tmp_path / "absent.wav"
    state = {}
    monkeypatch.setattr(ta, "_neotoma_cli_available", lambda: True)
    monkeypatch.setattr(ta, "_neotoma_auth_preflight", lambda: (True, "ok"))
    monkeypatch.setattr(ta, "_neotoma_prod_cli_argv", lambda args: ["neotoma", *args])

    def run(cmd, **kwargs):
        entity = json.loads(Path(cmd[cmd.index("--file") + 1]).read_text())[0]
        state["entity"] = entity
        state["cmd"] = cmd
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"structured": {"entities": [{"entity_id": "ent_meta"}]}}
            ),
            stderr="",
        )

    monkeypatch.setattr(ta.subprocess, "run", run)
    monkeypatch.setattr(
        ta,
        "_neotoma_cli_json",
        lambda args: {
            "entity": {"id": "ent_meta", "snapshot": state["entity"]}
        },
    )
    result = ta.save_transcription(
        missing,
        {
            "transcription_text": "Recovered transcript.",
            "transcription_engine": "local_whisper_cpp",
            "file_size_bytes": 2048,
            "audio_content_sha256": "a" * 64,
        },
        attach_audio_file=False,
        extra_entity_fields={"capture_method": "voice_memo", "consent_basis": "unknown"},
    )
    assert result["entity_id"] == "ent_meta"
    assert "--file-path" not in state["cmd"]
    assert state["entity"]["file_size_bytes"] == 2048
    assert state["entity"]["audio_content_sha256"] == "a" * 64


def test_file_backed_missing_audio_fails_closed(tmp_path, monkeypatch):
    missing = tmp_path / "absent.wav"
    monkeypatch.setattr(ta, "_neotoma_cli_available", lambda: True)
    monkeypatch.setattr(ta, "_neotoma_auth_preflight", lambda: (True, "ok"))
    monkeypatch.setattr(ta, "_neotoma_prod_cli_argv", lambda args: ["neotoma", *args])
    monkeypatch.setattr(
        ta.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("store must not run")),
    )
    with pytest.raises(RuntimeError, match="incomplete"):
        ta.save_transcription(
            missing,
            {
                "transcription_text": "Should not store.",
                "transcription_engine": "local_whisper_cpp",
            },
            attach_audio_file=True,
            extra_entity_fields={"capture_method": "voice_memo", "consent_basis": "unknown"},
        )


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
