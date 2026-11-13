"""Tests for the stable harness_event idempotency key and the detached sender.

Problem these pin: `emit_harness_event_raw` POSTed inline with an 8 s timeout
and a key of `harness-event-{slug}-{unix_seconds}`. When the Neotoma instance
was slow to accept writes, every Stop stalled for up to 8 s, the POST timed
out anyway, and a retry wrote a duplicate row because the key had changed.

Every network test here talks to a loopback stub started by the test and uses
a synthetic bearer; nothing reaches a real Neotoma instance.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

import _session_integrity as si  # noqa: E402

SYNTHETIC_TOKEN = "synthetic-sender-token-7f3a9c"  # gitleaks:allow — test sentinel
UNRELATED_SECRET = "synthetic-unrelated-secret-2b8e"  # gitleaks:allow — test sentinel
# A bearer with an embedded newline: http.client rejects it inside an
# exception message that quotes the whole header value.
NEWLINE_TOKEN_LEFT = "synthleft-91c2"
NEWLINE_TOKEN_RIGHT = "synthright-4d7e"
NEWLINE_TOKEN = f"{NEWLINE_TOKEN_LEFT}\n{NEWLINE_TOKEN_RIGHT}"  # gitleaks:allow


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(si, "_NEOTOMA_ENV_PATH", Path("/nonexistent/neotoma/.env"))
    state_dir = tmp_path / ".session_state"
    state_dir.mkdir()
    monkeypatch.setattr(si, "state_dir", lambda: state_dir)
    monkeypatch.setattr(
        si, "state_path", lambda sid: state_dir / f"{(sid or 'unknown')}.json"
    )


@contextmanager
def _http_server(handler: type[BaseHTTPRequestHandler]):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _slow_store_handler():
    """A /store stub that holds every request until `release` is set — a
    stand-in for an instance whose write lock is held for most of a minute."""
    release = threading.Event()
    arrived = threading.Event()
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            seen.append({
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": json.loads(body or b"{}"),
            })
            arrived.set()
            release.wait(30)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            return

    return Handler, release, arrived, seen


def _row(uuid: str, role: str = "assistant", text: str = "done") -> dict:
    return {
        "type": role,
        "uuid": uuid,
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def _write_transcript(path: Path, rows: list[dict]) -> str:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return str(path)


def _capture_launches(monkeypatch) -> list[dict]:
    payloads: list[dict] = []
    monkeypatch.setattr(si, "_launch_sender", lambda payload: payloads.append(payload))
    return payloads


def _ticking_clock(monkeypatch):
    """Advance time.time() 100 s per call, so any key built from the clock
    differs between two emissions of the same stop."""
    state = {"now": 1_800_000_000.0}

    def _tick():
        state["now"] += 100.0
        return state["now"]

    monkeypatch.setattr(si.time, "time", _tick)


# ---------------------------------------------------------------------------
# 1. The idempotency key is stable per logical stop.
# ---------------------------------------------------------------------------
class TestStableIdempotencyKey:
    def test_same_stop_yields_the_same_key_twice(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "http://127.0.0.1:9")
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", SYNTHETIC_TOKEN)
        payloads = _capture_launches(monkeypatch)
        _ticking_clock(monkeypatch)
        transcript = _write_transcript(tmp_path / "t.jsonl", [
            _row("u-1", "user", "do it"), _row("a-1"),
        ])

        for _ in range(2):  # the same stop, retried
            summary = si.scan_transcript(transcript)
            si.emit_harness_event("sess-1", summary, "exempt")

        keys = [p["body"]["idempotency_key"] for p in payloads]
        assert len(keys) == 2
        assert keys[0] == keys[1] == "harness-event-session-integrity-sess-1-a-1"

    def test_different_stops_yield_different_keys(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "http://127.0.0.1:9")
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", SYNTHETIC_TOKEN)
        payloads = _capture_launches(monkeypatch)
        path = tmp_path / "t.jsonl"
        rows = [_row("u-1", "user", "do it"), _row("a-1")]
        _write_transcript(path, rows)
        si.emit_harness_event("sess-1", si.scan_transcript(str(path)), "exempt")
        # The next stop: the transcript has grown by another turn.
        rows += [_row("u-2", "user", "and then"), _row("a-2")]
        _write_transcript(path, rows)
        si.emit_harness_event("sess-1", si.scan_transcript(str(path)), "exempt")

        keys = [p["body"]["idempotency_key"] for p in payloads]
        assert keys == [
            "harness-event-session-integrity-sess-1-a-1",
            "harness-event-session-integrity-sess-1-a-2",
        ]

    def test_decision_shape_discriminator_is_stable_and_advances(self, tmp_path):
        path = tmp_path / "t.jsonl"
        rows = [_row("u-1", "user"), _row("a-1")]
        _write_transcript(path, rows)
        first = si.transcript_event_discriminator(str(path))
        assert first == si.transcript_event_discriminator(str(path)) == "a-1"
        rows.append(_row("a-2"))
        _write_transcript(path, rows)
        assert si.transcript_event_discriminator(str(path)) == "a-2"
        assert (
            si.harness_event_idempotency_key("decision-shape-s", first)
            == "harness-event-decision-shape-s-a-1"
        )

    def test_rows_without_uuid_are_skipped_not_reset(self, tmp_path):
        path = tmp_path / "t.jsonl"
        _write_transcript(path, [_row("a-1"), {"type": "summary", "summary": "x"}])
        assert si.transcript_event_discriminator(str(path)) == "a-1"
        assert si.scan_transcript(str(path))["last_event_uuid"] == "a-1"

    def test_untrusted_uuid_is_sanitized(self, tmp_path):
        path = tmp_path / "t.jsonl"
        _write_transcript(path, [_row("a-1/../x y\nz")])
        assert si.transcript_event_discriminator(str(path)) == "a-1xyz"

    def test_no_discriminator_keeps_the_timestamp_shape(self, monkeypatch):
        monkeypatch.setattr(si.time, "time", lambda: 1_800_000_123.9)
        assert si.harness_event_idempotency_key("slug") == "harness-event-slug-1800000123"
        assert si.transcript_event_discriminator(None) == ""
        assert si.transcript_event_discriminator("/nonexistent/t.jsonl") == ""


# ---------------------------------------------------------------------------
# 2. The hook returns without waiting on a slow server.
# ---------------------------------------------------------------------------
class TestHookDoesNotWaitOnSlowServer:
    def test_stop_finalizer_returns_before_the_slow_post_completes(self, tmp_path):
        handler, release, arrived, seen = _slow_store_handler()
        transcript = _write_transcript(tmp_path / "t.jsonl", [
            _row("u-1", "user", "hello"), _row("a-1"),
        ])
        project = tmp_path / "project"
        project.mkdir()
        try:
            with _http_server(handler) as url:
                env = {
                    k: v for k, v in os.environ.items()
                    if k not in ("NEOTOMA_BASE_URL", "NEOTOMA_BEARER_TOKEN")
                }
                env.update({
                    "NEOTOMA_BASE_URL": url,
                    "NEOTOMA_BEARER_TOKEN": SYNTHETIC_TOKEN,
                    "CLAUDE_PROJECT_DIR": str(project),
                })
                env.pop("ATELES_SESSION_INTEGRITY_ENFORCE", None)
                started = time.monotonic()
                # capture_output: the harness reads hook stdio to EOF, so a
                # sender that inherited it would hold the hook open too.
                result = subprocess.run(
                    [sys.executable, str(HOOKS_DIR / "stop_finalizer.py")],
                    input=json.dumps({"session_id": "sess-slow", "transcript_path": transcript}),
                    capture_output=True, text=True, env=env, timeout=60,
                )
                elapsed = time.monotonic() - started
                assert result.returncode == 0
                assert elapsed < 4.0, f"hook waited {elapsed:.1f}s on the slow server"
                # The detached sender still delivers, with the stable key.
                assert arrived.wait(15), "detached sender never reached the stub"
                assert seen[0]["path"] == "/store"
                assert seen[0]["authorization"] == f"Bearer {SYNTHETIC_TOKEN}"
                assert seen[0]["body"]["idempotency_key"] == (
                    "harness-event-session-integrity-sess-slow-a-1"
                )
                assert SYNTHETIC_TOKEN not in result.stdout + result.stderr
                release.set()
        finally:
            release.set()

    def test_emit_returns_immediately_in_process(self, monkeypatch):
        handler, release, arrived, _seen = _slow_store_handler()
        procs = []
        real_launch = si._launch_sender
        monkeypatch.setattr(
            si, "_launch_sender", lambda payload: procs.append(real_launch(payload))
        )
        try:
            with _http_server(handler) as url:
                monkeypatch.setenv("NEOTOMA_BASE_URL", url)
                monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", SYNTHETIC_TOKEN)
                started = time.monotonic()
                si.emit_harness_event_raw("slow", {"event_type": "x"}, discriminator="d1")
                assert time.monotonic() - started < 2.0
                assert arrived.wait(15)
                release.set()
                procs[0].wait(timeout=30)
                assert procs[0].returncode == 0
        finally:
            release.set()


# ---------------------------------------------------------------------------
# 3. The bearer never appears on the child's argv (or in its environment).
# ---------------------------------------------------------------------------
class TestNoTokenOnChildArgv:
    def test_token_absent_from_child_argv_and_env(self, monkeypatch):
        handler, release, arrived, _seen = _slow_store_handler()
        popen_calls: list[tuple] = []
        real_popen = subprocess.Popen

        def _spy_popen(args, *a, **kw):
            argv = [str(part) for part in args]
            if any(part.endswith("_harness_event_sender.py") for part in argv):
                child_env = kw.get("env") or {}
                # Record booleans only, so a failure can never print the
                # environment of the process running the tests.
                popen_calls.append((
                    any(SYNTHETIC_TOKEN in part for part in argv),
                    "NEOTOMA_BEARER_TOKEN" in child_env,
                    any(SYNTHETIC_TOKEN in str(v) for v in child_env.values()),
                    "ATELES_TEST_UNRELATED_SECRET" in child_env,
                    any(UNRELATED_SECRET in str(v) for v in child_env.values()),
                    kw.get("start_new_session") is True,
                    "PATH" in child_env,
                ))
            return real_popen(args, *a, **kw)

        monkeypatch.setattr(si.subprocess, "Popen", _spy_popen)
        # Another secret the hook happens to hold must not ride along into a
        # child that can outlive it (environment allowlist).
        monkeypatch.setenv("ATELES_TEST_UNRELATED_SECRET", UNRELATED_SECRET)
        procs = []
        real_launch = si._launch_sender
        monkeypatch.setattr(
            si, "_launch_sender", lambda payload: procs.append(real_launch(payload))
        )
        try:
            with _http_server(handler) as url:
                monkeypatch.setenv("NEOTOMA_BASE_URL", url)
                monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", SYNTHETIC_TOKEN)
                si.emit_harness_event_raw("argv", {"event_type": "x"}, discriminator="d1")
                # While the child is blocked on the stub, read its live argv
                # from the process table — what any local user could see.
                assert arrived.wait(15)
                live = subprocess.run(
                    ["ps", "-o", "command=", "-p", str(procs[0].pid)],
                    capture_output=True, text=True, timeout=10,
                ).stdout
                assert "_harness_event_sender.py" in live
                assert (SYNTHETIC_TOKEN in live) is False
                release.set()
                procs[0].wait(timeout=30)
        finally:
            release.set()

        assert len(popen_calls) == 1
        (token_in_argv, bearer_key_in_env, token_in_env, other_key_in_env,
         other_value_in_env, new_session, has_path) = popen_calls[0]
        assert token_in_argv is False
        assert bearer_key_in_env is False
        assert token_in_env is False
        assert other_key_in_env is False
        assert other_value_in_env is False
        assert has_path is True  # the allowlist still carries what Python needs

    def test_child_is_started_in_its_own_session(self, monkeypatch):
        """If the harness signals the hook's process group, the sender must
        not be in it."""
        kwargs_seen: list[dict] = []

        class _FakeProc:
            stdin = None

        def _fake_popen(args, *a, **kw):
            kwargs_seen.append({
                "start_new_session": kw.get("start_new_session"),
                "stdout": kw.get("stdout"), "stderr": kw.get("stderr"),
                "close_fds": kw.get("close_fds"),
            })
            return _FakeProc()

        monkeypatch.setattr(si.subprocess, "Popen", _fake_popen)
        si._launch_sender({"token": SYNTHETIC_TOKEN})
        assert kwargs_seen == [{
            "start_new_session": True,
            "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }]


# ---------------------------------------------------------------------------
# 4. Redirect refusal still holds in the detached child.
# ---------------------------------------------------------------------------
class TestRedirectRefusalInChild:
    def test_child_refuses_cross_origin_redirect_without_forwarding_token(
        self, monkeypatch, tmp_path
    ):
        received_authorization: list[str | None] = []

        class SinkHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                received_authorization.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            do_POST = do_GET  # noqa: N815

            def log_message(self, *args):
                return

        procs = []
        real_launch = si._launch_sender
        monkeypatch.setattr(
            si, "_launch_sender", lambda payload: procs.append(real_launch(payload))
        )
        with _http_server(SinkHandler) as sink_url:
            class RedirectHandler(BaseHTTPRequestHandler):
                calls = 0

                def do_POST(self):  # noqa: N802
                    type(self).calls += 1
                    self.send_response(302)
                    self.send_header("Location", f"{sink_url}/captured")
                    self.end_headers()

                def log_message(self, *args):
                    return

            with _http_server(RedirectHandler) as source_url:
                monkeypatch.setenv("NEOTOMA_BASE_URL", source_url)
                monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", SYNTHETIC_TOKEN)
                si.emit_harness_event_raw(
                    "redirect", {"event_type": "x"}, log_tag="redirect-test",
                    discriminator="d1",
                )
                assert len(procs) == 1
                procs[0].wait(timeout=30)

        assert procs[0].returncode == 0
        # Refused, not retried: a redirect is not a transient failure.
        assert RedirectHandler.calls == 1
        assert received_authorization == []
        log_text = (si.state_dir() / si._SENDER_LOG_NAME).read_text()
        assert "refused a redirect (302)" in log_text
        assert SYNTHETIC_TOKEN not in log_text


# ---------------------------------------------------------------------------
# The child's retry reuses the same key, and a sender failure never raises.
# ---------------------------------------------------------------------------
class TestSenderRetry:
    def test_retry_after_timeout_reuses_the_same_key(self):
        bodies: list[dict] = []

        class FlakyHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                bodies.append(json.loads(self.rfile.read(length)))
                if len(bodies) == 1:
                    time.sleep(1.5)  # outlast the client timeout below
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                return

        with _http_server(FlakyHandler) as url:
            si.deliver_harness_event({
                "base_url": url, "token": SYNTHETIC_TOKEN,
                "body": {"idempotency_key": "harness-event-s-k1", "entities": []},
                "timeout": 0.5, "attempts": 2, "backoff": 0,
            })
        assert [b["idempotency_key"] for b in bodies] == ["harness-event-s-k1"] * 2

    def test_retry_is_bounded_and_failure_is_logged_not_raised(self, tmp_path):
        log_path = tmp_path / "sender.log"
        si.deliver_harness_event({
            "base_url": "http://127.0.0.1:9", "token": SYNTHETIC_TOKEN,
            "body": {"idempotency_key": "k"}, "timeout": 0.5, "attempts": 2,
            "backoff": 0, "log_path": str(log_path), "log_tag": "t",
        })
        text = log_path.read_text()
        assert "after 2 attempt(s)" in text
        assert SYNTHETIC_TOKEN not in text

    def test_sender_script_exits_zero_on_garbage_stdin(self):
        result = subprocess.run(
            [sys.executable, "-I", str(HOOKS_DIR / "_harness_event_sender.py")],
            input=b"not json", capture_output=True, timeout=30,
        )
        assert result.returncode == 0
        assert result.stdout == b"" and result.stderr == b""


# ---------------------------------------------------------------------------
# 5. A credential never reaches the sender log, in any escaping.
# ---------------------------------------------------------------------------
def _forbidden_forms() -> list[str]:
    return [
        NEWLINE_TOKEN,
        repr(NEWLINE_TOKEN)[1:-1],
        json.dumps(NEWLINE_TOKEN)[1:-1],
        NEWLINE_TOKEN_LEFT,
        NEWLINE_TOKEN_RIGHT,
    ]


def _assert_no_token_form(text: str) -> None:
    leaked = [i for i, form in enumerate(_forbidden_forms()) if form in text]
    # Report which form leaked by index only, never the text itself.
    assert leaked == [], f"token form(s) {leaked} present in the log"


class TestCredentialNeverLogged:
    def test_control_character_token_is_refused_before_spawn(self, monkeypatch):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "http://127.0.0.1:9")
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", NEWLINE_TOKEN)
        launches = _capture_launches(monkeypatch)
        si.emit_harness_event_raw("nl", {"event_type": "x"}, log_tag="t", discriminator="d1")
        assert launches == []
        text = (si.state_dir() / si._SENDER_LOG_NAME).read_text()
        assert "control character" in text
        assert "key=harness-event-nl-d1" in text
        _assert_no_token_form(text)

    def test_failure_is_logged_as_type_only_never_the_message(self, tmp_path):
        """Bypass the parent's check: even if such a token reached the
        sender, http.client's message quoting the header must not be logged."""
        log_path = tmp_path / "sender.log"
        si.deliver_harness_event({
            "base_url": "http://127.0.0.1:9", "token": NEWLINE_TOKEN,
            "body": {"idempotency_key": "harness-event-nl-k"}, "timeout": 2,
            "attempts": 2, "backoff": 0, "log_path": str(log_path), "log_tag": "t",
        })
        text = log_path.read_text()
        assert "ValueError" in text
        assert "key=harness-event-nl-k" in text
        # A malformed request is not transient, so it is not retried.
        assert "after 1 attempt(s)" in text
        _assert_no_token_form(text)


