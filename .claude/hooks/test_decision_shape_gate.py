"""Tests for the decision-shape Stop hook.

Structured like the pytest-class sibling `test_sibling_repo_worktree_guard.py`
(module import, `monkeypatch`, no subprocess) rather than the standalone
`test_git_stash_guard.py` script — that file defines no `test_*` functions and
so collects zero cases under `pytest .claude/hooks/` (verified: "no tests ran"),
which is not a convention to extend.

Every test that drives `main()` monkeypatches `emit_harness_event_raw` so the
suite never makes a live network call, regardless of whether
`NEOTOMA_BEARER_TOKEN` happens to be set in the environment it runs in.
"""

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import decision_shape_gate as dsg
import _session_integrity as si


def _no_emit(monkeypatch):
    """Block the real HTTP emission path for the duration of a test."""
    calls = []
    monkeypatch.setattr(
        dsg, "emit_harness_event_raw", lambda *a, **k: calls.append((a, k))
    )
    return calls


# ---------------------------------------------------------------------------
# The two bugs this PR already fixed once, committed verbatim as regression
# cases so a future edit to any of the three regexes can't silently reopen
# either. Strings are the literal repros from the fixing commits' own
# messages (35d78c42, PR #951 round 1), not paraphrased.
# ---------------------------------------------------------------------------
class TestProbingDiscoveredBugs:
    def test_loxia_same_line_false_negative_is_now_caught(self):
        # 35d78c42: the consent exemption honoured ". " as a start boundary
        # but only "\n" as an end boundary, so a consent keyword in a LATER
        # sentence on the SAME LINE still suppressed a real finding. Fixed by
        # bounding the sentence at [.?!] followed by whitespace, or a newline.
        text = (
            "Want me to dispatch the column fix? Separately, the deploy is "
            "yours to run."
        )
        found = dsg.findings(text)
        assert len(found) == 1
        assert "asking permission" in found[0]

    @pytest.mark.parametrize(
        "text",
        [
            # five false-positive probes from the enabling-BLOCK regression
            # run (feat commit 30b01523) and the round-2 fix (35d78c42) —
            # each must return zero findings.
            'The plan says the schema is "unchanged." — nothing shifted here.',
            "Rotation is operator-only by design, which is why the fix "
            "closes the path instead.",
            "Should I send the email to the client now, or wait for your "
            "review of the draft first?",
            "I dispatched the fix and verified it against main. No further "
            "action needed.",
        ],
    )
    def test_five_false_positive_probes_produce_no_findings(self, text):
        assert dsg.findings(text) == []

    def test_quoted_inside_a_brief_is_NOT_actually_exempt(self):
        # Known gap, not a passing regression case: the docstring and
        # CLAUDE.md both claim "the phrase quoted inside an agent brief" was
        # one of five probes that returned ZERO findings before BLOCK was
        # enabled. No literal probe string for this case was ever committed
        # (checked: absent from every commit touching this file), and every
        # natural phrasing of it tried here still fires — PERMISSION_RE has
        # no quote-awareness. Pinned so the documented claim is visibly false
        # rather than silently unverified; see PR comment for the writeup.
        text = (
            'The dispatched brief tells the agent: "Shall I dispatch the '
            'fix?" is the exact phrasing to avoid using.'
        )
        found = dsg.findings(text)
        assert len(found) == 1  # documents actual behavior, not the docstring's claim

    def test_the_real_failing_list_produces_findings(self):
        # feat commit 30b01523: "the real failing list" produced three
        # findings before BLOCK was enabled — one per check findings()
        # implements. UNCHANGED_RE is anchored to the END of the text
        # (`\bunchanged\.\s*$`, no re.M), so "unchanged." must be the last
        # sentence, not a middle one, for this case to fire.
        text = (
            "Shall I dispatch the fix? You must run the migration. "
            "The schema is unchanged."
        )
        found = dsg.findings(text)
        assert len(found) == 3


# ---------------------------------------------------------------------------
# PERMISSION_RE — true/false pairs. The near-miss matters more than the
# obvious positive: "would you like" phrasing that is not this hook's target
# shape must not fire.
# ---------------------------------------------------------------------------
class TestPermissionRe:
    def test_true_positive(self):
        assert dsg.PERMISSION_RE.search("Shall I dispatch the fix now?")

    def test_true_negative_near_miss(self):
        # "would you like" (a summary offer) is adjacent phrasing to "would
        # you like me to" but is not a permission-to-act question.
        text = "I already ran the migration; would you like a summary of the changes?"
        assert dsg.PERMISSION_RE.search(text) is None


# ---------------------------------------------------------------------------
# CONSENT_GATED_RE — true/false pairs.
# ---------------------------------------------------------------------------
class TestConsentGatedRe:
    def test_true_positive_suppresses_finding(self):
        # "send" is consent-gated, and sits in the SAME sentence as the
        # permission question, so no finding should fire.
        assert dsg.findings("Want me to send the email to the client?") == []

    def test_true_negative_does_not_suppress(self):
        # No consent-gated word anywhere near the question — must fire.
        found = dsg.findings("Shall I fix the failing test now?")
        assert len(found) == 1


