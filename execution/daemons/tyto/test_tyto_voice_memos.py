"""
Effect tests for Tyto's RecordingWatcher — macOS Voice Memos support.

Covers the two things that had to become per-watcher for memos to work at all:
the file-matching predicate (memo filenames carry no remote/system track name,
so the Audio Hijack predicate rejected every one of them) and the mic-pair
lookup (a memo is always single-file and must never take the two-file merge
path). Plus the backlog guard, which is the requirement that matters most:
the real Voice Memos directory holds hundreds of archived memos and a naive
first poll would fire one transcription and one notification per file.

Every test asserts an observable effect — which paths were handed to
_handle_recording, with what mic argument — never merely "no exception".
No real transcription is ever run: _handle_recording is patched throughout.

All fixture filenames are invented. No real memo names or content appear here.

Run with: pytest execution/daemons/tyto/test_tyto_voice_memos.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# tyto.py reads env at import time and pulls in lib.notify; neither performs I/O
# at module load, so a plain import is safe.
import tyto  # noqa: E402

_verify_runtime = tyto._verify_transcription_cli_runtime


@pytest.fixture(autouse=True)
def _isolated_signing_identity(monkeypatch):
    """Never read the operator's signing key during watcher tests."""
    monkeypatch.setattr(
        tyto, "agent_identity",
        lambda _name: {"key": "/fixture/tyto.jwk.json", "sub": "tyto@ateles-swarm", "kid": "fixture"},
        raising=False,
    )
    monkeypatch.setattr(tyto, "_verify_transcription_cli_runtime", lambda _env: None)


def test_unsupported_signer_stops_before_transcription(tmp_path, monkeypatch):
    runtime = tmp_path / "dist" / "cli"
    runtime.mkdir(parents=True)
    (runtime / "bootstrap.js").write_text("// synthetic CLI")
    (runtime / "aauth_signer.js").write_text("export const oldSigner = true;")
    monkeypatch.setattr(tyto, "NEOTOMA_RC_DIR", str(tmp_path))
    monkeypatch.setattr(tyto, "_verify_transcription_cli_runtime", _verify_runtime)
    watcher = _make_watcher(tmp_path, capture_method="voice_memo", paired=False)
    with pytest.raises(RuntimeError, match="supporting per-agent key paths"):
        watcher._run_transcription(_write(tmp_path, "memo.m4a"), None)


def test_deployed_signer_selects_synthetic_daemon_key(tmp_path):
    """Exercise the installed signer selected by Tyto, with no real credentials."""
    signer = Path(tyto.NEOTOMA_RC_DIR) / "dist" / "cli" / "aauth_signer.js"
    if not signer.is_file():
        pytest.skip("deployed Neotoma runtime not installed")
    shared = tmp_path / ".neotoma" / "aauth"
    shared.mkdir(parents=True)
    (shared / "private.jwk").write_text(json.dumps({"fixture": "shared"}))
    daemon = tmp_path / "daemon.jwk.json"
    daemon.write_text(json.dumps({"fixture": "daemon"}))
    env = dict(os.environ, NEOTOMA_AAUTH_PRIVATE_JWK_PATH=str(daemon))
    _verify_runtime(dict(env, NEOTOMA_CLI_SCRIPT=str(signer.with_name("bootstrap.js"))))
    check = """
        const os = await import('node:os');
        os.default.homedir = () => process.argv[2];
        const { pathToFileURL } = await import('node:url');
        const signer = await import(pathToFileURL(process.argv[1]).href);
        const config = await signer.loadCliSignerConfig();
        process.exit(config.privateJwk.fixture === 'daemon' ? 0 : 1);
    """
    completed = tyto.subprocess.run(
        [env.get("NODE_BIN", "node"), "--input-type=module", "-e", check, str(signer), str(tmp_path)],
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stderr



def _fake_verified_cli(completed):
    """Synthetic child transport supplies the private receipt protocol."""
    def run(command, **kwargs):
        audio = Path(command[2])
        mic = Path(command[command.index('--mic-file') + 1]) if '--mic-file' in command else None
        receipt = {
            'version': 1, 'entity_id': 'ent_test_auto' if 'ent_test_auto' in completed.stdout else (
                'ent_test_local' if 'ent_test_local' in completed.stdout else 'ent_test'),
            'identity': tyto.RecordingWatcher._source_receipt_identity(audio, mic),
            'capture_method': command[command.index('--capture-method') + 1],
            'audio_source_id': 'src_synthetic', 'audio_attachment_verified': True,
            'engine': 'local_whisper_cpp',
        }
        Path(command[command.index('--receipt-file') + 1]).write_text(json.dumps(receipt))
        return completed
    return run

def test_transcription_uses_daemon_identity_instead_of_shared_cli_default(tmp_path):
    watcher = _make_watcher(tmp_path, capture_method="voice_memo", paired=False)
    memo = _write(tmp_path, "memo.m4a")
    completed = MagicMock(returncode=0, stdout="NEOTOMA_TRANSCRIPTION_ENTITY_ID=ent_test\n", stderr="")
    with patch.object(tyto.subprocess, "run", side_effect=_fake_verified_cli(completed)) as run:
        watcher._run_transcription(memo, None)
    env = run.call_args.kwargs["env"]
    assert env["NEOTOMA_AAUTH_SUB"] == "tyto@ateles-swarm"
    assert env["NEOTOMA_AAUTH_PRIVATE_JWK_PATH"] == "/fixture/tyto.jwk.json"
    assert env["NEOTOMA_AAUTH_KID"] == "fixture"
    assert env["NEOTOMA_CLI_SCRIPT"] == str(Path(tyto.NEOTOMA_RC_DIR) / "dist" / "cli" / "bootstrap.js")


@pytest.mark.parametrize("identity", [None, {"key": "/fixture/other.jwk.json", "sub": "other@ateles-swarm", "kid": "other"}])
def test_missing_or_mismatched_identity_does_not_run_transcription(tmp_path, monkeypatch, identity):
    monkeypatch.setattr(tyto, "agent_identity", lambda _name: identity)
    watcher = _make_watcher(tmp_path, capture_method="voice_memo", paired=False)
    with patch.object(tyto.subprocess, "run") as run:
        with pytest.raises(RuntimeError, match="Tyto transcription requires"):
            watcher._run_transcription(_write(tmp_path, "memo.m4a"), None)
    run.assert_not_called()


def test_qta_default_processes_fresh_arrival_but_preserves_archive_guard(tmp_path):
    assert tyto.VOICE_MEMO_INCLUDE_QTA is True
    extensions = {".m4a", ".wav"} | ({".qta"} if tyto.VOICE_MEMO_INCLUDE_QTA else set())
    old = _write(tmp_path, "archive.qta", age_secs=OLD)
    watcher = _make_watcher(tmp_path, paired=False, extensions=extensions, seed_existing=True, max_age_secs=3600)
    fresh = _write(tmp_path, "arrival.qta", age_secs=FRESH)
    _settled(watcher)
    assert [p for p, _ in _poll(watcher)] == [fresh]
    assert old in watcher._transcribed

# Ages, in seconds, used to place fixture files either side of the age window.
OLD = 400 * 24 * 3600  # ~13 months — squarely "archive"
FRESH = 30


def _write(dir_path: Path, name: str, *, age_secs: float = 0.0, size: int = 2048) -> Path:
    """Create a fixture file with a controlled mtime."""
    path = dir_path / name
    path.write_bytes(b"\0" * size)
    if age_secs:
        when = time.time() - age_secs
        os.utime(path, (when, when))
    return path


def _make_watcher(watch_dir: Path, **kwargs) -> tyto.RecordingWatcher:
    return tyto.RecordingWatcher(watch_dir, MagicMock(), **kwargs)


def _settled(watcher: tyto.RecordingWatcher) -> None:
    """Pre-seed _seen so files count as settled on the very next poll.

    _is_settled requires an unchanged mtime across two sightings plus
    SETTLE_SECS of age. Recording the current mtime here collapses that to one
    poll without touching the settling logic under test elsewhere.
    """
    for path in watcher._dir.iterdir():
        if path.is_file():
            stat = path.stat()
            watcher._seen[path] = (stat.st_mtime_ns, stat.st_size)


def _poll(watcher: tyto.RecordingWatcher) -> list[tuple[Path, Path | None]]:
    """Run one poll with _handle_recording patched; return its call args."""
    calls: list[tuple[Path, Path | None]] = []

    async def _record(remote_path, mic_path):
        calls.append((remote_path, mic_path))
        return "success", None

    with patch.object(watcher, "_handle_recording", side_effect=_record):
        asyncio.run(watcher.poll_once())
    return calls


# ── Memo files are now picked up ─────────────────────────────────────────────


def test_memo_filename_is_picked_up(tmp_path):
    """A Voice Memos-style name (no 'remote'/'system' token) is transcribed.

    This is the core change: under the old predicate this file was skipped.
    """
    memo = _write(tmp_path, "20260907 193532-AAAA1111.m4a", age_secs=FRESH)
    watcher = _make_watcher(
        tmp_path, capture_method="voice_memo", paired=False,
        extensions={".m4a", ".wav"}, max_age_secs=3600,
    )
    _settled(watcher)

    calls = _poll(watcher)

    assert [c[0] for c in calls] == [memo]


def test_memo_carries_voice_memo_capture_method(tmp_path):
    """The memo watcher stamps a capture_method distinct from both others."""
    watcher = _make_watcher(tmp_path, capture_method="voice_memo", paired=False)
    assert watcher._capture_method == "voice_memo"
    assert watcher._capture_method not in ("audio_hijack_system", "platform_native")


def test_old_predicate_would_have_rejected_the_memo(tmp_path):
    """Documents the blocker: the paired predicate rejects memo filenames.

    Guards against someone 'simplifying' the memo watcher back onto the paired
    predicate, which would silently stop picking memos up again.
    """
    memo = _write(tmp_path, "20260907 193532-AAAA1111.m4a")
    paired = _make_watcher(tmp_path)  # defaults: paired=True

    assert paired._is_remote_file(memo) is False
    assert paired._is_eligible_file(memo) is False


# ── Backlog guard ────────────────────────────────────────────────────────────


def test_backlog_of_old_memos_is_never_transcribed(tmp_path):
    """A pre-populated archive fires zero transcriptions on the first poll.

    The headline requirement: the real directory holds hundreds of files.
    """
    for i in range(25):
        _write(tmp_path, f"2023010{i % 9} 12000{i % 9}-OLD{i:04d}.m4a", age_secs=OLD)

    watcher = _make_watcher(
        tmp_path, capture_method="voice_memo", paired=False,
        extensions={".m4a", ".wav"}, max_age_secs=3600, seed_existing=True,
    )
    _settled(watcher)

    assert _poll(watcher) == []


def test_startup_seeding_marks_existing_files_handled(tmp_path):
    """seed_existing records the whole archive as already-transcribed."""
    for i in range(5):
        _write(tmp_path, f"20230101 12000{i}-OLD{i:04d}.m4a", age_secs=OLD)

    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a"}, seed_existing=True,
    )

    assert len(watcher._transcribed) == 5


