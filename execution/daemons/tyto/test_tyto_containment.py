"""Normal watcher containment using synthetic audio, inference and store transport.

Unit B layers its structured persistence handoff onto the containment entrance.
Synthetic checks do not authorize operational recovery or daemon activation.
"""
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).resolve().parents[3]
for directory in (_ROOT, _ROOT / 'execution/scripts', Path(__file__).parent):
    sys.path.insert(0, str(directory))
import tyto  # noqa: E402
import transcribe_audio as ta  # noqa: E402
from test_transcription_results import complete_result  # noqa: E402
from test_transcription_contract import storage as storage  # noqa: E402


@pytest.mark.parametrize('paired,alias_kind,denied', [
    (False, None, False), (True, None, False),
    *[(paired, alias, True) for paired in (False, True) for alias in ('case', 'unicode', 'hardlink')],
])
def test_normal_handoff_stores_or_contains_denial_across_restart(storage, tmp_path, monkeypatch, paired, alias_kind, denied):
    state, _ = storage
    original = state['audio']
    audio = original.with_name('caf\u00e9 remote.wav' if paired else 'caf\u00e9.wav')
    original.rename(audio)
    state['source_hash'] = ta._audio_content_hash(audio)
    monkeypatch.setattr(tyto, 'ANALYZE_ENABLED', False)
    os.utime(audio, (time.time() - 100, time.time() - 100))
    mic = audio.with_name('caf\u00e9 mic.wav') if paired else None
    if mic is not None:
        mic.write_bytes(b'synthetic microphone')
        os.utime(mic, (time.time() - 100, time.time() - 100))
    monkeypatch.setenv('TRANSCRIBE_DISABLE_NEOTOMA_VOCAB', '1')
    monkeypatch.setattr(ta, '_merge_transcription_relate_targets', lambda *a: ([], None))
    inference = MagicMock(return_value=complete_result(audio, mic, text='Synthetic dialogue.', backend='fluid'))
    monkeypatch.setattr(ta, 'transcribe_two_files' if paired else 'transcribe_audio_file', inference)
    monkeypatch.setattr(tyto, 'TRANSCRIBE_SCRIPT', Path(ta.__file__))
    monkeypatch.setattr(tyto, 'agent_identity', lambda _: {
        'sub': 'tyto@ateles-swarm', 'key': '/fixture/key', 'kid': 'fixture'})
    monkeypatch.setattr(tyto, '_verify_transcription_cli_runtime', lambda _: None)
    child_commands = []
    external_transport = ta.subprocess.run
    def bridge(command, **kwargs):
        if str(ta.__file__) not in command:
            if denied:
                return SimpleNamespace(returncode=1, stdout='', stderr='x' * 400 + ' capability_denied')
            return external_transport(command, **kwargs)
        child_commands.append(command)
        assert '--receipt-file' in command and '--require-audio-attachment' in command
        assert '--store-only' not in command
        with monkeypatch.context() as child:
            child.setattr(sys, 'argv', command[1:])
            child.setenv('TRANSCRIBE_BACKEND', kwargs['env'].get('TRANSCRIBE_BACKEND', ''))
            child.setenv('RECORD_MEETING_DIARIZE', kwargs['env'].get('RECORD_MEETING_DIARIZE', ''))
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                try:
                    ta.main()
                    status = 0
                except SystemExit as exc:
                    status = exc.code
        return SimpleNamespace(returncode=status, stdout=output.getvalue(), stderr=errors.getvalue())
    monkeypatch.setattr(tyto.subprocess, 'run', bridge)
    journal = tmp_path / 'retries.json'
    watcher = tyto.RecordingWatcher(tmp_path, MagicMock(), paired=paired, retry_state_path=journal)
    monkeypatch.setattr(watcher, '_is_settled', lambda *a: True)
    asyncio.run(watcher.poll_once())
    assert inference.call_count == len(child_commands) == 1
    if not denied:
        assert json.loads(journal.read_text())['hard_failed'] == []
        assert json.loads(journal.read_text())['pending'] == []
        assert state['cmd'][1] == 'ingest'
        assert state['entity']['transcription_text'] == 'Synthetic dialogue.'
        return
    assert json.loads(journal.read_text())['hard_failed'][0]['error_code'] == 'capability_denied'
    assert audio.with_suffix('.wav.transcript.txt').exists()
    assert audio.with_suffix('.wav.stt_raw.json').exists()
    assert audio.with_suffix('.wav.pending-store.json').exists()
    if alias_kind == 'case':
        alias = audio.with_name(audio.name.upper())
    elif alias_kind == 'unicode':
        import unicodedata
        alias = audio.with_name(unicodedata.normalize('NFD', audio.name))
    else:
        alias = audio.with_name('hardlinked remote.wav' if paired else 'hardlinked.wav')
        os.link(audio, alias)
    if not alias.exists() or not alias.samefile(audio):
        pytest.skip('filesystem distinguishes this alias spelling; hardlink case is unconditional')
    restarted = tyto.RecordingWatcher(tmp_path, MagicMock(), paired=paired, retry_state_path=journal)
    monkeypatch.setattr(restarted, '_is_settled', lambda *a: True)
    asyncio.run(restarted.poll_once())
    asyncio.run(restarted.poll_once())
    assert inference.call_count == len(child_commands) == 1
    assert len(restarted._hard_failed) == 1
    assert audio.read_bytes() == b'RIFF-synthetic-audio'
    if mic is not None:
        assert mic.read_bytes() == b'synthetic microphone'


