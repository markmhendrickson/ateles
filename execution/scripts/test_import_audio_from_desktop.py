"""Tests for the import-run outcome reporting in import_audio_from_desktop.

Regression cover for the failure mode observed on 2026-09-12: every file failed
to transcribe (HTTP 429 from the metered Whisper endpoint), nothing was written
to Neotoma, and the pipeline still printed

    ✓ Import complete: 23 file(s) processed
      Transcriptions saved to Neotoma (transcription entities + WAV).

and exited 0. "Processed" counted files moved off the Desktop, not files
transcribed, and the Neotoma line was unconditional. A run that stored nothing
must not report success.

These tests exercise ``summarize_run`` directly — the pure function that turns
per-file transcription results into (exit_code, summary lines) — so the
contract is asserted without touching the filesystem, the network, or Neotoma.
"""

import sys
import types
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

if "config" not in sys.modules:
    _stub_config = types.ModuleType("config")
    _stub_config.get_data_dir = lambda: Path(_SCRIPTS_DIR / "_test_data_dir")
    sys.modules["config"] = _stub_config

import import_audio_from_desktop as iad  # noqa: E402


def _results(*successes: bool) -> list[dict]:
    """Build a transcription_results list with the given success flags."""
    out = []
    for i, ok in enumerate(successes):
        result = {"success": True, "output": ""} if ok else {"success": False, "error": "HTTP 429"}
        out.append({"file": Path(f"/imports/file{i}.wav"), "transcription_result": result})
    return out


# --------------------------------------------------------------------------
# The regression: a run that transcribed nothing must fail loudly.
# --------------------------------------------------------------------------


def test_all_transcriptions_failed_exits_nonzero() -> None:
    exit_code, _ = iad.summarize_run(
        imported_count=23, skipped=0, results=_results(*([False] * 23))
    )
    assert exit_code != 0


def test_all_transcriptions_failed_does_not_claim_neotoma_storage() -> None:
    _, lines = iad.summarize_run(
        imported_count=23, skipped=0, results=_results(*([False] * 23))
    )
    blob = "\n".join(lines)
    assert "Transcriptions saved to Neotoma" not in blob
    assert "✓ Import complete" not in blob


def test_all_transcriptions_failed_says_what_actually_happened() -> None:
    _, lines = iad.summarize_run(
        imported_count=23, skipped=0, results=_results(*([False] * 23))
    )
    blob = "\n".join(lines)
    # Names the real outcome: imported but zero transcribed.
    assert "0 of 23" in blob
    assert "transcribed" in blob.lower()


# --------------------------------------------------------------------------
# The counterpart: a fully successful run still reports success.
# --------------------------------------------------------------------------


def test_all_transcriptions_succeeded_exits_zero_and_reports_storage() -> None:
    exit_code, lines = iad.summarize_run(
        imported_count=3, skipped=1, results=_results(True, True, True)
    )
    blob = "\n".join(lines)
    assert exit_code == 0
    assert "✓ Import complete" in blob
    assert "3 of 3" in blob
    assert "Transcriptions saved to Neotoma" in blob
    assert "1 file(s) skipped" in blob


# --------------------------------------------------------------------------
# Partial failure is a failure: the exit status must not hide it.
# --------------------------------------------------------------------------


def test_partial_failure_exits_nonzero_and_counts_honestly() -> None:
    exit_code, lines = iad.summarize_run(
        imported_count=3, skipped=0, results=_results(True, False, True)
    )
    blob = "\n".join(lines)
    assert exit_code != 0
    assert "2 of 3" in blob
    assert "1 file(s) failed to transcribe" in blob


def test_partial_failure_still_reports_the_successes_stored() -> None:
    _, lines = iad.summarize_run(
        imported_count=3, skipped=0, results=_results(True, False, True)
    )
    blob = "\n".join(lines)
    # The two that worked did reach Neotoma; say so without claiming all did.
    assert "Transcriptions saved to Neotoma" in blob


# --------------------------------------------------------------------------
# Nothing attempted is not success either.
# --------------------------------------------------------------------------


def test_no_results_at_all_exits_nonzero() -> None:
    exit_code, lines = iad.summarize_run(imported_count=0, skipped=0, results=[])
    assert exit_code != 0
    assert "Transcriptions saved to Neotoma" not in "\n".join(lines)


@pytest.mark.parametrize("failures", [1, 5, 23])
def test_failure_count_is_reported_verbatim(failures: int) -> None:
    _, lines = iad.summarize_run(
        imported_count=failures, skipped=0, results=_results(*([False] * failures))
    )
    assert f"{failures} file(s) failed to transcribe" in "\n".join(lines)
