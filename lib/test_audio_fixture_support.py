"""The guard that turns a silent skip into a failure must itself be tested.

ateles#866 blocked on a suite that reported green because its subject never
ran. Fixing that by adding a guard only moves the question: what fails if the
guard stops working? These tests are that answer — they assert the fail branch
is reachable, so the branch cannot quietly rot into a skip again.

Written red-first: with `require_ffmpeg()` reverted to an unconditional
`pytest.skip`, `test_missing_ffmpeg_fails_when_required` fails (the expected
Failed is never raised), and with the env check inverted,
`test_missing_ffmpeg_skips_when_not_required` fails.
"""

from __future__ import annotations

import pytest
from _pytest.outcomes import Failed, Skipped

from lib import audio_fixture_support as afs


@pytest.fixture
def _no_ffmpeg(monkeypatch):
    """Simulate a runner with no ffmpeg/ffprobe on PATH."""
    monkeypatch.setattr(afs.shutil, "which", lambda _name: None)
    monkeypatch.delenv(afs.REQUIRE_ENV_VAR, raising=False)
    return monkeypatch


def test_missing_ffmpeg_fails_when_required(_no_ffmpeg):
    """The whole point: on a lane that claims the coverage, absence is RED.

    This is the assertion that makes the workflow's ffmpeg install a control
    rather than documentation. Drop the install step and this branch is what
    the CI run hits.
    """
    _no_ffmpeg.setenv(afs.REQUIRE_ENV_VAR, "1")

    # Catch Skipped explicitly and convert it to a failure. `pytest.raises(Failed)`
    # alone is NOT enough here: if require_ffmpeg() regresses to an unconditional
    # skip, the skip propagates out of the `raises` block and pytest reports this
    # test as SKIPPED — green-ish, and blind to exactly the regression it exists
    # to catch. Verified by reverting the guard: the assertion below is what
    # turns that revert into a FAILURE (CLAUDE.md: "A test that cannot fail on
    # the thing it watches is decoration").
    try:
        afs.require_ffmpeg()
    except Failed as exc:
        message = str(exc)
    except Skipped as exc:  # pragma: no cover - only on regression
        pytest.fail(
            "require_ffmpeg() SKIPPED with "
            f"{afs.REQUIRE_ENV_VAR}=1 instead of failing: {exc}. "
            "This is the ateles#866 defect returning — a lane that declares "
            "audio coverage must go red when ffmpeg is absent, not green."
        )
    else:
        pytest.fail(
            f"require_ffmpeg() returned normally with {afs.REQUIRE_ENV_VAR}=1 "
            "and no ffmpeg on PATH; the failure branch is unreachable."
        )

    assert "ffmpeg is not on PATH" in message
    assert afs.REQUIRE_ENV_VAR in message
    assert "Fix the runner" in message


def test_missing_ffmpeg_skips_when_not_required(_no_ffmpeg):
    """A contributor without ffmpeg still gets a usable local run."""
    with pytest.raises(Skipped):
        afs.require_ffmpeg()


@pytest.mark.parametrize("value", ["0", "", "true", "yes", "TRUE"])
def test_only_exact_1_arms_the_failure(_no_ffmpeg, value):
    """Arming is exact-match on "1".

    Deliberate: the flag is set by CI in one place, and a fuzzy truthy check
    would let a stray value silently arm or disarm the gate. Anything other
    than "1" leaves the local skip behaviour, which is the SAFE direction here
    — a lane that wants the coverage sets the flag explicitly.
    """
    _no_ffmpeg.setenv(afs.REQUIRE_ENV_VAR, value)
    with pytest.raises(Skipped):
        afs.require_ffmpeg()


def test_present_ffmpeg_neither_fails_nor_skips(monkeypatch):
    """The happy path returns normally so the test body actually runs."""
    monkeypatch.setattr(afs.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv(afs.REQUIRE_ENV_VAR, "1")
    assert afs.require_ffmpeg() is None


def test_half_installed_runner_counts_as_missing(monkeypatch):
    """ffmpeg without ffprobe is not enough — ffprobe is what the code calls.

    tyto's `_audio_channel_count` is ffprobe-only, so a runner with just
    ffmpeg would build fixtures the subject under test still cannot read.
    """
    monkeypatch.setattr(
        afs.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None
    )
    assert afs.ffmpeg_available() is False
    monkeypatch.setenv(afs.REQUIRE_ENV_VAR, "1")
    with pytest.raises(Failed):
        afs.require_ffmpeg()


def test_generator_raises_rather_than_returning_falsy(tmp_path, monkeypatch):
    """A build failure must not become another value a caller turns into a skip.

    The pre-#866 helper returned False on failure and every call site turned
    that into `pytest.skip` — the exact shape of the defect. Raising keeps the
    failure loud.
    """
    class _Result:
        returncode = 1
        stderr = "synthetic ffmpeg failure"
        stdout = ""

    monkeypatch.setattr(afs.subprocess, "run", lambda *a, **k: _Result())
    with pytest.raises(RuntimeError, match="synthetic ffmpeg failure"):
        afs.make_synthetic_audio(tmp_path / "x.wav", 1)
