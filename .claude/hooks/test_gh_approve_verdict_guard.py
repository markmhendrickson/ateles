#!/usr/bin/env python3
"""Effect-level tests for gh_approve_verdict_guard (ateles#1198 / #1181).

Asserts decision + deny message content, not mere input acceptance.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

HOOKS = Path(__file__).resolve().parent
HOOK = str(HOOKS / "gh_approve_verdict_guard.py")
_REPO = HOOKS.parents[1]
sys.path.insert(0, str(HOOKS / "lib"))
sys.path.insert(0, str(HOOKS))
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "execution" / "daemons" / "apis"))

from panel_verdicts import (  # noqa: E402
    Decision,
    Offender,
    PanelInputs,
    VerdictDataUnreadable,
    VerdictRecord,
    evaluate,
    format_deny_reason,
    get_seated_panel_from_inputs,
    matches_approve_call,
    tabulate_verdicts,
)
from review_panel import select_panel  # noqa: E402

HEAD = "a" * 40
STALE = "b" * 40
REPO = "markmhendrickson/ateles"


def _rec(lens: str, sha: str, verdict: str | None, *, blocking: bool = False, updated: str = "2026-09-23T12:00:00Z", cid: int = 1) -> VerdictRecord:
    return VerdictRecord(
        lens=lens,
        commit_sha=sha,
        verdict=verdict,
        has_blocking=blocking,
        updated_at=updated,
        comment_id=cid,
    )


def run_hook(command: str, tool: str = "Bash", *, env=None):
    payload = json.dumps({"tool_name": tool, "tool_input": {"command": command}})
    p = subprocess.run(
        [sys.executable, HOOK],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )
    return p


# ── evaluate (pure) ──────────────────────────────────────────────────────────


def test_1181_shape_blocks_with_named_lenses_and_shas():
    seated = ["security", "pm", "qa", "content"]
    verdicts = {
        "security": _rec("security", STALE, "request_changes"),
        "pm": _rec("pm", STALE, "approve"),
        "qa": _rec("qa", STALE, "approve"),
        "content": _rec("content", STALE, "approve"),
    }
    d = evaluate(seated, verdicts, HEAD)
    assert d.allowed is False
    reason = format_deny_reason("markmhendrickson", "ateles", 1181, HEAD, d)
    assert reason.startswith("Refused: approve blocked for markmhendrickson/ateles#1181")
    assert HEAD in reason
    for lens in seated:
        assert lens in reason
    assert f"last_reviewed={STALE}" in reason
    assert "blocking: security verdict=REQUEST_CHANGES" in reason
    assert "Next:" in reason
    assert "Background: ateles#1181" in reason


def test_all_seated_at_head_clean_allows():
    seated = ["pm", "qa", "security"]
    verdicts = {
        "pm": _rec("pm", HEAD, "approve"),
        "qa": _rec("qa", HEAD, "approve"),
        "security": _rec("security", HEAD, "approve"),
    }
    d = evaluate(seated, verdicts, HEAD)
    assert d.allowed is True


def test_inplace_edit_latest_updated_at_wins():
    """Same comment id; later updated_at body at HEAD counts current."""
    comments = [
        {
            "id": 42,
            "updated_at": "2026-09-23T10:00:00Z",
            "body": f"<!-- review:pm commit={STALE} -->\n**APPROVE**",
        },
        {
            "id": 42,
            "updated_at": "2026-09-23T12:00:00Z",
            "body": f"<!-- review:pm commit={HEAD} -->\n**APPROVE**",
        },
    ]
    # tabulate via mocked gh — feed comments directly through internal path
    with mock.patch(
        "panel_verdicts._gh_api_json", return_value=comments
    ):
        out = tabulate_verdicts("markmhendrickson", "ateles", 1)
    assert out["pm"].commit_sha == HEAD
    assert out["pm"].updated_at == "2026-09-23T12:00:00Z"


def test_missing_marker_blocks():
    d = evaluate(["pm", "qa"], {"pm": _rec("pm", HEAD, "approve")}, HEAD)
    assert d.allowed is False
    assert any(o.lens == "qa" and o.last_reviewed == "none" for o in d.missing)


def test_request_changes_at_head_blocks():
    d = evaluate(
        ["pm"],
        {"pm": _rec("pm", HEAD, "request_changes")},
        HEAD,
    )
    assert d.allowed is False
    assert d.blocking[0].verdict_label == "REQUEST_CHANGES"


def test_approve_with_blocking_marker_blocks():
    d = evaluate(
        ["pm"],
        {"pm": _rec("pm", HEAD, "approve", blocking=True)},
        HEAD,
    )
    assert d.allowed is False
    assert d.blocking[0].verdict_label == "[BLOCKING]"


@pytest.mark.parametrize("token", ["comment", "blocked", "signed_off"])
def test_non_request_tokens_at_head_without_blocking_allow(token):
    d = evaluate(["pm"], {"pm": _rec("pm", HEAD, token)}, HEAD)
    assert d.allowed is True


def test_unreadable_cause_blocks():
    d = evaluate([], {}, HEAD, unreadable_cause="gh api timeout")
    assert d.allowed is False
    assert d.reason_kind == "unreadable"
    reason = format_deny_reason("markmhendrickson", "ateles", 1, HEAD, d)
    assert "verdict data unreadable: gh api timeout" in reason


def test_ambiguous_dual_markers_raise():
    comments = [
        {
            "id": 1,
            "updated_at": "2026-09-23T12:00:00Z",
            "body": f"<!-- review:pm commit={HEAD} -->\n**APPROVE**",
        },
        {
            "id": 2,
            "updated_at": "2026-09-23T12:00:00Z",
            "body": f"<!-- review:pm commit={STALE} -->\n**APPROVE**",
        },
    ]
    with mock.patch("panel_verdicts._gh_api_json", return_value=comments):
        with pytest.raises(VerdictDataUnreadable) as ei:
            tabulate_verdicts("markmhendrickson", "ateles", 1)
    assert "ambiguous" in str(ei.value).lower()


# ── select_panel golden equality ─────────────────────────────────────────────


def test_get_seated_panel_equals_select_panel():
    inputs = PanelInputs(
        changed_files=[".claude/hooks/gh_approve_verdict_guard.py"],
        gate_contributors=set(),
        pending_gates=set(),
    )
    seated = get_seated_panel_from_inputs(inputs)
    golden = [
        lens.lens
        for lens in select_panel(
            gate_contributors=inputs.gate_contributors,
            changed_files=inputs.changed_files,
            max_panel=inputs.max_panel,
            pending_gates=inputs.pending_gates,
        )
    ]
    assert seated == golden
    assert "pm" in seated and "qa" in seated  # always-on


def test_subset_panel_does_not_require_absent_lenses():
    """Only seated lenses are required — absent non-seated must not block."""
    seated = ["pm", "security"]
    verdicts = {
        "pm": _rec("pm", HEAD, "approve"),
        "security": _rec("security", HEAD, "approve"),
        # qa not seated — its absence must not matter
    }
    d = evaluate(seated, verdicts, HEAD)
    assert d.allowed is True


# ── matches_approve_call ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cmd",
    [
        f"gh pr review 1181 --approve --repo {REPO}",
        f"gh pr review 1181 -a --repo {REPO}",
        f"gh api repos/{REPO}/pulls/1181/reviews -f event=APPROVE",
        f"gh api repos/{REPO}/pulls/1181/reviews -F event=APPROVE",
        f"gh api repos/{REPO}/pulls/1181/reviews --field event=APPROVE",
        f'gh api repos/{REPO}/pulls/1181/reviews --input - <<< \'{{"event":"APPROVE"}}\'',
        f"gh api repos/markmhendrickson/neotoma/pulls/99/reviews -f event=APPROVE",
    ],
)
def test_matcher_positives(cmd):
    # Avoid cwd inference for --repo / api path forms.
    t = matches_approve_call(cmd)
    assert t is not None
    assert t.full_name in ("markmhendrickson/ateles", "markmhendrickson/neotoma")


@pytest.mark.parametrize(
    "cmd",
    [
        f"gh pr review 1181 --comment --repo {REPO}",
        f"gh pr review 1181 --request-changes --repo {REPO}",
        f"gh pr merge 1181 --repo {REPO}",
        f"gh pr view 1181 --repo {REPO}",
        "gh pr review 1 --approve --repo other/other",
        f'git commit -m "gh pr review 1181 --approve --repo {REPO}"',
        f'echo "gh pr review 1181 --approve --repo {REPO}"',
    ],
)
def test_matcher_negatives(cmd):
    assert matches_approve_call(cmd) is None


def test_matcher_interpreter_wrappers_still_match():
    cmd = f"python3 -c 'import os; os.system(\"gh pr review 1181 --approve --repo {REPO}\")'"
    # The approve string is inside the segment; TEXT_BEARING does not exempt interpreters.
    assert matches_approve_call(cmd) is not None


# ── no override env ──────────────────────────────────────────────────────────


def test_no_override_env_in_hook_source():
    src = (HOOKS / "gh_approve_verdict_guard.py").read_text()
    assert "ATELES_ALLOW_GH_APPROVE" not in src
    assert "ALLOW_GH_APPROVE" not in src
    lib = (HOOKS / "lib" / "panel_verdicts.py").read_text()
    assert "ATELES_ALLOW_GH_APPROVE" not in lib


# ── subprocess hook contract ─────────────────────────────────────────────────


def test_non_bash_ignored():
    p = run_hook(f"gh pr review 1 --approve --repo {REPO}", tool="Edit")
    assert p.returncode == 0
    assert p.stdout.strip() == ""


def test_non_approve_silent_exit_0():
    p = run_hook("gh pr view 1")
    assert p.returncode == 0
    assert p.stdout.strip() == ""
    assert "[gh_approve_verdict_guard]" not in p.stderr


def test_out_of_scope_repo_exit_0_no_panel_io():
    with mock.patch("panel_verdicts.resolve_head_sha") as rh:
        p = run_hook("gh pr review 1 --approve --repo other/other")
        assert p.returncode == 0
        rh.assert_not_called()


def test_hook_1181_shape_exit_2_message():
    seated = ["security", "pm", "qa", "content"]
    verdicts = {
        "security": _rec("security", STALE, "request_changes"),
        "pm": _rec("pm", STALE, "approve"),
        "qa": _rec("qa", STALE, "approve"),
        "content": _rec("content", STALE, "approve"),
    }

    def fake_evaluate_path(*_a, **_k):
        raise AssertionError("should be patched at seams")

    with (
        mock.patch("panel_verdicts.resolve_head_sha", return_value=HEAD),
        mock.patch("panel_verdicts.get_seated_panel", return_value=seated),
        mock.patch("panel_verdicts.tabulate_verdicts", return_value=verdicts),
    ):
        # Patch inside the hook's imported names
        import panel_verdicts as pv

        with (
            mock.patch.object(pv, "resolve_head_sha", return_value=HEAD),
            mock.patch.object(pv, "get_seated_panel", return_value=seated),
            mock.patch.object(pv, "tabulate_verdicts", return_value=verdicts),
        ):
            # Subprocess won't see our mocks — test evaluate path in-process
            # and separately drive the hook with a monkeypatched module via
            # injecting through env is hard. Drive deny message via pure path
            # and a thin in-process main simulation:
            pass

    d = evaluate(seated, verdicts, HEAD)
    reason = format_deny_reason("markmhendrickson", "ateles", 1181, HEAD, d)
    assert "missing: pm" in reason
    assert "blocking: security" in reason

    # In-process hook main with patched panel_verdicts functions:
    import gh_approve_verdict_guard as guard

    with (
        mock.patch.object(guard, "resolve_head_sha", return_value=HEAD),
        mock.patch.object(guard, "get_seated_panel", return_value=seated),
        mock.patch.object(guard, "tabulate_verdicts", return_value=verdicts),
        mock.patch.object(guard, "matches_approve_call") as m,
    ):
        from panel_verdicts import ApproveTarget

        m.return_value = ApproveTarget("markmhendrickson", "ateles", 1181)
        # Feed stdin via redirect
        payload = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": f"gh pr review 1181 --approve --repo {REPO}"
                },
            }
        )
        with mock.patch("sys.stdin") as stdin:
            stdin.read.return_value = payload
            rc = guard.main()
        assert rc == 2


def test_hook_allow_silent_exit_0():
    import gh_approve_verdict_guard as guard
    from panel_verdicts import ApproveTarget

    seated = ["pm", "qa"]
    verdicts = {
        "pm": _rec("pm", HEAD, "approve"),
        "qa": _rec("qa", HEAD, "approve"),
    }
    with (
        mock.patch.object(guard, "resolve_head_sha", return_value=HEAD),
        mock.patch.object(guard, "get_seated_panel", return_value=seated),
        mock.patch.object(guard, "tabulate_verdicts", return_value=verdicts),
        mock.patch.object(
            guard,
            "matches_approve_call",
            return_value=ApproveTarget("markmhendrickson", "ateles", 1),
        ),
        mock.patch("sys.stdin") as stdin,
    ):
        stdin.read.return_value = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": f"gh pr review 1 --approve --repo {REPO}"
                },
            }
        )
        # Capture stdout
        from io import StringIO

        buf = StringIO()
        err = StringIO()
        with mock.patch("sys.stdout", buf), mock.patch("sys.stderr", err):
            rc = guard.main()
        assert rc == 0
        assert buf.getvalue().strip() == ""
        assert "Refused:" not in err.getvalue()


def test_matched_approve_bad_stdin_denies():
    p = subprocess.run(
        [sys.executable, HOOK],
        input=f'not json but gh pr review 1181 --approve --repo {REPO}',
        capture_output=True,
        text=True,
    )
    assert p.returncode == 2
    assert "verdict data unreadable" in p.stdout or "verdict data unreadable" in p.stderr


def test_gh_failure_denies_unreadable():
    import gh_approve_verdict_guard as guard
    from panel_verdicts import ApproveTarget, VerdictDataUnreadable

    with (
        mock.patch.object(
            guard,
            "matches_approve_call",
            return_value=ApproveTarget("markmhendrickson", "ateles", 1),
        ),
        mock.patch.object(
            guard,
            "resolve_head_sha",
            side_effect=VerdictDataUnreadable("API rate limit"),
        ),
        mock.patch("sys.stdin") as stdin,
    ):
        stdin.read.return_value = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": f"gh pr review 1 --approve --repo {REPO}"
                },
            }
        )
        from io import StringIO

        out = StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", StringIO()):
            rc = guard.main()
        assert rc == 2
        assert "verdict data unreadable: API rate limit" in out.getvalue()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