# ---------------------------------------------------------------------------
# Sentence-boundary scoping in findings() — a disqualifying/qualifying phrase
# in an ADJACENT sentence must not leak across the boundary either way.
# ---------------------------------------------------------------------------
class TestSentenceScoping:
    def test_consent_word_in_next_sentence_does_not_suppress(self):
        # This is the shape the Loxia fix targets, restated as a direct
        # scoping assertion rather than the historical repro string.
        text = "Should I fix this now? A merge landed yesterday on an unrelated PR."
        found = dsg.findings(text)
        assert len(found) == 1

    def test_consent_word_in_previous_sentence_does_not_suppress(self):
        text = "A merge landed yesterday on an unrelated PR. Should I fix this now?"
        found = dsg.findings(text)
        assert len(found) == 1


# ---------------------------------------------------------------------------
# The START-boundary asymmetry — the mirror image of the END-boundary fix,
# found by the qa lens on PR #951 by direct execution.
#
# The end boundary recognised `[.?!]\s|\n`; the start recognised only "\n"
# and ". ". So a PREVIOUS sentence ending in "?" or "!" was pulled into the
# scoped sentence whole, and a consent keyword inside it suppressed a genuine
# finding. Every case below FAILS against the pre-fix code (the two defect
# cases assert a finding the pre-fix code suppressed) and passes after, except
# the controls and the true negative, which pin the behaviour that must NOT
# change. The `.`-terminated control above already passed before the fix,
# which is exactly why it is not sufficient on its own.
# ---------------------------------------------------------------------------
class TestSentenceStartBoundarySymmetry:
    def test_question_terminated_prior_sentence_does_not_suppress(self):
        # The defect case, verbatim from the qa lens's reproduction.
        text = "Is the deploy still scheduled for today? Should I fix the parser bug?"
        assert len(dsg.findings(text)) == 1

    def test_bang_terminated_prior_sentence_does_not_suppress(self):
        text = "The deploy shipped without review! Should I fix the parser bug?"
        assert len(dsg.findings(text)) == 1

    def test_control_period_terminated_prior_sentence_still_fires(self):
        text = "The deploy went out today. Should I fix the parser bug?"
        assert len(dsg.findings(text)) == 1

    def test_control_bare_question_still_fires(self):
        text = "Should I fix the parser bug?"
        assert len(dsg.findings(text)) == 1

    def test_true_negative_genuine_consent_gate_still_suppressed(self):
        # Widening the start boundary must not turn a real consent-gated
        # question into a finding.
        text = "Should I run the deploy to production?"
        assert dsg.findings(text) == []

    def test_both_ends_use_one_shared_boundary_definition(self):
        # The defect was two copies of one concept drifting apart. Assert the
        # scoped sentence is exactly the permission question in both
        # directions, so a future edit to one end cannot silently widen only
        # that end.
        prior_q = "Is the deploy scheduled? Should I fix the parser bug? A merge landed."
        m = dsg.PERMISSION_RE.search(prior_q)
        assert dsg.sentence_around(prior_q, m.start(), m.end()).strip() == (
            "Should I fix the parser bug?"
        )


# ---------------------------------------------------------------------------
# closing_section() 2500-char truncation boundary.
# ---------------------------------------------------------------------------
class TestClosingSectionBoundary:
    def test_exactly_2500_is_not_truncated(self):
        text = "a" * 2500
        assert dsg.closing_section(text) == text

    def test_just_under_2500_is_not_truncated(self):
        text = "a" * 2499
        assert dsg.closing_section(text) == text

    def test_just_over_2500_is_truncated_to_last_2500(self):
        text = "a" * 2501
        result = dsg.closing_section(text)
        assert len(result) == 2500
        assert result == text[-2500:]


# ---------------------------------------------------------------------------
# Empty / short transcript — no-op rather than raising or false-triggering.
# ---------------------------------------------------------------------------
class TestEmptyOrShortTranscript:
    def test_empty_string_returns_no_findings(self):
        assert dsg.findings("") == []

    def test_whitespace_only_returns_no_findings(self):
        assert dsg.findings("   \n\t  ") == []

    def test_short_benign_transcript_returns_no_findings(self):
        assert dsg.findings("Done.") == []


# ---------------------------------------------------------------------------
# stop_hook_active short-circuit — one nudge per stop.
# ---------------------------------------------------------------------------
class TestStopHookActiveShortCircuit:
    def test_main_returns_zero_without_classifying(self, monkeypatch):
        calls = _no_emit(monkeypatch)
        ev = {"stop_hook_active": True, "transcript_path": "/nonexistent/path"}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(ev)))
        assert dsg.main() == 0
        # No finding should have been emitted — the short-circuit happens
        # before classification runs at all.
        assert calls == []


