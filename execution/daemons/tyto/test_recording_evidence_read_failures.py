"""Actual receipt and retry-journal read failures contain watcher acceptance.

All recordings, identities, inference and store transports are synthetic. The
normal/selected watcher, transcription CLI and evidence consumers stay real.
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
for _directory in (_ROOT, _ROOT / 'execution/scripts', Path(__file__).parent):
    sys.path.insert(0, str(_directory))
import transcribe_audio as ta  # noqa: E402
import tyto  # noqa: E402
from test_transcription_contract import storage as storage  # noqa: E402
from test_transcription_results import complete_result  # noqa: E402


@pytest.fixture
def receipt_flow(storage, tmp_path, monkeypatch):
    state, _ = storage
    store_transport = ta.subprocess.run

    def setup(paired, selected):
        audio = state['audio'].with_name('sample remote.wav' if paired else 'sample.wav')
        state['audio'].rename(audio)
        state['source_hash'] = ta._audio_content_hash(audio)
        mic = audio.with_name('sample mic.wav') if paired else None
        if mic is not None:
            mic.write_bytes(b'synthetic microphone')
        originals = {path: path.read_bytes() for path in (audio, mic) if path is not None}
        for path in originals:
            os.utime(path, (time.time() - 100, time.time() - 100))
        for name, value in {
            'TRANSCRIBE_BACKEND': '', 'RECORD_MEETING_DIARIZE': '',
            'TRANSCRIBE_DISABLE_NEOTOMA_VOCAB': '1',
            'TRANSCRIPTION_CONSENT_BASIS': 'explicit_attestation',
            'NEOTOMA_PROD_BASE_URL': 'https://synthetic.invalid',
            'NEOTOMA_REMOTE_UPLOAD_MAX_BYTES': '100000',
            'TYTO_RUNTIME_LOCK': str(tmp_path / 'runtime.lock'),
        }.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(ta, '_merge_transcription_relate_targets', lambda *a: ([], None))
        inference = MagicMock(return_value=complete_result(audio, mic, backend='fluid'))
        monkeypatch.setattr(ta, 'transcribe_two_files' if paired else 'transcribe_audio_file', inference)
        monkeypatch.setattr(tyto, 'TRANSCRIBE_SCRIPT', Path(ta.__file__))
        monkeypatch.setattr(tyto, 'agent_identity', lambda _: {
            'sub': 'tyto@ateles-swarm', 'key': '/fixture/key', 'kid': 'fixture'})
        monkeypatch.setattr(tyto, '_verify_transcription_cli_runtime', lambda _: None)
        monkeypatch.setattr(tyto, 'ANALYZE_ENABLED', True)
        analysis = MagicMock(side_effect=AssertionError('analysis accepted an unreadable receipt'))
        monkeypatch.setattr(tyto, '_run_analysis', analysis)
        journal = tmp_path / 'retries.json'
        notifier = MagicMock()
        watcher = tyto.RecordingWatcher(tmp_path, notifier, paired=paired,
            capture_method='audio_hijack_system' if paired else 'voice_memo', retry_state_path=journal)
        if selected:
            watcher._mark_hard_failed(audio, 'capability_denied')
        else:
            watcher._save_retry_state()
        journal_before = journal.read_bytes()
        monkeypatch.setattr(watcher, '_is_settled', lambda *a: True)
        monkeypatch.setattr(tyto, 'load_agent_definition_or_exit', lambda _: {})
        monkeypatch.setattr(tyto, 'enforce_status_or_exit', lambda *a: None)
        monkeypatch.setattr(tyto, '_recording_watchers', lambda _: [watcher])
        monkeypatch.setattr(sys, 'argv', ['tyto.py', '--recording-file', str(audio), '--recover-held'])
        produced = []

        def bridge(command, **kwargs):
            if '--receipt-file' not in command:
                return store_transport(command, **kwargs)
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
            assert status == 0, errors.getvalue()
            # Read bytes to establish a valid, present producer proof before
            # faulting its distinct Path.read_text consumer in the watcher.
            proof = json.loads(receipt_path.read_bytes())
            assert proof['version'] == 2 and proof['exact_result_verified'] is True
            assert proof['audio_attachment_verified'] is True
            assert proof['identity'] == watcher._source_receipt_identity(audio, mic)
            produced.append(receipt_path)
            return SimpleNamespace(returncode=status, stdout=output.getvalue(), stderr=errors.getvalue())

        monkeypatch.setattr(tyto.subprocess, 'run', bridge)
        return state, watcher, journal, journal_before, audio, originals, inference, analysis, notifier, produced
    return setup


@pytest.mark.parametrize('paired', [False, True])
@pytest.mark.parametrize('selected', [True, False], ids=['selected', 'normal-poll'])
def test_receipt_read_permission_error_refuses_completion(
    receipt_flow, monkeypatch, paired, selected, capsys, caplog,
):
    state, watcher, journal, before, audio, originals, inference, analysis, notifier, produced = receipt_flow(paired, selected)
    original_read = Path.read_text
    attempts = []
    fault = PermissionError('synthetic receipt evidence read failure')

    def fail_read(path, *args, **kwargs):
        if path.name == 'receipt.json':
            assert path in produced and path.is_file()
            attempts.append(path)
            raise fault
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', fail_read)
    if selected:
        save_journal = MagicMock(wraps=watcher._save_retry_state)
        monkeypatch.setattr(watcher, '_save_retry_state', save_journal)
        with pytest.raises(RuntimeError, match='verified transcription receipt') as refused:
            tyto.cli()
        assert refused.value.__cause__ is fault
        assert audio in watcher._hard_failed
        assert journal.read_bytes() == before
        save_journal.assert_not_called()
    else:
        asyncio.run(watcher.poll_once())
        assert audio not in watcher._transcribed
        analysis.assert_not_called()
        assert 'synthetic receipt evidence read failure' in caplog.text
        assert 'Missing or invalid verified transcription receipt' in caplog.text
        persisted = json.loads(journal.read_text())
        assert persisted['pending'][0]['path'] == str(audio)
        assert persisted['pending'][0]['attempts'] == 1
        restarted = tyto.RecordingWatcher(watcher._dir, MagicMock(), paired=paired,
            retry_state_path=journal)
        assert audio in restarted._pending_retries and restarted._attempts[audio] == 1
    assert len(attempts) == len(produced) == inference.call_count == 1
    assert state['cmd'][:2] == ['neotoma', 'ingest']
    assert audio not in watcher._transcribed
    analysis.assert_not_called()
    assert not any('Transcription complete' in call.args[0] for call in notifier.send.call_args_list)
    assert 'NEOTOMA_TRANSCRIPTION_ENTITY_ID=' not in capsys.readouterr().out
    assert all(path.read_bytes() == content for path, content in originals.items())


@pytest.mark.parametrize('source', ['meeting', 'native', 'memo'])
@pytest.mark.parametrize('selected', [False, True], ids=['normal-startup', 'selected-cli'])
def test_existing_retry_journal_read_failure_isolated_and_unseeded(
    tmp_path, monkeypatch, source, selected, caplog,
):
    bindings = {
        'meeting': ('RECORDINGS_DIR', 'RECORDING_RETRY_STATE', 'audio_hijack_system'),
        'native': ('NATIVE_RECORDINGS_DIR', 'NATIVE_RECORDING_RETRY_STATE', 'platform_native'),
        'memo': ('VOICE_MEMOS_DIR', 'VOICE_MEMO_RETRY_STATE', 'voice_memo'),
    }
    tracks, journals = {}, {}
    for name, (directory_var, journal_var, _) in bindings.items():
        directory = tmp_path / name
        directory.mkdir()
        audio = directory / ('sample.wav' if name == 'memo' else 'sample remote.wav')
        audio.write_bytes(b'synthetic ' + name.encode())
        tracks[name] = [audio]
        if name != 'memo':
            mic = directory / 'sample mic.wav'
            mic.write_bytes(b'synthetic microphone ' + name.encode())
            tracks[name].append(mic)
        for path in tracks[name]:
            os.utime(path, (time.time() - 100, time.time() - 100))
        journal = tmp_path / (name + '.json')
        journal.write_text(json.dumps({'version': 2, 'pending': [], 'hard_failed': []}))
        journals[name] = journal
        monkeypatch.setattr(tyto, directory_var, directory)
        monkeypatch.setattr(tyto, journal_var, journal)
    monkeypatch.setattr(tyto, 'TRANSCRIBE_ENABLED', True)
    monkeypatch.setattr(tyto, 'VOICE_MEMO_MAX_AGE_SECS', 0)
    monkeypatch.setenv('TYTO_RUNTIME_LOCK', str(tmp_path / 'runtime.lock'))
    monkeypatch.setattr(tyto, 'load_agent_definition_or_exit', lambda _: {})
    monkeypatch.setattr(tyto, 'enforce_status_or_exit', lambda *a: None)
    forbidden = MagicMock(side_effect=AssertionError('unreadable journal permitted processing'))
    monkeypatch.setattr(tyto.RecordingWatcher, '_run_transcription', forbidden)
    monkeypatch.setattr(tyto, '_run_analysis', forbidden)
    monkeypatch.setattr(ta, 'save_transcription', forbidden)
    originals = {path: path.read_bytes() for paths in tracks.values() for path in paths}
    failed_journal = journals[source]
    before = failed_journal.read_bytes()
    original_read, constructor = Path.read_text, tyto.RecordingWatcher
    attempts, constructed = [], []

    def fail_read(path, *args, **kwargs):
        if path == failed_journal:
            assert path.is_file()
            attempts.append(path)
            raise OSError('synthetic retry journal read failure')
        return original_read(path, *args, **kwargs)

    def construct(*args, **kwargs):
        watcher = constructor(*args, **kwargs)
        constructed.append(watcher)
        return watcher

    monkeypatch.setattr(Path, 'read_text', fail_read)
    monkeypatch.setattr(tyto, 'RecordingWatcher', construct)
    if selected:
        monkeypatch.setattr(sys, 'argv', ['tyto.py', '--recording-file', str(tracks[source][0]),
                                        '--recover-held', '--store-only'])
        with pytest.raises(RuntimeError, match='Retry journal is unavailable; selected processing refused'):
            tyto.cli()
    else:
        tyto._recording_watchers(MagicMock())
    failed = next(w for w in constructed if w._capture_method == bindings[source][2])
    assert not failed._retry_state_available
    assert failed._transcribed == set()
    asyncio.run(failed.poll_once())
    assert attempts == [failed_journal]
    assert 'synthetic retry journal read failure' in caplog.text
    assert 'existing files will not be seeded as handled' in caplog.text
    forbidden.assert_not_called()
    assert failed_journal.read_bytes() == before
    assert all(path.read_bytes() == content for path, content in originals.items())
    # A failed source must not poison another independently constructed watcher.
    healthy = [watcher for watcher in constructed if watcher is not failed]
    assert len(healthy) == 2 and all(w._retry_state_available for w in healthy)
    progressed = []

    async def healthy_handle(path, mic):
        progressed.append(path)
        return 'success', None

    for watcher in healthy:
        audio = watcher._dir / ('new.wav' if not watcher._paired else 'new remote.wav')
        audio.write_bytes(b'synthetic healthy progress')
        monkeypatch.setattr(watcher, '_is_settled', lambda *a: True)
        monkeypatch.setattr(watcher, '_handle_recording', healthy_handle)
        asyncio.run(watcher.poll_once())
        assert audio in progressed
    forbidden.assert_not_called()
