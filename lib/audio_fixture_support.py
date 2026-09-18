"""Synthetic audio fixtures for transcription-routing tests, and the rule for
what happens when the tool that generates them is missing.

Why this module exists
----------------------
The engine-routing tests (`execution/scripts/test_transcribe_audio.py`,
`execution/daemons/tyto/test_tyto_routing.py`) need real multi-channel audio:
the whole routing rule is "count the channels and decide", and the production
channel counters end in ``ffprobe``. Tyto's ``_audio_channel_count`` is
ffprobe-ONLY, so a fixture that dodges ffmpeg does not dodge the dependency —
it just moves the silent failure from fixture generation into the code under
test.

So the fixtures are generated with ffmpeg. The question this module answers is
what to do on a machine that has no ffmpeg.

``pytest.skip`` was the previous answer, and it is the defect ateles#866's QA
lens blocked on: on run 34240779554 the lane reported ``413 passed, 13
skipped`` and went green while every test that actually exercises
``select_transcription_engine`` was skipped. A suite that reports success
because its subject never ran is not a control (CLAUDE.md, "A mechanism that
does not bind is not a control"), and a skip is the dangerous shape of that
defect — an unbound test file is visibly absent, a skipping test looks like a
pass.

The rule
--------
``require_ffmpeg()`` consults ``ATELES_REQUIRE_AUDIO_FIXTURES``:

* set to ``1`` — a missing ffmpeg is a hard ``pytest.fail``. CI sets this on
  the lane that claims to cover routing, so dropping the runner's ffmpeg
  install turns the lane RED instead of silently green.
* unset — ``pytest.skip``, so a contributor without ffmpeg on their laptop
  still gets a usable local run.

The env var is the seam that makes the install step binding. Installing ffmpeg
in the workflow is not by itself a control: nothing fails if a later edit drops
the apt step, and the suite returns to passing-by-skipping. With this flag set,
removing the install makes the lane fail.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

#: CI sets this on any lane that claims to cover audio routing.
REQUIRE_ENV_VAR = "ATELES_REQUIRE_AUDIO_FIXTURES"

_FAILURE_MESSAGE = (
    "ffmpeg is not on PATH, but {var}=1 declares this lane covers "
    "transcription-engine routing.\n"
    "These tests are the only check that mono audio routes to local Whisper "
    "and multi-channel audio routes to ElevenLabs; the production channel "
    "counters end in ffprobe, so they cannot run without it.\n"
    "Skipping here would report green for a subject that never ran "
    "(ateles#866: '413 passed, 13 skipped').\n"
    "Fix the runner (install ffmpeg), not this assertion. To run locally "
    "without ffmpeg, leave {var} unset and these tests skip instead."
)


def ffmpeg_available() -> bool:
    """True when both ffmpeg and ffprobe are on PATH.

    Both are checked: ffmpeg writes the fixture, ffprobe is what the
    production channel counters call. A runner with one and not the other
    would generate audio the code under test still cannot read.
    """
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def require_ffmpeg() -> None:
    """Fail when ffmpeg is required and missing; skip when it is optional.

    Call at the top of any test that needs a synthetic audio fixture.
    """
    if ffmpeg_available():
        return
    if os.environ.get(REQUIRE_ENV_VAR) == "1":
        pytest.fail(_FAILURE_MESSAGE.format(var=REQUIRE_ENV_VAR), pytrace=False)
    pytest.skip(
        f"ffmpeg not available to generate a synthetic fixture "
        f"(set {REQUIRE_ENV_VAR}=1 to make this a failure instead)"
    )


def make_synthetic_audio(
    path: Path, channels: int, seconds: float = 1.0
) -> Path:
    """Write a synthetic tone with `channels` channels to `path`.

    SYNTHETIC ONLY — a generated sine wave. No operator recording is ever read
    by these tests.

    Raises on failure rather than returning a falsy value: by the time this is
    called, `require_ffmpeg()` has already settled the missing-tool case, so a
    failure here is a real fault worth surfacing rather than another silent
    downgrade.
    """
    layout = "mono" if channels == 1 else "stereo"
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={seconds}:sample_rate=16000",
            "-ac", str(channels),
            "-channel_layout", layout,
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not path.is_file():
        raise RuntimeError(
            f"ffmpeg failed to generate a {channels}-channel fixture at {path.name}: "
            f"{(result.stderr or result.stdout or '').strip()}"
        )
    return path
