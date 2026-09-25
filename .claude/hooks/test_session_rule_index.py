#!/usr/bin/env python3
"""Tests for session_rule_index.py (ateles#1261).

Runs the hook as a REAL subprocess (matching this directory's convention in
test_git_stash_guard.py / test_gh_identity_guard.py) rather than importing
it, for two reasons specific to this hook: (1) the coordinator's scope
addition requires proving cwd-independence, which only a real subprocess
with a different `cwd` can demonstrate — an in-process import always runs
with pytest's own cwd and would pass vacuously if the hook secretly depended
on it; (2) the `__main__` fail-open guard (the bare `except Exception:
sys.exit(0)` at the bottom of the file) only executes on a real process
run, not on an imported `main()` call.

A minimal `http.server` stands in for Neotoma so these tests hit a real
socket (proving the hook's actual HTTP path, not a monkeypatched stub) while
staying fully offline and deterministic.

Cases:
  1. Happy path, cwd INSIDE the repo — sanity baseline.
  2. Happy path, cwd OUTSIDE the repo entirely (a bare tmp dir with no
     .git) — the coordinator's required cwd-independence proof: the hook
     still renders the full index because it resolves `lib/` from
     `Path(__file__)`, never from cwd or CLAUDE_PROJECT_DIR.
  3. Unreachable Neotoma (NEOTOMA_BASE_URL pointed at a closed port) — one
     stderr line, stdout carries the fail-open notice, exit 0.
  3b. A 302 from Neotoma to another host is refused: the other host never
      receives a request (so never the bearer token), and the hook falls open.
  4. Preamble-first ordering survives the actual subprocess/JSON round trip.
"""
from __future__ import annotations

import http.server
import json
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

HOOK = str(Path(__file__).with_name("session_rule_index.py"))
REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _row(entity_id, rule="Do the thing.", applies_when="", scope="global", agent_sub="", status="active", domain="test"):
    return {
        "entity_id": entity_id,
        "snapshot": {
            "rule": rule,
            "applies_when": applies_when,
            "scope": scope,
            "agent_sub": agent_sub,
            "status": status,
            "domain": domain,
            "rule_kind": "mandatory",
        },
    }


class _FakeNeotomaHandler(http.server.BaseHTTPRequestHandler):
    rows: list[dict] = []

    def do_POST(self):  # noqa: N802 — stdlib method name
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)  # drain body, unused
        body = json.dumps({"entities": self.rows}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: A003 — silence test output
        pass


@pytest.fixture
def fake_neotoma():
    """Start a throwaway HTTP server that answers /entities/query with a
    fixed corpus; yields its base_url. Torn down after the test.
    """
    handler = type("Handler", (_FakeNeotomaHandler,), {"rows": []})
    port = _free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", handler
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _run(cwd: Path, base_url: str | None = None, extra_env: dict | None = None):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin"}
    if base_url:
        env["NEOTOMA_BASE_URL"] = base_url
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, HOOK],
        input="{}",
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
        timeout=15,
    )


