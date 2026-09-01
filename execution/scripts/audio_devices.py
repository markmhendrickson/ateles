#!/usr/bin/env python3
"""Choosing the audio input device by MEASURING it, not by guessing an index.

Why this module exists (ateles#648). The streaming path defaulted to a
hardcoded `--device :3`. The operator was wearing AirPods Max, so the capture
ran against the Studio Display Microphone and heard nothing:

    [2] Mark's AirPods Max          mean -39.1 dB   max -13.5 dB   (live)
    [3] Studio Display Microphone   mean -54.7 dB   max -38.6 dB   (silent)

Every frame from :3 sat below the -50 dBFS input gate, so the gate discarded
all of it — correctly. The pipeline then transcribed nothing for six minutes
while reporting itself healthy. The operator found out by asking why he wasn't
being heard.

A device index is not a stable name. It reorders when a headset connects, so
ANY hardcoded index is wrong the moment the operator changes headsets. The only
reliable question is "which of these inputs is currently carrying signal", and
that has to be measured.

Three distinctions this module refuses to collapse, each of which caused or
would have caused a wrong answer:

1. SILENT is not UNAVAILABLE. A device held by another process may return an
   I/O error, or may hand back a starved near-digital-silence stream. Measured
   during development: probing the AirPods while a live session held them read
   -83.3 dB rather than erroring. Treating that as "no signal here" would have
   auto-selected away from the operator's actual microphone. Unavailable
   devices are reported as unavailable and are never chosen, but they are also
   never counted as evidence that the device is dead.

2. A LOOPBACK device is not an input device. BlackHole, System-wide capture and
   ZoomAudioDevice capture what the machine is PLAYING. They frequently carry
   the strongest signal on the box, so a naive "loudest wins" auto-select picks
   one whenever anything is playing — and records the far end of someone else's
   call. Given ateles#646 (a private third-party conversation captured and
   transcribed), automatic selection excludes them unconditionally. An explicit
   `--device` still allows them: the operator asking for loopback by name is a
   different act from a heuristic reaching for it.

3. QUIET is not ABSENT. A device that is present and working but which nobody
   is speaking into reads low. That is why selection prefers the best measured
   candidate and reports the numbers, rather than asserting a threshold means
   the hardware is broken.
"""

from __future__ import annotations

import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

# Substrings identifying devices that capture SYSTEM AUDIO rather than a
# microphone. Matched case-insensitively against the device name.
#
# This list is a privacy control, not a convenience. Each of these routes
# whatever the machine is playing back into the capture, so auto-selecting one
# records the other side of any call in progress — including calls the operator
# is not a party to. ateles#646 is exactly that failure, and #647 closed the
# adjacent hole (attaching to a recording nobody started). Keep this exclusion
# explicit and commented so a later "why is this here" does not delete it.
LOOPBACK_MARKERS = (
    "blackhole",
    "system-wide capture",
    "soundflower",
    "loopback",
    "zoomaudiodevice",
    "aggregate",
    "multi-output",
)

# How long to listen to each device per pass, and how many passes to take.
#
# BOTH numbers are set by measurement, not taste. A single 2s probe is not a
# reliable discriminator: sampling the same two devices repeatedly in a quiet
# room produced
#
#     [2] AirPods Max          -35.1  -35.5  -77.1  -79.5  -82.5
#     [3] Display Microphone   -61.6  -59.4  -58.5  -57.8   -6.5
#
# The AirPods reading is BIMODAL — strong when the device delivers, near
# silence when another process is starving it — and the display mic threw a
# single -6.5 dB outlier off one transient noise. Ranking on one probe would
# have picked either device depending purely on when it ran.
#
# Taking the BEST of several passes fixes both failure modes at once: a device
# that ever delivers real level is live, and a device that never rises above
# room tone is not. Max-of-passes is the right reducer because signal is
# intermittent (speech has gaps, contention comes and goes) while room tone is
# constant — so the maximum separates them and the mean does not.
PROBE_SECONDS = 1.5
PROBE_PASSES = 3

# ffmpeg occasionally hangs opening a device that is in a bad state; without a
# timeout that hang becomes an indefinite startup stall, which is the same
# "looks fine, produces nothing" failure this module exists to end.
PROBE_TIMEOUT_SECONDS = 15.0

