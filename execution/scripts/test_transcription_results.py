"""Backend-shaped synthetic results shared by offline transcription tests."""
import copy

import pytest
import transcribe_audio as ta


def fluid_raw():
    event = {'start': 0.0, 'end': 1.0, 'text': 'synthetic', 'speaker': 'S1',
             'speaker_candidates': ['S1'], 'confidence': None}
    return {'tracks': [{'channel': None,
                        'diarization': {'segments': [{'startTimeSeconds': 0.0, 'endTimeSeconds': 1.0, 'speakerId': 'S1'}]},
                        'asr': {'text': 'synthetic', 'wordTimings': [{'startTime': 0.0, 'endTime': 1.0, 'word': 'synthetic'}]}}],
            'aligned_words': [event]}


def complete_result(audio, mic=None, *, text='Synthetic.', backend=None):
    backend = backend or ('fluid' if mic is not None else 'local')
    engines = {'local': 'local_whisper_cpp', 'fluid': 'local_fluid_audio',
               'openai': 'openai_whisper_api', 'elevenlabs': 'elevenlabs_stt'}
    result = {'transcription_text': text, 'language': 'en', 'backend': backend,
              'transcription_engine': engines[backend], 'transcription_model': 'synthetic-model',
              'audio_duration_seconds': 2.5,
              'file_size_bytes': audio.stat().st_size + (mic.stat().st_size if mic else 0)}
    if backend == 'local':
        result.update(rms_db=None, silence=False)
    elif backend == 'fluid':
        raw = fluid_raw()
        if mic is not None:
            events = []
            for label in ('System', 'Mic'):
                events.extend([{**word, 'speaker': label + '/' + word['speaker']} for word in raw['aligned_words']])
            raw = {'tracks': [{'channel': label, 'result': copy.deepcopy(raw)} for label in ('System', 'Mic')],
                   'aligned_words': events}
        else:
            result['silence'] = False
        result['raw_response'] = raw
    elif backend == 'elevenlabs':
        result['raw_response'] = {'text': text, 'words': []}
    if mic is not None:
        result.update(mic_file=str(mic), remote_file=str(audio))
    return result


# Contract-positive controls do not call a backend or claim hardware qualification.


@pytest.mark.parametrize('backend,paired', [('local', False), ('openai', False),
    ('fluid', False), ('fluid', True), ('elevenlabs', False), ('elevenlabs', True)])
def test_supported_complete_contracts_preserve_legitimate_optional_fields(tmp_path, backend, paired):
    audio = tmp_path / 'source.wav'
    audio.write_bytes(b'synthetic')
    mic = tmp_path / 'mic.wav' if paired else None
    if mic:
        mic.write_bytes(b'synthetic mic')
    result = complete_result(audio, mic, backend=backend)
    if backend != 'fluid':
        result['audio_duration_seconds'] = None
    if backend == 'local':
        result['rms_db'] = None
        assert 'raw_response' not in result
    if backend == 'openai':
        result['language'] = None
        assert 'raw_response' not in result
    if paired and backend == 'fluid':
        assert 'rms_db' not in result and 'silence' not in result
    if paired and backend == 'elevenlabs':
        result.pop('audio_duration_seconds')
    ta._validate_transcription_result(result, paired=paired)


def test_actual_local_wrapper_nullable_measurements_satisfy_contract(tmp_path, monkeypatch):
    audio = tmp_path / 'source.wav'
    audio.write_bytes(b'synthetic')
    monkeypatch.setattr(ta, 'get_audio_channel_count', lambda _: 1)
    monkeypatch.setattr(ta, 'get_audio_duration', lambda _: None)
    monkeypatch.setattr(ta, 'transcribe_local', lambda *a, **k: {
        'transcription_text': 'Synthetic.', 'language': 'en', 'rms_db': None, 'silence': False})
    result = ta.transcribe_audio_file(audio, backend='local')
    ta._validate_transcription_result(result, paired=False)
    assert result['rms_db'] is None and result['audio_duration_seconds'] is None


@pytest.mark.parametrize('paired', [False, True])
def test_actual_fluid_constructors_satisfy_complete_raw_contract(tmp_path, monkeypatch, paired):
    import local_fluid
    audio = tmp_path / 'source.wav'
    audio.write_bytes(b'synthetic')
    mic = tmp_path / 'mic.wav'
    mic.write_bytes(b'synthetic mic')
    monkeypatch.setattr(local_fluid, 'resolve_fluid_cli', lambda: 'synthetic-fluid')
    raw = fluid_raw()['tracks'][0]
    monkeypatch.setattr(local_fluid, '_run_json', lambda cli, args, output:
                        copy.deepcopy(raw['diarization'] if args[0] == 'process' else raw['asr']))
    monkeypatch.setattr(ta, 'get_audio_duration', lambda _: 2.5)
    result = ta.transcribe_two_files(mic, audio, backend='fluid') if paired else local_fluid.transcribe_fluid(audio)
    ta._validate_transcription_result(result, paired=paired)