def test_seeding_holds_even_when_an_archived_file_is_touched(tmp_path):
    """Seeding is the second half of the guard, independent of mtime.

    An iCloud sync or a Finder copy can refresh an archived memo's mtime,
    which would carry it into the age window. Seeding still holds it back.
    """
    old = _write(tmp_path, "20230101 120000-OLD0001.m4a", age_secs=OLD)
    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a"},
        max_age_secs=3600, seed_existing=True,
    )
    now = time.time()
    os.utime(old, (now, now))  # archive file suddenly looks fresh
    _settled(watcher)

    assert _poll(watcher) == []


def test_age_window_holds_back_a_file_that_appears_later_bearing_an_old_mtime(tmp_path):
    """The age window is the first half, independent of seeding.

    A memo synced down after startup is not covered by seeding, but arrives
    with its original (old) mtime — the window catches it.
    """
    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a"},
        max_age_secs=3600, seed_existing=True,
    )
    _write(tmp_path, "20230101 120000-SYNCED001.m4a", age_secs=OLD)  # after startup
    _settled(watcher)

    assert _poll(watcher) == []


def test_a_new_memo_after_startup_is_still_transcribed(tmp_path):
    """The guard must not be so strict it blocks the feature itself."""
    for i in range(5):
        _write(tmp_path, f"20230101 12000{i}-OLD{i:04d}.m4a", age_secs=OLD)

    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a"},
        max_age_secs=3600, seed_existing=True,
    )
    fresh = _write(tmp_path, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    _settled(watcher)

    assert [c[0] for c in _poll(watcher)] == [fresh]


def test_multiple_new_memos_after_startup_are_all_transcribed(tmp_path):
    """One poll processes every settled arrival, not only the newest memo."""
    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a", ".wav"},
        max_age_secs=3600, seed_existing=True,
    )
    first = _write(tmp_path, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    second = _write(tmp_path, "20260908 101501-NEW0002.wav", age_secs=FRESH)
    _settled(watcher)

    assert [call[0] for call in _poll(watcher)] == [first, second]


def test_bounded_watcher_processes_backlog_fairly_across_polls(tmp_path):
    """A meeting backlog may progress, but one watcher cannot monopolize a poll."""
    backlog = [
        _write(tmp_path, f"20260908 {index:04d} system.m4a", age_secs=FRESH)
        for index in range(110)
    ]
    watcher = _make_watcher(tmp_path, max_files_per_poll=1)
    _settled(watcher)

    polls = [_poll(watcher) for _ in backlog]

    assert all(len(poll) == 1 for poll in polls)
    assert {poll[0][0] for poll in polls} == set(backlog)


def test_failed_backlog_item_is_retried_without_blocking_later_items(tmp_path):
    """A bounded watcher defers a failure, advances, then retries the failure."""
    first = _write(tmp_path, "20260908 1015 system.m4a", age_secs=FRESH)
    second = _write(tmp_path, "20260908 1016 system.m4a", age_secs=FRESH)
    watcher = _make_watcher(tmp_path, max_files_per_poll=1, retry_secs=300)
    _settled(watcher)

    calls = []

    async def fail_first(remote_path, mic_path):
        calls.append((remote_path, mic_path))
        return "transient", None

    with patch.object(watcher, "_handle_recording", side_effect=fail_first):
        asyncio.run(watcher.poll_once())

    assert [call[0] for call in calls] == [first]
    assert first in watcher._pending_retries
    assert first not in watcher._transcribed

    assert [call[0] for call in _poll(watcher)] == [second]
    watcher._pending_retries[first] = 0
    assert [call[0] for call in _poll(watcher)] == [first]


def test_hung_meeting_watcher_does_not_starve_voice_memo_poll():
    """Each source needs an independent loop so a provider hang stays isolated."""
    async def scenario():
        never = asyncio.Event()
        meeting_started = asyncio.Event()
        memo_polled = asyncio.Event()

        async def hung_meeting():
            meeting_started.set()
            await never.wait()

        async def poll_memos():
            memo_polled.set()

        meeting_task = asyncio.create_task(
            tyto._poll_watcher_forever("meetings", hung_meeting, interval=0)
        )
        memo_task = asyncio.create_task(
            tyto._poll_watcher_forever("voice_memos", poll_memos, interval=0.01)
        )
        try:
            await asyncio.wait_for(meeting_started.wait(), timeout=0.2)
            await asyncio.wait_for(memo_polled.wait(), timeout=0.2)
        finally:
            meeting_task.cancel()
            memo_task.cancel()
            await asyncio.gather(meeting_task, memo_task, return_exceptions=True)

    asyncio.run(scenario())


def test_max_age_zero_disables_the_window_only(tmp_path):
    """max_age_secs=0 means no age limit — the meeting watchers' behavior."""
    old = _write(tmp_path, "20230101 120000-OLD0001.m4a", age_secs=OLD)
    watcher = _make_watcher(tmp_path, paired=False, extensions={".m4a"}, max_age_secs=0)
    _settled(watcher)

    assert [c[0] for c in _poll(watcher)] == [old]


# ── Sidecars and extensions ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "20260907 193532-AAAA1111.waveform",
        "20260907 193532-AAAA1111.composition",
        "CloudRecordings.db",
        "CloudRecordings.db-wal",
        "CloudRecordings.db-shm",
        "CloudRecordings_ckAssets",
    ],
)
def test_sidecar_files_are_never_treated_as_recordings(tmp_path, name):
    memo = _write(tmp_path, "20260907 193532-AAAA1111.m4a", age_secs=FRESH)
    _write(tmp_path, name, age_secs=FRESH)

    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a", ".wav"}, max_age_secs=3600,
    )
    _settled(watcher)

    assert [c[0] for c in _poll(watcher)] == [memo]


