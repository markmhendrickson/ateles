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

import builtins
import json
import os
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _session_integrity as si  # noqa: E402
import stop_finalizer as sf  # noqa: E402


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


@contextmanager
def _http_server(handler: type[BaseHTTPRequestHandler]):
    """Run a loopback-only HTTP server for one transport integration test."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ---------------------------------------------------------------------------
# neotoma_credentials(): endpoint and bearer are an atomic, provenance-bound
# pair. A complete environment pair wins; otherwise a complete dotenv pair is
# used. Values from separate sources are never combined.
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

    def test_complete_env_pair_wins_over_complete_file_pair(self, monkeypatch, tmp_path):
        env_file = _write_env_file(
            tmp_path, NEOTOMA_BEARER_TOKEN="file-token",
            NEOTOMA_BASE_URL="https://file.example",
        )
        monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", env_file)
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "env-token")
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://env.example")
        base_url, token = si.neotoma_credentials()
        assert token == "env-token"
        assert base_url == "https://env.example"

    @pytest.mark.parametrize(
        ("env_key", "env_value"),
        [
            ("NEOTOMA_BEARER_TOKEN", "env-token"),
            ("NEOTOMA_BASE_URL", "https://env.example"),
        ],
    )
    def test_partial_env_uses_complete_file_pair(
        self, monkeypatch, tmp_path, env_key, env_value
    ):
        env_file = _write_env_file(
            tmp_path,
            NEOTOMA_BEARER_TOKEN="file-token",
            NEOTOMA_BASE_URL="https://file.example",
        )
        monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", env_file)
        monkeypatch.setenv(env_key, env_value)

        assert si.neotoma_credentials() == (
            "https://file.example",
            "file-token",
        )

    @pytest.mark.parametrize(
        ("env_key", "env_value", "file_pairs"),
        [
            (
                "NEOTOMA_BEARER_TOKEN",
                "env-token",
                {"NEOTOMA_BASE_URL": "https://file.example"},
            ),
            (
                "NEOTOMA_BASE_URL",
                "https://env.example",
                {"NEOTOMA_BEARER_TOKEN": "file-token"},
            ),
        ],
    )
    def test_complementary_partial_sources_do_not_form_a_pair(
        self, monkeypatch, tmp_path, env_key, env_value, file_pairs
    ):
        env_file = _write_env_file(tmp_path, **file_pairs)
        monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", env_file)
        monkeypatch.setenv(env_key, env_value)

        assert si.neotoma_credentials() == ("", "")

    def test_both_absent_returns_empty_token(self):
        base_url, token = si.neotoma_credentials()
        assert token == ""
        assert base_url == ""

    def test_malformed_file_lines_are_skipped_not_raised(self, monkeypatch, tmp_path):
        p = tmp_path / ".env"
        p.write_text("not-a-kv-line\nNEOTOMA_BEARER_TOKEN=quoted-token\n# comment\n")
        monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", p)
        assert si._read_neotoma_env_file()["NEOTOMA_BEARER_TOKEN"] == "quoted-token"

    def test_quoted_values_are_unquoted(self, monkeypatch, tmp_path):
        p = tmp_path / ".env"
        p.write_text('NEOTOMA_BEARER_TOKEN="quoted-token"\n')
        monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", p)
        assert si._read_neotoma_env_file()["NEOTOMA_BEARER_TOKEN"] == "quoted-token"

    def test_missing_file_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", Path("/really/does/not/exist/.env"))
        base_url, token = si.neotoma_credentials()
        assert token == ""

    def test_non_utf8_bytes_do_not_raise(self, monkeypatch, tmp_path):
        """Loxia review + qa/security lenses on PR #1298 round 1: the
        original version caught only `OSError`, so a non-UTF-8-encoded file
        raised an unhandled `UnicodeDecodeError` (a `ValueError` subclass)
        straight out of `_read_neotoma_env_file`, `neotoma_credentials`, and
        `emit_harness_event_raw` — reproduced concretely by both lenses.
        This pins the fix: a good key/value line followed by invalid UTF-8
        bytes must degrade to whatever the readable portion parses to,
        never raise. Written to fail before the fix (bare `except OSError`,
        `read_text(encoding="utf-8")` with no `errors=`) and pass after."""
        p = tmp_path / ".env"
        p.write_bytes(b"NEOTOMA_BEARER_TOKEN=good-token\n\xff\xfe\x00garbage\n")
        monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", p)
        assert si._read_neotoma_env_file()["NEOTOMA_BEARER_TOKEN"] == "good-token"

    def test_non_utf8_only_content_yields_empty_not_raise(self, monkeypatch, tmp_path):
        """Same defect, no readable line at all — must still return empty
        credentials rather than propagate a UnicodeDecodeError."""
        p = tmp_path / ".env"
        p.write_bytes(b"\xff\xfe\xff\xfe\xff\xfe")
        monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", p)
        base_url, token = si.neotoma_credentials()
        assert token == ""

    def test_path_is_a_directory_does_not_raise(self, monkeypatch, tmp_path):
        """A directory sitting where the .env file is expected: `.exists()`
        is True, but `.read_text()` raises `IsADirectoryError` (an
        `OSError` subclass — already caught before this fix, but pinned
        here as one of the four required cases so a future narrowing of the
        except clause back to a specific OSError subtype cannot silently
        drop it)."""
        p = tmp_path / ".env"
        p.mkdir()
        monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", p)
        base_url, token = si.neotoma_credentials()
        assert token == ""

    def test_permission_denied_file_does_not_raise(self, monkeypatch, tmp_path):
        """A file that exists but cannot be read: `.read_text()` raises
        `PermissionError` (an `OSError` subclass). Skipped when running as
        root, where a permission bit on a file you own is not honored."""
        if os.geteuid() == 0:
            pytest.skip("running as root — file permissions are not enforced")
        p = tmp_path / ".env"
        p.write_text("NEOTOMA_BEARER_TOKEN=unreadable-token\n")
        p.chmod(0o000)
        try:
            monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", p)
            base_url, token = si.neotoma_credentials()
            assert token == ""
        finally:
            p.chmod(0o644)  # restore so tmp_path cleanup can remove it


# ---------------------------------------------------------------------------
# emit_harness_event_raw: missing-env + file-present means credentials load
# and the POST is attempted; both missing means the warning fires.
# ---------------------------------------------------------------------------
class TestEmitHarnessEventRawCredentialFallback:
    def test_authenticated_post_refuses_cross_origin_redirect_without_forwarding_token(
        self, monkeypatch, capsys
    ):
        """Security planted-red: urllib's default redirect handler forwards the
        synthetic bearer to a different origin (a second loopback port).

        The fixed transport must stop at the redirect response. Both servers
        are process-local and the credential is a synthetic sentinel.
        """
        received_authorization: list[str | None] = []

        class SinkHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib handler API
                received_authorization.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                return

        with _http_server(SinkHandler) as sink_url:
            class RedirectHandler(BaseHTTPRequestHandler):
                calls = 0

                def do_POST(self):  # noqa: N802 - stdlib handler API
                    type(self).calls += 1
                    self.send_response(302)
                    self.send_header("Location", f"{sink_url}/captured")
                    self.end_headers()

                def log_message(self, *args):
                    return

            with _http_server(RedirectHandler) as source_url:
                monkeypatch.setenv("NEOTOMA_BASE_URL", source_url)
                monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "synthetic-redirect-token")

                si.emit_harness_event_raw(
                    "redirect-test",
                    {"event_type": "synthetic"},
                    log_tag="redirect-test",
                    session_id="sess-redirect",
                )

        assert RedirectHandler.calls == 1
        assert received_authorization == []
        assert "redirect" in capsys.readouterr().err.lower()

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

    @pytest.mark.parametrize(
        ("env_key", "env_value", "file_pairs"),
        [
            (
                "NEOTOMA_BEARER_TOKEN",
                "env-token",
                {"NEOTOMA_BASE_URL": "https://file.example"},
            ),
            (
                "NEOTOMA_BASE_URL",
                "https://env.example",
                {"NEOTOMA_BEARER_TOKEN": "file-token"},
            ),
        ],
    )
    def test_complementary_partial_sources_never_attempt_a_request(
        self, monkeypatch, tmp_path, capsys, env_key, env_value, file_pairs
    ):
        env_file = _write_env_file(tmp_path, **file_pairs)
        monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", env_file)
        monkeypatch.setenv(env_key, env_value)
        requests = []
        monkeypatch.setattr(
            si.urllib.request,
            "urlopen",
            lambda *args, **kwargs: requests.append((args, kwargs)),
        )

        si.emit_harness_event_raw(
            "test-slug", {"event_type": "x"}, log_tag="test", session_id="sess-partial",
        )

        assert requests == []
        assert "audit emission is being skipped" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("env_key", "env_value"),
        [
            ("NEOTOMA_BEARER_TOKEN", "env-token"),
            ("NEOTOMA_BASE_URL", "https://env.example"),
        ],
    )
    def test_partial_env_with_complete_file_uses_only_file_pair_for_request(
        self, monkeypatch, tmp_path, env_key, env_value
    ):
        env_file = _write_env_file(
            tmp_path,
            NEOTOMA_BEARER_TOKEN="file-token",
            NEOTOMA_BASE_URL="https://file.example",
        )
        monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", env_file)
        monkeypatch.setenv(env_key, env_value)
        requests = []

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"{}"

        def _fake_urlopen(req, timeout):
            requests.append(req)
            return _FakeResponse()

        monkeypatch.setattr(si.urllib.request, "urlopen", _fake_urlopen)

        si.emit_harness_event_raw(
            "test-slug", {"event_type": "x"}, log_tag="test", session_id="sess-file",
        )

        assert len(requests) == 1
        assert requests[0].full_url == "https://file.example/store"
        assert requests[0].headers.get("Authorization") == "Bearer file-token"

    def test_both_missing_emits_one_visible_warning(self, capsys):
        si.emit_harness_event_raw(
            "test-slug", {"event_type": "x"}, log_tag="test-tag", session_id="sess-b",
        )
        captured = capsys.readouterr()
        assert "[test-tag] WARNING" in captured.err
        assert "NEOTOMA_BEARER_TOKEN" in captured.err
        assert "audit emission is being skipped" in captured.err
        assert "Stop-hook enforcement still runs" in captured.err

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


class TestMissingCredentialOperatorPresentation:
    @staticmethod
    def _exempt_summary():
        return {
            "turns": 1,
            "wrote_domain": False,
            "bound_plan": False,
            "bound_task": False,
            "captured_learning": False,
            "write_types": set(),
        }

    def _run_stop(self, monkeypatch, session_id: str) -> int:
        monkeypatch.setattr(
            sf,
            "read_hook_input",
            lambda: {"session_id": session_id, "transcript_path": "unused"},
        )
        monkeypatch.setattr(sf, "scan_transcript", lambda _: self._exempt_summary())
        monkeypatch.setattr(sf, "ENFORCE", False)
        return sf.main()

    def test_successful_stop_presents_missing_credentials_via_system_message_once(
        self, monkeypatch, capsys
    ):
        """UX planted-red: a successful Stop must use the harness-supported
        user-visible field, not stderr, and deduplicate only after delivery.
        """
        assert self._run_stop(monkeypatch, "sess-visible") == 0
        first = capsys.readouterr()
        payload = json.loads(first.out)
        assert "systemMessage" in payload
        assert "audit emission is being skipped" in payload["systemMessage"]
        assert first.err == ""

        assert self._run_stop(monkeypatch, "sess-visible") == 0
        second = capsys.readouterr()
        assert second.out == ""
        assert second.err == ""

    def test_failed_stdout_does_not_mark_notice_delivered_and_next_stop_recovers(
        self, monkeypatch, capsys
    ):
        """A failed presentation attempt must not consume the one-shot notice."""
        original_print = builtins.print

        def _broken_print(*args, **kwargs):
            raise OSError("synthetic stdout failure")

        monkeypatch.setattr(builtins, "print", _broken_print)
        with pytest.raises(OSError, match="synthetic stdout failure"):
            self._run_stop(monkeypatch, "sess-recovery")

        monkeypatch.setattr(builtins, "print", original_print)
        assert self._run_stop(monkeypatch, "sess-recovery") == 0
        payload = json.loads(capsys.readouterr().out)
        assert "audit emission is being skipped" in payload["systemMessage"]

    def test_failed_stdout_print_does_not_skip_audit_emission(self, monkeypatch, capsys):
        """Round 5: emit_harness_event must run BEFORE the systemMessage print,
        so a failed print (the case above) does not also silently skip the
        harness_event audit row this whole PR exists to make reliable —
        that would just move the original silent-skip bug rather than fix
        it. Spies on emit_harness_event via si to prove it was actually
        invoked even though the subsequent print raises.
        """
        calls = []
        monkeypatch.setattr(
            sf, "emit_harness_event",
            lambda *a, **kw: calls.append((a, kw)),
        )

        def _broken_print(*args, **kwargs):
            raise OSError("synthetic stdout failure")

        monkeypatch.setattr(builtins, "print", _broken_print)
        with pytest.raises(OSError, match="synthetic stdout failure"):
            self._run_stop(monkeypatch, "sess-emit-before-print")

        assert len(calls) == 1

    def test_violated_and_enforced_merges_notice_into_block_payload_not_stderr(
        self, monkeypatch, capsys
    ):
        """Round 5 planted-red: the round-4 fix gated systemMessage on
        `status != "violated"`, so a write-bearing session with no bound
        plan/task AND missing credentials — the exact scenario audit
        ent_b66293f0dcc8c887d4fdbeae was filed over — never got the visible
        notice; only the stderr-only fallback fired, which per this PR's own
        stated rationale the harness does not surface. Fixed by merging
        systemMessage into the SAME stdout JSON object as the block decision.
        """
        monkeypatch.setattr(sf, "ENFORCE", True)
        monkeypatch.setattr(
            sf, "read_hook_input",
            lambda: {"session_id": "sess-violated-enforced", "transcript_path": "unused"},
        )
        monkeypatch.setattr(
            sf, "scan_transcript",
            lambda _: {
                "turns": 1, "wrote_domain": True, "bound_plan": False,
                "bound_task": False, "captured_learning": False,
                "write_types": {"task"},
            },
        )
        monkeypatch.setattr(sf, "load_state", lambda _: {})

        assert sf.main() == 2
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["decision"] == "block"
        assert "audit emission is being skipped" in payload["systemMessage"]
        # Not duplicated onto stderr by emit_harness_event_raw's own default
        # warning path — stop_finalizer.py suppresses it and presents once.
        assert "audit emission is being skipped" not in captured.err

    def test_violated_warn_mode_also_merges_notice_into_system_message(
        self, monkeypatch, capsys
    ):
        """Same gap, WARN (non-enforcing) mode: a violated session that is
        not blocking (ATELES_SESSION_INTEGRITY_ENFORCE unset) still printed
        nothing at all pre-round-5 other than a stderr WARN line — the
        credentials notice must reach systemMessage here too, not only on
        the ENFORCE=True block path.
        """
        monkeypatch.setattr(sf, "ENFORCE", False)
        monkeypatch.setattr(
            sf, "read_hook_input",
            lambda: {"session_id": "sess-violated-warn", "transcript_path": "unused"},
        )
        monkeypatch.setattr(
            sf, "scan_transcript",
            lambda _: {
                "turns": 1, "wrote_domain": True, "bound_plan": False,
                "bound_task": False, "captured_learning": False,
                "write_types": {"task"},
            },
        )
        monkeypatch.setattr(sf, "load_state", lambda _: {})

        assert sf.main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert "audit emission is being skipped" in payload["systemMessage"]


def test_missing_audit_credentials_do_not_disable_stop_enforcement(monkeypatch, capsys):
    """Audit transport and the local enforcement decision are independent.

    With no complete Neotoma pair, a positively identified violation must
    still block in enforcement mode even though harness_event emission cannot
    run. This is the observable effect the warning now describes.

    Round 5 (PR #1298): the missing-credentials notice is merged into the
    SAME stdout JSON object as the block decision on this exact path —
    write-bearing + non-integral + missing credentials — rather than only
    reaching stderr. This is the highest-stakes case the original audit
    (ent_b66293f0dcc8c887d4fdbeae) was about, and round 4's systemMessage
    fix (gated on `status != "violated"`) never reached it; round 5 closes
    that gap. The detail/reason text is still ALSO written to stderr
    (unchanged, existing BLOCK-mode behavior), but the credentials notice
    itself now lives in `systemMessage` on stdout, not stderr — so this test
    was rewritten from asserting stderr to asserting the merged payload.
    """
    monkeypatch.setattr(sf, "ENFORCE", True)
    monkeypatch.setattr(
        sf,
        "read_hook_input",
        lambda: {"session_id": "sess-enforced", "transcript_path": "unused"},
    )
    monkeypatch.setattr(
        sf,
        "scan_transcript",
        lambda _: {
            "turns": 1,
            "wrote_domain": True,
            "bound_plan": False,
            "bound_task": False,
            "captured_learning": False,
            "write_types": {"task"},
        },
    )
    monkeypatch.setattr(sf, "load_state", lambda _: {})

    assert sf.main() == 2

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["decision"] == "block"
    assert "Session integrity violation" in payload["reason"]
    assert "audit emission is being skipped" in payload["systemMessage"]
    assert "Session integrity violation" in captured.err
    # The credentials notice itself must not ALSO be written to stderr by
    # emit_harness_event_raw's own default warning path — stop_finalizer.py
    # suppresses that and presents it exactly once, via systemMessage.
    assert "audit emission is being skipped" not in captured.err
