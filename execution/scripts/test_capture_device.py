#!/usr/bin/env python3
"""Tests for binding live capture to a named device.

The behaviour under test is mostly a REFUSAL — that no substitute microphone is
ever chosen — so most of these assert what does NOT happen. That is the point:
the failure being prevented is a capture that works, looks healthy, and records
the wrong room.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import capture_device
from capture_device import (
    AudioDevice,
    DeviceBinding,
    SegmentedRecording,
    describe,
    list_audio_devices,
    normalize_device_name,
    permitted_devices,
)

# The real enumeration from the operator's machine, apostrophe and all.
LISTING = """\
[AVFoundation indev @ 0x1] AVFoundation video devices:
[AVFoundation indev @ 0x1] [0] Studio Display Camera
[AVFoundation indev @ 0x1] [1] Capture screen 0
[AVFoundation indev @ 0x1] AVFoundation audio devices:
[AVFoundation indev @ 0x1] [0] BlackHole 2ch
[AVFoundation indev @ 0x1] [1] System-wide capture
[AVFoundation indev @ 0x1] [2] Someone’s Headphones
[AVFoundation indev @ 0x1] [3] Studio Display Microphone
[AVFoundation indev @ 0x1] [4] ZoomAudioDevice
"""

# The SAME machine after the headset dropped. This is the measured renumbering
# that makes a pinned index address a different physical microphone.
LISTING_NO_HEADSET = """\
[AVFoundation indev @ 0x1] AVFoundation audio devices:
[AVFoundation indev @ 0x1] [0] BlackHole 2ch
[AVFoundation indev @ 0x1] [1] System-wide capture
[AVFoundation indev @ 0x1] [2] Studio Display Microphone
[AVFoundation indev @ 0x1] [3] ZoomAudioDevice
"""

AIRPODS = "Someone’s Headphones"


def devices_from(listing: str) -> list[AudioDevice]:
    lines = [ln for ln in listing.splitlines()]
    out: list[AudioDevice] = []
    in_audio = False
    for line in lines:
        if "audio devices:" in line:
            in_audio = True
            continue
        if "video devices:" in line:
            in_audio = False
            continue
        m = capture_device._DEVICE_LINE.match(line.strip())
        if m and in_audio:
            out.append(AudioDevice(int(m.group(1)), m.group(2)))
    return out


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------


def test_enumeration_parses_audio_devices(monkeypatch):
    monkeypatch.setattr(
        capture_device.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", LISTING),
    )
    devices = list_audio_devices()
    assert [(d.index, d.name) for d in devices] == [
        (0, "BlackHole 2ch"),
        (1, "System-wide capture"),
        (2, AIRPODS),
        (3, "Studio Display Microphone"),
        (4, "ZoomAudioDevice"),
    ]


def test_enumeration_ignores_video_devices(monkeypatch):
    """Video indices are a SEPARATE namespace — capturing ':0' from the video
    block would open a camera, not a microphone."""
    monkeypatch.setattr(
        capture_device.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", LISTING),
    )
    names = [d.name for d in list_audio_devices()]
    assert "Studio Display Camera" not in names
    assert "Capture screen 0" not in names


def test_enumeration_failure_is_empty_not_raise(monkeypatch):
    def boom(*a, **k):
        raise OSError("ffmpeg missing")

    monkeypatch.setattr(capture_device.subprocess, "run", boom)
    # Empty means "no permitted device present", which stops capture. It must
    # never become "capture something else".
    assert list_audio_devices() == []


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------


def test_typographic_apostrophe_matches_ascii():
    """macOS reports U+2019; a human types U+0027. Unequal as strings, so
    without folding a correct-looking binding would silently never match."""
    assert normalize_device_name(AIRPODS) == normalize_device_name("Someone's Headphones")


@pytest.mark.parametrize(
    "variant",
    [
        "someone's headphones",
        "SOMEONE'S HEADPHONES",
        "Someone's  Headphones",
        " Someone's Headphones ",
    ],
)
def test_case_and_whitespace_variants_match(variant):
    assert normalize_device_name(variant) == normalize_device_name(AIRPODS)


def test_different_devices_do_not_collide():
    assert normalize_device_name("Studio Display Microphone") != normalize_device_name(
        AIRPODS
    )


# ---------------------------------------------------------------------------
# Resolution — the renumbering defect
# ---------------------------------------------------------------------------


def test_resolves_name_to_current_index():
    binding = DeviceBinding((AIRPODS,), "neotoma")
    assert binding.resolve(devices_from(LISTING)).ffmpeg_arg == ":2"


def test_same_name_resolves_to_a_different_index_after_renumbering():
    """The measured defect: index 2 was the headset, then became the display
    mic. Resolving by NAME must follow the device, not the position."""
    binding = DeviceBinding(("Studio Display Microphone",), "neotoma")
    assert binding.resolve(devices_from(LISTING)).ffmpeg_arg == ":3"
    assert binding.resolve(devices_from(LISTING_NO_HEADSET)).ffmpeg_arg == ":2"


def test_absent_device_resolves_to_none_not_a_substitute():
    """The core requirement. Four other microphones are present and connected;
    none of them is chosen."""
    binding = DeviceBinding((AIRPODS,), "neotoma")
    assert binding.resolve(devices_from(LISTING_NO_HEADSET)) is None


def test_absent_device_does_not_fall_back_to_loudest_or_first():
    binding = DeviceBinding((AIRPODS,), "neotoma")
    resolved = binding.resolve(devices_from(LISTING_NO_HEADSET))
    assert resolved is None
    # Explicitly: not the first device, not a loopback, not the room mic.
    assert resolved not in devices_from(LISTING_NO_HEADSET)


def test_ordered_preference_list_picks_the_first_present():
    binding = DeviceBinding((AIRPODS, "Studio Display Microphone"), "neotoma")
    assert binding.resolve(devices_from(LISTING)).name == AIRPODS
    # Headset gone: the operator's own second choice, still never an unnamed one.
    assert (
        binding.resolve(devices_from(LISTING_NO_HEADSET)).name
        == "Studio Display Microphone"
    )


def test_empty_device_list_resolves_to_none():
    assert DeviceBinding((AIRPODS,), "neotoma").resolve([]) is None


# ---------------------------------------------------------------------------
# Configuration sourcing and degradation
# ---------------------------------------------------------------------------


def test_explicit_override_skips_neotoma(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("must not consult Neotoma when told a device")

    monkeypatch.setattr(capture_device, "_fetch_from_neotoma", fail)
    binding = permitted_devices("Some Desk Mic")
    assert binding.names == ("Some Desk Mic",)
    assert binding.source == "explicit"


def test_explicit_override_permits_a_loopback_device(monkeypatch):
    """A deliberate recording from another input must stay possible — the
    operator naming a device is a different act from a heuristic reaching for
    one. This is the use the single-device default must not block."""
    monkeypatch.setattr(capture_device, "_fetch_from_neotoma", lambda t: ("x",))
    binding = permitted_devices("BlackHole 2ch")
    assert binding.resolve(devices_from(LISTING)).ffmpeg_arg == ":0"


def test_explicit_override_accepts_a_comma_separated_list(monkeypatch):
    monkeypatch.setattr(capture_device, "_fetch_from_neotoma", lambda t: ("x",))
    assert permitted_devices("A Mic, B Mic").names == ("A Mic", "B Mic")


def test_reads_the_configured_binding(monkeypatch):
    monkeypatch.setattr(capture_device, "_fetch_from_neotoma", lambda t: (AIRPODS,))
    monkeypatch.setattr(capture_device, "_write_cache", lambda n: None)
    binding = permitted_devices()
    assert binding.names == (AIRPODS,)
    assert binding.source == "neotoma"
    assert binding.is_fresh if hasattr(binding, "is_fresh") else True


def test_falls_back_to_cache_when_neotoma_is_unreachable(monkeypatch, tmp_path):
    """Transcription runs where Neotoma may be down; an outage must not take
    capture offline."""
    cache = tmp_path / "capture_device.json"
    cache.write_text(json.dumps({"names": [AIRPODS], "written_at": 0}))
    monkeypatch.setattr(capture_device, "CACHE_PATH", cache)

    def boom(timeout):
        raise ConnectionError("neotoma down")

    monkeypatch.setattr(capture_device, "_fetch_from_neotoma", boom)
    binding = permitted_devices()
    assert binding.names == (AIRPODS,)
    assert binding.source == "cache"


def test_unreachable_with_no_cache_permits_nothing(monkeypatch, tmp_path):
    """The deliberate design call: NO seed device name. A built-in name is
    either operator config in a public repo or a guess, and a guess is exactly
    the 'capture whatever is there' behaviour this prevents."""
    monkeypatch.setattr(capture_device, "CACHE_PATH", tmp_path / "missing.json")

    def boom(timeout):
        raise ConnectionError("neotoma down")

    monkeypatch.setattr(capture_device, "_fetch_from_neotoma", boom)
    binding = permitted_devices()
    assert binding.names == ()
    assert not binding.is_configured
    assert binding.source == "unconfigured"
    # And with nothing permitted, no device resolves even though five exist.
    assert binding.resolve(devices_from(LISTING)) is None


def test_a_live_read_is_cached_for_the_next_outage(monkeypatch, tmp_path):
    cache = tmp_path / "capture_device.json"
    monkeypatch.setattr(capture_device, "CACHE_PATH", cache)
    monkeypatch.setattr(capture_device, "_fetch_from_neotoma", lambda t: (AIRPODS,))
    permitted_devices()
    assert json.loads(cache.read_text())["names"] == [AIRPODS]


def test_source_is_visible_so_a_stale_binding_is_diagnosable(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"names": [AIRPODS], "written_at": 0}))
    monkeypatch.setattr(capture_device, "CACHE_PATH", cache)
    monkeypatch.setattr(
        capture_device, "_fetch_from_neotoma", lambda t: (_ for _ in ()).throw(OSError())
    )
    binding = permitted_devices()
    assert "stale" in binding.detail


def test_unwritable_cache_does_not_break_capture(monkeypatch, tmp_path):
    target = tmp_path / "nope"
    target.write_text("i am a file, not a directory")
    monkeypatch.setattr(capture_device, "CACHE_PATH", target / "c.json")
    monkeypatch.setattr(capture_device, "_fetch_from_neotoma", lambda t: (AIRPODS,))
    assert permitted_devices().names == (AIRPODS,)


# ---------------------------------------------------------------------------
# Operator-visible description
# ---------------------------------------------------------------------------


def test_describe_names_the_device_and_the_source():
    binding = DeviceBinding((AIRPODS,), "neotoma")
    line = describe(binding, binding.resolve(devices_from(LISTING)))
    assert "Headphones" in line and ":2" in line and "neotoma" in line


def test_describe_absent_says_capture_is_stopped():
    binding = DeviceBinding((AIRPODS,), "neotoma")
    line = describe(binding, binding.resolve(devices_from(LISTING_NO_HEADSET)))
    assert "NOTHING" in line or "not connected" in line


def test_describe_unconfigured_says_capture_will_not_start():
    line = describe(DeviceBinding((), "unconfigured", "no token"), None)
    assert "will not start" in line


# ---------------------------------------------------------------------------
# The durable recording across stop/resume
# ---------------------------------------------------------------------------


def _tone(path: Path, freq: int, seconds: float) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}", "-ac", "1",
         "-c:a", "aac", "-b:a", "96k", "-y", str(path)],
        check=True,
    )


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


ffmpeg_required = pytest.mark.skipif(
    subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0,
    reason="ffmpeg not installed",
)


@ffmpeg_required
def test_recording_does_not_fragment_across_two_device_drops(tmp_path):
    """The constraint: the durable .m4a must be ONE file however many times
    the device came and went."""
    rec = SegmentedRecording(tmp_path / "20260902 1200 stream.m4a")
    for i in (1, 2, 3):
        _tone(rec.next_part(), 300 * i, 2)
    final = rec.finalize()

    assert final == tmp_path / "20260902 1200 stream.m4a"
    assert final.exists()
    # 3 x 2s joined. The ~23ms over is AAC encoder priming padding, not drift.
    assert 6.0 <= _duration(final) <= 6.1


@ffmpeg_required
def test_segments_are_kept_after_the_join(tmp_path):
    """If the process dies before the join, the parts are the recording. So
    they are never deleted on the strength of a join having succeeded."""
    rec = SegmentedRecording(tmp_path / "s.m4a")
    for i in (1, 2):
        _tone(rec.next_part(), 300 * i, 1)
    rec.finalize()
    assert len(list(rec.parts_dir.glob("*.m4a"))) == 2


@ffmpeg_required
def test_undropped_session_pays_nothing_for_the_machinery(tmp_path):
    """The ordinary case must not re-mux: one segment is moved into place."""
    rec = SegmentedRecording(tmp_path / "s.m4a")
    _tone(rec.next_part(), 440, 2)
    final = rec.finalize()
    assert final == tmp_path / "s.m4a"
    assert abs(_duration(final) - 2.0) < 0.05
    assert not rec.parts_dir.exists()


@ffmpeg_required
def test_a_truncated_segment_does_not_lose_the_others(tmp_path):
    """An ffmpeg killed before writing the moov atom leaves an unreadable
    stub; concat would fail the whole join over it."""
    rec = SegmentedRecording(tmp_path / "s.m4a")
    _tone(rec.next_part(), 440, 2)
    rec.next_part().write_bytes(b"\x00" * 44)
    final = rec.finalize()
    assert final is not None
    assert abs(_duration(final) - 2.0) < 0.05


def test_nothing_captured_returns_none_rather_than_an_empty_file(tmp_path):
    rec = SegmentedRecording(tmp_path / "s.m4a")
    rec.next_part()
    assert rec.finalize() is None
    assert not (tmp_path / "s.m4a").exists()


def test_each_segment_gets_its_own_path(tmp_path):
    rec = SegmentedRecording(tmp_path / "s.m4a")
    assert len({rec.next_part() for _ in range(3)}) == 3


def test_zero_byte_segments_are_not_offered_to_the_join(tmp_path):
    """A device that enumerates but refuses to open (exclusively held, or
    mid-connect) leaves an empty part. Concat would fail the whole join over
    one of them, losing every real segment alongside it."""
    rec = SegmentedRecording(tmp_path / "s.m4a")
    rec.next_part().write_bytes(b"")
    rec.next_part().write_bytes(b"\x00" * 100)
    assert rec._usable_parts() == []
    assert rec.finalize() is None