def test_sidecar_directories_are_skipped(tmp_path):
    """CaptureRecovery / Capture are directories, not files."""
    (tmp_path / "CaptureRecovery").mkdir()
    (tmp_path / "Capture").mkdir()
    memo = _write(tmp_path, "20260907 193532-AAAA1111.m4a", age_secs=FRESH)

    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a"}, max_age_secs=3600,
    )
    _settled(watcher)

    assert [c[0] for c in _poll(watcher)] == [memo]


def test_qta_excluded_by_default_included_when_opted_in(tmp_path):
    qta = _write(tmp_path, "20230101 120000-QTA00001.qta", age_secs=FRESH)

    off = _make_watcher(tmp_path, paired=False, extensions={".m4a", ".wav"})
    assert off._is_eligible_file(qta) is False

    on = _make_watcher(tmp_path, paired=False, extensions={".m4a", ".wav", ".qta"})
    assert on._is_eligible_file(qta) is True


# ── Memos never take the two-file merge path ─────────────────────────────────


def test_memo_is_never_paired_with_a_mic_file(tmp_path):
    """mic_path is always None for an unpaired watcher.

    Even with a file literally named 'mic' sitting in the directory — the
    merge path must not be reachable for a single-speaker memo.
    """
    memo = _write(tmp_path, "20260907 193532-AAAA1111.m4a", age_secs=FRESH)
    _write(tmp_path, "20260907 193532 mic.m4a", age_secs=FRESH)

    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a"}, max_age_secs=3600,
    )
    _settled(watcher)
    calls = _poll(watcher)

    assert all(mic is None for _, mic in calls)
    assert memo in [c[0] for c in calls]


def test_unpaired_watcher_never_calls_find_mic_pair(tmp_path):
    _write(tmp_path, "20260907 193532-AAAA1111.m4a", age_secs=FRESH)
    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a"}, max_age_secs=3600,
    )
    _settled(watcher)

    with patch.object(watcher, "_find_mic_pair") as find_pair:
        _poll(watcher)

    find_pair.assert_not_called()


# ── Regression: the meeting-recording watchers must be unchanged ─────────────


def test_meeting_watcher_defaults_are_unchanged(tmp_path):
    """Constructed the old way, the watcher behaves exactly as before."""
    watcher = _make_watcher(tmp_path)

    assert watcher._paired is True
    assert watcher._extensions == {e.lower() for e in watcher.RECORDING_EXTENSIONS}
    assert watcher._max_age_secs == 0
    assert watcher._capture_method == "audio_hijack_system"
    assert watcher._transcribed == set()  # no seeding by default


def test_meeting_watcher_still_merges_the_mic_pair(tmp_path):
    remote = _write(tmp_path, "20260907 1935 remote.mp4", age_secs=FRESH)
    mic = _write(tmp_path, "20260907 1935 mic.mp4", age_secs=FRESH)

    watcher = _make_watcher(tmp_path)
    _settled(watcher)

    assert _poll(watcher) == [(remote, mic)]


def test_meeting_watcher_still_ignores_the_mic_track_alone(tmp_path):
    _write(tmp_path, "20260907 1935 mic.mp4", age_secs=FRESH)
    watcher = _make_watcher(tmp_path)
    _settled(watcher)

    assert _poll(watcher) == []


def test_meeting_watcher_still_handles_the_system_track_name(tmp_path):
    system = _write(tmp_path, "20260907 1935 system.m4a", age_secs=FRESH)
    watcher = _make_watcher(tmp_path)
    _settled(watcher)

    assert [c[0] for c in _poll(watcher)] == [system]


def test_meeting_watcher_transcribes_an_old_recording(tmp_path):
    """No age window is applied to the meeting watchers — an old file still runs.

    A recording left settling across a daemon restart must not be dropped.
    """
    old = _write(tmp_path, "20230101 1200 remote.mp4", age_secs=OLD)
    watcher = _make_watcher(tmp_path)
    _settled(watcher)

    assert [c[0] for c in _poll(watcher)] == [old]


def test_meeting_watcher_falls_back_to_remote_only_without_a_mic_file(tmp_path):
    remote = _write(tmp_path, "20260907 1935 remote.mp4", age_secs=FRESH)
    watcher = _make_watcher(tmp_path)
    _settled(watcher)

    assert _poll(watcher) == [(remote, None)]


# ── Misc watcher invariants ──────────────────────────────────────────────────


def test_missing_directory_is_tolerated(tmp_path):
    """An absent Voice Memos dir warns and retries rather than crashing."""
    missing = tmp_path / "does-not-exist"
    watcher = _make_watcher(missing, paired=False, extensions={".m4a"}, seed_existing=True)

    assert _poll(watcher) == []


def test_empty_directory_is_a_quiet_success(tmp_path):
    """An existing empty watch directory produces no work and no error."""
    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a", ".wav"},
        max_age_secs=3600, seed_existing=True,
    )

    assert _poll(watcher) == []


def test_a_memo_is_transcribed_only_once(tmp_path):
    memo = _write(tmp_path, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a"}, max_age_secs=3600,
    )
    _settled(watcher)

    first = _poll(watcher)
    second = _poll(watcher)

    assert [c[0] for c in first] == [memo]
    assert second == []


