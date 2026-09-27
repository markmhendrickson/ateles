#!/usr/bin/env python3
"""Tests for session_rule_delivery.py (ateles#1261 follow-up, audit
ent_b66293f0dcc8c887d4fdbeae) and the shared rule_index_state helpers it and
session_rule_index.py both use.

Runs the hook as a REAL subprocess, matching this directory's convention
(test_session_rule_index.py, test_git_stash_guard.py): a minimal
`http.server` stands in for Neotoma so these tests hit a real socket while
staying fully offline and deterministic, and the `__main__` fail-open guard
only executes on a real process run.

Cases:
  1. Unchanged set -> nothing injected, exit 0.
  2. A row added mid-session -> injected once (next unchanged call injects
     nothing again).
  3. A row's content changed (new last_observation_at) -> injected as
     changed, not silently treated as unchanged.
  4. A mandatory row is rendered with FULL rule text; a non-mandatory row
     gets the one-line summary form only.
  5. Output stays bounded (BUDGET_CHARS, itself below the measured
     10,000-char hook-stdout cap).
  6. Fail-open: Neotoma unreachable -> exit 0, nothing printed, one stderr
     line; no crash.
  7. session_rule_index.py (SessionStart) records a delivered signature, so
     the FIRST session_rule_delivery.py call after it finds nothing changed.
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

HOOK = str(Path(__file__).with_name("session_rule_delivery.py"))
INDEX_HOOK = str(Path(__file__).with_name("session_rule_index.py"))
REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _row(entity_id, rule="Do the thing.", applies_when="always", scope="global",
         agent_sub="", status="active", domain="test", rule_kind="mandatory",
         title="", last_observation_at="2026-01-01T00:00:00.000Z"):
    return {
        "entity_id": entity_id,
        "last_observation_at": last_observation_at,
        "snapshot": {
            "rule": rule,
            "title": title,
            "applies_when": applies_when,
            "scope": scope,
            "agent_sub": agent_sub,
            "status": status,
            "domain": domain,
            "rule_kind": rule_kind,
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


@pytest.fixture
def project_dir():
    """A throwaway CLAUDE_PROJECT_DIR so .claude/.session_state/ writes never
    touch the real repo's state directory."""
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _run(hook: str, cwd: Path, project_dir: Path, session_id: str,
         base_url: str | None = None, extra_env: dict | None = None):
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "CLAUDE_PROJECT_DIR": str(project_dir),
    }
    if base_url:
        env["NEOTOMA_BASE_URL"] = base_url
    if extra_env:
        env.update(extra_env)
    payload = json.dumps({"session_id": session_id})
    return subprocess.run(
        [sys.executable, hook],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
        timeout=15,
    )


# ---------------------------------------------------------------------------
# 1. Unchanged set injects nothing.
# ---------------------------------------------------------------------------
class TestUnchangedSetInjectsNothing:
    def test_second_call_with_same_rows_prints_nothing(self, fake_neotoma, project_dir):
        base_url, handler = fake_neotoma
        handler.rows = [_row("ent_a", rule="Rule A.", applies_when="always")]
        first = _run(HOOK, REPO_ROOT, project_dir, "sess-1", base_url=base_url)
        assert first.returncode == 0
        assert first.stdout.strip() != ""  # first call: nothing delivered yet, injects

        second = _run(HOOK, REPO_ROOT, project_dir, "sess-1", base_url=base_url)
        assert second.returncode == 0
        assert second.stdout.strip() == "", (
            f"expected no injection on an unchanged set, got: {second.stdout!r}"
        )


# ---------------------------------------------------------------------------
# 2. A mid-session addition is injected once.
# ---------------------------------------------------------------------------
class TestMidSessionAdditionInjectedOnce:
    def test_new_row_injected_then_not_repeated(self, fake_neotoma, project_dir):
        base_url, handler = fake_neotoma
        handler.rows = [_row("ent_a", rule="Rule A.", applies_when="always")]
        baseline = _run(HOOK, REPO_ROOT, project_dir, "sess-2", base_url=base_url)
        assert baseline.returncode == 0

        # A new rule appears mid-session.
        handler.rows = [
            _row("ent_a", rule="Rule A.", applies_when="always"),
            _row("ent_b", rule="Rule B, brand new.", applies_when="doing X",
                 rule_kind="advisory", title="Do X carefully"),
        ]
        added = _run(HOOK, REPO_ROOT, project_dir, "sess-2", base_url=base_url)
        assert added.returncode == 0
        assert "ent_b" in added.stdout
        assert "ent_a" not in added.stdout, "unchanged row must not be re-injected"

        # Same corpus again -> nothing.
        repeat = _run(HOOK, REPO_ROOT, project_dir, "sess-2", base_url=base_url)
        assert repeat.returncode == 0
        assert repeat.stdout.strip() == ""


