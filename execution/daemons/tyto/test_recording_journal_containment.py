"""Invalid inputs and every journal-write branch preserve normal-source safety."""
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import sys
_ROOT = Path(__file__).resolve().parents[3]
for directory in (_ROOT, _ROOT / 'execution/scripts', Path(__file__).parent):
    sys.path.insert(0, str(directory))
import tyto  # noqa: E402

KINDS = ['directory', 'fifo', 'outside_symlink', 'internal_symlink', 'dangling_symlink', 'loop_symlink']

def invalid(path, kind, tmp_path):
    if kind == 'directory':
        path.mkdir()
    elif kind == 'fifo':
        os.mkfifo(path)
    elif kind == 'outside_symlink':
        outside = tmp_path / 'outside.wav'
        outside.write_bytes(b'outside fixture')
        path.symlink_to(outside)
    elif kind == 'internal_symlink':
        target = path.parent / 'not-an-input.bin'
        target.write_bytes(b'internal fixture')
        path.symlink_to(target)
    elif kind == 'dangling_symlink':
        path.symlink_to(tmp_path / 'missing.wav')
    elif kind == 'loop_symlink':
        path.symlink_to(path)

@pytest.mark.parametrize('paired,track', [(True, 'remote'), (True, 'mic'), (False, 'remote')])
@pytest.mark.parametrize('kind', KINDS)
def test_invalid_item_is_durable_bounded_and_never_starves_healthy(tmp_path, monkeypatch, paired, track, kind):
    source = tmp_path / 'source'
    source.mkdir()
    bad = source / ('a remote.wav' if paired else 'a.wav')
    if track == 'mic':
        bad.write_bytes(b'bad pair remote fixture')
        rejected = source / 'a mic.wav'
    else:
        rejected = bad
    invalid(rejected, kind, tmp_path)
    healthy = source / ('z remote.wav' if paired else 'z.wav')
    healthy.write_bytes(b'healthy remote fixture')
    mic = source / 'z mic.wav' if paired else None
    if mic is not None:
        mic.write_bytes(b'healthy mic fixture')
    journal = tmp_path / 'journal.json'
    notifier = MagicMock()
    observed = []
    async def handle(path, mic_path):
        assert path == healthy and mic_path == mic, 'unsafe inference or remote-only pair fallback'
        observed.append((path, mic_path))
        return 'success', None
    def watcher():
        item = tyto.RecordingWatcher(source, notifier, paired=paired, retry_state_path=journal,
                                     max_files_per_poll=1, max_attempts=2)
        monkeypatch.setattr(item, '_is_settled', lambda *a: True)
        monkeypatch.setattr(item, '_handle_recording', handle)
        return item
    first = watcher()
    for _ in range(3):
        asyncio.run(first.poll_once())
    assert observed == [(healthy, mic)]
    state = json.loads(journal.read_text())
    assert len(state['hard_failed']) == 1
    assert state['hard_failed'][0]['error_code'] == 'ERR_RECORDING_SOURCE_REJECTED'
    assert state['hard_failed'][0]['notified'] is True
    assert state['pending'] == []
    assert notifier.send.call_count == 1
    second = watcher()
    for _ in range(3):
        asyncio.run(second.poll_once())
    assert observed == [(healthy, mic)] * 2
    assert json.loads(journal.read_text()) == state
    assert notifier.send.call_count == 1
    assert os.path.lexists(rejected)
    assert healthy.read_bytes() == b'healthy remote fixture'
    if mic is not None:
        assert mic.read_bytes() == b'healthy mic fixture'

@pytest.mark.parametrize('paired', [False, True])
def test_healthy_control_keeps_expected_single_or_paired_call(tmp_path, monkeypatch, paired):
    healthy = tmp_path / ('z remote.wav' if paired else 'z.wav')
    healthy.write_bytes(b'fixture')
    mic = tmp_path / 'z mic.wav' if paired else None
    if mic is not None:
        mic.write_bytes(b'fixture mic')
    item = tyto.RecordingWatcher(tmp_path, MagicMock(), paired=paired, retry_state_path=tmp_path / 'journal.json')
    monkeypatch.setattr(item, '_is_settled', lambda *a: True)
    calls = []
    async def handle(path, mic_path):
        calls.append((path, mic_path))
        return 'success', None
    monkeypatch.setattr(item, '_handle_recording', handle)
    for _ in range(3):
        asyncio.run(item.poll_once())
    assert calls == [(healthy, mic)]


@pytest.mark.parametrize('paired', [False, True])
@pytest.mark.parametrize('entry', ['restored_notice', 'first_pending', 'clear_success', 'permanent', 'transient'])
def test_retry_state_save_failure_stops_later_pending_inference(tmp_path, monkeypatch, paired, entry):
    bad = tmp_path / ('a remote.wav' if paired else 'a.wav')
    if entry == 'restored_notice':
        bad.mkdir()
    else:
        bad.write_bytes(b'earlier fixture')
    healthy = tmp_path / ('z remote.wav' if paired else 'z.wav')
    healthy.write_bytes(b'pending healthy fixture')
    journal = tmp_path / 'journal.json'
    pending = [{'path': str(healthy), 'retry_after': 0, 'attempts': 1}]
    holds = []
    if entry == 'restored_notice':
        holds = [{'path': str(bad), 'error_code': 'ERR_RECORDING_SOURCE_REJECTED', 'notified': False}]
    elif entry != 'first_pending':
        pending.append({'path': str(bad), 'retry_after': 0, 'attempts': 1})
    journal.write_text(json.dumps({'version': 2, 'pending': pending, 'hard_failed': holds}))
    item = tyto.RecordingWatcher(tmp_path, MagicMock(), paired=paired, retry_state_path=journal,
                                 max_files_per_poll=1 if paired else None)
    monkeypatch.setattr(item, '_is_settled', lambda *a: True)
    monkeypatch.setattr(Path, 'replace', MagicMock(side_effect=OSError('synthetic journal failure')))
    observed = []
    async def handle(path, mic):
        observed.append((path, mic))
        if path == bad and entry == 'permanent':
            return 'permanent', 'capability_denied'
        if path == bad and entry == 'transient':
            return 'transient', None
        return 'success', None
    monkeypatch.setattr(item, '_handle_recording', handle)
    asyncio.run(item.poll_once())
    assert item._retry_state_available is False
    expected = [] if entry in ('restored_notice', 'first_pending') else [(bad, None)]
    assert observed == expected, 'continued inference after retry-journal persistence failure'

