#!/usr/bin/env python3
"""Bind live capture to a NAMED device, sourced from Neotoma.

Why a name and not an index
---------------------------
avfoundation indices are positions in an enumeration, not identities. They
renumber whenever hardware connects or disconnects. Measured on the operator's
machine within a single session:

    with the headset connected      [2] Mark's AirPods Max
                                    [3] Studio Display Microphone
    after the headset dropped       [2] Studio Display Microphone

So ``--device :2`` addressed the headset at the start of the session and a
room microphone at the end of it, with no error and no change in the command.
A pinned index is a silent aliasing bug that only manifests when the hardware
changes — which for a headset is constantly.

Names are stable across that renumbering, so this module resolves name -> index
at every capture start and NEVER caches the index. The whole point is defeated
by remembering the answer.

Why absence means STOP, not fall back
-------------------------------------
When the named device is gone, this module reports it gone. It does not pick a
substitute. Two reasons, and the second is the important one:

* Correctness. The substitute is chosen by signal level, and the loudest input
  is routinely one nobody can hear — ``ZoomAudioDevice`` measured -91 dB while
  presenting as a perfectly openable input. Falling back means a plausible
  pipeline transcribing nothing.
* Consent. A device the operator is WEARING is evidence of intent to be
  recorded. A room microphone is not. ateles#646 was a 2.5-hour capture that
  picked up a private third-party conversation; "whichever input has audio" is
  precisely the ambient-capture shape behind it. Capturing nothing is a
  recoverable failure. Capturing a room the operator did not offer is not.

Composes with the auto-selection in #652 rather than replacing it: that PR
chooses WITHIN a permitted set, this module defines the set. When the set has
one member — the default — the choice is already made and probing is skipped.

Fallback when Neotoma is unreachable
------------------------------------
Transcription runs on a laptop that may have no Neotoma reachability, so a hard
dependency would take capture down on an outage. Follows the degradation
pattern established by ``spoken_languages`` (#687): live read, else the
last-good on-disk cache, else a conservative seed — with the source visible in
the returned value so a stale binding is diagnosable rather than looking fresh.

There is no seed DEVICE NAME, deliberately. A built-in device name would be
either operator-specific config smuggled into a public repo, or a guess that
reintroduces exactly the "capture from whatever is there" behaviour this exists
to prevent. With no configuration and no cache, the seed is *no permitted
device*, which surfaces as a refusal to capture. That is the safe direction:
the operator learns his configuration is missing instead of being recorded by
an unknown microphone.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

CACHE_PATH = Path(
    os.environ.get(
        "STREAM_TRANSCRIPT_DEVICE_CACHE",
        str(Path.home() / ".cache" / "ateles" / "capture_device.json"),
    )
)

CACHE_TTL_SECONDS = int(
    os.environ.get("STREAM_TRANSCRIPT_DEVICE_CACHE_TTL", str(30 * 24 * 3600))
)

# The capability slot in `vendor_binding` that carries the permitted device(s).
CAPABILITY = "live_transcription_input"

# How often the supervisor re-enumerates while waiting for the device to come
# back. Enumeration spawns an ffmpeg process (~0.15s) — cheap, but not free,
# and it contends with the running capture. 5s is well under the time it takes
# to put a headset on, so the operator never notices the wait.
DEVICE_POLL_SECONDS = float(os.environ.get("STREAM_TRANSCRIPT_DEVICE_POLL_S", "5"))

_AUDIO_HEADER = re.compile(r"AVFoundation audio devices:")
_DEVICE_LINE = re.compile(r"^\[AVFoundation indev @ [^\]]+\] \[(\d+)\] (.+)$")


def normalize_device_name(name: str) -> str:
    """Fold a device name to a comparable form.

    macOS reports ``Mark’s AirPods Max`` with a TYPOGRAPHIC apostrophe
    (U+2019), while anything a human types into a config field carries the
    ASCII one. They are different strings and compare unequal, so a binding
    that looks obviously correct would never match. Verified against the live
    enumeration on the operator's machine, where this exact mismatch applies.

    Also folds case and collapses whitespace, so a binding is not defeated by
    capitalization or a double space.
    """
    folded = unicodedata.normalize("NFKC", name)
    # Every Unicode apostrophe/quote variant macOS might emit -> ASCII.
    for ch in ("’", "‘", "ʼ", "´", "`"):
        folded = folded.replace(ch, "'")
    return " ".join(folded.split()).casefold()


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str

    @property
    def ffmpeg_arg(self) -> str:
        """avfoundation addresses audio-only input as ``:<index>``."""
        return f":{self.index}"


def list_audio_devices(timeout: float = 10.0) -> list[AudioDevice]:
    """Enumerate avfoundation audio inputs, current as of right now.

    ffmpeg exits non-zero for this invocation by design (there is no input file
    to open), and writes the listing to stderr, so neither the return code nor
    stdout is the signal.
    """
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "avfoundation",
             "-list_devices", "true", "-i", ""],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning(f"could not enumerate audio devices: {exc}")
        return []

    devices: list[AudioDevice] = []
    in_audio = False
    for line in proc.stderr.splitlines():
        if _AUDIO_HEADER.search(line):
            in_audio = True
            continue
        match = _DEVICE_LINE.match(line.strip())
        if not match:
            continue
        if not in_audio:
            # Still in the VIDEO block; those indices are a separate namespace
            # and capturing ":0" from it would open a camera.
            continue
        devices.append(AudioDevice(index=int(match.group(1)), name=match.group(2)))
    return devices


@dataclass(frozen=True)
class DeviceBinding:
    """The permitted device set, plus WHERE it came from.

    ``source`` is load-bearing rather than decorative: a caller that cannot
    distinguish a live read from a month-old cache cannot distinguish a correct
    binding from a silently stale one.
    """

    names: tuple[str, ...]
    source: str  # "neotoma" | "cache" | "explicit" | "unconfigured"
    detail: str = ""

    @property
    def is_configured(self) -> bool:
        return bool(self.names)

    def resolve(self, devices: list[AudioDevice] | None = None) -> AudioDevice | None:
        """Find the current index for the first permitted name that is present.

        Returns None when no permitted device is connected. Callers must treat
        that as "do not capture", never as "capture something else".
        """
        if devices is None:
            devices = list_audio_devices()
        by_norm = {normalize_device_name(d.name): d for d in devices}
        for wanted in self.names:
            found = by_norm.get(normalize_device_name(wanted))
            if found is not None:
                return found
        return None


def _auth_headers() -> dict[str, str]:
    if not NEOTOMA_BEARER_TOKEN:
        return {}
    return {"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"}


def _read_cache() -> DeviceBinding | None:
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        names = tuple(raw["names"])
        if not names:
            return None
        age = time.time() - float(raw.get("written_at", 0))
        stale = " (stale)" if age > CACHE_TTL_SECONDS else ""
        return DeviceBinding(names, "cache", f"cached {age / 3600:.1f}h ago{stale}")
    except Exception:
        return None


def _write_cache(names: tuple[str, ...]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps({"names": list(names), "written_at": time.time()}),
            encoding="utf-8",
        )
    except OSError as exc:  # a read-only cache dir must not break transcription
        log.warning(f"could not cache capture device binding: {exc}")


def _parse_names(value: object) -> tuple[str, ...]:
    """Accept either a single name or a comma-separated list."""
    if isinstance(value, list):
        return tuple(v.strip() for v in value if isinstance(v, str) and v.strip())
    if isinstance(value, str) and value.strip():
        return tuple(part.strip() for part in value.split(",") if part.strip())
    return ()


def _fetch_from_neotoma(timeout: float) -> tuple[str, ...]:
    """Read the vendor_binding. Raises on any failure; the caller degrades."""
    import httpx

    # POST /entities/query is the canonical list route; /retrieve_entities is
    # the MCP TOOL name and 404s on the hosted instance.
    resp = httpx.post(
        f"{NEOTOMA_BASE_URL}/entities/query",
        json={
            "entity_type": "vendor_binding",
            "limit": 50,
            "include_snapshots": True,
        },
        headers=_auth_headers(),
        timeout=timeout,
    )
    resp.raise_for_status()

    for entity in resp.json().get("entities", []):
        snap = entity.get("snapshot") or {}
        if isinstance(snap.get("snapshot"), dict):  # tolerate one extra nesting
            snap = snap["snapshot"]
        if snap.get("capability") != CAPABILITY:
            continue
        names = _parse_names(snap.get("vendor"))
        if names:
            return names
        raise LookupError(
            f"vendor_binding:{CAPABILITY} carried no device name: {snap.get('vendor')!r}"
        )

    raise LookupError(f"no vendor_binding with capability={CAPABILITY!r}")


def permitted_devices(
    explicit: str | None = None, timeout: float = 5.0
) -> DeviceBinding:
    """Resolve the permitted device names, degrading rather than raising.

    ``explicit`` is the operator naming a device on the command line, which is
    a different act from a heuristic reaching for one: it wins outright and
    skips Neotoma entirely. That keeps a deliberate recording from another
    input possible — a desk mic for a room session, a loopback device for
    capturing a call the operator IS party to — without weakening the default.
    """
    if explicit:
        names = _parse_names(explicit)
        return DeviceBinding(names, "explicit", "named on the command line")

    try:
        names = _fetch_from_neotoma(timeout)
    except Exception as exc:
        cached = _read_cache()
        if cached is not None:
            log.warning(
                f"vendor_binding:{CAPABILITY} unreachable ({exc}); using cached "
                f"device binding {cached.names} — {cached.detail}"
            )
            return cached
        log.warning(
            f"vendor_binding:{CAPABILITY} unreachable ({exc}) and no cache "
            "written. NO device is permitted, so capture will not start. "
            "This is deliberate: guessing a microphone is the failure this "
            "binding exists to prevent."
        )
        return DeviceBinding((), "unconfigured", str(exc))

    _write_cache(names)
    return DeviceBinding(names, "neotoma", f"read from vendor_binding:{CAPABILITY}")


class SegmentedRecording:
    """Keeps ONE durable recording across however many capture stops occur.

    The constraint this exists to satisfy
    -------------------------------------
    ffmpeg is launched against a device and cannot be told to change device
    mid-run, so following a device across a disconnect means restarting the
    process. Restarting it naively points a second ffmpeg at the same path and
    truncates the first one's work — you get the LAST segment and lose the
    session. The established rule in this path (and in ateles#679) is that the
    durable recording is the one thing never sacrificed.

    So each capture run writes its own numbered part, and the parts are joined
    on shutdown with the concat demuxer at ``-c copy`` — no re-encode, so no
    generation loss and no realtime cost. Verified: three 2.000s AAC parts join
    to 6.023s, the 23ms being encoder priming padding rather than drift.

    Why parts survive the join
    --------------------------
    The parts are NOT deleted. If the process is killed between the last
    segment and the join, the join never runs, and deleting on success only
    would still leave the crash case holding parts — so they are kept
    unconditionally and the joined file is written alongside. A recording that
    exists in pieces is recoverable; one that was deleted is not.
    """

    def __init__(self, final_path: Path) -> None:
        self.final_path = final_path
        self.parts_dir = final_path.with_name(f"{final_path.stem} parts")
        self.parts: list[Path] = []

    def next_part(self) -> Path:
        """Allocate the path for the next capture run."""
        self.parts_dir.mkdir(parents=True, exist_ok=True)
        part = self.parts_dir / f"{self.final_path.stem} {len(self.parts):03d}.m4a"
        self.parts.append(part)
        return part

    def _usable_parts(self) -> list[Path]:
        # A part whose ffmpeg died before writing the moov atom is a few dozen
        # bytes and unreadable; concat would fail the whole join over it.
        return [p for p in self.parts if p.exists() and p.stat().st_size > 1024]

    def finalize(self) -> Path | None:
        """Join the parts into the single durable recording.

        Returns the final path, or None when nothing usable was captured.
        Never raises: losing the join must not also lose the parts.
        """
        usable = self._usable_parts()
        if not usable:
            log.warning("no usable capture segments to join")
            return None

        if len(usable) == 1:
            # The ordinary case — device never dropped. Move rather than
            # re-mux, so a normal session pays nothing for this machinery.
            try:
                usable[0].replace(self.final_path)
                self.parts = []
                with contextlib.suppress(OSError):
                    self.parts_dir.rmdir()
                return self.final_path
            except OSError as exc:
                log.warning(f"could not move single segment into place: {exc}")
                return usable[0]

        listing = self.parts_dir / "parts.txt"
        try:
            listing.write_text(
                "".join(f"file '{p.name}'\n" for p in usable), encoding="utf-8"
            )
            proc = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-f", "concat", "-safe", "0", "-i", str(listing),
                 "-c", "copy", "-y", str(self.final_path)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if proc.returncode != 0:
                log.warning(
                    f"could not join {len(usable)} capture segments "
                    f"({proc.stderr.strip()[:200]}) — the segments are kept at "
                    f"{self.parts_dir}"
                )
                return None
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning(
                f"could not join capture segments ({exc}) — "
                f"the segments are kept at {self.parts_dir}"
            )
            return None
        return self.final_path


def describe(binding: DeviceBinding, device: AudioDevice | None) -> str:
    """One operator-readable line about what capture is bound to."""
    if not binding.is_configured:
        return (
            "NO capture device configured "
            f"({binding.source}: {binding.detail}) — capture will not start"
        )
    wanted = ", ".join(binding.names)
    if device is None:
        return (
            f"waiting for {wanted} — not connected "
            f"(binding from {binding.source}); capturing NOTHING until it returns"
        )
    return (
        f"bound to {device.name!r} at {device.ffmpeg_arg} "
        f"(binding from {binding.source}; index resolved fresh, never cached)"
    )