# A device whose BEST pass reaches this level is carrying real signal rather
# than room tone. Room tone measured -58 to -61 dBFS on this hardware and live
# speech measured -35 dBFS, so -50 sits in the gap between them. It coincides
# with the input gate's threshold for a good reason: the gate is what decides
# whether audio from the chosen device ever reaches the model, so a device that
# cannot clear the gate cannot produce a transcript no matter how it ranks.
SIGNAL_FLOOR_DBFS = -50.0

_DEVICE_LINE = re.compile(r"^\[AVFoundation indev @ [^\]]+\] \[(\d+)\] (.+)$")
_MEAN_VOLUME = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB")
_MAX_VOLUME = re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB")


@dataclass(frozen=True)
class AudioDevice:
    """One avfoundation audio input, as enumerated."""

    index: int
    name: str

    @property
    def spec(self) -> str:
        """The `-i` argument that selects this device."""
        return f":{self.index}"

    @property
    def is_loopback(self) -> bool:
        lowered = self.name.lower()
        return any(marker in lowered for marker in LOOPBACK_MARKERS)


@dataclass(frozen=True)
class ProbeResult:
    """What listening to one device actually measured.

    `available` False means the device could not be read (busy, missing,
    permission denied). That is explicitly NOT the same as a device that read
    quietly, and the two must not be merged: a busy device is very often the
    one the operator is really using.
    """

    device: AudioDevice
    mean_dbfs: float | None
    max_dbfs: float | None
    available: bool
    detail: str = ""
    # Every pass's mean, so the operator can see an intermittent device for
    # what it is rather than seeing only the number that won.
    passes: tuple[float, ...] = ()

    @property
    def has_signal(self) -> bool:
        return (
            self.available
            and self.mean_dbfs is not None
            and self.mean_dbfs >= SIGNAL_FLOOR_DBFS
        )

    @property
    def is_intermittent(self) -> bool:
        """True when passes disagree wildly — the signature of contention.

        A device another process is holding alternates between delivering and
        being starved. Worth surfacing: it usually means the device the
        operator actually wants is already in use, and the fix is to stop the
        other capture rather than to pick a different microphone.
        """
        if len(self.passes) < 2:
            return False
        return (max(self.passes) - min(self.passes)) >= 20.0

    def describe(self) -> str:
        if not self.available:
            return f"[{self.device.index}] {self.device.name}: unavailable ({self.detail})"
        if self.mean_dbfs is None:
            return f"[{self.device.index}] {self.device.name}: no measurement ({self.detail})"
        note = " [loopback — excluded from auto-selection]" if self.device.is_loopback else ""
        head = f"[{self.device.index}] {self.device.name}: best mean {self.mean_dbfs:.1f} dBFS"
        if self.max_dbfs is not None:
            head += f", peak {self.max_dbfs:.1f} dBFS"
        if len(self.passes) > 1:
            head += f" (passes: {', '.join(f'{p:.1f}' for p in self.passes)})"
        if self.is_intermittent:
            head += " [intermittent — likely in use by another process]"
        return head + note


class NoUsableInputDevice(RuntimeError):
    """Raised when auto-selection cannot find a device carrying signal.

    Carries the full probe table so the caller can tell the operator WHAT was
    tried and WHAT each device measured. "No microphone found" with no numbers
    is the unactionable version of this error.
    """

    def __init__(self, message: str, results: list[ProbeResult]) -> None:
        super().__init__(message)
        self.results = results


def parse_device_listing(stderr: str) -> list[AudioDevice]:
    """Pull the AUDIO devices out of `-list_devices true` output.

    ffmpeg lists video devices first with an independent index sequence, so
    parsing without tracking which section you are in yields video indices —
    which then select the wrong input, or a camera.
    """
    devices: list[AudioDevice] = []
    in_audio = False
    for line in stderr.splitlines():
        if "AVFoundation video devices" in line:
            in_audio = False
            continue
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if not in_audio:
            continue
        match = _DEVICE_LINE.match(line.strip())
        if match:
            devices.append(AudioDevice(index=int(match.group(1)), name=match.group(2).strip()))
    return devices