def test_normal_legacy_alias_conflicts_and_pending_limits_remain_restrictive(tmp_path, monkeypatch):
    original = tmp_path / 'held.wav'
    original.write_bytes(b'synthetic')
    alias = tmp_path / 'hardlinked.wav'
    os.link(original, alias)
    journal = tmp_path / 'retries.json'
    journal.write_text(json.dumps({'version': 2, 'pending': [
        {'path': str(original), 'retry_after': 100, 'attempts': 2},
        {'path': str(alias), 'retry_after': 200, 'attempts': 5}], 'hard_failed': [
        {'path': str(original), 'error_code': 'capability_denied'},
        {'path': str(alias), 'error_code': 'ERR_IDEMPOTENCY_MISMATCH'}]}))
    watcher = tyto.RecordingWatcher(tmp_path, MagicMock(), paired=False, retry_state_path=journal)
    assert list(watcher._pending_retries.values()) == [200]
    assert list(watcher._attempts.values()) == [5]
    assert list(watcher._hard_failed.values())[0]['error_code'] == 'conflicting_journal_holds'
    monkeypatch.setattr(watcher, '_is_settled', lambda *a: True)
    inference = MagicMock(side_effect=AssertionError('held file was transcribed'))
    monkeypatch.setattr(watcher, '_run_transcription', inference)
    asyncio.run(watcher.poll_once())
    inference.assert_not_called()
    rows = json.loads(journal.read_text())
    assert len(rows['hard_failed']) == len(rows['pending']) == 1
    assert rows['hard_failed'][0]['error_code'] == 'conflicting_journal_holds'


def test_normal_poll_preserves_distinct_files_and_deduplicates_hardlinks(tmp_path, monkeypatch):
    original = tmp_path / 'held.wav'
    original.write_bytes(b'synthetic')
    separate = tmp_path / 'separate.wav'
    separate.write_bytes(b'synthetic')
    alias = tmp_path / 'hardlinked.wav'
    os.link(separate, alias)
    watcher = tyto.RecordingWatcher(tmp_path, MagicMock(), paired=False,
                                   retry_state_path=tmp_path / 'retries.json')
    watcher._mark_hard_failed(original, 'capability_denied')
    monkeypatch.setattr(watcher, '_is_settled', lambda *a: True)
    observed = []
    async def handle(path, mic):
        observed.append(path)
        return 'success', None
    monkeypatch.setattr(watcher, '_handle_recording', handle)
    asyncio.run(watcher.poll_once())
    assert len(observed) == 1 and observed[0].samefile(separate)
    assert original in watcher._hard_failed