class TestSenderLogFile:
    def test_log_is_created_0600(self, tmp_path):
        log_path = tmp_path / "sender.log"
        si._append_sender_log(str(log_path), "line one; key=k")
        assert (log_path.stat().st_mode & 0o777) == 0o600

    def test_existing_world_readable_log_is_tightened(self, tmp_path):
        log_path = tmp_path / "sender.log"
        log_path.write_text("older line\n")
        log_path.chmod(0o644)
        si._append_sender_log(str(log_path), "line two; key=k")
        assert (log_path.stat().st_mode & 0o777) == 0o600
        assert "older line" in log_path.read_text()

    def test_log_rotates_to_a_single_dot1_file_instead_of_emptying(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(si, "_SENDER_LOG_MAX_BYTES", 200)
        log_path = tmp_path / "sender.log"
        rotated = tmp_path / "sender.log.1"
        log_path.write_text("first-batch\n" * 30)  # past the cap
        si._append_sender_log(str(log_path), "after-first-rotation")
        assert "first-batch" in rotated.read_text()
        assert "first-batch" not in log_path.read_text()
        assert "after-first-rotation" in log_path.read_text()
        assert (rotated.stat().st_mode & 0o777) == 0o600

        log_path.write_text("second-batch\n" * 30)
        si._append_sender_log(str(log_path), "after-second-rotation")
        assert "second-batch" in rotated.read_text()
        assert "first-batch" not in rotated.read_text()  # only one .1 is kept
        assert not (tmp_path / "sender.log.2").exists()
        assert log_path.stat().st_size < 200


# ---------------------------------------------------------------------------
# 6. Each attempt has a wall-clock deadline; the sender's lifetime is capped.
# ---------------------------------------------------------------------------
def _slow_drip_handler(seconds: float = 8.0):
    """Sends headers, then one body byte every 0.2 s: each socket read
    succeeds well inside a 1 s socket timeout, so only a wall-clock deadline
    can end the attempt."""
    stop = threading.Event()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            calls.append(1)
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Length", "100000")
            self.end_headers()
            deadline = time.monotonic() + seconds
            try:
                while not stop.is_set() and time.monotonic() < deadline:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.2)
            except OSError:
                return

        def log_message(self, *args):
            return

    return Handler, stop, calls