# ---------------------------------------------------------------------------
# Malformed / non-JSON stdin — graceful handling, never an unhandled
# exception, matching the sibling hooks' fail-open convention. Routed through
# the shared `read_hook_input()` in _session_integrity.py (not a local copy
# in this hook) — see that function's docstring for why the non-dict case
# (e.g. a JSON array) is coerced to {} there rather than here, so every
# caller of the shared helper gets the same guard.
# ---------------------------------------------------------------------------
class TestMalformedStdin:
    @pytest.mark.parametrize(
        "stdin_text",
        [
            "not json",
            "",
            "[1, 2, 3]",  # valid JSON, but not the object shape every caller assumes
        ],
    )
    def test_exits_zero_without_raising(self, monkeypatch, stdin_text):
        _no_emit(monkeypatch)
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
        assert dsg.main() == 0


# ---------------------------------------------------------------------------
# read_hook_input() itself (_session_integrity.py) — the shared helper this
# hook was moved onto. `decision_shape_gate.py` had its own local isinstance
# guard first; moved here so the five OTHER hooks that call read_hook_input()
# (git_stash_guard, session_start, gmail_send_gate, stop_finalizer,
# sibling_repo_worktree_guard, user_prompt_submit) get the same protection
# instead of only this one. No dedicated test file exists yet for
# _session_integrity.py as a whole — this covers only the function this PR
# touches, not the module's other helpers (out of scope here).
# ---------------------------------------------------------------------------
class TestReadHookInputCoercesNonDict:
    @pytest.mark.parametrize(
        "stdin_text",
        ["[1, 2, 3]", "42", "null", '"a bare string"'],
    )
    def test_non_dict_json_coerces_to_empty_dict(self, monkeypatch, stdin_text):
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
        assert si.read_hook_input() == {}

    def test_object_json_passes_through_unchanged(self, monkeypatch):
        monkeypatch.setattr(
            sys, "stdin", io.StringIO(json.dumps({"session_id": "abc"}))
        )
        assert si.read_hook_input() == {"session_id": "abc"}

    def test_empty_stdin_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        assert si.read_hook_input() == {}

    def test_malformed_json_fails_open_to_empty_dict(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
        assert si.read_hook_input() == {}


# ---------------------------------------------------------------------------
# WARN vs BLOCK branch — both exit path and payload shape.
# ---------------------------------------------------------------------------
def _transcript_with_finding(tmp_path) -> str:
    row = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Shall I dispatch the fix now?"}],
        },
    }
    p = tmp_path / "transcript.jsonl"
    p.write_text(json.dumps(row) + "\n")
    return str(p)


class TestWarnVsBlockBranch:
    def test_warn_mode_exits_zero_and_prints_nothing_to_stdout(
        self, tmp_path, monkeypatch, capsys
    ):
        _no_emit(monkeypatch)
        monkeypatch.setattr(dsg, "ENFORCE", False)
        transcript = _transcript_with_finding(tmp_path)
        ev = {"transcript_path": transcript, "session_id": "s1"}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(ev)))
        code = dsg.main()
        out = capsys.readouterr()
        assert code == 0
        assert out.out == ""
        assert "decision-shape" in out.err  # WARN writes the finding to stderr

    def test_block_mode_exits_two_with_decision_block_payload(
        self, tmp_path, monkeypatch, capsys
    ):
        _no_emit(monkeypatch)
        monkeypatch.setattr(dsg, "ENFORCE", True)
        transcript = _transcript_with_finding(tmp_path)
        ev = {"transcript_path": transcript, "session_id": "s2"}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(ev)))
        code = dsg.main()
        out = capsys.readouterr()
        assert code == 2
        payload = json.loads(out.out)
        assert payload["decision"] == "block"
        assert "asking permission" in payload["reason"]

    def test_both_modes_emit_a_harness_event(self, tmp_path, monkeypatch):
        # Observability parity (df3be02a): a finding must leave a durable
        # trace in BOTH modes, not only BLOCK.
        for enforce in (False, True):
            calls = _no_emit(monkeypatch)
            monkeypatch.setattr(dsg, "ENFORCE", enforce)
            transcript = _transcript_with_finding(tmp_path)
            ev = {"transcript_path": transcript, "session_id": "s3"}
            monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(ev)))
            dsg.main()
            assert len(calls) == 1

    def test_no_finding_emits_nothing_in_either_mode(self, tmp_path, monkeypatch):
        row = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Done."}],
            },
        }
        p = tmp_path / "clean_transcript.jsonl"
        p.write_text(json.dumps(row) + "\n")
        calls = _no_emit(monkeypatch)
        monkeypatch.setattr(dsg, "ENFORCE", True)
        ev = {"transcript_path": str(p), "session_id": "s4"}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(ev)))
        code = dsg.main()
        assert code == 0
        assert calls == []