# ---------------------------------------------------------------------------
# 3. A changed row (new last_observation_at, same id) is treated as changed.
# ---------------------------------------------------------------------------
class TestChangedRowIsRedelivered:
    def test_updated_timestamp_triggers_reinjection(self, fake_neotoma, project_dir):
        base_url, handler = fake_neotoma
        handler.rows = [
            _row("ent_c", rule="Original text.", applies_when="doing Y",
                 rule_kind="advisory", title="Do Y", last_observation_at="2026-01-01T00:00:00Z"),
        ]
        baseline = _run(HOOK, REPO_ROOT, project_dir, "sess-3", base_url=base_url)
        assert baseline.returncode == 0

        handler.rows = [
            _row("ent_c", rule="Corrected text.", applies_when="doing Y",
                 rule_kind="advisory", title="Do Y (revised)", last_observation_at="2026-01-02T00:00:00Z"),
        ]
        changed = _run(HOOK, REPO_ROOT, project_dir, "sess-3", base_url=base_url)
        assert changed.returncode == 0
        assert "ent_c" in changed.stdout


# ---------------------------------------------------------------------------
# 4. Mandatory rows get full text; advisory rows get the one-line form only,
#    in a DELTA render (a baseline already exists; this exercises
#    _render_delta specifically, not the no-baseline full-index path, which
#    has its own render shape covered by TestNoBaselineDeliversFullIndex).
# ---------------------------------------------------------------------------
class TestRenderShapeByRuleKind:
    def test_mandatory_full_text_advisory_summary_only(self, fake_neotoma, project_dir):
        base_url, handler = fake_neotoma
        # Establish a baseline first (conditional, non-"always" rows only —
        # this test is about _render_delta's per-kind rendering, not about
        # what counts as preamble) so the second call is a genuine delta.
        handler.rows = [
            _row("ent_mand", rule="Some other mandatory rule.",
                 applies_when="doing W", rule_kind="mandatory", title="Do W"),
        ]
        baseline = _run(HOOK, REPO_ROOT, project_dir, "sess-4", base_url=base_url)
        assert baseline.returncode == 0

        handler.rows = [
            _row("ent_mand", rule="THE FULL MANDATORY RULE BODY TEXT.",
                 applies_when="doing W", rule_kind="mandatory", title="Do W",
                 last_observation_at="2026-02-01T00:00:00Z"),
            _row("ent_adv", rule="THE FULL ADVISORY RULE BODY TEXT.",
                 applies_when="doing Z", rule_kind="advisory", title="Do Z"),
        ]
        result = _run(HOOK, REPO_ROOT, project_dir, "sess-4", base_url=base_url)
        assert result.returncode == 0
        assert "THE FULL MANDATORY RULE BODY TEXT" in result.stdout
        # The advisory row's full rule text must NOT appear — only its
        # one-line trigger + imperative summary.
        assert "THE FULL ADVISORY RULE BODY TEXT" not in result.stdout
        assert "ent_adv" in result.stdout
        assert "Do Z" in result.stdout


