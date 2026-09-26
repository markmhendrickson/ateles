"""Tests for _session_integrity.py's Neotoma credential loading and the
once-per-session missing-credentials warning (ateles#1261 follow-up, audit
ent_b66293f0dcc8c887d4fdbeae).

Audit finding: `stop_finalizer.py` skipped ALL 45 runs in the audited
session with "no bearer token — skipping harness_event emission", silently,
because that hook's environment never carried NEOTOMA_BEARER_TOKEN. No
session-integrity check ran for the session's entire lifetime and nothing
visible said so.

Structured like the sibling `test_decision_shape_gate.py` (module import,
monkeypatch, no subprocess, no real network/filesystem dependency) rather
than the subprocess convention `test_git_stash_guard.py` / the rule-index
hooks use — `_session_integrity.py` is a shared library module other hooks
import, not itself an executable hook with its own `__main__` guard to
exercise.

Every test monkeypatches `_NEOTOMA_ENV_PATH` to a per-test tmp file (or a
path that does not exist) and clears/sets `NEOTOMA_BASE_URL` /
`NEOTOMA_BEARER_TOKEN` in `os.environ` explicitly, so the suite's outcome
never depends on whatever the real ~/.config/neotoma/.env or the actual
process environment happen to hold when pytest runs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _session_integrity as si  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Every test starts with NO Neotoma env vars set, a dotenv path pointed
    at a file that does not exist, and its OWN throwaway session-state
    directory — so no test ever reads or writes this repo's real
    .claude/.session_state/ (which is exactly how an earlier revision of
    this suite leaked a "already warned" state file across runs and made
    test_both_missing_emits_one_visible_warning order-dependent)."""
    monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", Path("/nonexistent/neotoma/.env"))
    state_dir = tmp_path / ".session_state"
    state_dir.mkdir()
    monkeypatch.setattr(si, "state_dir", lambda: state_dir)
    monkeypatch.setattr(
        si, "state_path",
        lambda session_id: state_dir / f"{(session_id or 'unknown')}.json",
    )


def _write_env_file(tmp_path: Path, **pairs: str) -> Path:
    p = tmp_path / ".env"
    lines = [f"{k}={v}" for k, v in pairs.items()]
    # A comment and a blank line, matching the real file's shape, to prove
    # the parser skips both rather than tripping on them.
    p.write_text("# a comment\n\n" + "\n".join(lines) + "\n")
    return p


# ---------------------------------------------------------------------------
# neotoma_credentials(): env first, dotenv fallback, never both silently
# empty when either source has a value.
# ---------------------------------------------------------------------------
class TestNeotomaCredentials:
    def test_env_present_file_absent_uses_env(self, monkeypatch):
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "env-token")
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://env.example")
        base_url, token = si.neotoma_credentials()
        assert token == "env-token"
        assert base_url == "https://env.example"

    def test_env_absent_file_present_uses_file(self, monkeypatch, tmp_path):
        env_file = _write_env_file(
            tmp_path, NEOTOMA_BEARER_TOKEN="file-token",
            NEOTOMA_BASE_URL="https://file.example",
        )
        monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", env_file)
        base_url, token = si.neotoma_credentials()
        assert token == "file-token"
        assert base_url == "https://file.example"

    def test_env_wins_over_file_when_both_present(self, monkeypatch, tmp_path):
        env_file = _write_env_file(
            tmp_path, NEOTOMA_BEARER_TOKEN="file-token",
            NEOTOMA_BASE_URL="https://file.example",
        )
        monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", env_file)
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "env-token")
        base_url, token = si.neotoma_credentials()
        assert token == "env-token"
        assert base_url == "https://file.example"  # base_url still falls back per-key

    def test_both_absent_returns_empty_token(self):
        base_url, token = si.neotoma_credentials()
        assert token == ""
        assert base_url  # base_url always has the hardcoded default, never empty

    def test_malformed_file_lines_are_skipped_not_raised(self, monkeypatch, tmp_path):
        p = tmp_path / ".env"
        p.write_text("not-a-kv-line\nNEOTOMA_BEARER_TOKEN=quoted-token\n# comment\n")
        monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", p)
        base_url, token = si.neotoma_credentials()
        assert token == "quoted-token"

    def test_quoted_values_are_unquoted(self, monkeypatch, tmp_path):
        p = tmp_path / ".env"
        p.write_text('NEOTOMA_BEARER_TOKEN="quoted-token"\n')
        monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", p)
        _, token = si.neotoma_credentials()
        assert token == "quoted-token"

    def test_missing_file_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", Path("/really/does/not/exist/.env"))
        base_url, token = si.neotoma_credentials()
        assert token == ""


