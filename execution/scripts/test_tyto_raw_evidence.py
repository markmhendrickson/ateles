"""Selected recovery refuses impossible raw output without replacing evidence."""
import copy
import json

import pytest
import transcribe_audio as ta
import tyto
import test_tyto_strict_persistence as strict
from test_transcription_contract import storage as storage
from test_tyto_strict_persistence import selected_flow as selected_flow
from test_tyto_complete_result import _assert_rejected
from test_transcription_results import complete_result, fluid_raw


@pytest.fixture
def backend_flow(selected_flow, monkeypatch):
    def setup(backend, paired=False, raw=None):
        def inference(audio, mic=None, **kwargs):
            result = complete_result(audio, mic, backend=backend, **kwargs)
            if raw is not None:
                result['raw_response'] = copy.deepcopy(raw)
                if backend == 'fluid' and not raw['aligned_words']:
                    result['transcription_text'] = ta.format_events([])
                    if not paired:
                        result['silence'] = True
            return result
        monkeypatch.setattr(strict, 'complete_result', inference)
        return selected_flow(paired)
    return setup


_WORD = {'type': 'word', 'text': 'Synthetic.', 'start': 0.0, 'end': 1.0}
_CHANNEL = {'text': 'Synthetic.', 'words': [_WORD], 'language_code': None}
_VALID_EL = [
    {'text': 'Synthetic.'}, {'text': 'Synthetic.', 'words': None, 'language_code': None},
    {'words': [_WORD]}, {'text': None, 'words': [_WORD]},
    {'transcripts': [_CHANNEL, {'text': '', 'words': []}]},
    {'transcripts': [{'text': 'Synthetic.'}]},
    {'segments': [{'transcripts': [_CHANNEL]}, {'transcripts': [{'text': '', 'words': []}]}]},
]
_BAD_EL = [
    {}, {'words': []}, {'text': 7}, {'words': 7}, {'text': 'unchanged', 'words': 7},
    {'transcripts': 7}, {'transcripts': {}}, {'transcripts': [7]},
    {'transcripts': [{}]}, {'transcripts': [{'text': 7}]},
    {'transcripts': [{'text': 'unchanged', 'words': 7}]},
    {'segments': []}, {'segments': [{}]}, {'segments': [{'transcripts': 7}]},
    {'segments': [{'transcripts': [_CHANNEL, {'words': 7}]}]},
    {'segments': [{'transcripts': [{'text': 'not timestamped'}]}]},
    {'words': [7]}, {'words': [{'text': 7, 'start': 0}]},
    {'words': [{'text': 'word', 'start': []}]},
    {'words': [{'text': 'word', 'start': 1, 'end': 0}]},
    {'transcripts': [{'text': 'ok', 'language_code': 7}]},
    {'segments': [{'transcripts': [_CHANNEL]}, {'transcripts': [{}]}]},
]


@pytest.mark.parametrize('paired', [False, True])
@pytest.mark.parametrize('raw', _BAD_EL)
def test_impossible_elevenlabs_cache_preserves_all_evidence(backend_flow, paired, raw):
    _assert_rejected(backend_flow('elevenlabs', paired),
                     lambda result: result.update(raw_response=copy.deepcopy(raw)))


@pytest.mark.parametrize('paired', [False, True])
@pytest.mark.parametrize('raw', _VALID_EL)
def test_supported_elevenlabs_raw_variants_complete_selected_recovery(backend_flow, paired, raw):
    state, phase, watcher, journal, audio, _mic, inference, receipts, _ = backend_flow('elevenlabs', paired, raw)
    sidecar = audio.with_suffix(audio.suffix + '.stt_raw.json')
    preserved = sidecar.read_bytes()
    phase.update(mode='fresh')
    tyto.cli()
    assert state['cmd'] and len(receipts) == 1
    assert sidecar.read_bytes() == preserved
    assert audio not in watcher._hard_failed and json.loads(journal.read_text())['hard_failed'] == []
    inference.assert_not_called()