class TestWallClockDeadline:
    def test_slow_drip_attempt_is_cut_at_its_deadline(self, tmp_path):
        handler, stop, calls = _slow_drip_handler()
        log_path = tmp_path / "sender.log"
        try:
            with _http_server(handler) as url:
                started = time.monotonic()
                si.deliver_harness_event({
                    "base_url": url, "token": SYNTHETIC_TOKEN,
                    "body": {"idempotency_key": "harness-event-drip"},
                    "timeout": 1.0, "attempts": 1, "backoff": 0,
                    "log_path": str(log_path), "log_tag": "t",
                })
                elapsed = time.monotonic() - started
                stop.set()
        finally:
            stop.set()
        assert elapsed < 3.0, f"attempt ran {elapsed:.1f}s past a 1 s deadline"
        assert "HarnessEventDeadlineExceeded" in log_path.read_text()

    def test_sender_process_exits_within_its_total_bound(self, tmp_path):
        handler, stop, calls = _slow_drip_handler()
        log_path = tmp_path / "sender.log"
        try:
            with _http_server(handler) as url:
                payload = {
                    "base_url": url, "token": SYNTHETIC_TOKEN,
                    "body": {"idempotency_key": "harness-event-drip2"},
                    "timeout": 1.0, "attempts": 2, "backoff": 0,
                    "log_path": str(log_path), "log_tag": "t",
                }
                started = time.monotonic()
                result = subprocess.run(
                    [sys.executable, "-I", str(HOOKS_DIR / "_harness_event_sender.py")],
                    input=json.dumps(payload).encode(), capture_output=True, timeout=60,
                )
                elapsed = time.monotonic() - started
                stop.set()
        finally:
            stop.set()
        assert result.returncode == 0
        assert elapsed < 5.0, f"sender lived {elapsed:.1f}s for 2 x 1 s attempts"
        assert len(calls) == 2  # the deadline is transient: one same-key retry
        assert "after 2 attempt(s): HarnessEventDeadlineExceeded" in log_path.read_text()

    def test_lifetime_cap_covers_every_attempt_and_is_bounded(self):
        assert si.HARNESS_EVENT_SENDER_MAX_LIFETIME_S == 2 * 60 + 2 + 10
        assert si.sender_lifetime_s({}) == si.HARNESS_EVENT_SENDER_MAX_LIFETIME_S
        assert si.sender_lifetime_s({"timeout": 1, "attempts": 2, "backoff": 0}) == 13
        # A payload can shorten the cap but never lengthen it.
        assert si.sender_lifetime_s({"timeout": 10_000}) == si.HARNESS_EVENT_SENDER_MAX_LIFETIME_S
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_sender_for_test", HOOKS_DIR / "_harness_event_sender.py"
        )
        sender = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sender)
        assert sender._BOOTSTRAP_LIFETIME_S == si.HARNESS_EVENT_SENDER_MAX_LIFETIME_S