# ---------------------------------------------------------------------------
# 1 & 2. cwd independence
# ---------------------------------------------------------------------------
class TestCwdIndependence:
    def test_happy_path_cwd_inside_repo(self, fake_neotoma):
        base_url, handler = fake_neotoma
        handler.rows = [
            _row("ent_always1", rule="Never skip the thing.", applies_when="always"),
            _row("ent_cond1", rule="Check before merging.", applies_when="opening a PR"),
        ]
        result = _run(REPO_ROOT, base_url=base_url)
        assert result.returncode == 0
        assert "ent_always1" in result.stdout
        assert "ent_cond1" in result.stdout

    def test_happy_path_cwd_outside_any_ateles_checkout(self, fake_neotoma):
        """The coordinator's required proof: cwd is a bare tmp dir with no
        .git and no relationship to any ateles checkout at all. The hook
        must still render the full index, because it resolves `lib/`
        relative to its OWN file location, not cwd or CLAUDE_PROJECT_DIR —
        exactly what makes it safe to wire from a user-level
        ~/.claude/settings.json by absolute path into ~/ateles-rc-src.
        """
        base_url, handler = fake_neotoma
        handler.rows = [
            _row("ent_always1", rule="Never skip the thing.", applies_when="always"),
            _row("ent_cond1", rule="Check before merging.", applies_when="opening a PR"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "nowhere-near-a-repo"
            outside.mkdir()
            result = _run(outside, base_url=base_url)
        assert result.returncode == 0
        assert "ent_always1" in result.stdout
        assert "ent_cond1" in result.stdout
        # Prove it is the SAME rendered content as the in-repo run, not a
        # degraded stub — both must carry the preamble heading.
        assert "## Always-applies rules" in result.stdout

    def test_outside_cwd_result_matches_inside_cwd_result(self, fake_neotoma):
        base_url, handler = fake_neotoma
        handler.rows = [_row("ent_x", rule="Rule X.", applies_when="always")]
        inside = _run(REPO_ROOT, base_url=base_url)
        with tempfile.TemporaryDirectory() as tmp:
            outside_dir = Path(tmp) / "elsewhere"
            outside_dir.mkdir()
            outside = _run(outside_dir, base_url=base_url)
        assert inside.stdout == outside.stdout


# ---------------------------------------------------------------------------
# 3. Unreachable Neotoma
# ---------------------------------------------------------------------------
class TestUnreachableNeotomaFallsOpen:
    def test_closed_port_prints_one_line_notice_and_exits_zero(self):
        closed_port_url = f"http://127.0.0.1:{_free_port()}"  # nothing listening
        result = _run(REPO_ROOT, base_url=closed_port_url)
        assert result.returncode == 0
        assert "could not be loaded" in result.stdout
        assert "[agent_policy]" in result.stdout
        # Fail-open diagnostics go to stderr, not stdout, and the failure
        # reason is logged there (not swallowed entirely).
        assert "[session-rule-index]" in result.stderr

    def test_fail_open_notice_never_includes_a_rule_body(self):
        """Even the fail-open path must not have leaked a rule's `rule` text
        anywhere — there is none to leak (transport never succeeded), but
        this pins that the notice text itself is a fixed, generic string.
        """
        closed_port_url = f"http://127.0.0.1:{_free_port()}"
        result = _run(REPO_ROOT, base_url=closed_port_url)
        assert "operator" not in result.stdout.lower()
        assert "payment" not in result.stdout.lower()


# ---------------------------------------------------------------------------
# 3b. A redirect is refused, so the bearer token never reaches another host
#     (Falco, ateles#1268 round 3). The fake Neotoma answers 302 pointing at
#     a second server on another port, which records any request it gets.
# ---------------------------------------------------------------------------
class TestRedirectIsRefusedOverSubprocess:
    def test_bearer_token_never_follows_a_redirect_to_another_host(self):
        seen: list[dict] = []

        class Sink(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                seen.append(dict(self.headers))
                body = json.dumps({"entities": [_row("ent_from_sink", applies_when="x")]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer  # noqa: N815

            def log_message(self, *a):  # noqa: A003
                pass

        sink = http.server.HTTPServer(("127.0.0.1", 0), Sink)
        sink_url = f"http://127.0.0.1:{sink.server_address[1]}"

        class Redirector(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(302)
                self.send_header("Location", f"{sink_url}/entities/query")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):  # noqa: A003
                pass

        origin = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
        threads = [
            threading.Thread(target=srv.serve_forever, daemon=True) for srv in (sink, origin)
        ]
        for t in threads:
            t.start()
        try:
            result = _run(
                REPO_ROOT,
                base_url=f"http://127.0.0.1:{origin.server_address[1]}",
                extra_env={"NEOTOMA_BEARER_TOKEN": "fake-token-for-test"},
            )
        finally:
            origin.shutdown()
            sink.shutdown()
        assert result.returncode == 0
        assert seen == [], "the hook followed the redirect to another host"
        assert "ent_from_sink" not in result.stdout
        assert "could not be loaded" in result.stdout  # fail-open notice
        assert "fake-token-for-test" not in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# 4. Preamble-first, over the real subprocess/JSON path
# ---------------------------------------------------------------------------
class TestPreambleFirstOverSubprocess:
    def test_always_rule_appears_before_conditional_in_subprocess_output(self, fake_neotoma):
        base_url, handler = fake_neotoma
        handler.rows = [
            _row("ent_conditional_first_in_feed", applies_when="doing X"),
            _row("ent_always_rule", applies_when="always"),
        ]
        result = _run(REPO_ROOT, base_url=base_url)
        assert result.returncode == 0
        assert result.stdout.index("ent_always_rule") < result.stdout.index(
            "ent_conditional_first_in_feed"
        )


# ---------------------------------------------------------------------------
# 5. A large corpus degrades (tiers), it does not fail open.
# ---------------------------------------------------------------------------
class TestLargeCorpusDegradesRatherThanFailsOpen:
    def test_60_rules_over_the_real_subprocess_path_still_renders_a_tier(self, fake_neotoma):
        base_url, handler = fake_neotoma
        handler.rows = [
            _row("ent_always1", rule="Never skip the safety check.", applies_when="always"),
        ] + [
            _row(f"ent_cond{i:03d}", rule="Rule body text here.", applies_when=f"condition {i}")
            for i in range(60)
        ]
        result = _run(REPO_ROOT, base_url=base_url)
        assert result.returncode == 0
        # NOT the fail-open notice — a real rendered index, some tier.
        assert "could not be loaded" not in result.stdout
        assert "<!-- tier:" in result.stdout
        assert "ent_always1" in result.stdout


# ---------------------------------------------------------------------------
# Settings-contract: the hook is actually wired at repo level.
# ---------------------------------------------------------------------------
class TestSettingsContract:
    @pytest.fixture
    def settings(self):
        return json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())

    def test_session_rule_index_wired_for_startup_resume_clear_compact(self, settings):
        entries = [
            entry
            for entry in settings["hooks"]["SessionStart"]
            if any(
                "session_rule_index.py" in h.get("command", "")
                for h in entry.get("hooks", [])
            )
        ]
        assert entries, "no SessionStart entry wires session_rule_index.py"
        matchers = {e.get("matcher", "") for e in entries}
        assert any(
            set(m.split("|")) >= {"startup", "resume", "clear", "compact"}
            for m in matchers
        )
