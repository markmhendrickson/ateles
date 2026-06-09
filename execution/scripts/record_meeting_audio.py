#!/usr/bin/env python3
"""
Record system audio (e.g. from BlackHole) and optionally your microphone to one WAV.

Use with a virtual audio device (e.g. BlackHole 2ch on macOS) and a Multi-Output
Device so meeting audio from any app (Zoom, Meet, Teams, Webex) is captured.
Optional env: RECORD_MEETING_DEVICE (system capture substring, default BlackHole),
RECORD_MEETING_MIC (mic substring; empty disables mic; unset uses default input).
Optional env: RECORD_MEETING_REALTIME_INTERVAL (seconds between live transcription
chunks; 0 or unset = off).

Usage:
    python record_meeting_audio.py                    # BlackHole + default mic
    python record_meeting_audio.py --mic-device "Studio Display"  # BlackHole + mic mixed
    python record_meeting_audio.py --mic-device "Studio Display" --separate-sources
    python record_meeting_audio.py --realtime-interval 30   # live transcript every 30s
    python record_meeting_audio.py --list-devices
"""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import wave
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue

import numpy as np
import sounddevice as sd

# Add project root for config
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "execution" / "scripts"))

try:
    from scripts.config import get_data_dir
except ImportError:
    from config import get_data_dir

DATA_DIR = get_data_dir()
DEFAULT_OUTPUT_DIR = DATA_DIR / "imports" / "audio"
DEFAULT_DEVICE_QUERY = os.environ.get("RECORD_MEETING_DEVICE", "BlackHole")
SAMPLE_RATE = 48000
CHANNELS = 2
BLOCKSIZE = 1024

_env_realtime = os.environ.get("RECORD_MEETING_REALTIME_INTERVAL", "0")
try:
    DEFAULT_REALTIME_INTERVAL = int(_env_realtime)
except ValueError:
    DEFAULT_REALTIME_INTERVAL = 0

_recording = {
    "stream": None,
    "stream_mic": None,
    "wav": None,
    "running": True,
    "mixer_thread": None,
    # realtime chunking
    "chunk_lock": threading.Lock(),
    "chunk_frames": [],  # list of np.ndarray written since last flush
    "chunk_thread": None,
    "chunk_index": 0,
}


