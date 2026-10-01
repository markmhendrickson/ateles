"""Effect tests for the cross-harness reporting contract and Stop gate."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import report_quality_gate as gate


def _claude_row(role: str, text: str) -> dict:
    return {
        "type": role,
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def _codex_row(phase: str, text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": phase,
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _write(tmp_path: Path, rows: list[dict]) -> str:
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return str(path)


@pytest.mark.parametrize(
    "rows",
    [
        [_claude_row("assistant", "I’ll inspect the file next."),
         _claude_row("assistant", "I’ll run the focused test next."),
         _claude_row("assistant", "Implemented. This prevents duplicate work. Next: review owns verification.")],
        [_codex_row("commentary", "I’ll inspect the file next."),
         _codex_row("commentary", "I’ll run the focused test next."),
         _codex_row("final_answer", "Implemented. This prevents duplicate work. Next: review owns verification.")],
    ],
)
def test_claude_and_codex_fixtures_block_per_tool_narration(tmp_path, rows):
    transcript = gate.read_transcript(_write(tmp_path, rows))
    assert any("per-tool narration" in item for item in gate.findings(transcript))


@pytest.mark.parametrize(
    "rows",
    [
        [_claude_row("assistant", "The capacity reader now covers every configured provider. "
          "That removes the false exhausted state blocking the parent reliability plan. "
          "Next: the reviewer verifies recovery from stale readings.")],
        [_codex_row("commentary", "Capacity moved from stale to live."),
         _codex_row("final_answer", "The capacity reader now covers every configured provider. "
          "That removes the false exhausted state blocking the parent reliability plan. "
          "Next: the reviewer verifies recovery from stale readings.")],
    ],
)
def test_claude_and_codex_operator_altitude_reports_pass(tmp_path, rows):
    transcript = gate.read_transcript(_write(tmp_path, rows))
    assert gate.findings(transcript) == []


def test_mechanism_dense_final_without_impact_or_next_action_blocks():
    transcript = gate.Transcript(
        commentary=(),
        final=(
            "Changed execution/daemons/apis/router.py at 21d60ce0; PID 4821, "
            "rc=0, 37 tests passed, ent_1234567890abcdef.",
        ),
    )
    found = gate.findings(transcript)
    assert any("mechanism-dense" in item for item in found)
    assert any("next owner/action" in item for item in found)


def test_impact_language_does_not_count_as_a_next_action():
    transcript = gate.Transcript(
        commentary=(),
        final=(
            "Changed execution/daemons/apis/router.py at 21d60ce0; PID 4821, "
            "rc=0, 37 tests passed. This will prevent regressions.",
        ),
    )
    found = gate.findings(transcript)
    assert any("next owner/action" in item for item in found)


def test_jargon_dense_final_without_translation_blocks():
    transcript = gate.Transcript(
        commentary=(),
        final=("The JSONL payload matcher wrote stdout, stderr, and exit code.",),
    )
    assert any("jargon-dense" in item for item in gate.findings(transcript))


def test_short_final_that_defers_to_collapsed_commentary_blocks():
    transcript = gate.Transcript(
        commentary=("The full result and reasoning are above.",),
        final=("Done — see the updates above.",),
    )
    assert any("self-contained" in item for item in gate.findings(transcript))


def test_short_settled_final_after_commentary_is_self_contained():
    transcript = gate.Transcript(
        commentary=("The review checked the requested reporting behavior.",),
        final=("Review complete. No changes are needed.",),
    )
    assert gate.findings(transcript) == []


def test_claude_stop_hook_preserves_exit_two_contract(tmp_path, monkeypatch, capsys):
    path = _write(
        tmp_path,
        [
            _codex_row("commentary", "I’ll inspect the file next."),
            _codex_row("commentary", "I’ll run the test next."),
            _codex_row("final_answer", "Done — see above."),
        ],
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"transcript_path": path})))
    assert gate.main() == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "block"


def test_codex_stable_last_message_field_owns_final(tmp_path, monkeypatch, capsys):
    path = _write(
        tmp_path,
        [_codex_row("commentary", "The transcript wire format could change.")],
    )
    event = {
        "hook_event_name": "Stop",
        "model": "codex-test-model",
        "transcript_path": path,
        "last_assistant_message": "Done — see the updates above.",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    assert gate.main() == 0
    assert "self-contained" in json.loads(capsys.readouterr().out)["reason"]


def test_codex_last_message_preserves_all_phased_commentary(
    tmp_path, monkeypatch, capsys
):
    path = _write(
        tmp_path,
        [
            _codex_row("commentary", "I’ll inspect the file next."),
            _codex_row("commentary", "I’ll run the test next."),
        ],
    )
    event = {
        "hook_event_name": "Stop",
        "model": "codex-test-model",
        "transcript_path": path,
        "last_assistant_message": (
            "Review complete. This confirms the behavior. No changes are needed."
        ),
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    assert gate.main() == 0
    assert "per-tool narration" in json.loads(capsys.readouterr().out)["reason"]


def test_missing_transcript_fails_open(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert gate.main() == 0


def test_prior_turn_narration_does_not_poison_current_turn(tmp_path):
    rows = [
        _claude_row("assistant", "I’ll inspect the file next."),
        _claude_row("assistant", "I’ll run the test next."),
        {"type": "user", "message": {"role": "user", "content": "Continue."}},
        _claude_row(
            "assistant",
            "The fix is complete. It prevents repeat dispatch failure. Next: review owns verification.",
        ),
    ]
    transcript = gate.read_transcript(_write(tmp_path, rows))
    assert gate.findings(transcript) == []
