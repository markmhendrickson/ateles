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
import os
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

os.environ.setdefault("DATA_DIR", str(_SCRIPTS_DIR / "_test_data_dir"))

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


# --------------------------------------------------------------------------
# Already-imported lookup fails closed. A lookup that could not answer is
# UNKNOWN, not "new": treating it as new re-staged the whole library during
# every lookup outage.
# --------------------------------------------------------------------------

import json  # noqa: E402
import subprocess  # noqa: E402


class _Completed:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _lookup_returning(monkeypatch, *outcomes):
    """Stub the neotoma CLI: each call consumes the next outcome.

    An outcome is a _Completed or an exception instance to raise.
    """
    monkeypatch.setattr(iad.shutil, "which", lambda _name: "/usr/bin/neotoma")
    queue = list(outcomes)

    def fake_run(*_args, **_kwargs):
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(iad.subprocess, "run", fake_run)


_EMPTY = _Completed(0, json.dumps({"entities": []}))
_HIT = _Completed(0, json.dumps({"entities": [{"entity_id": "ent_x"}]}))


@pytest.mark.parametrize(
    "failure",
    [
        _Completed(1, ""),
        _Completed(0, ""),
        _Completed(0, "not json"),
        _Completed(0, json.dumps({"error": "unauthorized"})),
        subprocess.TimeoutExpired(cmd="neotoma", timeout=15),
        OSError("exec failed"),
    ],
    ids=["nonzero-exit", "empty-output", "unparseable", "no-entities-key", "timeout", "exception"],
)
def test_failed_lookup_is_unknown_not_new(monkeypatch, failure) -> None:
    _lookup_returning(monkeypatch, failure)
    assert iad.import_status("memo.m4a") == iad.LOOKUP_UNKNOWN
    assert iad.already_imported("memo.m4a") is False


def test_missing_cli_is_unknown_not_new(monkeypatch) -> None:
    monkeypatch.setattr(iad.shutil, "which", lambda _name: None)
    assert iad.import_status("memo.m4a") == iad.LOOKUP_UNKNOWN


def test_clean_not_found_is_new(monkeypatch) -> None:
    _lookup_returning(monkeypatch, _EMPTY)
    assert iad.import_status("memo.m4a") == iad.NOT_IMPORTED


def test_one_failed_leg_with_one_clean_miss_is_unknown(monkeypatch) -> None:
    _lookup_returning(monkeypatch, _EMPTY, _Completed(1, ""))
    assert iad.import_status("memo.m4a") == iad.LOOKUP_UNKNOWN


def test_match_is_imported(monkeypatch) -> None:
    _lookup_returning(monkeypatch, _HIT)
    assert iad.import_status("memo.m4a") == iad.IMPORTED
    assert iad.already_imported("memo.m4a") is True


def test_fallback_match_wins_over_failed_first_leg(monkeypatch) -> None:
    _lookup_returning(monkeypatch, _Completed(1, ""), _HIT)
    assert iad.import_status("memo.m4a") == iad.IMPORTED


# --------------------------------------------------------------------------
# End-to-end through main(): staging behaviour.
# --------------------------------------------------------------------------


def _make_library(directory: Path, count: int, prefix: str = "backlog") -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    for i in range(count):
        f = directory / f"{prefix}-{i:04d}.m4a"
        f.write_bytes(b"\0" * (16 + i % 7))
        files.append(f)
    return files


def _wire_main(monkeypatch, tmp_path: Path, status: str):
    recordings = tmp_path / "Recordings"
    staging = tmp_path / "staging"
    monkeypatch.setattr(iad, "VOICE_MEMOS_PATH", recordings)
    monkeypatch.setattr(iad, "AUDIO_IMPORTS_DIR", staging)
    monkeypatch.setattr(iad, "import_status", lambda _name: status)
    transcribed: list[Path] = []

    def fake_transcribe(audio_file, language=None, original_source_file=None):
        transcribed.append(audio_file)
        return {"success": True, "output": ""}

    monkeypatch.setattr(iad, "transcribe_file", fake_transcribe)
    return recordings, staging, transcribed