def test_failed_memo_is_retried_and_cleared_only_after_success(tmp_path):
    """A backend failure stays eligible instead of becoming handled."""
    memo_dir = tmp_path / "memos"
    memo_dir.mkdir()
    state_path = tmp_path / "retry.json"
    watcher = _make_watcher(
        memo_dir,
        capture_method="voice_memo",
        paired=False,
        extensions={".m4a"},
        max_age_secs=3600,
        seed_existing=True,
        retry_state_path=state_path,
        retry_secs=0,
    )
    memo = _write(memo_dir, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    _settled(watcher)
    outcomes = iter((("transient", None), ("success", None)))
    calls = []

    async def _record(remote_path, mic_path):
        calls.append((remote_path, mic_path))
        return next(outcomes)

    with patch.object(watcher, "_handle_recording", side_effect=_record):
        asyncio.run(watcher.poll_once())
        assert memo not in watcher._transcribed
        assert memo in watcher._pending_retries
        assert json.loads(state_path.read_text())["pending"][0]["path"] == str(memo)

        asyncio.run(watcher.poll_once())

    assert calls == [(memo, None), (memo, None)]
    assert memo in watcher._transcribed
    assert watcher._pending_retries == {}
    assert json.loads(state_path.read_text())["pending"] == []


# ── Permanent-failure retry cap (ateles#1083) ────────────────────────────────
#
# A deterministic rejection like ERR_FILE_PATH_IS_SERVER_LOCAL cannot be fixed
# by retrying — the call shape is wrong every time. Retrying it anyway is what
# produced 22,878 error-log lines and a Telegram 429 that hid the failure
# behind a ~3.7 hour rate limit, silently starving every other daemon's
# alerts on the same channel. These tests plant that exact permanent
# rejection and assert the memo is hard-failed once, journaled durably, and
# never retried again — with exactly one notification, not one per poll.


def test_permanent_store_error_is_classified_and_not_transient(tmp_path):
    """_classify_store_error recognizes the exact rejection from ateles#1083."""
    exc = RuntimeError(
        'Neotoma store failed: {"error_code":"ERR_FILE_PATH_IS_SERVER_LOCAL",'
        '"message":"ERR_FILE_PATH_IS_SERVER_LOCAL: \'file_path\' is resolved '
        'on the server\'s filesystem"}'
    )
    assert tyto._classify_store_error(exc) == "ERR_FILE_PATH_IS_SERVER_LOCAL"
    assert tyto._classify_store_error(RuntimeError("connection reset")) is None


def test_handle_recording_returns_permanent_for_the_server_local_path_error(tmp_path):
    """The real call path classifies this exact error as permanent, not transient."""
    memo = _write(tmp_path, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    watcher = _make_watcher(tmp_path, capture_method="voice_memo", paired=False)
    failed = MagicMock(
        returncode=1,
        stdout="TRANSCRIPTION_BACKEND_SELECTED=local\n",
        stderr=(
            'Error transcribing audio: Neotoma store failed: '
            '{"error_code":"ERR_FILE_PATH_IS_SERVER_LOCAL",'
            '"message":"ERR_FILE_PATH_IS_SERVER_LOCAL: '
            "'file_path' is resolved on the server's filesystem\"}"
        ),
    )

    with patch.object(tyto.subprocess, "run", return_value=failed):
        outcome, error_code = asyncio.run(watcher._handle_recording(memo, None))

    assert outcome == "permanent"
    assert error_code == "ERR_FILE_PATH_IS_SERVER_LOCAL"


def test_permanent_failure_is_hard_failed_once_and_never_retried(tmp_path):
    """A planted permanent rejection must not become an unbounded retry loop."""
    memo_dir = tmp_path / "memos"
    memo_dir.mkdir()
    state_path = tmp_path / "retry.json"
    watcher = _make_watcher(
        memo_dir,
        capture_method="voice_memo",
        paired=False,
        extensions={".m4a"},
        max_age_secs=3600,
        seed_existing=True,
        retry_state_path=state_path,
        retry_secs=0,  # would immediately retry a transient failure
    )
    memo = _write(memo_dir, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    _settled(watcher)

    calls = []

    async def _permanent(remote_path, mic_path):
        calls.append((remote_path, mic_path))
        return "permanent", "ERR_FILE_PATH_IS_SERVER_LOCAL"

    with patch.object(watcher, "_handle_recording", side_effect=_permanent):
        asyncio.run(watcher.poll_once())
        asyncio.run(watcher.poll_once())
        asyncio.run(watcher.poll_once())

    # Exactly one attempt across three polls — the retry cap, not the backoff
    # window, is what stopped it (retry_secs=0 would otherwise permit an
    # attempt on every single poll).
    assert calls == [(memo, None)]
    assert memo in watcher._hard_failed
    assert watcher._hard_failed[memo]["error_code"] == "ERR_FILE_PATH_IS_SERVER_LOCAL"
    assert memo not in watcher._pending_retries
    assert memo not in watcher._transcribed

    journal = json.loads(state_path.read_text())
    assert journal["version"] == 2
    assert journal["pending"] == []
    assert len(journal["hard_failed"]) == 1
    assert journal["hard_failed"][0]["path"] == str(memo)
    assert journal["hard_failed"][0]["error_code"] == "ERR_FILE_PATH_IS_SERVER_LOCAL"

    # Exactly one BLOCKER notification for the permanent failure, not one per
    # poll (the 22k-error/429 storm this guards against).
    blocker_messages = [
        call.args[0]
        for call in watcher._notifier.send.call_args_list
        if call.kwargs.get("priority") == tyto.Priority.BLOCKER
    ]
    permanent_messages = [m for m in blocker_messages if "permanently failed" in m]
    assert len(permanent_messages) == 1


def test_hard_failed_journal_survives_a_restart(tmp_path):
    """A permanent failure must not be re-attempted after the daemon restarts."""
    memo_dir = tmp_path / "memos"
    memo_dir.mkdir()
    state_path = tmp_path / "retry.json"
    first = _make_watcher(
        memo_dir,
        capture_method="voice_memo",
        paired=False,
        extensions={".m4a"},
        max_age_secs=3600,
        seed_existing=True,
        retry_state_path=state_path,
        retry_secs=0,
    )
    memo = _write(memo_dir, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    _settled(first)

    async def _permanent(_remote_path, _mic_path):
        return "permanent", "ERR_FILE_PATH_IS_SERVER_LOCAL"

    with patch.object(first, "_handle_recording", side_effect=_permanent):
        asyncio.run(first.poll_once())

    restarted = _make_watcher(
        memo_dir,
        capture_method="voice_memo",
        paired=False,
        extensions={".m4a"},
        max_age_secs=3600,
        seed_existing=True,
        retry_state_path=state_path,
        retry_secs=0,
    )
    assert memo in restarted._hard_failed

    calls = _poll(restarted)

    assert calls == []  # never handed to _handle_recording again


def test_v1_journal_loads_with_no_hard_failed_entries(tmp_path):
    """A journal written before this change (no hard_failed key) must still load."""
    memo_dir = tmp_path / "memos"
    memo_dir.mkdir()
    memo = _write(memo_dir, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    state_path = tmp_path / "retry.json"
    state_path.write_text(json.dumps({
        "version": 1,
        "pending": [{"path": str(memo), "retry_after": 0}],
    }))

    watcher = _make_watcher(
        memo_dir,
        capture_method="voice_memo",
        paired=False,
        extensions={".m4a"},
        max_age_secs=3600,
        seed_existing=True,
        retry_state_path=state_path,
    )

    assert watcher._retry_state_available is True
    assert memo in watcher._pending_retries
    assert watcher._hard_failed == {}


def test_startup_seeding_preserves_a_failed_new_memo_retry(tmp_path):
    """Restarting cannot absorb a previously failed arrival into the backlog."""
    memo_dir = tmp_path / "memos"
    memo_dir.mkdir()
    state_path = tmp_path / "retry.json"
    first = _make_watcher(
        memo_dir,
        capture_method="voice_memo",
        paired=False,
        extensions={".m4a"},
        max_age_secs=3600,
        seed_existing=True,
        retry_state_path=state_path,
        retry_secs=0,
    )
    memo = _write(memo_dir, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    _settled(first)

    async def _fail(_remote_path, _mic_path):
        return "transient", None

    with patch.object(first, "_handle_recording", side_effect=_fail):
        asyncio.run(first.poll_once())

    restarted = _make_watcher(
        memo_dir,
        capture_method="voice_memo",
        paired=False,
        extensions={".m4a"},
        max_age_secs=3600,
        seed_existing=True,
        retry_state_path=state_path,
        retry_secs=0,
    )
    _settled(restarted)

    assert memo not in restarted._transcribed
    assert memo in restarted._pending_retries
    assert [call[0] for call in _poll(restarted)] == [memo]
    assert json.loads(state_path.read_text())["pending"] == []


def test_failed_memo_obeys_retry_backoff(tmp_path):
    """A failing backend is retried later without being hammered every poll."""
    memo_dir = tmp_path / "memos"
    memo_dir.mkdir()
    state_path = tmp_path / "retry.json"
    watcher = _make_watcher(
        memo_dir,
        capture_method="voice_memo",
        paired=False,
        extensions={".m4a"},
        max_age_secs=3600,
        seed_existing=True,
        retry_state_path=state_path,
        retry_secs=300,
    )
    memo = _write(memo_dir, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    _settled(watcher)
    calls = []

    async def _fail(remote_path, mic_path):
        calls.append((remote_path, mic_path))
        return "transient", None

    with patch.object(watcher, "_handle_recording", side_effect=_fail):
        asyncio.run(watcher.poll_once())
        asyncio.run(watcher.poll_once())

    assert calls == [(memo, None)]
    assert watcher._pending_retries[memo] > time.time()


def test_corrupt_retry_journal_disables_memo_processing_fail_closed(tmp_path):
    """Unreadable retry state must not let startup seeding hide failed work."""
    memo_dir = tmp_path / "memos"
    memo_dir.mkdir()
    memo = _write(memo_dir, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    state_path = tmp_path / "retry.json"
    state_path.write_text("not valid JSON")

    watcher = _make_watcher(
        memo_dir,
        capture_method="voice_memo",
        paired=False,
        extensions={".m4a"},
        max_age_secs=3600,
        seed_existing=True,
        retry_state_path=state_path,
    )
    _settled(watcher)

    assert watcher._retry_state_available is False
    assert memo not in watcher._transcribed
    assert _poll(watcher) == []


def test_non_object_retry_journal_disables_memo_processing_fail_closed(tmp_path):
    """Valid JSON with the wrong top-level shape must not crash Tyto startup."""
    memo_dir = tmp_path / "memos"
    memo_dir.mkdir()
    memo = _write(memo_dir, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    state_path = tmp_path / "retry.json"
    state_path.write_text("[]")

    watcher = _make_watcher(
        memo_dir,
        capture_method="voice_memo",
        paired=False,
        extensions={".m4a"},
        max_age_secs=3600,
        seed_existing=True,
        retry_state_path=state_path,
    )
    _settled(watcher)

    assert watcher._retry_state_available is False
    assert memo not in watcher._transcribed
    assert _poll(watcher) == []


def test_failed_memo_backoff_starts_after_transcription_finishes(tmp_path):
    """A slow failed attempt must not consume its own retry delay."""
    memo_dir = tmp_path / "memos"
    memo_dir.mkdir()
    state_path = tmp_path / "retry.json"
    watcher = _make_watcher(
        memo_dir,
        capture_method="voice_memo",
        paired=False,
        extensions={".m4a"},
        max_age_secs=3600,
        retry_state_path=state_path,
        retry_secs=1,
    )
    memo = _write(memo_dir, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    _settled(watcher)

    async def _slow_failure(_remote_path, _mic_path):
        await asyncio.sleep(1.1)
        return "transient", None

    with patch.object(watcher, "_handle_recording", side_effect=_slow_failure):
        asyncio.run(watcher.poll_once())

    assert watcher._pending_retries[memo] >= time.time() + 0.8


@pytest.mark.parametrize("channels", [1, 2])
def test_voice_memo_uses_channel_appropriate_local_backend(
    tmp_path, monkeypatch, capsys, channels
):
    """Run the CLI with Tyto's actual arguments and routing environment."""
    from execution.scripts import transcribe_audio as cli

    memo = tmp_path / "20260908 101500-NEW0001.wav"
    with wave.open(str(memo), "wb") as audio:
        audio.setparams((channels, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0" * 16000 * channels * 2)
    original = memo.read_bytes()
    watcher = _make_watcher(tmp_path, capture_method="voice_memo", paired=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "configured-but-not-used")
    monkeypatch.setenv("TRANSCRIBE_BACKEND", "elevenlabs")
    monkeypatch.setenv("RECORD_MEETING_DIARIZE", "0")
    monkeypatch.setenv("TRANSCRIBE_DISABLE_NEOTOMA_VOCAB", "1")
    whisper = MagicMock(return_value={"transcription_text": "Synthetic memo.", "language": "en"})
    fluid = MagicMock(return_value={
        "transcription_text": "[Channel_0/S1] First channel.\n[Channel_1/S1] Second channel.",
        "transcription_engine": "local_fluid_audio",
        "language": "en",
    })
    monkeypatch.setattr(cli, "transcribe_local", whisper)
    monkeypatch.setattr(cli, "transcribe_fluid", fluid)
    monkeypatch.setattr(cli, "get_audio_duration", lambda _path: 1.0)
    cloud = MagicMock(side_effect=AssertionError("Voice Memos must stay local"))
    monkeypatch.setattr(cli, "transcribe_with_elevenlabs_speech_to_text", cloud)
    monkeypatch.setattr(cli, "OpenAI", cloud)

    def save(path, result, *, extra_entity_fields, **_kwargs):
        assert path == memo
        assert extra_entity_fields["capture_method"] == "voice_memo"
        print("NEOTOMA_TRANSCRIPTION_ENTITY_ID=ent_test_local")
        return {**result, "entity_id": "ent_test_local", "audio_source_id": "src_synthetic", "audio_attachment_verified": True}

    monkeypatch.setattr(cli, "save_transcription", save)

    def run_cli(command, *, env, **_kwargs):
        with monkeypatch.context() as child:
            child.setattr(sys, "argv", command[1:])
            for variable in ("TRANSCRIBE_BACKEND", "RECORD_MEETING_DIARIZE"):
                child.setenv(variable, env[variable])
            try:
                cli.main()
                status = 0
            except SystemExit as exc:
                status = exc.code
        captured = capsys.readouterr()
        return tyto.subprocess.CompletedProcess(
            command, status, stdout=captured.out, stderr=captured.err
        )

    with patch.object(tyto.subprocess, "run", side_effect=run_cli):
        assert watcher._run_transcription(memo, None) == "ent_test_local"

    if channels == 2:
        fluid.assert_called_once_with(memo, language=None, verbose=True, channel_count=2)
        whisper.assert_not_called()
    else:
        whisper.assert_called_once_with(memo, language=None, verbose=True)
        fluid.assert_not_called()
    cloud.assert_not_called()
    assert memo.read_bytes() == original


def test_single_file_meeting_requests_local_diarization(tmp_path, monkeypatch):
    """Tyto must not reintroduce key-presence routing beside the shared selector."""
    recording = _write(tmp_path, "20260908 101500 system.m4a", age_secs=FRESH)
    watcher = _make_watcher(tmp_path, capture_method="audio_hijack_system")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "configured")
    monkeypatch.delenv("RECORD_MEETING_DIARIZE", raising=False)
    completed = MagicMock(
        returncode=0,
        stdout="NEOTOMA_TRANSCRIPTION_ENTITY_ID=ent_test_auto\n",
        stderr="",
    )

    with patch.object(tyto.subprocess, "run", side_effect=_fake_verified_cli(completed)) as run:
        assert watcher._run_transcription(recording, None) == "ent_test_auto"

    command = run.call_args.args[0]
    assert "--diarize" in command
    assert "--no-diarize" not in command
    assert "--backend" not in command


def test_failed_meeting_diarization_never_retries_plain_asr(tmp_path, monkeypatch):
    recording = _write(tmp_path, "20260908 101500 system.m4a", age_secs=FRESH)
    watcher = _make_watcher(tmp_path, capture_method="audio_hijack_system")
    monkeypatch.delenv("TRANSCRIBE_BACKEND", raising=False)
    monkeypatch.delenv("RECORD_MEETING_DIARIZE", raising=False)
    completed = MagicMock(returncode=1, stdout="", stderr="local model missing")
    with patch.object(tyto.subprocess, "run", return_value=completed) as run:
        with pytest.raises(RuntimeError, match="local_fluid_audio.*local model missing"):
            watcher._run_transcription(recording, None)
    assert run.call_count == 1
    assert "--diarize" in run.call_args.args[0]


def test_failed_paired_meeting_keeps_both_tracks_for_retry(tmp_path, monkeypatch):
    remote = _write(tmp_path, "20260908 101500 system.m4a", age_secs=FRESH)
    mic = _write(tmp_path, "20260908 101500 mic.m4a", age_secs=FRESH)
    watcher = _make_watcher(tmp_path, capture_method="audio_hijack_system")
    monkeypatch.delenv("TRANSCRIBE_BACKEND", raising=False)
    monkeypatch.delenv("RECORD_MEETING_DIARIZE", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    completed = MagicMock(returncode=1, stdout="", stderr="local model missing")
    with patch.object(tyto.subprocess, "run", return_value=completed) as run:
        with pytest.raises(RuntimeError, match="both tracks retained"):
            watcher._run_transcription(remote, mic)
    assert run.call_count == 1
    assert "--mic-file" in run.call_args.args[0]
    assert str(mic) in run.call_args.args[0]
    assert remote.exists() and mic.exists()


@pytest.mark.parametrize(
    ("env_name", "env_value"),
    [
        ("TRANSCRIBE_BACKEND", "local"),
        ("RECORD_MEETING_DIARIZE", "0"),
    ],
)
def test_paired_meeting_respects_local_backend_overrides(
    tmp_path, monkeypatch, env_name, env_value
):
    """Unsupported overrides fail without discarding either physical track."""
    remote = _write(tmp_path, "20260908 101500 system.m4a", age_secs=FRESH)
    mic = _write(tmp_path, "20260908 101500 mic.m4a", age_secs=FRESH)
    watcher = _make_watcher(tmp_path, capture_method="audio_hijack_system")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "configured")
    monkeypatch.delenv("TRANSCRIBE_BACKEND", raising=False)
    monkeypatch.delenv("RECORD_MEETING_DIARIZE", raising=False)
    monkeypatch.setenv(env_name, env_value)
    completed = MagicMock(
        returncode=0,
        stdout=(
            "TRANSCRIPTION_ENGINE=local_whisper_cpp\n"
            "NEOTOMA_TRANSCRIPTION_ENTITY_ID=ent_test_local\n"
        ),
        stderr="",
    )

    with patch.object(tyto.subprocess, "run", return_value=completed) as run:
        with pytest.raises(RuntimeError, match="both tracks retained"):
            watcher._run_transcription(remote, mic)
    run.assert_not_called()
    assert remote.is_file() and mic.is_file()


def test_completion_notification_names_actual_backend(tmp_path):
    """The operator must be told whether audio stayed local or left-device."""
    memo = _write(tmp_path, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    watcher = _make_watcher(tmp_path, capture_method="voice_memo", paired=False)
    completed = MagicMock(
        returncode=0,
        stdout=(
            "TRANSCRIPTION_ENGINE=local_whisper_cpp\n"
            "NEOTOMA_TRANSCRIPTION_ENTITY_ID=ent_test_local\n"
        ),
        stderr="",
    )

    with patch.object(tyto.subprocess, "run", side_effect=_fake_verified_cli(completed)):
        watcher._run_transcription(memo, None)

    messages = [call.args[0] for call in watcher._notifier.send.call_args_list]
    assert any("backend=local_whisper_cpp" in message for message in messages)


def test_failure_notification_names_attempted_backend(tmp_path):
    """A failed call must still disclose whether it attempted an external service."""
    memo = _write(tmp_path, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    watcher = _make_watcher(tmp_path, capture_method="voice_memo", paired=False)
    failed = MagicMock(
        returncode=1,
        stdout="TRANSCRIPTION_BACKEND_SELECTED=local\n",
        stderr="synthetic failure",
    )

    with patch.object(tyto.subprocess, "run", return_value=failed):
        outcome, error_code = asyncio.run(watcher._handle_recording(memo, None))
    assert outcome == "transient"
    assert error_code is None

    messages = [call.args[0] for call in watcher._notifier.send.call_args_list]
    assert any("backend=local_whisper_cpp" in message for message in messages)


def test_transcription_subprocess_timeout_is_bounded_and_attributed(tmp_path):
    """A hung backend must terminate at Tyto's explicit outer deadline."""
    memo = _write(tmp_path, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    watcher = _make_watcher(tmp_path, capture_method="voice_memo", paired=False)
    expired = tyto.subprocess.TimeoutExpired(
        cmd=["transcribe_audio.py"],
        timeout=tyto.TRANSCRIBE_PROCESS_TIMEOUT_SECS,
        output="TRANSCRIPTION_BACKEND_SELECTED=local\n",
    )

    with patch.object(tyto.subprocess, "run", side_effect=expired) as run:
        with pytest.raises(RuntimeError, match="backend=local_whisper_cpp.*timed out"):
            watcher._run_transcription(memo, None)

    assert run.call_args.kwargs["timeout"] == tyto.TRANSCRIBE_PROCESS_TIMEOUT_SECS


def test_zero_byte_memo_is_not_transcribed(tmp_path):
    """A memo still being written has size 0 and must wait."""
    _write(tmp_path, "20260908 101500-NEW0001.m4a", age_secs=FRESH, size=0)
    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a"}, max_age_secs=3600,
    )
    _settled(watcher)

    assert _poll(watcher) == []


def test_memo_must_stop_growing_before_transcription(tmp_path):
    """Stable mtime alone cannot make a still-growing memo look settled."""
    memo = _write(tmp_path, "20260908 101500-NEW0001.m4a", age_secs=FRESH)
    watcher = _make_watcher(
        tmp_path, paired=False, extensions={".m4a"}, max_age_secs=3600,
    )

    # First sighting records the initial (mtime, size) signature.
    assert _poll(watcher) == []
    original = memo.stat()

    # Simulate a writer that appends content while preserving mtime.  A
    # timestamp-only settling check would transcribe on this poll.
    with memo.open("ab") as handle:
        handle.write(b"more audio")
    os.utime(memo, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert _poll(watcher) == []

    # Once the complete signature is unchanged, the memo is eligible.
    assert [call[0] for call in _poll(watcher)] == [memo]


# ── Per-recording attempt cap (2026-09-25) ───────────────────────────────────
#
# ateles#1084 stopped retries only for failures it could name as permanent.
# Everything else was transient and retried every five minutes without limit,
# so a failure nobody classified (a read-back mismatch, then
# ERR_IDEMPOTENCY_MISMATCH) looped for a week across 128 memos and grew the
# error log to ~575 MB. The attempt cap bounds the failure nobody anticipated.


def _memo_watcher(memo_dir: Path, state_path: Path | None, **kwargs) -> tyto.RecordingWatcher:
    return _make_watcher(
        memo_dir,
        capture_method="voice_memo",
        paired=False,
        extensions={".m4a"},
        max_age_secs=3600,
        seed_existing=True,
        retry_state_path=state_path,
        retry_secs=0,
        **kwargs,
    )


def _always_transient(calls):
    async def _fail(remote_path, mic_path):
        calls.append(remote_path)
        return "transient", None

    return _fail


def _permanent_messages(watcher) -> list[str]:
    return [
        call.args[0]
        for call in watcher._notifier.send.call_args_list
        if "permanently failed" in call.args[0]
    ]


def test_unclassified_failure_stops_at_the_attempt_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(tyto, "RETRY_MAX_ATTEMPTS", 3, raising=False)
    memo_dir = tmp_path / "memos"
    memo_dir.mkdir()
    state_path = tmp_path / "retry.json"
    watcher = _memo_watcher(memo_dir, state_path)
    memo = _write(memo_dir, "20260919 183801-CAP00001.m4a", age_secs=FRESH)
    _settled(watcher)
    calls: list[Path] = []

    with patch.object(watcher, "_handle_recording", side_effect=_always_transient(calls)):
        for _ in range(8):
            asyncio.run(watcher.poll_once())

    assert calls == [memo, memo, memo]
    assert memo in watcher._hard_failed
    assert watcher._hard_failed[memo]["error_code"].startswith("ERR_RETRY_LIMIT_EXCEEDED")
    assert memo not in watcher._pending_retries
    journal = json.loads(state_path.read_text())
    assert journal["pending"] == []
    assert journal["hard_failed"][0]["error_code"].startswith("ERR_RETRY_LIMIT_EXCEEDED")
    assert len(_permanent_messages(watcher)) == 1


def test_attempt_count_survives_a_restart(tmp_path, monkeypatch):
    """A restart must not hand every failing memo a fresh set of attempts."""
    monkeypatch.setattr(tyto, "RETRY_MAX_ATTEMPTS", 3, raising=False)
    memo_dir = tmp_path / "memos"
    memo_dir.mkdir()
    state_path = tmp_path / "retry.json"
    first = _memo_watcher(memo_dir, state_path)
    memo = _write(memo_dir, "20260919 183801-CAP00002.m4a", age_secs=FRESH)
    _settled(first)
    calls: list[Path] = []
    with patch.object(first, "_handle_recording", side_effect=_always_transient(calls)):
        asyncio.run(first.poll_once())
        asyncio.run(first.poll_once())
    assert json.loads(state_path.read_text())["pending"][0]["attempts"] == 2

    restarted = _memo_watcher(memo_dir, state_path)
    _settled(restarted)
    with patch.object(restarted, "_handle_recording", side_effect=_always_transient(calls)):
        for _ in range(4):
            asyncio.run(restarted.poll_once())

    assert calls == [memo, memo, memo]
    assert memo in restarted._hard_failed


def test_journal_entry_without_attempts_loads_as_zero(tmp_path):
    """Journals written before the cap carry no attempts field."""
    memo_dir = tmp_path / "memos"
    memo_dir.mkdir()
    memo = _write(memo_dir, "20260919 183801-CAP00003.m4a", age_secs=FRESH)
    state_path = tmp_path / "retry.json"
    state_path.write_text(json.dumps({
        "version": 2,
        "pending": [{"path": str(memo), "retry_after": 0}],
        "hard_failed": [],
    }))
    watcher = _memo_watcher(memo_dir, state_path)
    assert watcher._retry_state_available is True
    assert memo in watcher._pending_retries
    assert watcher._attempts.get(memo, 0) == 0


def test_success_clears_the_attempt_count(tmp_path, monkeypatch):
    monkeypatch.setattr(tyto, "RETRY_MAX_ATTEMPTS", 3, raising=False)
    memo_dir = tmp_path / "memos"
    memo_dir.mkdir()
    state_path = tmp_path / "retry.json"
    watcher = _memo_watcher(memo_dir, state_path)
    memo = _write(memo_dir, "20260919 183801-CAP00004.m4a", age_secs=FRESH)
    _settled(watcher)
    outcomes = iter((("transient", None), ("transient", None), ("success", None)))

    async def _record(_remote_path, _mic_path):
        return next(outcomes)

    with patch.object(watcher, "_handle_recording", side_effect=_record):
        for _ in range(3):
            asyncio.run(watcher.poll_once())

    assert memo in watcher._transcribed
    assert memo not in watcher._hard_failed
    assert watcher._attempts == {}


def test_retry_delay_doubles_and_is_capped(tmp_path):
    watcher = _make_watcher(tmp_path, retry_secs=300, max_retry_delay_secs=1000)
    assert [watcher._retry_delay(n) for n in (1, 2, 3, 4)] == [300, 600, 1000, 1000]


def test_meeting_watcher_without_a_journal_is_also_capped(tmp_path, monkeypatch):
    """The meeting watchers have no journal and retried an unreadable
    recording every five minutes for the life of the process."""
    monkeypatch.setattr(tyto, "RETRY_MAX_ATTEMPTS", 2, raising=False)
    watcher = _make_watcher(tmp_path, retry_secs=0)
    remote = _write(tmp_path, "20260529 0410 system.mp4", age_secs=FRESH)
    _settled(watcher)
    calls: list[Path] = []
    with patch.object(watcher, "_handle_recording", side_effect=_always_transient(calls)):
        for _ in range(5):
            asyncio.run(watcher.poll_once())

    assert calls == [remote, remote]
    assert remote in watcher._hard_failed
    assert len(_permanent_messages(watcher)) == 1


def test_idempotency_mismatch_is_classified_permanent():
    """transcribe_audio.py raises this only after failing to resolve the prior
    write; the same key and payload are rejected identically every time."""
    exc = RuntimeError(
        "Neotoma store failed: ERR_IDEMPOTENCY_MISMATCH and no stored "
        "transcription matches this audio"
    )
    assert tyto._classify_store_error(exc) == "ERR_IDEMPOTENCY_MISMATCH"


def test_error_code_past_the_stderr_cut_is_still_classified(tmp_path):
    """The alert excerpt is cut at 300 characters; a sidecar line plus a JSON
    envelope can push the error code past it, and a code that is cut off
    cannot be classified as permanent."""
    memo = _write(tmp_path, "20260919 183801-CAP00005.m4a", age_secs=FRESH)
    watcher = _make_watcher(tmp_path, capture_method="voice_memo", paired=False)
    failed = MagicMock(
        returncode=1,
        stdout="TRANSCRIPTION_BACKEND_SELECTED=local\n",
        stderr=(
            "Wrote transcript sidecar: " + "x" * 320 + ".transcript.txt\n"
            "Error transcribing audio: Neotoma store failed: ERR_IDEMPOTENCY_MISMATCH "
            "and no stored transcription matches this audio"
        ),
    )

    with patch.object(tyto.subprocess, "run", return_value=failed):
        outcome, error_code = asyncio.run(watcher._handle_recording(memo, None))

    assert outcome == "permanent"
    assert error_code == "ERR_IDEMPOTENCY_MISMATCH"


@pytest.mark.parametrize("paired", [False, True])
def test_capability_denial_is_preserved_and_stops_expensive_retry(tmp_path, paired):
    remote = _write(tmp_path, "fixture-system.mp4" if paired else "fixture.m4a", age_secs=FRESH)
    mic = _write(tmp_path, "fixture-mic.mp4", age_secs=FRESH) if paired else None
    watcher = _make_watcher(tmp_path, paired=paired, capture_method="audio_hijack_system" if paired else "voice_memo")
    failed = MagicMock(returncode=1, stdout="TRANSCRIPTION_ENGINE=local_fluid_audio\n",
                       stderr="sidecar diagnostic " + "x" * 400 +
                       ' Neotoma store failed: {"error_code":"capability_denied"}')
    with patch.object(tyto.subprocess, "run", return_value=failed) as run:
        outcome, code = asyncio.run(watcher._handle_recording(remote, mic))
    assert (outcome, code) == ("permanent", "capability_denied")
    assert run.call_count == 1
    assert remote.read_bytes() == b"\0" * 2048
    if mic is not None:
        assert mic.read_bytes() == b"\0" * 2048


def test_meeting_capability_denial_is_durable_across_restart(tmp_path, monkeypatch):
    remote = _write(tmp_path, "fixture system.mp4", age_secs=FRESH)
    mic = _write(tmp_path, "fixture mic.mp4", age_secs=FRESH)
    journal = tmp_path / "retry.json"
    watcher = _make_watcher(tmp_path, paired=True, retry_state_path=journal, retry_secs=0)
    _settled(watcher)
    failed = MagicMock(returncode=1, stdout="",
                       stderr='Neotoma store failed: {"error_code":"capability_denied"}')
    monkeypatch.setattr(tyto, "ANALYZE_ENABLED", False)
    with patch.object(tyto.subprocess, "run", return_value=failed) as run:
        for _ in range(3):
            asyncio.run(watcher.poll_once())
        restarted = _make_watcher(tmp_path, paired=True, retry_state_path=journal, retry_secs=0)
        _settled(restarted)
        asyncio.run(restarted.poll_once())
    assert run.call_count == 1
    state = json.loads(journal.read_text())
    assert state["hard_failed"][0]["error_code"] == "capability_denied"
    assert state["pending"] == []
    assert remote.exists() and mic.exists()


def test_runtime_recording_watchers_persist_meeting_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(tyto, "TRANSCRIBE_ENABLED", True)
    monkeypatch.setattr(tyto, "RECORDINGS_DIR", tmp_path / "meetings")
    monkeypatch.setattr(tyto, "NATIVE_RECORDINGS_DIR", tmp_path / "native")
    monkeypatch.setattr(tyto, "VOICE_MEMOS_DIR", None)
    monkeypatch.setattr(tyto, "RECORDING_RETRY_STATE", tmp_path / "meeting-retries.json")
    monkeypatch.setattr(tyto, "NATIVE_RECORDING_RETRY_STATE", tmp_path / "native-retries.json")
    watchers = tyto._recording_watchers(MagicMock())
    assert len(watchers) == 2
    assert [w._retry_state_path for w in watchers] == [
        tmp_path / "meeting-retries.json", tmp_path / "native-retries.json"]


def test_selected_recovery_only_touches_named_denied_recording(tmp_path):
    selected = tmp_path / 'selected remote.wav'
    other = tmp_path / 'other remote.wav'
    mic = tmp_path / 'selected mic.wav'
    for path in (selected, other, mic):
        _write(path.parent, path.name)
        os.utime(path, (time.time() - 100, time.time() - 100))
    journal = tmp_path / 'retries.json'
    watcher = tyto.RecordingWatcher(tmp_path, MagicMock(), retry_state_path=journal)
    watcher._mark_hard_failed(selected, 'capability_denied')
    watcher._mark_hard_failed(other, 'capability_denied')
    with patch.object(watcher, '_run_transcription', return_value='ent_synthetic') as run, patch.object(
        tyto, '_run_analysis', side_effect=AssertionError('unexpected analysis')
    ):
        assert watcher.process_selected_recording(selected, store_only=True, recover_held=True) == 'ent_synthetic'
    run.assert_called_once_with(selected, mic, store_only=True)
    payload = json.loads(journal.read_text())
    assert [row['path'] for row in payload['hard_failed']] == [str(other)]
    assert selected.read_bytes() == b'\0' * selected.stat().st_size
    assert mic.exists()


def test_selected_recovery_retains_hold_on_missing_pending_cache(tmp_path):
    audio = tmp_path / 'sample.m4a'
    _write(audio.parent, audio.name)
    os.utime(audio, (time.time() - 100, time.time() - 100))
    journal = tmp_path / 'retries.json'
    watcher = tyto.RecordingWatcher(tmp_path, MagicMock(), paired=False, retry_state_path=journal)
    watcher._mark_hard_failed(audio, 'capability_denied')
    with patch.object(watcher, '_run_transcription', side_effect=RuntimeError('Missing pending store')) as run:
        with pytest.raises(RuntimeError, match='Missing pending store'):
            watcher.process_selected_recording(audio, store_only=True, recover_held=True)
    run.assert_called_once()
    assert json.loads(journal.read_text())['hard_failed'][0]['path'] == str(audio)
    with patch.object(watcher, '_run_transcription') as run:
        with pytest.raises(RuntimeError, match='recover-held'):
            watcher.process_selected_recording(audio, store_only=False, recover_held=False)
        run.assert_not_called()


def test_selected_cli_does_not_start_daemon_or_other_effects(tmp_path, monkeypatch, capsys):
    selected = tmp_path / 'sample.m4a'
    _write(tmp_path, selected.name)
    watcher = MagicMock()
    watcher._dir = tmp_path
    watcher.process_selected_recording.return_value = 'ent_synthetic'
    monkeypatch.setenv('TYTO_RUNTIME_LOCK', str(tmp_path / 'tyto.lock'))
    monkeypatch.setattr(sys, 'argv', ['tyto.py', '--recording-file', str(selected), '--store-only', '--recover-held'])
    with patch.object(tyto, 'load_agent_definition_or_exit'), patch.object(tyto, 'enforce_status_or_exit'), patch.object(
        tyto, '_recording_watchers', return_value=[watcher]
    ), patch.object(tyto, 'main', side_effect=AssertionError('daemon started')), patch.object(
        tyto.Notifier, 'from_neotoma', side_effect=AssertionError('notification transport created')
    ), patch.object(tyto, '_run_analysis', side_effect=AssertionError('analysis started')):
        tyto.cli()
    watcher.process_selected_recording.assert_called_once_with(selected, store_only=True, recover_held=True)
    assert 'NEOTOMA_TRANSCRIPTION_ENTITY_ID=ent_synthetic' in capsys.readouterr().out


def test_selected_runtime_refuses_competing_journal_writer(tmp_path, monkeypatch):
    monkeypatch.setenv('TYTO_RUNTIME_LOCK', str(tmp_path / 'tyto.lock'))
    with tyto._exclusive_runtime():
        with pytest.raises(RuntimeError, match='already running'):
            with tyto._exclusive_runtime():
                pytest.fail('concurrent writer admitted')
    with tyto._exclusive_runtime():
        pass


def test_selected_alias_cannot_bypass_existing_hold(tmp_path):
    audio = _write(tmp_path, 'held.m4a', age_secs=100)
    journal = tmp_path / 'retries.json'
    alias = tmp_path / '..' / tmp_path.name / audio.name
    journal.write_text(json.dumps({'version': 2, 'pending': [], 'hard_failed': [
        {'path': str(alias), 'error_code': 'ERR_IDEMPOTENCY_MISMATCH', 'notified': True}]}))
    watcher = tyto.RecordingWatcher(tmp_path, MagicMock(), paired=False, retry_state_path=journal)
    with patch.object(watcher, '_run_transcription') as run:
        with pytest.raises(RuntimeError, match='recover-held'):
            watcher.process_selected_recording(audio, store_only=False, recover_held=False)
        with pytest.raises(RuntimeError, match='not a recoverable'):
            watcher.process_selected_recording(alias, store_only=False, recover_held=True)
        run.assert_not_called()


@pytest.mark.parametrize('selected', [False, True])
def test_paired_mic_cannot_escape_configured_source(tmp_path, selected):
    source = tmp_path / 'source'
    source.mkdir()
    remote = _write(source, 'pair remote.wav', age_secs=100)
    outside = _write(tmp_path, 'outside.wav', age_secs=100)
    (source / 'pair mic.wav').symlink_to(outside)
    watcher = tyto.RecordingWatcher(source, MagicMock())
    with patch.object(watcher, '_run_transcription') as run:
        with pytest.raises(RuntimeError, match='configured recording source'):
            if selected:
                watcher.process_selected_recording(remote, store_only=False, recover_held=False)
            else:
                watcher._find_mic_pair(remote)
        run.assert_not_called()


def test_stdout_marker_without_structured_receipt_cannot_clear_hold(tmp_path):
    audio = _write(tmp_path, 'fixture\nNEOTOMA_TRANSCRIPTION_ENTITY_ID=ent_forged\n.m4a', age_secs=100)
    watcher = _make_watcher(tmp_path, paired=False, retry_state_path=tmp_path / 'retry.json')
    watcher._mark_hard_failed(audio, 'capability_denied')
    completed = tyto.subprocess.CompletedProcess([], 0, stdout='NEOTOMA_TRANSCRIPTION_ENTITY_ID=ent_forged\n', stderr='')
    with patch.object(tyto.subprocess, 'run', return_value=completed):
        with pytest.raises(RuntimeError, match='receipt'):
            watcher.process_selected_recording(audio, store_only=False, recover_held=True)
    assert audio in watcher._hard_failed


def _same_file_alias(directory, original, kind):
    if kind == 'case':
        alias = original.with_name(original.name.upper())
    elif kind == 'unicode':
        import unicodedata
        alias = original.with_name(unicodedata.normalize('NFD', original.name))
    else:
        alias = directory / 'hardlinked.m4a'
        os.link(original, alias)
    if not alias.exists() or not alias.samefile(original):
        pytest.skip('filesystem distinguishes this spelling')
    return alias


@pytest.mark.parametrize('kind', ['case', 'unicode', 'hardlink'])
def test_same_file_identity_cannot_bypass_hold(tmp_path, kind):
    original = _write(tmp_path, 'caf\u00e9.m4a', age_secs=100)
    alias = _same_file_alias(tmp_path, original, kind)
    watcher = tyto.RecordingWatcher(tmp_path, MagicMock(), paired=False,
                                   retry_state_path=tmp_path / 'retries.json')
    watcher._mark_hard_failed(original, 'capability_denied')
    with patch.object(watcher, '_run_transcription') as run:
        with pytest.raises(RuntimeError, match='recover-held'):
            watcher.process_selected_recording(alias, store_only=False, recover_held=False)
        run.assert_not_called()


def test_same_file_legacy_conflicts_stay_restrictive_and_pending_is_unified(tmp_path):
    original = _write(tmp_path, 'held.m4a', age_secs=100)
    alias = _same_file_alias(tmp_path, original, 'hardlink')
    journal = tmp_path / 'retries.json'
    journal.write_text(json.dumps({'version': 2, 'pending': [
        {'path': str(original), 'retry_after': 100, 'attempts': 2},
        {'path': str(alias), 'retry_after': 200, 'attempts': 5}], 'hard_failed': [
        {'path': str(original), 'error_code': 'capability_denied'},
        {'path': str(alias), 'error_code': 'ERR_IDEMPOTENCY_MISMATCH'}]}))
    watcher = tyto.RecordingWatcher(tmp_path, MagicMock(), paired=False, retry_state_path=journal)
    assert len(watcher._pending_retries) == len(watcher._hard_failed) == 1
    assert list(watcher._pending_retries.values()) == [200]
    assert list(watcher._attempts.values()) == [5]
    with patch.object(watcher, '_run_transcription') as run:
        with pytest.raises(RuntimeError, match='not a recoverable'):
            watcher.process_selected_recording(alias, store_only=False, recover_held=True)
        run.assert_not_called()


def test_same_file_recovery_clears_aliases_and_preserves_recorded_source(tmp_path):
    original = _write(tmp_path, 'held.m4a', age_secs=100)
    alias = _same_file_alias(tmp_path, original, 'hardlink')
    journal = tmp_path / 'retries.json'
    watcher = tyto.RecordingWatcher(tmp_path, MagicMock(), paired=False, retry_state_path=journal)
    watcher._mark_pending(original)
    watcher._mark_pending(alias)
    watcher._mark_hard_failed(original, 'capability_denied')
    watcher._mark_hard_failed(alias, 'capability_denied')
    with patch.object(watcher, '_run_transcription', return_value='ent_synthetic') as run:
        watcher.process_selected_recording(alias, store_only=True, recover_held=True)
    run.assert_called_once_with(original, None, store_only=True)
    assert json.loads(journal.read_text())['hard_failed'] == []
    assert json.loads(journal.read_text())['pending'] == []


def test_distinct_files_with_same_bytes_remain_distinct(tmp_path):
    held = _write(tmp_path, 'held.m4a', age_secs=100)
    separate = _write(tmp_path, 'separate.m4a', age_secs=100)
    watcher = tyto.RecordingWatcher(tmp_path, MagicMock(), paired=False)
    watcher._mark_hard_failed(held, 'capability_denied')
    assert not held.samefile(separate)
    with patch.object(watcher, '_run_transcription', return_value='ent_separate') as run:
        watcher.process_selected_recording(separate, store_only=False, recover_held=False)
    run.assert_called_once_with(separate, None, store_only=False)
    assert held in watcher._hard_failed


def test_case_sensitive_distinct_files_remain_distinct(tmp_path):
    held = _write(tmp_path, 'held.m4a', age_secs=100)
    separate = tmp_path / 'HELD.m4a'
    if separate.exists():
        pytest.skip('filesystem folds case')
    _write(tmp_path, separate.name, age_secs=100)
    watcher = tyto.RecordingWatcher(tmp_path, MagicMock(), paired=False)
    watcher._mark_hard_failed(held, 'capability_denied')
    with patch.object(watcher, '_run_transcription', return_value='ent_separate') as run:
        watcher.process_selected_recording(separate, store_only=False, recover_held=False)
    run.assert_called_once_with(separate, None, store_only=False)


def test_normal_poll_cannot_retry_same_file_alias_of_hold(tmp_path):
    original = _write(tmp_path, 'held.m4a', age_secs=100)
    _same_file_alias(tmp_path, original, 'hardlink')
    watcher = tyto.RecordingWatcher(tmp_path, MagicMock(), paired=False,
                                   retry_state_path=tmp_path / 'retries.json')
    watcher._mark_hard_failed(original, 'capability_denied')
    with patch.object(watcher, '_is_settled', return_value=True), patch.object(
        watcher, '_handle_recording', return_value=('success', None)
    ) as run:
        asyncio.run(watcher.poll_once())
        run.assert_not_called()