# ---------------------------------------------------------------------------
# emit_harness_event_raw: missing-env + file-present means credentials load
# and the POST is attempted; both missing means the warning fires.
# ---------------------------------------------------------------------------
class TestEmitHarnessEventRawCredentialFallback:
    def test_missing_env_file_present_loads_credentials_and_attempts_post(
        self, monkeypatch, tmp_path, capsys
    ):
        env_file = _write_env_file(
            tmp_path, NEOTOMA_BEARER_TOKEN="file-token",
            NEOTOMA_BASE_URL="https://file.example",
        )
        monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", env_file)

        seen_requests = []

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        def _fake_urlopen(req, timeout):
            seen_requests.append(req)
            return _FakeResponse()

        monkeypatch.setattr(si.urllib.request, "urlopen", _fake_urlopen)

        si.emit_harness_event_raw(
            "test-slug", {"event_type": "x"}, log_tag="test", session_id="sess-a",
        )
        assert len(seen_requests) == 1
        assert seen_requests[0].full_url == "https://file.example/store"
        assert seen_requests[0].headers.get("Authorization") == "Bearer file-token"
        # No warning printed — credentials WERE found (via the file).
        captured = capsys.readouterr()
        assert "WARNING" not in captured.err

    def test_both_missing_emits_one_visible_warning(self, capsys):
        si.emit_harness_event_raw(
            "test-slug", {"event_type": "x"}, log_tag="test-tag", session_id="sess-b",
        )
        captured = capsys.readouterr()
        assert "[test-tag] WARNING" in captured.err
        assert "NEOTOMA_BEARER_TOKEN" in captured.err

    def test_warning_never_includes_a_secret_value(self, monkeypatch, capsys, tmp_path):
        # Even with a token present somewhere the warning path could see
        # (it should not reach the warning branch at all, but this pins that
        # if it ever did, no value leaks) — belt and suspenders.
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "super-secret-value")
        # Force credentials() to report empty despite the env var by
        # pointing at a broken dotenv AND deleting env inside the call is
        # not how the real function works, so instead assert directly that
        # the fixed warning text is static and contains no interpolated value.
        si._warn_missing_credentials_once("sess-c", "test-tag")
        captured = capsys.readouterr()
        assert "super-secret-value" not in captured.err

    def test_warning_is_once_per_session_not_once_per_call(self, capsys):
        # _clean_env (autouse) already isolates state_dir/state_path to a
        # per-test tmp directory, so this needs no fixture-local patching.
        si.emit_harness_event_raw("slug1", {}, log_tag="tag", session_id="sess-d")
        si.emit_harness_event_raw("slug2", {}, log_tag="tag", session_id="sess-d")
        captured = capsys.readouterr()
        assert captured.err.count("WARNING") == 1

    def test_different_sessions_each_get_their_own_warning(self, capsys):
        si.emit_harness_event_raw("slug1", {}, log_tag="tag", session_id="sess-e")
        si.emit_harness_event_raw("slug2", {}, log_tag="tag", session_id="sess-f")
        captured = capsys.readouterr()
        assert captured.err.count("WARNING") == 2

    def test_no_session_id_warns_every_call_but_never_raises(self, capsys):
        """No session_id to key state on -> can't deduplicate, so it warns
        every call rather than silently skipping the warning. Documents the
        degrade, doesn't require dedup without a key to dedup on."""
        si.emit_harness_event_raw("slug1", {}, log_tag="tag", session_id="")
        si.emit_harness_event_raw("slug2", {}, log_tag="tag", session_id="")
        captured = capsys.readouterr()
        assert captured.err.count("WARNING") == 2
