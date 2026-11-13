"""Unreadable, present evidence must never become selected-store acceptance.

The CLI, cache reader, strict verifier, receipt and recovery journal are real.
Only inference/identity and external store/read transport use synthetic fixtures.
"""
import json
from pathlib import Path
import sys

import pytest
import transcribe_audio as ta
from test_transcription_contract import storage as storage
from test_tyto_strict_persistence import selected_flow as selected_flow
import tyto


_REAL_CLI_JSON = ta._neotoma_cli_json


def _evidence(audio, mic, journal):
    """Include optional raw output even for the local single-track fixture."""
    raw = audio.with_suffix(audio.suffix + '.stt_raw.json')
    if not raw.exists():
        raw.write_text(json.dumps({'text': 'Existing synthetic raw evidence.'}))
    paths = [audio, journal, raw,
             audio.with_suffix(audio.suffix + '.transcript.txt'),
             audio.with_suffix(audio.suffix + '.pending-store.json')]
    if mic is not None:
        paths.append(mic)
    return {path: path.read_bytes() for path in paths}


def _assert_retained(preserved, watcher, audio, inference, receipts):
    assert all(path.read_bytes() == before for path, before in preserved.items())
    assert audio in watcher._hard_failed
    assert audio not in watcher._transcribed
    inference.assert_not_called()
    assert receipts == []


@pytest.mark.parametrize('paired', [False, True])
@pytest.mark.parametrize('store_only', [False, True], ids=['implicit-reuse', 'store-only'])
def test_pending_store_read_oserror_preserves_evidence_without_asr(
    selected_flow, monkeypatch, paired, store_only, capsys,
):
    state, phase, watcher, journal, audio, mic, inference, receipts, _ = selected_flow(paired)
    preserved = _evidence(audio, mic, journal)
    pending = audio.with_suffix(audio.suffix + '.pending-store.json')
    # Establish that this is a complete, readable cache, not the missing or
    # malformed-record branch. The fixture already used it after a denied store.
    cached = json.loads(pending.read_text())
    ta._validate_transcription_result(cached['result'], paired=paired)
    phase.update(mode='fresh')
    if not store_only:
        monkeypatch.setattr(sys, 'argv', [arg for arg in sys.argv if arg != '--store-only'])
    original_read = Path.read_text
    attempts, causes = [], []
    fault = PermissionError('synthetic pending evidence read failure')
    real_pending_reader = ta._read_pending_store

    def fail_read(path, *args, **kwargs):
        if path == pending:
            assert path.is_file()
            attempts.append(path)
            raise fault
        return original_read(path, *args, **kwargs)

    def pending_reader(*args, **kwargs):
        try:
            return real_pending_reader(*args, **kwargs)
        except RuntimeError as exc:
            causes.append(exc.__cause__)
            raise

    monkeypatch.setattr(Path, 'read_text', fail_read)
    monkeypatch.setattr(ta, '_read_pending_store', pending_reader)
    with pytest.raises(RuntimeError, match='pending store; no ASR was run'):
        tyto.cli()
    assert causes == [fault]
    assert attempts == [pending]
    assert 'cmd' not in state
    assert 'NEOTOMA_TRANSCRIPTION_ENTITY_ID=' not in capsys.readouterr().out
    _assert_retained(preserved, watcher, audio, inference, receipts)


@pytest.mark.parametrize('paired', [False, True])
@pytest.mark.parametrize('mode', ['fresh', 'prior'])
@pytest.mark.parametrize('kind', ['entities', 'sources'])
def test_strict_read_transport_failure_cannot_issue_receipt(
    selected_flow, monkeypatch, paired, mode, kind, capsys,
):
    state, phase, watcher, journal, audio, mic, inference, receipts, _ = selected_flow(paired)
    preserved = _evidence(audio, mic, journal)
    phase.update(mode=mode)
    synthetic_read = ta._neotoma_cli_json
    synthetic_transport = ta.subprocess.run
    attempts, unknown = [], []
    fault = OSError('synthetic strict evidence transport failure')

    def fail_transport(command, **kwargs):
        if command[:3] == ['neotoma', kind, 'get']:
            attempts.append(command)
            raise fault
        return synthetic_transport(command, **kwargs)

    def read(args):
        if args[:2] == [kind, 'get']:
            # Keep the real exception-handling helper and strict supervisor;
            # only the exact synthetic read transport fails.
            result = _REAL_CLI_JSON(args)
            unknown.append(result)
            return result
        return synthetic_read(args)

    monkeypatch.setattr(ta.subprocess, 'run', fail_transport)
    monkeypatch.setattr(ta, '_neotoma_cli_json', read)
    reason = 'persisted read-back mismatch' if kind == 'entities' else 'audio source hash mismatch'
    with pytest.raises(RuntimeError, match='ERR_PERSISTED_RESULT_MISMATCH') as refused:
        tyto.cli()
    assert reason in str(refused.value)
    assert len(attempts) == 1 and attempts[0][:3] == ['neotoma', kind, 'get']
    assert unknown == [None]
    # An ingest was attempted before the readback became unknown. Withhold
    # acceptance without claiming that the earlier remote write was undone.
    assert state['cmd'][:2] == ['neotoma', 'ingest']
    assert 'NEOTOMA_TRANSCRIPTION_ENTITY_ID=' not in capsys.readouterr().out
    _assert_retained(preserved, watcher, audio, inference, receipts)