def list_audio_devices(*, timeout: float = 10.0) -> list[AudioDevice]:
    """Enumerate avfoundation audio inputs.

    ffmpeg reports the listing on stderr and exits non-zero (there is no real
    input to open), so a returncode check here would discard the answer.
    """
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner",
                "-f", "avfoundation",
                "-list_devices", "true",
                "-i", "",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    return parse_device_listing(proc.stderr or "")


def parse_volumedetect(stderr: str) -> tuple[float | None, float | None]:
    """Extract (mean, max) dBFS from a volumedetect run."""
    mean = _MEAN_VOLUME.search(stderr or "")
    peak = _MAX_VOLUME.search(stderr or "")
    return (
        float(mean.group(1)) if mean else None,
        float(peak.group(1)) if peak else None,
    )


def probe_device(
    device: AudioDevice,
    *,
    seconds: float = PROBE_SECONDS,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """Listen to one device briefly and measure what arrived.

    `-t` sits BEFORE `-i` so it bounds the capture itself; after `-i` it would
    bound only the output and the probe would run until the device felt like
    stopping.
    """
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner",
                "-f", "avfoundation",
                "-t", f"{seconds:g}",
                "-i", device.spec,
                "-af", "volumedetect",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(device, None, None, available=False, detail="probe timed out")
    except OSError as exc:
        return ProbeResult(device, None, None, available=False, detail=str(exc))

    stderr = proc.stderr or ""
    mean, peak = parse_volumedetect(stderr)

    if mean is None:
        # No measurement came back. Either the device refused to open (busy,
        # denied) or it produced nothing readable. Both are "unavailable" —
        # NOT "silent". Conflating them is how a busy microphone gets written
        # off as dead and auto-selection walks away from the right device.
        detail = "in use by another process or unreadable"
        lowered = stderr.lower()
        if "permission" in lowered or "not permitted" in lowered:
            detail = "permission denied"
        elif "input/output error" in lowered:
            detail = "I/O error — device busy or disconnected"
        return ProbeResult(device, None, None, available=False, detail=detail.strip())

    return ProbeResult(device, mean, peak, available=True)


def probe_device_repeated(
    device: AudioDevice,
    *,
    seconds: float = PROBE_SECONDS,
    passes: int = PROBE_PASSES,
) -> ProbeResult:
    """Probe one device several times and keep its BEST pass.

    See PROBE_PASSES for why one pass is not enough. Passes for a single device
    run in SEQUENCE — they are repeated reads of the same hardware, and running
    them concurrently would have them contend with each other and manufacture
    the very starvation they are meant to detect.
    """
    observed: list[ProbeResult] = []
    for _ in range(max(1, passes)):
        observed.append(probe_device(device, seconds=seconds))

    measured = [r for r in observed if r.available and r.mean_dbfs is not None]
    if not measured:
        return observed[-1]

    best = max(measured, key=lambda r: r.mean_dbfs or float("-inf"))
    return ProbeResult(
        device=device,
        mean_dbfs=best.mean_dbfs,
        max_dbfs=max((r.max_dbfs for r in measured if r.max_dbfs is not None), default=None),
        available=True,
        passes=tuple(r.mean_dbfs for r in measured if r.mean_dbfs is not None),
    )


def probe_devices(
    devices: list[AudioDevice],
    *,
    seconds: float = PROBE_SECONDS,
    passes: int = PROBE_PASSES,
    parallel: bool = True,
) -> list[ProbeResult]:
    """Measure every device.

    Probing across devices is parallel because it is otherwise pure startup
    latency — five devices at ~2.7s each is 13s of the operator talking to
    nothing before capture begins, which is most of the delay this change
    exists to remove. Distinct devices are independent reads, so they do not
    contend; repeated passes of the SAME device deliberately do not overlap.
    """
    if not devices:
        return []
    if not parallel or len(devices) == 1:
        return [probe_device_repeated(d, seconds=seconds, passes=passes) for d in devices]
    with ThreadPoolExecutor(max_workers=len(devices)) as pool:
        return list(
            pool.map(
                lambda d: probe_device_repeated(d, seconds=seconds, passes=passes),
                devices,
            )
        )


def select_device(
    results: list[ProbeResult],
    *,
    allow_loopback: bool = False,
) -> ProbeResult | None:
    """Pick the input carrying the most signal, excluding loopback devices.

    Ranked on MEAN rather than peak: a single keyboard click lifts a silent
    device's peak to within a few dB of a live microphone, so ranking on peak
    picks the quiet device in a quiet room roughly at random. Mean over the
    probe window is what "is someone talking into this" actually looks like.
    """
    candidates = [r for r in results if r.has_signal]
    if not allow_loopback:
        candidates = [r for r in candidates if not r.device.is_loopback]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.mean_dbfs or float("-inf"))