def _run_main(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["import_audio_from_desktop.py", *argv])
    try:
        iad.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_two_new_memos_with_large_unstored_backlog_stage_only_the_two(
    monkeypatch, tmp_path
) -> None:
    recordings, staging, transcribed = _wire_main(monkeypatch, tmp_path, iad.NOT_IMPORTED)
    _make_library(recordings, 300)  # unstored backlog
    new = _make_library(recordings, 2, prefix="new")

    rc = _run_main(
        monkeypatch,
        ["--no-analyze", "--file", str(new[0]), "--file", str(new[1])],
    )

    assert rc == 0
    staged = sorted(p.name for p in staging.iterdir())
    assert len(staged) == 2
    assert all(name.endswith(("_new-0000.m4a", "_new-0001.m4a")) for name in staged)
    assert len(transcribed) == 2
    # Voice Memos originals are copied, never moved.
    assert all(f.exists() for f in new)


def test_unknown_lookup_stages_nothing_and_signals_retry(monkeypatch, tmp_path) -> None:
    recordings, staging, transcribed = _wire_main(monkeypatch, tmp_path, iad.LOOKUP_UNKNOWN)
    _make_library(recordings, 5)

    rc = _run_main(monkeypatch, ["--no-analyze", "--source", str(recordings)])

    assert rc == iad.EXIT_LOOKUP_UNAVAILABLE
    assert not staging.exists() or not any(staging.iterdir())
    assert transcribed == []


def test_unknown_lookup_count_is_reported(monkeypatch, tmp_path, capsys) -> None:
    recordings, staging, _ = _wire_main(monkeypatch, tmp_path, iad.LOOKUP_UNKNOWN)
    _make_library(recordings, 3)
    _run_main(monkeypatch, ["--no-analyze", "--source", str(recordings)])
    assert "Skipping 3 file(s) this run" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Reuse an existing staged copy instead of copying again.
# --------------------------------------------------------------------------


def test_existing_staged_copy_is_reused(tmp_path) -> None:
    source = _make_library(tmp_path / "Recordings", 1, prefix="memo")[0]
    staging = tmp_path / "staging"
    staging.mkdir()
    prior = staging / f"2026-01-01-000000_{source.name}"
    prior.write_bytes(source.read_bytes())

    result = iad.import_audio_file(source, staging, copy=True)

    assert result == prior
    assert len(list(staging.iterdir())) == 1


def test_staged_copy_of_different_size_is_not_reused(tmp_path) -> None:
    source = _make_library(tmp_path / "Recordings", 1, prefix="memo")[0]
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / f"2026-01-01-000000_{source.name}").write_bytes(b"truncated")

    result = iad.import_audio_file(source, staging, copy=True)

    assert result.read_bytes() == source.read_bytes()
    assert len(list(staging.iterdir())) == 2


def test_evicted_staged_copy_is_not_reused(tmp_path, monkeypatch) -> None:
    source = _make_library(tmp_path / "Recordings", 1, prefix="memo")[0]
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / f"2026-01-01-000000_{source.name}").write_bytes(source.read_bytes())
    monkeypatch.setattr(iad, "is_cloud_evicted", lambda _st: True)

    iad.import_audio_file(source, staging, copy=True)

    assert len(list(staging.iterdir())) == 2


def test_staged_copy_of_a_different_original_is_not_reused(tmp_path) -> None:
    source = _make_library(tmp_path / "Recordings", 1, prefix="memo")[0]
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / f"2026-01-01-000000_other-{source.name}").write_bytes(source.read_bytes())

    iad.import_audio_file(source, staging, copy=True)

    assert len(list(staging.iterdir())) == 2


def test_repeated_run_over_same_memos_does_not_copy_again(monkeypatch, tmp_path) -> None:
    recordings, staging, _ = _wire_main(monkeypatch, tmp_path, iad.NOT_IMPORTED)
    # Each run gets a distinct timestamp, so a second copy would not simply
    # overwrite the first under the same name.
    from datetime import datetime as _real_datetime, timedelta

    ticks = iter(range(1000))

    class _Clock:
        @staticmethod
        def now():
            return _real_datetime(2026, 1, 1) + timedelta(minutes=next(ticks))

    monkeypatch.setattr(iad, "datetime", _Clock)
    new = _make_library(recordings, 2, prefix="new")
    argv = ["--no-analyze", "--file", str(new[0]), "--file", str(new[1])]
    assert _run_main(monkeypatch, argv) == 0
    assert _run_main(monkeypatch, argv) == 0
    assert len(list(staging.iterdir())) == 2
