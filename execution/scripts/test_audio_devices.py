"""Tests for measured audio-input selection (ateles#648).

The incident these guard against: the streaming path defaulted to a hardcoded
`--device :3` while the operator wore AirPods Max, so every captured frame fell
below the input gate and nothing was transcribed for six minutes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audio_devices as ad
from audio_devices import AudioDevice, NoUsableInputDevice, ProbeResult


LISTING = """\
[AVFoundation indev @ 0x7f] AVFoundation video devices:
[AVFoundation indev @ 0x7f] [0] Studio Display Camera
[AVFoundation indev @ 0x7f] [1] Capture screen 0
[AVFoundation indev @ 0x7f] AVFoundation audio devices:
[AVFoundation indev @ 0x7f] [0] BlackHole 2ch
[AVFoundation indev @ 0x7f] [1] System-wide capture
[AVFoundation indev @ 0x7f] [2] Mark’s AirPods Max
[AVFoundation indev @ 0x7f] [3] Studio Display Microphone
[AVFoundation indev @ 0x7f] [4] ZoomAudioDevice
"""


def dev(index: int, name: str) -> AudioDevice:
    return AudioDevice(index=index, name=name)


def probe(index: int, name: str, mean: float | None, *, available: bool = True,
          passes: tuple[float, ...] = ()) -> ProbeResult:
    return ProbeResult(
        device=dev(index, name),
        mean_dbfs=mean,
        max_dbfs=None if mean is None else mean + 15,
        available=available,
        passes=passes,
    )


# --- enumeration ------------------------------------------------------------


def test_parses_only_audio_devices_not_video():
    """Video devices carry their OWN index sequence.

    Parsing without tracking the section yields video indices, which then
    select a camera or the wrong input entirely.
    """
    devices = ad.parse_device_listing(LISTING)
    assert [d.index for d in devices] == [0, 1, 2, 3, 4]
    assert [d.name for d in devices][:2] == ["BlackHole 2ch", "System-wide capture"]
    assert not any("Camera" in d.name or "screen" in d.name for d in devices)


def test_device_spec_is_the_ffmpeg_argument():
    assert dev(3, "Studio Display Microphone").spec == ":3"


# --- loopback classification ------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "BlackHole 2ch",
        "System-wide capture",
        "ZoomAudioDevice",
        "Loopback Audio",
        "Soundflower (2ch)",
        "BLACKHOLE 16CH",
    ],
)
def test_loopback_devices_are_recognised(name):
    assert dev(0, name).is_loopback


@pytest.mark.parametrize(
    "name",
    ["Mark’s AirPods Max", "Studio Display Microphone", "MacBook Pro Microphone"],
)
def test_real_microphones_are_not_loopback(name):
    assert not dev(0, name).is_loopback


def test_auto_selection_refuses_loopback_even_when_it_is_loudest():
    """The privacy control, under the condition that actually matters.

    Loopback devices routinely carry the strongest signal on the machine, so
    'loudest wins' selects one whenever anything is playing — recording the far
    end of a call the operator may not be part of (ateles#646).
    """
    results = [
        probe(0, "BlackHole 2ch", -8.0),
        probe(1, "System-wide capture", -6.0),
        probe(2, "Mark’s AirPods Max", -35.0),
        probe(4, "ZoomAudioDevice", -5.0),
    ]
    chosen = ad.select_device(results)
    assert chosen is not None
    assert chosen.device.index == 2, "auto-selected a system-capture device"


def test_loopback_may_be_selected_when_explicitly_allowed():
    """Opting in is a different act from a heuristic reaching for it."""
    results = [
        probe(0, "BlackHole 2ch", -8.0),
        probe(2, "Mark’s AirPods Max", -35.0),
    ]
    chosen = ad.select_device(results, allow_loopback=True)
    assert chosen is not None and chosen.device.index == 0


# --- silent vs unavailable --------------------------------------------------


def test_unavailable_device_is_never_selected():
    results = [
        probe(2, "Mark’s AirPods Max", None, available=False),
        probe(3, "Studio Display Microphone", -30.0),
    ]
    chosen = ad.select_device(results)
    assert chosen is not None and chosen.device.index == 3


def test_unavailable_is_distinct_from_silent():
    """A busy device is not a dead one, and must not be described as one."""
    busy = probe(2, "AirPods", None, available=False)
    quiet = probe(3, "Display Mic", -80.0)
    assert not busy.has_signal and not quiet.has_signal
    assert not busy.available and quiet.available
    assert "unavailable" in busy.describe()
    assert "unavailable" not in quiet.describe()


def test_probe_reports_unavailable_on_io_error(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args, 1, stdout="", stderr="[AVFoundation] Input/output error"
        )

    monkeypatch.setattr(ad.subprocess, "run", fake_run)
    result = ad.probe_device(dev(2, "AirPods"))
    assert not result.available
    assert "busy or disconnected" in result.detail


def test_probe_reports_unavailable_on_timeout(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=15)

    monkeypatch.setattr(ad.subprocess, "run", fake_run)
    result = ad.probe_device(dev(2, "AirPods"))
    assert not result.available and "timed out" in result.detail


# --- multi-pass probing -----------------------------------------------------


def test_best_pass_wins_so_a_starved_read_does_not_hide_a_live_device(monkeypatch):
    """The measured failure mode that forced multi-pass probing.

    A contended device alternates between delivering (-35 dB) and being starved
    (-77 dB). Observed on real hardware: the operator's AirPods read
    -35.1, -35.5, -77.1, -79.5, -82.5 across five consecutive probes. A single
    probe picks the wrong device depending purely on when it ran.
    """
    readings = iter([-77.0, -35.0, -79.0])

    def fake_probe(device, **kwargs):
        return probe(device.index, device.name, next(readings))

    monkeypatch.setattr(ad, "probe_device", fake_probe)
    result = ad.probe_device_repeated(dev(2, "AirPods"), passes=3)
    assert result.mean_dbfs == -35.0
    assert result.passes == (-77.0, -35.0, -79.0)
    assert result.has_signal


def test_intermittent_devices_are_flagged():
    result = probe(2, "AirPods", -35.0, passes=(-77.0, -35.0, -79.0))
    assert result.is_intermittent
    assert "in use by another process" in result.describe()


def test_steady_device_is_not_flagged_intermittent():
    result = probe(3, "Display Mic", -57.8, passes=(-58.5, -57.8, -59.4))
    assert not result.is_intermittent


def test_repeated_probe_reports_unavailable_when_every_pass_fails(monkeypatch):
    monkeypatch.setattr(
        ad, "probe_device",
        lambda device, **kw: probe(device.index, device.name, None, available=False),
    )
    result = ad.probe_device_repeated(dev(2, "AirPods"), passes=3)
    assert not result.available


# --- selection floor --------------------------------------------------------


def test_selects_the_device_carrying_signal_over_room_tone():
    """The incident, in miniature: AirPods live, display mic effectively silent."""
    results = [
        probe(2, "Mark’s AirPods Max", -39.1),
        probe(3, "Studio Display Microphone", -54.7),
    ]
    chosen = ad.select_device(results)
    assert chosen is not None and chosen.device.index == 2


def test_room_tone_alone_clears_no_device():
    results = [
        probe(2, "AirPods", -63.0),
        probe(3, "Display Mic", -58.0),
    ]
    assert ad.select_device(results) is None


# --- auto_select_device end to end -----------------------------------------


def _stub_environment(monkeypatch, devices, readings):
    monkeypatch.setattr(ad, "list_audio_devices", lambda **kw: devices)
    monkeypatch.setattr(
        ad, "probe_device_repeated",
        lambda device, **kw: probe(device.index, device.name, readings[device.index]),
    )


def test_auto_select_never_probes_loopback_devices(monkeypatch):
    """Opening a system-capture device reads audio that is none of our business."""
    probed: list[int] = []

    monkeypatch.setattr(ad, "list_audio_devices", lambda **kw: ad.parse_device_listing(LISTING))

    def record(device, **kwargs):
        probed.append(device.index)
        return probe(device.index, device.name, -30.0)

    monkeypatch.setattr(ad, "probe_device_repeated", record)
    ad.auto_select_device()
    assert probed == [2, 3], f"probed a loopback device: {probed}"


def test_auto_select_returns_confident_choice(monkeypatch):
    _stub_environment(
        monkeypatch,
        ad.parse_device_listing(LISTING),
        {2: -35.0, 3: -55.0},
    )
    selection = ad.auto_select_device()
    assert selection.spec == ":2" and selection.confident


def test_auto_select_degrades_rather_than_refusing_in_a_quiet_room(monkeypatch):
    """Refusing to start because nobody has spoken yet is a WORSE failure.

    The original bug transcribes nothing while claiming health; refusing here
    would block a session the operator explicitly asked for. So it proceeds,
    says loudly that it is not confident, and leaves the runtime gate-starved
    check to catch a genuinely wrong device.
    """
    _stub_environment(
        monkeypatch,
        ad.parse_device_listing(LISTING),
        {2: -63.0, 3: -58.0},
    )
    selection = ad.auto_select_device()
    assert selection.spec == ":3", "should still pick the best available device"
    assert not selection.confident
    assert "NOTHING WOULD BE TRANSCRIBED" in selection.warning
    # The operator must be able to see WHAT was measured, not just that it failed.
    assert "-58.0" in selection.warning and "Studio Display Microphone" in selection.warning


def test_auto_select_raises_when_no_device_can_be_read(monkeypatch):
    monkeypatch.setattr(ad, "list_audio_devices", lambda **kw: ad.parse_device_listing(LISTING))
    monkeypatch.setattr(
        ad, "probe_device_repeated",
        lambda device, **kw: probe(device.index, device.name, None, available=False),
    )
    with pytest.raises(NoUsableInputDevice) as excinfo:
        ad.auto_select_device()
    assert "no audio input device could be read" in str(excinfo.value)
    assert "Every candidate failed to open" in str(excinfo.value)


def test_auto_select_raises_when_no_devices_exist(monkeypatch):
    monkeypatch.setattr(ad, "list_audio_devices", lambda **kw: [])
    with pytest.raises(NoUsableInputDevice):
        ad.auto_select_device()


def test_failure_names_every_device_and_its_level(monkeypatch):
    """'No microphone found' with no numbers is unactionable."""
    _stub_environment(
        monkeypatch,
        ad.parse_device_listing(LISTING),
        {2: -63.0, 3: -58.0},
    )
    selection = ad.auto_select_device()
    table = ad.format_probe_table(selection.results)
    assert "Mark’s AirPods Max" in table
    assert "Studio Display Microphone" in table
    assert "BlackHole 2ch" in table  # listed as excluded, not silently dropped
    assert "-63.0" in table and "-58.0" in table