def _int16_from_float32(block: np.ndarray) -> bytes:
    """Convert float32 [-1, 1] to int16 for WAV."""
    return (np.clip(block, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def _normalize_device_name(name: str) -> str:
    """Normalize so straight and curly apostrophes match."""
    return (name or "").replace("\u2019", "'").replace("\u2018", "'").strip()


def _portaudio_default_input_name() -> str | None:
    """Current PortAudio default input device name, or None if unavailable."""
    try:
        idx = sd.default.device[0]
        if idx is None:
            return None
        idx = int(idx)
        if idx < 0:
            return None
        return sd.query_devices(idx, "input")["name"]
    except Exception:
        return None


def _resolve_input_device(device: str | int) -> int:
    """Resolve device name (or index) to input device index. Handles curly apostrophes in names."""
    if isinstance(device, int):
        return device
    want = _normalize_device_name(device)
    for i, dev in enumerate(sd.query_devices()):
        if (
            dev["max_input_channels"] > 0
            and want.lower() in _normalize_device_name(dev["name"]).lower()
        ):
            return i
    raise ValueError(f"No input device matching {device!r}")


def list_devices() -> None:
    """Print all input devices with indices and names."""
    print("Input devices (use --device / --mic-device <name or index>):\n")
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            default = " (default)" if i == sd.default.device[0] else ""
            print(f"  {i}: {dev['name']}{default}")
            print(
                f"      channels={dev['max_input_channels']}, sr={dev['default_samplerate']}"
            )


def _make_callback(queue: Queue):
    def callback(indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            print(f"[record] {status}", file=sys.stderr)
        try:
            queue.put_nowait(indata.copy())
        except Exception:
            pass

    return callback


def recording_callback(indata: np.ndarray, frames: int, time_info, status) -> None:
    if status:
        print(f"[record] {status}", file=sys.stderr)
    block = indata.copy()
    if _recording.get("wav") and _recording["wav"] is not None:
        _recording["wav"].writeframes(_int16_from_float32(block))
    with _recording["chunk_lock"]:
        _recording["chunk_frames"].append(block)


def _flush_chunk(
    frames: list,
    channels: int,
    sample_rate: int,
    chunk_index: int,
    transcribe_script: Path,
    python_bin: Path,
) -> None:
    """Write accumulated frames to a temp WAV and run transcribe_audio.py on it."""
    if not frames:
        return
    tmp = tempfile.NamedTemporaryFile(
        suffix=f"_chunk{chunk_index:04d}.wav", delete=False, prefix="meeting_rt_"
    )
    try:
        combined = np.concatenate(frames, axis=0)
        with wave.open(tmp.name, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(_int16_from_float32(combined))
        print(
            f"\n[realtime] chunk {chunk_index} — transcribing {len(combined)/sample_rate:.0f}s …",
            flush=True,
        )
        result = subprocess.run(
            [
                str(python_bin),
                str(transcribe_script),
                tmp.name,
                "--no-store",
                "--no-diarize",
            ],
            capture_output=True,
            text=True,
        )
        output = (result.stdout or "").strip()
        if output:
            print(f"[realtime] chunk {chunk_index}:\n{output}\n", flush=True)
        if result.returncode != 0 and result.stderr:
            print(
                f"[realtime] chunk {chunk_index} error: {result.stderr.strip()}",
                file=sys.stderr,
            )
    finally:
        try:
            Path(tmp.name).unlink(missing_ok=True)
        except Exception:
            pass


def _realtime_transcribe_loop(
    interval_sec: int,
    channels: int,
    sample_rate: int,
    transcribe_script: Path,
    python_bin: Path,
) -> None:
    """Background thread: every interval_sec, flush buffered audio and transcribe."""
    import time

    while _recording["running"]:
        time.sleep(interval_sec)
        if not _recording["running"]:
            break
        with _recording["chunk_lock"]:
            frames = list(_recording["chunk_frames"])
            _recording["chunk_frames"] = []
            idx = _recording["chunk_index"]
            _recording["chunk_index"] += 1
        _flush_chunk(frames, channels, sample_rate, idx, transcribe_script, python_bin)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record system audio from a virtual device (e.g. BlackHole) for meeting transcription."
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List input devices and exit.",
    )
    parser.add_argument(
        "--device",
        type=str,
        default=DEFAULT_DEVICE_QUERY,
        help=f"Input device name (substring match) or index (default: {DEFAULT_DEVICE_QUERY}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to save WAV file (default: $DATA_DIR/imports/audio).",
    )
    parser.add_argument(
        "--label",
        type=str,
        default="meeting",
        help="Label for filename (default: meeting).",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=SAMPLE_RATE,
        help=f"Sample rate in Hz (default: {SAMPLE_RATE}).",
    )
    parser.add_argument(
        "--channels",
        type=int,
        default=CHANNELS,
        help=f"Number of channels (default: {CHANNELS}).",
    )
    parser.add_argument(
        "--mic-device",
        type=str,
        default=None,
        help=(
            "Mic name (substring) or index; default is RECORD_MEETING_MIC env, else "
            "the PortAudio default input device."
        ),
    )
    parser.add_argument(
        "--no-mic",
        action="store_true",
        help="Record BlackHole only (no microphone).",
    )
    parser.add_argument(
        "--mic-gain",
        type=float,
        default=1.0,
        help="Gain for mic when mixing (default: 1.0).",
    )
    parser.add_argument(
        "--separate-sources",
        action="store_true",
        help=(
            "When recording with mic, store BlackHole and mic on separate channels "
            "(ch1=system, ch2=mic) for better diarization. Forces --channels 2."
        ),
    )
    parser.add_argument(
        "--realtime-interval",
        type=int,
        default=DEFAULT_REALTIME_INTERVAL,
        metavar="SECONDS",
        help=(
            "Emit a live partial transcription every N seconds while recording "
            "(0 = off, env: RECORD_MEETING_REALTIME_INTERVAL). "
            "Uses Whisper only (no diarization, no Neotoma store)."
        ),
    )
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    safe_label = (
        "".join(c if c.isalnum() or c in " -_" else "_" for c in args.label).strip()
        or "meeting"
    )
    out_path = args.output_dir / f"{timestamp}_{safe_label}.wav"

    if args.no_mic:
        mic_device_arg = None
    else:
        if args.mic_device is not None:
            mic_device_arg = args.mic_device
        else:
            env_mic = os.environ.get("RECORD_MEETING_MIC")
            if env_mic is not None:
                mic_device_arg = env_mic.strip() or None
            else:
                mic_device_arg = _portaudio_default_input_name()
    use_mic = mic_device_arg is not None and str(mic_device_arg).strip() != ""
    separate_sources = bool(args.separate_sources and use_mic)
    if args.separate_sources and not use_mic:
        print(
            "Warning: --separate-sources requested without mic; recording BlackHole only.",
            file=sys.stderr,
        )
    if separate_sources:
        args.channels = 2

    # Resolve devices
    try:
        device = int(args.device)
    except ValueError:
        device = args.device
    if use_mic:
        try:
            mic_device = int(str(mic_device_arg).strip())
        except ValueError:
            try:
                mic_device = _resolve_input_device(mic_device_arg)
            except ValueError:
                print(
                    f"Warning: mic device {mic_device_arg!r} not found; recording BlackHole only.",
                    file=sys.stderr,
                )
                use_mic = False

    realtime_interval = args.realtime_interval
    transcribe_script = PROJECT_ROOT / "execution" / "scripts" / "transcribe_audio.py"
    python_bin = Path(sys.executable)

    def stop(signum=None, frame=None) -> None:
        _recording["running"] = False
        for key in ("stream", "stream_mic"):
            if _recording.get(key) is not None:
                try:
                    _recording[key].stop()
                    _recording[key].close()
                except Exception:
                    pass
                _recording[key] = None
        if _recording.get("mixer_thread") is not None:
            _recording["mixer_thread"].join(timeout=2.0)
            _recording["mixer_thread"] = None
        if _recording.get("chunk_thread") is not None:
            _recording["chunk_thread"].join(timeout=5.0)
            _recording["chunk_thread"] = None
        if _recording.get("wav") is not None:
            try:
                _recording["wav"].close()
            except Exception:
                pass
            _recording["wav"] = None
        print(f"\nSaved: {out_path}")

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    try:
        wav = wave.open(str(out_path), "wb")
        wav.setnchannels(args.channels)
        wav.setsampwidth(2)  # 16-bit
        wav.setframerate(args.sample_rate)
        _recording["wav"] = wav

        if use_mic:
            queue_blackhole = Queue()
            queue_mic = Queue()

            def mixer_thread_fn() -> None:
                while _recording["running"]:
                    try:
                        b = queue_blackhole.get(timeout=0.5)
                        m = queue_mic.get(timeout=0.5)
                    except Empty:
                        continue
                    wav_file = _recording.get("wav")
                    if wav_file is None:
                        continue
                    # Mic might be mono; broadcast to stereo if needed
                    if m.shape[1] == 1:
                        m = np.repeat(m, args.channels, axis=1)
                    if separate_sources:
                        # Keep sources isolated per channel for downstream diarization:
                        # channel 1 = system (BlackHole), channel 2 = microphone.
                        b_mono = np.mean(b, axis=1, keepdims=True)
                        m_mono = np.mean(m, axis=1, keepdims=True)
                        dual = np.concatenate([b_mono, args.mic_gain * m_mono], axis=1)
                        wav_file.writeframes(_int16_from_float32(dual))
                        if realtime_interval > 0:
                            with _recording["chunk_lock"]:
                                _recording["chunk_frames"].append(dual)
                    else:
                        mixed = b + args.mic_gain * m
                        wav_file.writeframes(_int16_from_float32(mixed))
                        if realtime_interval > 0:
                            with _recording["chunk_lock"]:
                                _recording["chunk_frames"].append(mixed)

            mixer = threading.Thread(target=mixer_thread_fn, daemon=True)
            _recording["mixer_thread"] = mixer
            mixer.start()

            stream = sd.InputStream(
                device=device,
                channels=args.channels,
                samplerate=args.sample_rate,
                dtype="float32",
                blocksize=BLOCKSIZE,
                callback=_make_callback(queue_blackhole),
            )
            stream_mic = sd.InputStream(
                device=mic_device,
                channels=min(
                    2, sd.query_devices(mic_device, kind="input")["max_input_channels"]
                ),
                samplerate=args.sample_rate,
                dtype="float32",
                blocksize=BLOCKSIZE,
                callback=_make_callback(queue_mic),
            )
            _recording["stream"] = stream
            _recording["stream_mic"] = stream_mic
            stream.start()
            stream_mic.start()
            if separate_sources:
                print(
                    f"Recording (separate channels): '{args.device}' + mic '{mic_device_arg}' -> {out_path}"
                )
            else:
                print(
                    f"Recording: '{args.device}' + mic '{mic_device_arg}' -> {out_path}"
                )
        else:
            stream = sd.InputStream(
                device=device,
                channels=args.channels,
                samplerate=args.sample_rate,
                dtype="float32",
                blocksize=BLOCKSIZE,
                callback=recording_callback,
            )
            _recording["stream"] = stream
            stream.start()
            print(f"Recording from device '{args.device}' -> {out_path}")

        if realtime_interval > 0:
            if not transcribe_script.exists():
                print(
                    f"Warning: transcribe_audio.py not found at {transcribe_script}; "
                    "realtime transcription disabled.",
                    file=sys.stderr,
                )
            else:
                chunk_t = threading.Thread(
                    target=_realtime_transcribe_loop,
                    args=(
                        realtime_interval,
                        args.channels,
                        args.sample_rate,
                        transcribe_script,
                        python_bin,
                    ),
                    daemon=True,
                )
                _recording["chunk_thread"] = chunk_t
                chunk_t.start()
                print(
                    f"Live transcription: every {realtime_interval}s (Whisper, no store)."
                )

        print("Press Ctrl+C to stop.")
        while _recording["running"]:
            sd.sleep(500)
    except sd.PortAudioError as e:
        print(f"Audio error: {e}", file=sys.stderr)
        print(
            "Run with --list-devices to see available inputs. Install BlackHole if needed.",
            file=sys.stderr,
        )
        return 1
    finally:
        stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
