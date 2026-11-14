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
    if paired:
        import local_whisper
        import two_track_transcribe as t3
        monkeypatch.setattr(local_whisper, 'resolve_ffmpeg', lambda *a, **k: 'ffmpeg')
        monkeypatch.setenv('TRANSCRIBE_PAIRED_ASR', 'parakeet')
        monkeypatch.setattr(t3, 'to_wav16', lambda ffmpeg, src, dest: dest)
        monkeypatch.setattr(t3, 'frame_levels', lambda ffmpeg, wav: [-20.0] * 30)
        monkeypatch.setattr(t3, 'mix_for_diarization', lambda ffmpeg, s, m, dest: dest)
        monkeypatch.setattr(local_fluid, 'diarize_mono', lambda wav: {'status': 'failed', 'category': 'cli not found'})
    result = ta.transcribe_two_files(mic, audio, backend='fluid') if paired else local_fluid.transcribe_fluid(audio)
    ta._validate_transcription_result(result, paired=paired)


@pytest.mark.parametrize('paired', [False, True])
def test_actual_elevenlabs_constructors_nullable_duration_contract(tmp_path, monkeypatch, paired):
    audio = tmp_path / 'source.wav'
    audio.write_bytes(b'synthetic')
    mic = tmp_path / 'mic.wav'
    mic.write_bytes(b'synthetic mic')
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'synthetic-test-only')
    monkeypatch.setenv('TRANSCRIBE_DISABLE_NEOTOMA_VOCAB', '1')
    monkeypatch.setattr(ta, 'get_audio_channel_count', lambda _: 1)
    monkeypatch.setattr(ta, 'get_audio_duration', lambda _: None)
    monkeypatch.setattr(ta, 'prepare_elevenlabs_upload_file', lambda path, **k: (path, None))
    monkeypatch.setattr(ta, '_post_elevenlabs_stt_raw', lambda *a: {'text': 'Synthetic.', 'words': []})
    monkeypatch.setattr(ta, '_combine_tracks_to_stereo', lambda *a, **k: (audio, None))
    result = ta.transcribe_two_files(mic, audio, backend='elevenlabs') if paired else ta.transcribe_audio_file(audio, backend='elevenlabs')
    ta._validate_transcription_result(result, paired=paired)
    assert result['audio_duration_seconds'] is None


def test_actual_openai_constructor_has_no_raw_guarantee(tmp_path, monkeypatch):
    from types import SimpleNamespace
    audio = tmp_path / 'source.wav'
    audio.write_bytes(b'synthetic')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-test-only')
    monkeypatch.setenv('TRANSCRIBE_DISABLE_NEOTOMA_VOCAB', '1')
    monkeypatch.setattr(ta, 'get_audio_channel_count', lambda _: 1)
    monkeypatch.setattr(ta, 'get_audio_duration', lambda _: None)
    monkeypatch.setattr(ta, 'OpenAI', lambda **k: object())
    monkeypatch.setattr(ta, 'transcribe_with_retry', lambda **k: SimpleNamespace(text='Synthetic.', language=None))
    result = ta.transcribe_audio_file(audio, backend='openai')
    ta._validate_transcription_result(result, paired=False)
    assert result['language'] is None and result['audio_duration_seconds'] is None
    assert 'raw_response' not in result
