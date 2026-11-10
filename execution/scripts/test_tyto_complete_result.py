"""Incomplete cache/backend results must preserve evidence before any write."""
import copy
import json

import pytest
import transcribe_audio as ta
from test_transcription_contract import storage as storage
from test_tyto_strict_persistence import selected_flow as selected_flow
import tyto

_COMMON = ('transcription_text', 'transcription_engine', 'transcription_model', 'language',
           'audio_duration_seconds', 'file_size_bytes', 'backend')
_FIELDS = [(paired, field) for paired in (False, True) for field in _COMMON]
_FIELDS += [(False, 'rms_db'), (False, 'silence'),
            (True, 'raw_response'), (True, 'mic_file'), (True, 'remote_file')]


def _assert_rejected(flow, mutate):
    state, phase, watcher, journal, audio, mic, inference, receipts, _text = flow
    pending = audio.with_suffix(audio.suffix + '.pending-store.json')
    payload = json.loads(pending.read_text())
    mutate(payload['result'])
    pending.write_text(json.dumps(payload))
    preserved = {path: path.read_bytes() for path in (
        pending, journal, audio, audio.with_suffix(audio.suffix + '.transcript.txt'))}
    raw = audio.with_suffix(audio.suffix + '.stt_raw.json')
    if raw.exists():
        preserved[raw] = raw.read_bytes()
    if mic:
        preserved[mic] = mic.read_bytes()
    phase.update(mode='fresh')
    with pytest.raises(RuntimeError):
        tyto.cli()
    assert 'cmd' not in state and receipts == []
    assert audio in watcher._hard_failed
    assert all(path.read_bytes() == content for path, content in preserved.items())
    inference.assert_not_called()


@pytest.mark.parametrize('paired,field', _FIELDS)
@pytest.mark.parametrize('corruption', ['missing', 'wrong_type'])
def test_incomplete_cached_result_is_rejected_before_any_effect(selected_flow, paired, field, corruption):
    def mutate(result):
        if corruption == 'missing':
            result.pop(field)
        else:
            result[field] = {'invalid': True} if field in ('audio_duration_seconds', 'file_size_bytes', 'rms_db', 'silence') else 7
    _assert_rejected(selected_flow(paired), mutate)


@pytest.mark.parametrize('path', [
    ('tracks',), ('aligned_words',), ('tracks', 0, 'result', 'tracks'),
    ('tracks', 0, 'result', 'aligned_words'), ('tracks', 0, 'result', 'tracks', 0, 'diarization'),
    ('tracks', 0, 'result', 'tracks', 0, 'asr'), ('tracks', 0, 'result', 'tracks', 0, 'channel'),
    ('tracks', 0, 'result', 'tracks', 0, 'diarization', 'segments'),
    ('tracks', 0, 'result', 'tracks', 0, 'asr', 'wordTimings'),
    *[('aligned_words', 0, field) for field in ('start', 'end', 'text', 'speaker', 'speaker_candidates', 'confidence')],
])
def test_missing_fluid_raw_field_never_replaces_complete_sidecar(selected_flow, path):
    def mutate(result):
        node = result['raw_response']
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]
    _assert_rejected(selected_flow(True), mutate)


@pytest.mark.parametrize('value', [None, 7, [], {'tracks': [], 'aligned_words': []}])
def test_incomplete_fluid_raw_envelope_preserves_complete_evidence(selected_flow, value):
    _assert_rejected(selected_flow(True), lambda result: result.update(raw_response=value))


@pytest.mark.parametrize('field,value', [('backend', 'unknown'), ('transcription_engine', 'unknown'),
                                        ('file_size_bytes', True), ('audio_duration_seconds', float('nan')),
                                        ('audio_duration_seconds', float('inf')), ('audio_duration_seconds', -1)])
def test_invalid_cached_disposition_and_numeric_values_fail_closed(selected_flow, field, value):
    _assert_rejected(selected_flow(False), lambda result: result.update({field: value}))


@pytest.mark.parametrize('paired', [False, True])
@pytest.mark.parametrize('field', ['transcription_text', 'transcription_engine', 'transcription_model', 'backend'])
def test_malformed_fresh_result_preserves_existing_sidecars(selected_flow, monkeypatch, paired, field):
    state, phase, watcher, _journal, audio, _mic, inference, receipts, _text = selected_flow(paired)
    pending = audio.with_suffix(audio.suffix + '.pending-store.json')
    payload = json.loads(pending.read_text())
    original_cache = pending.with_suffix('.preserved.json')
    pending.rename(original_cache)
    result = copy.deepcopy(payload['result'])
    del result[field]
    preserved = {path: path.read_bytes() for path in (
        original_cache, audio.with_suffix(audio.suffix + '.transcript.txt'))}
    raw = audio.with_suffix(audio.suffix + '.stt_raw.json')
    if raw.exists():
        preserved[raw] = raw.read_bytes()
    inference.side_effect = None
    inference.return_value = result
    monkeypatch.setattr(ta.sys, 'argv', ['tyto.py', '--recording-file', str(audio), '--recover-held'])
    phase.update(mode='fresh')
    with pytest.raises(RuntimeError):
        tyto.cli()
    assert 'cmd' not in state and receipts == [] and audio in watcher._hard_failed
    assert not pending.exists()
    assert all(path.read_bytes() == content for path, content in preserved.items())
