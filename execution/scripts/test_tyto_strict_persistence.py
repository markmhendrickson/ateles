"""Selected recovery must prove the requested result persisted, not just dedupe.

Only inference, identity/status lookups and external transport are synthetic.
The watcher, CLI, pending result, storage/readback, receipt and journal are real.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import transcribe_audio as ta
from test_transcription_results import complete_result
from test_transcription_contract import storage as storage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemons/tyto'))
import tyto  # noqa: E402

_FIELDS = ('transcription_text', 'capture_method', 'transcription_engine', 'consent_basis',
           'original_source_file', 'audio_content_sha256', 'audio_file_path',
           'file_size_bytes', 'language', 'transcription_model', 'audio_duration_seconds')


@pytest.fixture
def selected_flow(storage, tmp_path, monkeypatch):
    state, _save = storage
    external_transport = ta.subprocess.run
    def setup(paired):
        audio = state['audio']
        target = audio.with_name('meeting remote.wav' if paired else 'memo.wav')
        audio.rename(target)
        audio = target
        state['source_hash'] = ta._audio_content_hash(audio)
        os.utime(audio, (time.time() - 100, time.time() - 100))
        mic = audio.with_name('meeting mic.wav') if paired else None
        if mic:
            mic.write_bytes(b'synthetic mic')
            os.utime(mic, (time.time() - 100, time.time() - 100))
        journal = tmp_path / 'retries.json'
        watcher = tyto.RecordingWatcher(tmp_path, MagicMock(), paired=paired,
            capture_method='audio_hijack_system' if paired else 'voice_memo', retry_state_path=journal)
        monkeypatch.setenv('TYTO_RUNTIME_LOCK', str(tmp_path / 'runtime.lock'))
        monkeypatch.setenv('TRANSCRIBE_BACKEND', '')
        monkeypatch.setenv('RECORD_MEETING_DIARIZE', '')
        monkeypatch.setenv('TRANSCRIPTION_CONSENT_BASIS', 'explicit_attestation')
        monkeypatch.setenv('TRANSCRIBE_DISABLE_NEOTOMA_VOCAB', '1')
        monkeypatch.setenv('NEOTOMA_PROD_BASE_URL', 'https://synthetic.invalid')
        monkeypatch.setenv('NEOTOMA_REMOTE_UPLOAD_MAX_BYTES', '100000')
        monkeypatch.setattr(tyto, 'TRANSCRIBE_SCRIPT', Path(ta.__file__))
        monkeypatch.setattr(tyto, 'load_agent_definition_or_exit', lambda _: {})
        monkeypatch.setattr(tyto, 'enforce_status_or_exit', lambda *a: None)
        monkeypatch.setattr(tyto, '_recording_watchers', lambda _: [watcher])
        monkeypatch.setattr(tyto, 'agent_identity', lambda _: {
            'sub': 'tyto@ateles-swarm', 'key': '/fixture/key', 'kid': 'fixture'})
        monkeypatch.setattr(tyto, '_verify_transcription_cli_runtime', lambda _: None)
        monkeypatch.setattr(ta, '_merge_transcription_relate_targets', lambda *a: ([], None))
        text = 'System/1: remote words.\nMic/1: microphone words.' if paired else 'Synthetic memo.'
        inference = MagicMock(return_value=complete_result(audio, mic, text=text))
        monkeypatch.setattr(ta, 'transcribe_two_files' if paired else 'transcribe_audio_file', inference)
        phase = {'mode': 'denied', 'field': None, 'change': None}
        receipts = []
        def bridge(command, **kwargs):
            if '--receipt-file' not in command:
                if phase['mode'] == 'denied':
                    return SimpleNamespace(returncode=1, stdout='', stderr='capability_denied')
                state['mismatch'] = phase['mode'] == 'prior'
                result = external_transport(command, **kwargs)
                if phase['mode'] == 'prior':
                    snapshot = dict(state['entity'])
                    snapshot.update(import_date='2001-01-01', transcription_date='2001-01-01')
                    field = phase['field']
                    if field:
                        if phase['change'] == 'missing':
                            snapshot.pop(field)
                        elif field in ('file_size_bytes', 'audio_duration_seconds'):
                            snapshot[field] = snapshot[field] + 1
                        elif field == 'transcription_text' and paired:
                            snapshot[field] = 'System/1: remote words.'  # same remote; wrong mic-derived result
                        else:
                            snapshot[field] = 'different-synthetic-value'
                    state['prior'] = {'id': 'ent_prior', 'snapshot': snapshot}
                elif phase['field']:
                    if phase['change'] == 'different':
                        state['snapshot_overrides'] = {phase['field']: state['entity'][phase['field']] + 1}
                    else:
                        state['drop'] = phase['field']
                        state['declared'] = set(state['entity']) - {phase['field']}
                return result
            output, errors = io.StringIO(), io.StringIO()
            with monkeypatch.context() as child:
                child.setattr(sys, 'argv', command[1:])
                for name in ('TRANSCRIBE_BACKEND', 'RECORD_MEETING_DIARIZE'):
                    child.setenv(name, kwargs['env'].get(name, ''))
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    try:
                        ta.main()
                        status = 0
                    except SystemExit as exc:
                        status = exc.code
            receipt_path = Path(command[command.index('--receipt-file') + 1])
            if receipt_path.exists():
                receipts.append(json.loads(receipt_path.read_text()))
            return SimpleNamespace(returncode=status, stdout=output.getvalue(), stderr=errors.getvalue())
        monkeypatch.setattr(ta.subprocess, 'run', bridge)
        monkeypatch.setattr(sys, 'argv', ['tyto.py', '--recording-file', str(audio)])
        with pytest.raises(RuntimeError, match='capability_denied'):
            tyto.cli()
        assert inference.call_count == 1 and receipts == []
        assert audio in watcher._hard_failed
        inference.reset_mock()
        inference.side_effect = AssertionError('store-only recovery reran inference')
        monkeypatch.setattr(sys, 'argv', ['tyto.py', '--recording-file', str(audio), '--recover-held', '--store-only'])
        return state, phase, watcher, journal, audio, mic, inference, receipts, text
    return setup


@pytest.mark.parametrize('paired', [False, True])
@pytest.mark.parametrize('field', _FIELDS)
@pytest.mark.parametrize('change', ['different', 'missing'])
def test_strict_prior_field_mismatch_never_receipts_or_clears(selected_flow, paired, field, change):
    state, phase, watcher, journal, audio, mic, inference, receipts, _text = selected_flow(paired)
    phase.update(mode='prior', field=field, change=change)
    with pytest.raises(RuntimeError):
        tyto.cli()
    assert receipts == []
    assert audio in watcher._hard_failed
    assert json.loads(journal.read_text())['hard_failed'][0]['path'] == str(audio)
    inference.assert_not_called()
    assert audio.read_bytes() == b'RIFF-synthetic-audio'
    if mic:
        assert mic.read_bytes() == b'synthetic mic'


@pytest.mark.parametrize('paired', [False, True])
def test_equivalent_date_only_prior_write_completes_without_inference(selected_flow, paired, capsys):
    state, phase, watcher, journal, audio, _mic, inference, receipts, text = selected_flow(paired)
    phase.update(mode='prior')
    tyto.cli()
    assert capsys.readouterr().out.strip() == 'NEOTOMA_TRANSCRIPTION_ENTITY_ID=ent_prior'
    assert json.loads(journal.read_text())['hard_failed'] == []
    assert audio not in watcher._hard_failed
    inference.assert_not_called()
    assert receipts[0]['exact_result_verified'] is True
    assert receipts[0]['transcription_text_sha256'] == hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.parametrize('field', _FIELDS)
def test_strict_fresh_write_cannot_skip_missing_fields_via_schema(selected_flow, field):
    _state, phase, watcher, journal, audio, _mic, inference, receipts, _text = selected_flow(False)
    phase.update(mode='fresh', field=field)
    with pytest.raises(RuntimeError):
        tyto.cli()
    assert receipts == [] and audio in watcher._hard_failed
    assert json.loads(journal.read_text())['hard_failed']
    inference.assert_not_called()


def test_repaired_persistence_hold_still_requires_exact_evidence(selected_flow):
    _state, phase, watcher, journal, audio, _mic, inference, receipts, _text = selected_flow(False)
    watcher._hard_failed[audio]['error_code'] = 'ERR_PERSISTED_RESULT_MISMATCH'
    watcher._save_retry_state()
    phase.update(mode='prior', field='capture_method', change='different')
    with pytest.raises(RuntimeError):
        tyto.cli()
    assert receipts == [] and audio in watcher._hard_failed
    phase.update(field=None)
    tyto.cli()
    assert json.loads(journal.read_text())['hard_failed'] == []
    inference.assert_not_called()


def test_source_recovery_checks_both_tracks_before_store_only(selected_flow, tmp_path):
    _state, phase, watcher, journal, audio, mic, inference, receipts, _text = selected_flow(True)
    watcher._hard_failed[audio]['error_code'] = tyto.SOURCE_REJECTION_ERROR_CODE
    watcher._save_retry_state()
    original_mic = mic.read_bytes()
    mic.unlink()
    outside = tmp_path / 'outside.wav'
    outside.write_bytes(b'outside synthetic')
    mic.symlink_to(outside)
    phase.update(mode='prior')
    with pytest.raises(RuntimeError, match='configured recording source'):
        tyto.cli()
    assert receipts == [] and audio in watcher._hard_failed
    mic.unlink()
    mic.write_bytes(original_mic)
    os.utime(mic, (time.time() - 100, time.time() - 100))
    tyto.cli()
    assert json.loads(journal.read_text())['hard_failed'] == []
    inference.assert_not_called()


@pytest.mark.parametrize('paired', [False, True])
@pytest.mark.parametrize('change', ['different', 'missing'])
def test_strict_fresh_duration_mismatch_retains_selected_hold(selected_flow, paired, change):
    _state, phase, watcher, journal, audio, _mic, inference, receipts, _text = selected_flow(paired)
    phase.update(mode='fresh', field='audio_duration_seconds', change=change)
    with pytest.raises(RuntimeError):
        tyto.cli()
    assert receipts == [] and audio in watcher._hard_failed
    assert json.loads(journal.read_text())['hard_failed']
    inference.assert_not_called()