def format_probe_table(results: list[ProbeResult]) -> str:
    return "\n".join(f"  {r.describe()}" for r in results)


@dataclass(frozen=True)
class Selection:
    """The chosen device plus the evidence and the confidence behind it."""

    spec: str
    results: list[ProbeResult]
    confident: bool
    warning: str = ""


def auto_select_device(
    *,
    seconds: float = PROBE_SECONDS,
    passes: int = PROBE_PASSES,
    allow_loopback: bool = False,
    parallel: bool = True,
) -> Selection:
    """Enumerate, probe, and choose.

    Two outcomes short of a clean answer, kept distinct because they call for
    different behaviour:

    * NOTHING READABLE — every device failed to open. There is no device to
      capture from, so this raises NoUsableInputDevice.

    * NOTHING ABOVE THE FLOOR — devices work but none is carrying speech right
      now. This returns the best candidate with `confident=False` rather than
      refusing, because the overwhelmingly likely cause is that the operator is
      simply not talking yet. Refusing here would replace "transcribes nothing
      and says it is fine" with "refuses to start because the room was quiet",
      which is a worse failure: the first is a bug, the second blocks work the
      operator explicitly asked for. The caller warns loudly and starts, and
      the runtime below-gate health check catches a genuinely wrong device
      within its window.
    """
    devices = list_audio_devices()
    if not devices:
        raise NoUsableInputDevice(
            "no avfoundation audio input devices found — is ffmpeg installed and "
            "does this process have microphone permission?",
            [],
        )

    # Loopback devices are excluded before probing when they cannot be chosen
    # anyway: probing them costs latency and, more importantly, opening a
    # system-capture device reads audio that is none of our business.
    to_probe = devices if allow_loopback else [d for d in devices if not d.is_loopback]
    skipped = [
        ProbeResult(d, None, None, available=False, detail="loopback device — not probed")
        for d in devices
        if not allow_loopback and d.is_loopback
    ]

    results = probe_devices(to_probe, seconds=seconds, passes=passes, parallel=parallel) + skipped
    results.sort(key=lambda r: r.device.index)

    chosen = select_device(results, allow_loopback=allow_loopback)
    if chosen is not None:
        return Selection(chosen.device.spec, results, confident=True)

    # Nothing cleared the floor. Fall back to the loudest device that at least
    # READ, so a quiet room still gets a working capture.
    fallback_pool = [r for r in results if r.available and r.mean_dbfs is not None]
    if not allow_loopback:
        fallback_pool = [r for r in fallback_pool if not r.device.is_loopback]

    if not fallback_pool:
        raise NoUsableInputDevice(
            "no audio input device could be read — nothing would be transcribed.\n"
            f"Probed {len(results)} device(s):\n{format_probe_table(results)}\n"
            "Every candidate failed to open. Check microphone permissions, or pass "
            "--device explicitly.",
            results,
        )

    best = max(fallback_pool, key=lambda r: r.mean_dbfs or float("-inf"))
    warning = (
        "NO INPUT DEVICE IS CARRYING SPEECH — the loudest candidate measured "
        f"{best.mean_dbfs:.1f} dBFS, below the {SIGNAL_FLOOR_DBFS:.0f} dBFS floor, so "
        "audio from it would be discarded by the input gate and NOTHING WOULD BE "
        "TRANSCRIBED.\n"
        f"Probed {len(results)} device(s):\n{format_probe_table(results)}\n"
        f"Proceeding with [{best.device.index}] {best.device.name} on the assumption "
        "nobody is speaking yet. If you ARE speaking, this is the wrong device — stop "
        "and pass --device explicitly."
    )
    return Selection(best.device.spec, results, confident=False, warning=warning)
