"""Voice Memos imported by both Tyto and Piculet are transcribed and stored once.

2026-11-25 race: Tyto began a memo, Piculet began the same memo 19 s later
from its own staged copy and stored first, then Tyto's exact read-back failed
(ERR_PERSISTED_RESULT_MISMATCH) and the memo was journalled as permanently
failed. Piculet's row also carried capture_method=unknown.

Only inference and Neotoma transport are synthetic (the shared ``storage``
fixture); the claim, lookup, verification and receipt are real.
"""
import fcntl
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import transcribe_audio as ta
from test_transcription_results import complete_result
from test_transcription_contract import storage  # noqa: F401 — fixture

sys.path.insert(0, str(Path(__file__).resolve().parent))
import import_audio_from_desktop as iad  # noqa: E402


@pytest.fixture
def memo_main(storage, tmp_path, monkeypatch):  # noqa: F811
    state, _save = storage
    audio = state["audio"]
    claims = tmp_path / "claims"
    monkeypatch.setenv("ATELES_TRANSCRIPTION_CLAIM_DIR", str(claims))
    monkeypatch.setenv("TRANSCRIBE_DISABLE_NEOTOMA_VOCAB", "1")
    monkeypatch.setenv("NEOTOMA_PROD_BASE_URL", "https://synthetic.invalid")
    monkeypatch.setenv("NEOTOMA_REMOTE_UPLOAD_MAX_BYTES", "100000")
    monkeypatch.setattr(ta, "_merge_transcription_relate_targets", lambda *a: ([], None))
    inference = MagicMock(return_value=complete_result(audio, text="Synthetic memo."))
    monkeypatch.setattr(ta, "transcribe_audio_file", inference)

    def run(*extra):
        receipt = tmp_path / "receipt.json"
        monkeypatch.setattr(sys, "argv", [
            "transcribe_audio.py", str(audio), "--capture-method", "voice_memo",
            "--require-audio-attachment", "--receipt-file", str(receipt), *extra])
        try:
            ta.main()
            code = 0
        except SystemExit as exc:
            code = exc.code
        return code, (json.loads(receipt.read_text()) if receipt.exists() else None)

    return state, audio, claims, inference, run


def _other_importer_holds_claim(claims: Path, content_hash: str) -> int:
    claims.mkdir(parents=True, exist_ok=True)
    fd = os.open(claims / f"{content_hash}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_second_importer_waits_then_reports_already_stored_without_asr(memo_main, monkeypatch, capsys):
    state, audio, claims, inference, run = memo_main
    content_hash = ta._audio_content_hash(audio)
    fd = _other_importer_holds_claim(claims, content_hash)
    stored_by_other = {
        "audio_content_sha256": content_hash, "file_size_bytes": audio.stat().st_size,
        "capture_method": "voice_memo", "consent_basis": "unknown",
        "transcription_text": "Synthetic memo.", "transcription_engine": "local_whisper_cpp",
        "audio_file_path": "/elsewhere/imports/audio/staged-copy.wav",
        "audio_file_name": "staged-copy.wav",
    }

    def other_importer_finishes(_secs):
        # The first importer stores its row and exits, releasing the claim.
        state["prior"] = {"id": "ent_prior", "snapshot": stored_by_other}
        os.close(fd)

    monkeypatch.setattr(ta, "_claim_sleep", other_importer_finishes)
    code, receipt = run()
    assert code == 0
    inference.assert_not_called()
    out = capsys.readouterr().out
    assert "TRANSCRIPTION_ALREADY_STORED=ent_prior" in out
    assert "entity" not in state  # no second store was attempted
    assert receipt["outcome"] == "already_stored"
    assert receipt["capture_method"] == "voice_memo"
    assert receipt["audio_attachment_verified"] is True


def test_claim_still_held_at_deadline_defers_without_asr(memo_main, monkeypatch):
    _state, audio, claims, inference, run = memo_main
    fd = _other_importer_holds_claim(claims, ta._audio_content_hash(audio))
    monkeypatch.setenv("ATELES_TRANSCRIPTION_CLAIM_WAIT_SECS", "0")
    try:
        code, receipt = run()
    finally:
        os.close(fd)
    assert code == ta.neotoma_store_guard.EXIT_STORE_DEFERRED
    assert receipt is None
    inference.assert_not_called()


def test_already_stored_row_with_other_capture_method_fails_closed(memo_main):
    state, audio, _claims, inference, run = memo_main
    state["prior"] = {"id": "ent_prior", "snapshot": {
        "audio_content_sha256": ta._audio_content_hash(audio),
        "file_size_bytes": audio.stat().st_size, "capture_method": "unknown",
        "transcription_text": "Synthetic memo.", "audio_file_name": "staged-copy.wav"}}
    code, receipt = run()
    assert code == 1 and receipt is None
    inference.assert_not_called()


def test_unclaimed_memo_transcribes_and_stores_once(memo_main):
    state, _audio, _claims, inference, run = memo_main
    code, receipt = run()
    assert code == 0
    assert inference.call_count == 1
    assert state["entity"]["capture_method"] == "voice_memo"
    assert state["entity"]["consent_basis"] == "unknown"
    assert receipt["exact_result_verified"] is True


def _run_import(monkeypatch, tmp_path, memo_dir_is_source):
    memos = tmp_path / "Recordings"
    memos.mkdir()
    other = tmp_path / "Desktop"
    other.mkdir()
    memo = (memos if memo_dir_is_source else other) / "20261125 043242-SYNTHETIC.m4a"
    memo.write_bytes(b"synthetic")
    monkeypatch.setattr(iad, "VOICE_MEMOS_PATH", memos)
    monkeypatch.setattr(iad, "AUDIO_IMPORTS_DIR", tmp_path / "imports")
    monkeypatch.setattr(iad, "import_status", lambda _n: iad.LookupResult(iad.NOT_IMPORTED, None))
    commands = []

    class _Done:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(cmd, **_kwargs):
        commands.append(cmd)
        return _Done()

    monkeypatch.setattr(iad.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["import_audio_from_desktop.py", "--file", str(memo), "--no-analyze"])
    try:
        iad.main()
    except SystemExit:
        pass
    [cmd] = [c for c in commands if str(iad.TRANSCRIBE_SCRIPT) in c]
    return cmd


def test_piculet_import_of_a_voice_memo_stamps_capture_method(monkeypatch, tmp_path):
    cmd = _run_import(monkeypatch, tmp_path, True)
    assert cmd[cmd.index("--capture-method") + 1] == "voice_memo"
    assert "--consent-basis" not in cmd


def test_import_outside_voice_memos_does_not_claim_voice_memo(monkeypatch, tmp_path):
    cmd = _run_import(monkeypatch, tmp_path, False)
    assert "--capture-method" not in cmd