# ---------------------------------------------------------------------------
# 7. Production retry policy is pinned.
# ---------------------------------------------------------------------------
class TestRetryPolicyPinned:
    def test_emit_hands_the_sender_the_production_defaults(self, monkeypatch):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "http://127.0.0.1:9")
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", SYNTHETIC_TOKEN)
        payloads = _capture_launches(monkeypatch)
        si.emit_harness_event_raw("pin", {"event_type": "x"}, discriminator="d1")
        assert len(payloads) == 1
        assert payloads[0]["attempts"] == 2
        assert payloads[0]["timeout"] == 60
        assert payloads[0]["backoff"] == 2

    def test_a_payload_without_attempts_retries_once(self, tmp_path):
        log_path = tmp_path / "sender.log"
        si.deliver_harness_event({
            "base_url": "http://127.0.0.1:9", "token": SYNTHETIC_TOKEN,
            "body": {"idempotency_key": "k"}, "timeout": 0.5, "backoff": 0,
            "log_path": str(log_path), "log_tag": "t",
        })
        assert "after 2 attempt(s)" in log_path.read_text()

    def test_a_4xx_is_not_retried(self, tmp_path):
        calls = []

        class BadRequestHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                calls.append(1)
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                return

        log_path = tmp_path / "sender.log"
        with _http_server(BadRequestHandler) as url:
            si.deliver_harness_event({
                "base_url": url, "token": SYNTHETIC_TOKEN,
                "body": {"idempotency_key": "harness-event-400"}, "timeout": 5,
                "attempts": 2, "backoff": 0, "log_path": str(log_path), "log_tag": "t",
            })
        assert calls == [1]
        text = log_path.read_text()
        assert "after 1 attempt(s): HTTPError 400; key=harness-event-400" in text


# ---------------------------------------------------------------------------
# 8. One definition of "last row with a uuid wins".
# ---------------------------------------------------------------------------
class TestSharedUuidRule:
    @pytest.mark.parametrize("rows", [
        [_row("u-1", "user"), _row("a-1")],
        [_row("a-1"), {"type": "summary", "summary": "no uuid"}],
        [_row("a-1"), {"type": "x", "uuid": 7}, {"type": "x", "uuid": ""}],
        [{"type": "summary"}],
    ])
    def test_scan_and_discriminator_agree(self, tmp_path, rows):
        path = _write_transcript(tmp_path / "t.jsonl", rows)
        assert si.transcript_event_discriminator(path) == si.scan_transcript(path)["last_event_uuid"]

    def test_tail_without_a_uuid_falls_back_to_a_full_pass(self, tmp_path):
        big = {"type": "progress", "data": "y" * (si._TRANSCRIPT_TAIL_BYTES + 10_000)}
        path = _write_transcript(tmp_path / "t.jsonl", [_row("a-early"), big])
        assert si.transcript_event_discriminator(path) == "a-early"
