"""Tests for the workstream-coverage Stop hook.

Session work pickup plan (`ent_0b3ec4b252ee88a2cd88ab25`), Phase 1. Structured
like the sibling `test_decision_shape_gate.py` (module import, no subprocess,
`emit_harness_event_raw` monkeypatched so the suite never makes a live network
call regardless of whether `NEOTOMA_BEARER_TOKEN` happens to be set).

Regression anchor: the task that motivated this hook reproduced two concrete
failure modes live — an agent stalled waiting on a Monitor, and a session
reporting it had delegated work it never actually dispatched. Both are
encoded below as the `stalled_monitor` / `false_delegation_report` cases.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import workstream_coverage_gate as wg  # noqa: E402


def _write(tmp_path: Path, name: str, rows: list[dict]) -> str:
    p = tmp_path / name
    with p.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return str(p)


def _no_emit(monkeypatch):
    calls = []
    monkeypatch.setattr(
        wg, "emit_harness_event_raw", lambda *a, **k: calls.append((a, k))
    )
    return calls


def _text_row(role: str, text: str) -> dict:
    return {"type": role, "message": {"role": role, "content": text}}


def _tool_use_row(name: str, input_: dict | None = None) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": name, "input": input_ or {}}],
        },
    }


def _assistant_text_row(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _tool_result_row(payload) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "content": json.dumps(payload)}],
        },
    }


# ---------------------------------------------------------------------------
# The two live-reproduced failure modes this hook exists to catch.
# ---------------------------------------------------------------------------
class TestReproducedFailureModes:
    def test_stalled_monitor_is_flagged(self, tmp_path):
        """An Agent dispatch with no later resolution-shaped call is unresolved."""
        rows = [
            _text_row("user", "dispatch the fix and watch it"),
            _tool_use_row("Agent", {"description": "fix the bug"}),
            _text_row("user", "ok"),
            _assistant_text_row("Dispatched. Waiting on the agent."),
        ]
        path = _write(tmp_path, "t.jsonl", rows)
        summary = wg.scan(path)
        assert "Agent" in summary["unresolved_dispatches"]

    def test_false_delegation_report_is_flagged_even_though_it_claims_done(self, tmp_path):
        """A turn claiming 'dispatched, all done' with no follow-up tool call
        must still be caught — the finding is about tool-level evidence, not
        about what the closing prose asserts."""
        rows = [
            _text_row("user", "dispatch the fix and check on it"),
            _tool_use_row("Agent", {"description": "fix the bug"}),
            _text_row("user", "ok"),
            _assistant_text_row(
                "I dispatched an agent to fix the bug. All done, nothing "
                "further needed."
            ),
        ]
        path = _write(tmp_path, "t.jsonl", rows)
        summary = wg.scan(path)
        closing = wg._closing_section(wg._final_assistant_text(path))
        # "Agent" the tool name never literally appears in the closing prose,
        # so the mention-suppression correctly does not credit a claim of
        # completion with having surfaced the dispatch.
        found = wg.uncovered_findings(summary, closing)
        assert any("Agent" in f for f in found)

    def test_dispatch_followed_by_sendmessage_is_resolved(self, tmp_path):
        rows = [
            _tool_use_row("Agent", {"description": "fix the bug"}),
            _tool_use_row("SendMessage", {"to": "agent-1"}),
            _assistant_text_row("Checked in with the agent; fix landed."),
        ]
        path = _write(tmp_path, "t.jsonl", rows)
        summary = wg.scan(path)
        assert summary["unresolved_dispatches"] == []


# ---------------------------------------------------------------------------
# Open-entity detection (durable Neotoma state touched but not surfaced).
# ---------------------------------------------------------------------------
class TestOpenEntityDetection:
    def test_open_checkpoint_seen_and_unmentioned_is_flagged(self, tmp_path):
        rows = [
            _tool_use_row("mcp__ateles__list_checkpoints"),
            _tool_result_row([{
                "entity_id": "ent_abc123",
                "entity_type": "checkpoint_brief",
                "status": "awaiting_operator",
                "title": "DEPLOY gate: something important",
            }]),
            _assistant_text_row("All clear, nothing left to do here today."),
        ]
        path = _write(tmp_path, "t.jsonl", rows)
        summary = wg.scan(path)
        assert summary["open_entities"] != []
        closing = wg._closing_section(wg._final_assistant_text(path))
        found = wg.uncovered_findings(summary, closing)
        assert any("ent_abc123" in f or "DEPLOY" in f for f in found)

    def test_open_checkpoint_mentioned_in_closing_is_not_flagged(self, tmp_path):
        rows = [
            _tool_use_row("mcp__ateles__list_checkpoints"),
            _tool_result_row([{
                "entity_id": "ent_abc123",
                "entity_type": "checkpoint_brief",
                "status": "awaiting_operator",
                "title": "DEPLOY gate: something important",
            }]),
            _assistant_text_row(
                "One open item remains: the DEPLOY gate checkpoint "
                "(ent_abc123) is awaiting your decision."
            ),
        ]
        path = _write(tmp_path, "t.jsonl", rows)
        summary = wg.scan(path)
        closing = wg._closing_section(wg._final_assistant_text(path))
        assert wg.uncovered_findings(summary, closing) == []

    def test_resolved_checkpoint_status_does_not_flag(self, tmp_path):
        """A checkpoint already `approved`/`rejected` is not an open workstream."""
        rows = [
            _tool_use_row("mcp__ateles__list_checkpoints"),
            _tool_result_row([{
                "entity_id": "ent_abc123",
                "entity_type": "checkpoint_brief",
                "status": "approved",
                "title": "DEPLOY gate: something important",
            }]),
            _assistant_text_row("Done."),
        ]
        path = _write(tmp_path, "t.jsonl", rows)
        summary = wg.scan(path)
        assert summary["open_entities"] == []


# ---------------------------------------------------------------------------
# Silence on healthy / no-signal sessions.
# ---------------------------------------------------------------------------
class TestSilentOnHealthy:
    def test_no_dispatch_no_entity_produces_no_findings(self, tmp_path):
        rows = [
            _text_row("user", "what's the weather"),
            _assistant_text_row("I don't have that information."),
        ]
        path = _write(tmp_path, "t.jsonl", rows)
        summary = wg.scan(path)
        assert summary["unresolved_dispatches"] == []
        assert summary["open_entities"] == []

    def test_below_min_turns_is_not_flagged(self, tmp_path, monkeypatch):
        calls = _no_emit(monkeypatch)
        rows = [_tool_use_row("Agent", {"description": "x"})]
        path = _write(tmp_path, "t.jsonl", rows)
        stdin = json.dumps({"session_id": "s1", "transcript_path": path})
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(stdin))
        monkeypatch.setattr(wg, "ENFORCE", True)
        assert wg.main() == 0
        assert calls == []  # never even reached the emission/finding path


# ---------------------------------------------------------------------------
# stop_hook_active guard — one forced turn only, matching the sibling hooks.
# ---------------------------------------------------------------------------
class TestStopHookActiveGuard:
    def test_second_stop_is_never_reblocked(self, tmp_path, monkeypatch):
        rows = [
            _tool_use_row("Agent", {"description": "fix"}),
            _text_row("user", "ok"),
            _assistant_text_row("Dispatched."),
        ]
        path = _write(tmp_path, "t.jsonl", rows)
        stdin = json.dumps({
            "session_id": "s1", "transcript_path": path, "stop_hook_active": True,
        })
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(stdin))
        monkeypatch.setattr(wg, "ENFORCE", True)
        assert wg.main() == 0


# ---------------------------------------------------------------------------
# WARN vs BLOCK mode, and the never-consent-gated bound.
# ---------------------------------------------------------------------------
class TestModeAndScopeBound:
    def test_warn_mode_never_blocks(self, tmp_path, monkeypatch):
        calls = _no_emit(monkeypatch)
        rows = [
            _tool_use_row("Agent", {"description": "fix"}),
            _text_row("user", "ok"),
            _assistant_text_row("Dispatched, all done."),
        ]
        path = _write(tmp_path, "t.jsonl", rows)
        stdin = json.dumps({"session_id": "s1", "transcript_path": path})
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(stdin))
        monkeypatch.setattr(wg, "ENFORCE", False)
        assert wg.main() == 0
        assert len(calls) == 1  # still emits the audit row even while warning

    def test_block_mode_blocks_and_emits(self, tmp_path, monkeypatch, capsys):
        calls = _no_emit(monkeypatch)
        rows = [
            _tool_use_row("Agent", {"description": "fix"}),
            _text_row("user", "ok"),
            _assistant_text_row("Dispatched, all done."),
        ]
        path = _write(tmp_path, "t.jsonl", rows)
        stdin = json.dumps({"session_id": "s1", "transcript_path": path})
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(stdin))
        monkeypatch.setattr(wg, "ENFORCE", True)
        assert wg.main() == 2
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["decision"] == "block"
        assert len(calls) == 1

    def test_finding_text_never_asks_for_a_consent_gated_action(self, tmp_path):
        """The reason text must only ask for mention/dispatch/report — never
        a send, publish, merge, or other irreversible external action. This
        is the plan's explicit bound: escalation reorders but never signs."""
        rows = [
            _tool_use_row("Agent", {"description": "fix"}),
            _assistant_text_row("Dispatched, all done."),
        ]
        path = _write(tmp_path, "t.jsonl", rows)
        summary = wg.scan(path)
        closing = wg._closing_section(wg._final_assistant_text(path))
        found = wg.uncovered_findings(summary, closing)
        assert found
        for banned in ("send it", "publish it", "merge it", "approve it"):
            assert banned not in " ".join(found).lower()


# ---------------------------------------------------------------------------
# Fail-open: malformed input must never crash or block.
# ---------------------------------------------------------------------------
class TestFailOpen:
    def test_garbage_stdin_exits_zero(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("not json {{{"))
        monkeypatch.setattr(wg, "ENFORCE", True)
        assert wg.main() == 0

    def test_empty_stdin_exits_zero(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(""))
        monkeypatch.setattr(wg, "ENFORCE", True)
        assert wg.main() == 0

    def test_missing_transcript_file_exits_zero(self, monkeypatch):
        stdin = json.dumps({
            "session_id": "s1", "transcript_path": "/no/such/file.jsonl",
        })
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(stdin))
        monkeypatch.setattr(wg, "ENFORCE", True)
        assert wg.main() == 0

    def test_scan_never_raises_on_malformed_rows(self, tmp_path):
        p = tmp_path / "bad.jsonl"
        p.write_text("{not valid json\n{\"type\": \"assistant\"}\n", encoding="utf-8")
        summary = wg.scan(str(p))
        assert summary["turns"] >= 0  # did not raise


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