def _damage_fluid(result, case, paired):
    raw = result['raw_response']
    leaf = raw['tracks'][0]['result'] if paired else raw
    if case == 'aligned':
        raw['aligned_words'] = []
    elif case == 'child_aligned':
        leaf['aligned_words'] = []
    elif case == 'words':
        leaf['tracks'][0]['asr']['wordTimings'] = []
    elif case == 'partial':
        leaf['tracks'][0]['asr']['wordTimings'].append(
            {'startTime': 1, 'endTime': 2, 'word': 'lost evidence'})
    elif case == 'all_empty':
        leaf['tracks'][0]['asr'] = {'text': '', 'wordTimings': []}
        leaf['tracks'][0]['diarization'] = {'segments': []}
        leaf['aligned_words'] = []
        raw['aligned_words'] = []


@pytest.mark.parametrize('paired', [False, True])
@pytest.mark.parametrize('case', ['aligned', 'child_aligned', 'words', 'partial', 'all_empty'])
def test_impossible_fluid_cached_alignment_preserves_all_evidence(backend_flow, paired, case):
    _assert_rejected(backend_flow('fluid', paired), lambda result: _damage_fluid(result, case, paired))


@pytest.mark.parametrize('paired', [False, True])
@pytest.mark.parametrize('backend,case', [('elevenlabs', raw) for raw in _BAD_EL] +
                         [('fluid', case) for case in ('aligned', 'words', 'all_empty')])
def test_fresh_impossible_raw_preserves_prior_sidecars_and_originals(backend_flow, monkeypatch, paired, backend, case):
    state, phase, watcher, journal, audio, mic, inference, receipts, _ = backend_flow(backend, paired)
    pending = audio.with_suffix(audio.suffix + '.pending-store.json')
    result = json.loads(pending.read_text())['result']
    if backend == 'elevenlabs':
        result['raw_response'] = copy.deepcopy(case)
    else:
        _damage_fluid(result, case, paired)
    original_cache = pending.with_suffix('.preserved.json')
    pending.rename(original_cache)
    preserved = {p: p.read_bytes() for p in (original_cache, journal, audio,
        audio.with_suffix(audio.suffix + '.transcript.txt'), audio.with_suffix(audio.suffix + '.stt_raw.json'))}
    if mic:
        preserved[mic] = mic.read_bytes()
    inference.side_effect = None
    inference.return_value = result
    monkeypatch.setattr(ta.sys, 'argv', ['tyto.py', '--recording-file', str(audio), '--recover-held'])
    phase.update(mode='fresh')
    with pytest.raises(RuntimeError):
        tyto.cli()
    assert 'cmd' not in state and not receipts and not pending.exists()
    assert audio in watcher._hard_failed
    assert all(p.read_bytes() == value for p, value in preserved.items())


@pytest.mark.parametrize('paired', [False, True])
def test_actual_fluid_silence_can_recover(backend_flow, paired):
    raw = fluid_raw()
    raw['tracks'][0]['asr'] = {'text': '', 'wordTimings': []}
    raw['tracks'][0]['diarization'] = {'segments': []}
    raw['aligned_words'] = []
    if paired:
        raw = {'tracks': [{'channel': label, 'result': copy.deepcopy(raw)} for label in ('System', 'Mic')],
               'aligned_words': []}
    state, phase, watcher, journal, audio, _mic, inference, receipts, _ = backend_flow('fluid', paired, raw)
    phase.update(mode='fresh')
    tyto.cli()
    assert state['cmd'] and receipts and audio not in watcher._hard_failed
    assert json.loads(journal.read_text())['hard_failed'] == []
    inference.assert_not_called()


@pytest.mark.parametrize('paired', [False, True])
def test_fluid_confidence_remains_backend_passthrough(backend_flow, paired):
    state, phase, watcher, _journal, audio, _mic, inference, receipts, _ = backend_flow('fluid', paired)
    pending = audio.with_suffix(audio.suffix + '.pending-store.json')
    payload = json.loads(pending.read_text())
    payload['result']['raw_response']['aligned_words'][0]['confidence'] = {'producer': 'passthrough'}
    pending.write_text(json.dumps(payload))
    phase.update(mode='fresh')
    tyto.cli()
    assert state['cmd'] and receipts and audio not in watcher._hard_failed
    inference.assert_not_called()