# ---------------------------------------------------------------------------
# 5. Output stays bounded.
# ---------------------------------------------------------------------------
class TestOutputStaysBounded:
    def test_large_delta_stays_under_measured_cap(self, fake_neotoma, project_dir):
        base_url, handler = fake_neotoma
        handler.rows = [_row(f"ent_seed{i:03d}", applies_when=f"seed {i}") for i in range(5)]
        baseline = _run(HOOK, REPO_ROOT, project_dir, "sess-5", base_url=base_url)
        assert baseline.returncode == 0

        # Every seed row changes, plus many new ones — a large delta.
        handler.rows = [
            _row(f"ent_seed{i:03d}", applies_when=f"seed {i}",
                 last_observation_at="2026-02-01T00:00:00Z")
            for i in range(5)
        ] + [
            _row(f"ent_new{i:03d}", rule="A brand new rule body of moderate length here.",
                 applies_when=f"new condition {i}", rule_kind="mandatory")
            for i in range(60)
        ]
        result = _run(HOOK, REPO_ROOT, project_dir, "sess-5", base_url=base_url)
        assert result.returncode == 0
        assert len(result.stdout) < 10_000, (
            f"delivery hook stdout is {len(result.stdout)} chars — at or "
            "past the measured 10,000-char hook-stdout cap."
        )

    def test_budget_below_measured_cap(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("session_rule_delivery", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.BUDGET_CHARS < 10_000


# ---------------------------------------------------------------------------
# 6. Fail-open on transport failure.
# ---------------------------------------------------------------------------
class TestFailOpen:
    def test_unreachable_neotoma_exits_zero_prints_nothing(self, project_dir):
        closed_port_url = f"http://127.0.0.1:{_free_port()}"
        result = _run(HOOK, REPO_ROOT, project_dir, "sess-6", base_url=closed_port_url)
        assert result.returncode == 0
        assert result.stdout.strip() == ""
        assert "[session-rule-delivery]" in result.stderr

    def test_missing_session_id_is_a_noop(self, fake_neotoma, project_dir):
        base_url, handler = fake_neotoma
        handler.rows = [_row("ent_a", applies_when="always")]
        env = {"PATH": "/usr/bin:/bin:/usr/local/bin",
               "CLAUDE_PROJECT_DIR": str(project_dir),
               "NEOTOMA_BASE_URL": base_url}
        result = subprocess.run(
            [sys.executable, HOOK], input="{}", capture_output=True, text=True,
            cwd=str(REPO_ROOT), env=env, timeout=15,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# 7. session_rule_index.py's delivery is recorded, so the delivery hook's
#    FIRST call afterwards finds nothing changed.
# ---------------------------------------------------------------------------
class TestSessionStartHandoff:
    def test_index_hook_delivery_suppresses_first_delivery_hook_call(
        self, fake_neotoma, project_dir
    ):
        base_url, handler = fake_neotoma
        handler.rows = [
            _row("ent_always1", rule="Never skip the thing.", applies_when="always"),
            _row("ent_cond1", rule="Check before merging.", applies_when="opening a PR",
                 rule_kind="advisory", title="Check first"),
        ]
        index_result = _run(INDEX_HOOK, REPO_ROOT, project_dir, "sess-7", base_url=base_url)
        assert index_result.returncode == 0
        assert "ent_always1" in index_result.stdout

        delivery_result = _run(HOOK, REPO_ROOT, project_dir, "sess-7", base_url=base_url)
        assert delivery_result.returncode == 0
        assert delivery_result.stdout.strip() == "", (
            "the delivery hook re-printed content the SessionStart index "
            f"hook JUST delivered: {delivery_result.stdout!r}"
        )


def _read_state(project_dir: Path, session_id: str) -> dict:
    p = project_dir / ".claude" / ".session_state" / f"{session_id}.json"
    return json.loads(p.read_text())


def _delivered_ids(project_dir: Path, session_id: str) -> set[str]:
    state = _read_state(project_dir, session_id)
    return set((state.get("rule_index_delivered") or {}).get("rows") or {})


# ---------------------------------------------------------------------------
# 8. No baseline (session never got a SessionStart delivery, e.g. it started
#    before the hook pair was wired) -> the FULL tiered index is delivered
#    on the delivery hook's own first call, and every rendered row's id is
#    recorded as the baseline. Regression test for the incident that
#    motivated this fix (task ent_3f5bc7138628cf5b4116d569): the delivery
#    hook used to treat "no baseline" as "diff against {}", run the result
#    through the small delta budget, and then record EVERY candidate row as
#    delivered regardless of whether it was actually rendered.
# ---------------------------------------------------------------------------
class TestNoBaselineDeliversFullIndex:
    def test_no_baseline_renders_full_index_not_a_budget_bound_delta(
        self, fake_neotoma, project_dir
    ):
        base_url, handler = fake_neotoma
        # More rows than a small delta budget would ever render in full, but
        # comfortably inside the full index's own (larger) budget — this
        # must come back as the full tiered index, not a "(N more ...
        # omitted)" delta.
        handler.rows = [
            _row("ent_always", rule="An always rule.", applies_when="always"),
        ] + [
            _row(f"ent_cond{i:03d}", rule=f"Rule body {i}.",
                 applies_when=f"condition {i}", rule_kind="advisory",
                 title=f"Do thing {i}")
            for i in range(20)
        ]
        result = _run(HOOK, REPO_ROOT, project_dir, "sess-8", base_url=base_url)
        assert result.returncode == 0
        assert "no prior delivery baseline" in result.stdout
        # Every row must appear (as at least an id) — this is the full
        # index, not a size-bounded delta that would drop most of them.
        for i in range(20):
            assert f"ent_cond{i:03d}" in result.stdout, (
                f"ent_cond{i:03d} missing from the no-baseline full index"
            )
        assert "more changed rule(s) omitted" not in result.stdout, (
            "no-baseline path must not render the delta form at all"
        )

        delivered = _delivered_ids(project_dir, "sess-8")
        assert "ent_always" in delivered
        for i in range(20):
            assert f"ent_cond{i:03d}" in delivered, (
                f"ent_cond{i:03d} was rendered but not recorded as delivered"
            )

        # Baseline now recorded -> an immediately repeated call is a no-op.
        repeat = _run(HOOK, REPO_ROOT, project_dir, "sess-8", base_url=base_url)
        assert repeat.returncode == 0
        assert repeat.stdout.strip() == "", (
            "a baseline was just recorded; the very next call with an "
            f"unchanged corpus must inject nothing, got: {repeat.stdout!r}"
        )


# ---------------------------------------------------------------------------
# 9. An overflowing DELTA (baseline exists, many rows change/added at once,
#    more than the small per-turn budget can render) must never mark an
#    omitted row as delivered — omitted rows stay undelivered so a later
#    prompt retries them.
# ---------------------------------------------------------------------------
class TestOverflowingDeltaNeverMarksOmittedRowsDelivered:
    def test_omitted_rows_stay_undelivered_and_are_retried_next_turn(
        self, fake_neotoma, project_dir
    ):
        base_url, handler = fake_neotoma
        handler.rows = [_row("ent_seed", rule="Seed.", applies_when="always")]
        baseline = _run(HOOK, REPO_ROOT, project_dir, "sess-9", base_url=base_url)
        assert baseline.returncode == 0
        assert "ent_seed" in _delivered_ids(project_dir, "sess-9")

        # A large batch of NEW mandatory rows lands in one turn — more than
        # the delta budget (BUDGET_CHARS=9000) can render in full at once.
        handler.rows = [_row("ent_seed", rule="Seed.", applies_when="always")] + [
            _row(f"ent_new{i:03d}",
                 rule="A brand new mandatory rule with a reasonably long body "
                      "of text so the delta renders down to only a few in full.",
                 applies_when=f"new condition {i}", rule_kind="mandatory",
                 title=f"Do new thing {i}")
            for i in range(80)
        ]
        result = _run(HOOK, REPO_ROOT, project_dir, "sess-9", base_url=base_url)
        assert result.returncode == 0
        assert "more changed rule(s) omitted for space" in result.stdout, (
            "this batch must overflow the delta budget for the test to be "
            "meaningful"
        )

        rendered_new_ids = {
            f"ent_new{i:03d}" for i in range(80) if f"ent_new{i:03d}" in result.stdout
        }
        assert 0 < len(rendered_new_ids) < 80, (
            "expected a partial render (some rendered, some omitted) — got "
            f"{len(rendered_new_ids)} of 80"
        )

        delivered = _delivered_ids(project_dir, "sess-9")
        omitted_ids = {f"ent_new{i:03d}" for i in range(80)} - rendered_new_ids
        assert omitted_ids, "expected at least one omitted row for this test to be meaningful"
        for eid in omitted_ids:
            assert eid not in delivered, (
                f"{eid} was OMITTED from the rendered delta but recorded as "
                "delivered anyway — it will never be retried"
            )
        for eid in rendered_new_ids:
            assert eid in delivered, f"{eid} was rendered but not recorded as delivered"

        # The omitted rows must still show up as changed on the NEXT turn
        # (unchanged corpus this time) precisely because they were never
        # marked delivered.
        retry = _run(HOOK, REPO_ROOT, project_dir, "sess-9", base_url=base_url)
        assert retry.returncode == 0
        retried_ids = {
            eid for eid in omitted_ids if eid in retry.stdout
        }
        assert retried_ids, (
            "omitted rows from the previous turn were not retried on the "
            f"next prompt with an unchanged corpus: {retry.stdout!r}"
        )