@pytest.mark.parametrize('kind', ['directory_remote', 'fifo_remote', 'directory_mic', 'symlink_mic'])
def test_rejected_recording_cannot_starve_healthy_source(tmp_path, monkeypatch, kind):
    source = tmp_path / 'source'
    source.mkdir()
    bad = source / 'a remote.wav'
    if kind == 'directory_remote':
        bad.mkdir()
    elif kind == 'fifo_remote':
        os.mkfifo(bad)
    else:
        bad.write_bytes(b'synthetic remote')
        mic = source / 'a mic.wav'
        if kind == 'directory_mic':
            mic.mkdir()
        else:
            outside = tmp_path / 'outside.wav'
            outside.write_bytes(b'outside synthetic')
            mic.symlink_to(outside)
    healthy = source / 'z remote.wav'
    healthy.write_bytes(b'healthy remote')
    healthy_mic = source / 'z mic.wav'
    healthy_mic.write_bytes(b'healthy microphone')
    journal = tmp_path / 'retries.json'
    observed = []
    async def handle(path, mic):
        observed.append((path, mic))
        return 'success', None
    watcher = tyto.RecordingWatcher(source, MagicMock(), retry_state_path=journal,
                                   max_files_per_poll=1, max_attempts=2)
    monkeypatch.setattr(watcher, '_is_settled', lambda *a: True)
    monkeypatch.setattr(watcher, '_handle_recording', handle)
    for _ in range(3):
        asyncio.run(watcher.poll_once())
    assert observed == [(healthy, healthy_mic)]
    payload = json.loads(journal.read_text())
    assert len(payload['hard_failed']) == 1
    assert payload['hard_failed'][0]['path'] == str(bad)
    assert payload['hard_failed'][0]['error_code'] == 'ERR_RECORDING_SOURCE_REJECTED'
    assert payload['hard_failed'][0]['notified'] is True
    assert payload['pending'] == []
    restarted = tyto.RecordingWatcher(source, MagicMock(), retry_state_path=journal,
                                     max_files_per_poll=1, max_attempts=2)
    monkeypatch.setattr(restarted, '_is_settled', lambda *a: True)
    after_restart = []
    async def resumed_handle(path, mic):
        after_restart.append((path, mic))
        return 'success', None
    monkeypatch.setattr(restarted, '_handle_recording', resumed_handle)
    for _ in range(3):
        asyncio.run(restarted.poll_once())
    assert after_restart == [(healthy, healthy_mic)]
    assert bad.exists()
    assert healthy.read_bytes() == b'healthy remote'
    assert healthy_mic.read_bytes() == b'healthy microphone'


def test_normal_case_sensitive_distinct_files_remain_distinct(tmp_path, monkeypatch):
    original = tmp_path / 'held.wav'
    original.write_bytes(b'synthetic')
    separate = tmp_path / 'HELD.wav'
    if separate.exists():
        pytest.skip('filesystem folds case; distinct-inode equal-byte coverage runs independently')
    separate.write_bytes(b'synthetic')
    watcher = tyto.RecordingWatcher(tmp_path, MagicMock(), paired=False)
    watcher._mark_hard_failed(original, 'capability_denied')
    monkeypatch.setattr(watcher, '_is_settled', lambda *a: True)
    observed = []
    async def handle(path, mic):
        observed.append(path)
        return 'success', None
    monkeypatch.setattr(watcher, '_handle_recording', handle)
    asyncio.run(watcher.poll_once())
    assert observed == [separate]
    assert original in watcher._hard_failed


def test_source_quarantine_journal_failure_stops_inference(tmp_path, monkeypatch):
    (tmp_path / 'a remote.wav').mkdir()
    (tmp_path / 'z remote.wav').write_bytes(b'healthy synthetic')
    watcher = tyto.RecordingWatcher(tmp_path, MagicMock(), retry_state_path=tmp_path / 'retry.json')
    monkeypatch.setattr(watcher, '_is_settled', lambda *a: True)
    monkeypatch.setattr(Path, 'replace', MagicMock(side_effect=OSError('synthetic journal failure')))
    inference = MagicMock(side_effect=AssertionError('inference after journal failure'))
    monkeypatch.setattr(watcher, '_run_transcription', inference)
    asyncio.run(watcher.poll_once())
    assert watcher._retry_state_available is False
    inference.assert_not_called()


def test_internal_remote_symlink_remains_rejected(tmp_path, monkeypatch):
    source = tmp_path / 'source.wav'
    source.write_bytes(b'synthetic')
    (tmp_path / 'a remote.wav').symlink_to(source)
    watcher = tyto.RecordingWatcher(tmp_path, MagicMock(), retry_state_path=tmp_path / 'retry.json')
    monkeypatch.setattr(watcher, '_is_settled', lambda *a: True)
    inference = MagicMock(side_effect=AssertionError('symlink accepted'))
    monkeypatch.setattr(watcher, '_run_transcription', inference)
    asyncio.run(watcher.poll_once())
    inference.assert_not_called()
    assert list(watcher._hard_failed.values())[0]['error_code'] == 'ERR_RECORDING_SOURCE_REJECTED'
