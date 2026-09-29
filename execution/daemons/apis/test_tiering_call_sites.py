"""Every Apis dispatch site names an action class (operator ruling 2026-09-29).

#1348 added the tiering machinery; this file pins its USE. For each site that
calls ``run_skill`` it asserts the action class the site passes and that the
escalation signals it passes raise the tier — resolved through the real
``model_tiering.resolve_tier`` against the committed example policy, so a class
the example policy does not map, or a signal that never reaches the resolver,
fails here rather than silently running the site on the provider default.

The world the dispatcher reads (changed files, diff size, fix-round count) is
stubbed through ``_World`` so each test states the one fact it varies.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import apis
import model_tiering
import review_delta
import swarm_dispatch
from skill_runner import SkillResult
from swarm_dispatch import SwarmDispatcher
from test_swarm_dispatch import (
    _FakeSpecStore,
    _StubNotifier,
    _async_return,
    _config,
    _empty_spec_state,
    _install_pipeline_stubs,
    _issue_trigger,
    _pr_dispatcher_with_stubs,
    _trigger,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_DIR = _REPO_ROOT / "docs" / "examples" / "model-tiering"
EXAMPLE_POLICY = EXAMPLE_DIR / "action-policy.json"
EXAMPLE_BINDING = EXAMPLE_DIR / "vendor-binding.json"


class _World:
    """The facts `SwarmDispatcher` measures about a PR, settable per test."""

    def __init__(self) -> None:
        self.files: list[str] = ["src/feature.py"]
        self.lines: int | None = 40
        self.fix_rounds: int = 0
        self.new_blocking_finding: bool = False
        # The re-round's own change since the last reviewed head; None means
        # "not measurable / round 1", so the whole PR is measured.
        self.delta: review_delta.Delta | None = None
        self.delta_reads: int = 0
        self.changed_files_stub = None  # re-applied after helpers that override it


@pytest.fixture
def world(monkeypatch, tmp_path) -> _World:
    w = _World()
    # The committed example policy IS the policy under test.
    monkeypatch.setenv("APIS_ACTION_POLICY_FILE", str(EXAMPLE_POLICY))
    monkeypatch.setenv("APIS_VENDOR_BINDING_FILE", str(EXAMPLE_BINDING))
    monkeypatch.delenv("APIS_ACTION_POLICY", raising=False)
    monkeypatch.delenv("APIS_VENDOR_BINDING", raising=False)

    async def files(self, trigger):
        return list(w.files)

    async def lines(self, trigger):
        return w.lines

    async def rounds(self, trigger):
        return w.fix_rounds

    async def new_finding(self, trigger):
        return w.new_blocking_finding

    async def delta(self, trigger):
        w.delta_reads += 1
        return w.delta

    async def noop(self, *a, **k):
        return None

    w.changed_files_stub = files
    monkeypatch.setattr(SwarmDispatcher, "_changed_files", files)
    monkeypatch.setattr(SwarmDispatcher, "_diff_lines_changed", lines, raising=False)
    monkeypatch.setattr(SwarmDispatcher, "_fix_round_count", rounds)
    monkeypatch.setattr(SwarmDispatcher, "_review_delta", delta, raising=False)
    monkeypatch.setattr(SwarmDispatcher, "_record_fix_round", noop)
    monkeypatch.setattr(
        SwarmDispatcher, "_new_blocking_finding", new_finding, raising=False
    )
    monkeypatch.setattr(
        SwarmDispatcher, "_blocking_findings_from_reviewed_head_comments",
        lambda self, t, h: _async_return({}),
    )
    monkeypatch.setattr(
        SwarmDispatcher, "_pr_head_sha", lambda self, t: _async_return("a" * 40)
    )
    return w


class _Recorder:
    """Captures every `run_skill` call as (skill, kwargs)."""

    def __init__(self, monkeypatch, *, stdout=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._stdout = stdout or {}

        async def fake_run_skill(skill, prompt, **kwargs):
            self.calls.append((skill, kwargs))
            return SkillResult(
                skill, True, 0,
                self._stdout.get(skill, "**COMMENT**\nlgtm"), "",
            )

        monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)

    def of(self, skill: str) -> list[dict]:
        return [kw for s, kw in self.calls if s == skill]

    def only(self, skill: str) -> dict:
        found = self.of(skill)
        assert len(found) == 1, f"expected one {skill} dispatch, got {len(found)}"
        return found[0]


def tier_of(kwargs: dict) -> model_tiering.ResolvedTier:
    """Resolve what the runner would resolve for these run_skill kwargs."""
    assert kwargs.get("action_class"), (
        "dispatch site passed no action_class — it would run on the provider "
        "default (the most expensive model)"
    )
    return model_tiering.resolve_tier(
        kwargs["action_class"], signals=kwargs.get("escalation_signals")
    )


def _dispatcher() -> SwarmDispatcher:
    # `require_label` is read from the environment at import; pin it off so a
    # shell exporting ATELES_SWARM_REQUIRE_LABEL cannot skip these PRs locally.
    return SwarmDispatcher(_StubNotifier(), _config(require_label=""))


# ── _handle_pr: Lanius gate check, the panel, Vanellus ───────────────────────


def _pr_dispatcher(monkeypatch, world) -> SwarmDispatcher:
    d = _pr_dispatcher_with_stubs(
        monkeypatch, vanellus_stdout="**APPROVE**\nlgtm", calls=[]
    )
    # The shared helper stubs `_changed_files` with a fixed list; put the
    # test's own back so the file signals under test are the ones measured.
    monkeypatch.setattr(SwarmDispatcher, "_changed_files", world.changed_files_stub)
    return d


def _run_handle_pr(monkeypatch, world) -> _Recorder:
    d = _pr_dispatcher(monkeypatch, world)
    # These tests pin what each lens's OWN dispatch runs at. The combined
    # pm/qa/ux pass and the narrowed re-round change how many dispatches a
    # panel makes, not the tier a lens resolves to; both are covered in
    # test_review_carry.py, so they are switched off here.
    d.config = dataclasses.replace(
        d.config, combined_review_pass=False, narrow_rereview=False
    )
    # `_pr_dispatcher_with_stubs` installed its own run_skill; replace it with
    # the recorder so kwargs are captured, keeping its verdict outputs.
    rec = _Recorder(
        monkeypatch,
        stdout={
            "lanius": "GATE_INHERITANCE: clear",
            "vanellus": "**APPROVE**\nlgtm",
        },
    )
    asyncio.run(d._handle_pr(_trigger(body="Closes #80.")))
    return rec


def test_panel_lens_review_passes_its_lens_class_and_runs_mid_for_pm(
    monkeypatch, world
):
    rec = _run_handle_pr(monkeypatch, world)
    pavo = rec.only("pavo")
    assert pavo["action_class"] == "lens_review:pm"
    assert tier_of(pavo).tier == "mid"


def test_panel_arch_lens_stays_on_top(monkeypatch, world):
    rec = _run_handle_pr(monkeypatch, world)
    waxwing = rec.only("waxwing")
    assert waxwing["action_class"] == "lens_review:arch"
    assert tier_of(waxwing).tier == "top"


def test_panel_small_rereview_of_a_mid_lens_stays_mid(monkeypatch, world):
    """Ruling `small_rereview_rounds_run_mid` (2026-09-29): a pm/qa/ux re-review
    of a small change, no security path, no NEW blocking finding, runs mid."""
    world.fix_rounds = 1  # a fix round already happened: this is round 2
    rec = _run_handle_pr(monkeypatch, world)
    for agent in ("pavo", "phoenicurus"):
        resolved = tier_of(rec.only(agent))
        assert (resolved.tier, resolved.source) == ("mid", "policy"), agent


def test_panel_rereview_of_arch_and_security_stays_top(monkeypatch, world):
    world.fix_rounds = 1
    rec = _run_handle_pr(monkeypatch, world)
    resolved = tier_of(rec.only("waxwing"))
    assert resolved.tier == "top"
    assert any(r.startswith("review_round=") for r in resolved.escalation_reasons)


def test_panel_rereview_with_a_new_blocking_finding_goes_top(monkeypatch, world):
    world.fix_rounds = 2
    world.new_blocking_finding = True
    rec = _run_handle_pr(monkeypatch, world)
    resolved = tier_of(rec.only("pavo"))
    assert resolved.tier == "top"
    assert "new_blocking_finding" in resolved.escalation_reasons


def test_panel_large_rereview_of_a_mid_lens_goes_top(monkeypatch, world):
    world.fix_rounds = 1
    world.lines = 900
    rec = _run_handle_pr(monkeypatch, world)
    assert tier_of(rec.only("pavo")).tier == "top"


def test_panel_security_path_rereview_of_a_mid_lens_goes_top(monkeypatch, world):
    world.fix_rounds = 1
    world.files = [".claude/hooks/gate.py"]
    rec = _run_handle_pr(monkeypatch, world)
    assert tier_of(rec.only("pavo")).tier == "top"


def test_panel_large_diff_raises_a_mid_lens_to_top(monkeypatch, world):
    world.lines = 5000
    rec = _run_handle_pr(monkeypatch, world)
    assert tier_of(rec.only("pavo")).tier == "top"


def test_panel_security_sensitive_path_raises_a_mid_lens_to_top(monkeypatch, world):
    world.files = ["execution/hooks/credential_read_guard.py"]
    rec = _run_handle_pr(monkeypatch, world)
    resolved = tier_of(rec.only("pavo"))
    assert resolved.tier == "top"
    assert "touches_security_sensitive_path" in resolved.escalation_reasons


def test_panel_unmeasurable_diff_raises_to_top_not_down(monkeypatch, world):
    """An API failure must never read as a small diff."""
    world.lines = None
    rec = _run_handle_pr(monkeypatch, world)
    resolved = tier_of(rec.only("pavo"))
    assert resolved.tier == "top"
    assert "diff_unreadable" in resolved.escalation_reasons


def test_lanius_pr_check_is_a_carry_forward_check_on_mid(monkeypatch, world):
    rec = _run_handle_pr(monkeypatch, world)
    lanius = rec.only("lanius")
    assert lanius["action_class"] == "carry_forward_check"
    assert tier_of(lanius).tier == "mid"


def test_lanius_pr_check_does_not_measure_the_diff(monkeypatch, world):
    """Gate bookkeeping does not read the diff: a huge diff must not push it
    to top, and neither does the round: a carry-forward check is defined by
    the PR being carried through another round."""
    world.lines = 5000
    world.files = ["execution/hooks/x.py"]
    rec = _run_handle_pr(monkeypatch, world)
    assert tier_of(rec.only("lanius")).tier == "mid"
    world.fix_rounds = 2
    rec = _run_handle_pr(monkeypatch, world)
    resolved = tier_of(rec.only("lanius"))
    assert (resolved.tier, resolved.source) == ("mid", "policy")


def test_lanius_verdict_retry_escalates_as_a_failed_prior_attempt(
    monkeypatch, world
):
    d = _pr_dispatcher(monkeypatch, world)
    rec = _Recorder(
        monkeypatch,
        stdout={"lanius": "no verdict line here", "vanellus": "**APPROVE**\nlgtm"},
    )
    asyncio.run(d._handle_pr(_trigger(body="Closes #80.")))
    first, retry = rec.of("lanius")[:2]
    assert tier_of(first).tier == "mid"
    resolved = tier_of(retry)
    assert retry["action_class"] == "carry_forward_check"
    assert resolved.tier == "top"
    assert "prior_attempt_failed" in resolved.escalation_reasons


def test_vanellus_aggregation_names_a_class_and_runs_top(monkeypatch, world):
    rec = _run_handle_pr(monkeypatch, world)
    vanellus = rec.only("vanellus")
    assert vanellus["action_class"] == "panel_aggregation"
    resolved = tier_of(vanellus)
    assert resolved.tier == "top"
    assert resolved.source == "unresolved_class"


# ── _redispatch_missing_lens ─────────────────────────────────────────────────


def _pr_dict():
    return {
        "number": 87,
        "title": "PR",
        "body": "Closes #80.",
        "user": {"login": "someone"},
        "html_url": "https://github.com/owner/repo/pull/87",
        "head": {"ref": "feature", "sha": "a" * 40},
        "base": {"ref": "main"},
    }


def _run_missing_lens(monkeypatch, lens: str) -> _Recorder:
    rec = _Recorder(monkeypatch)

    async def noop(self, *a, **k):
        return None

    monkeypatch.setattr(SwarmDispatcher, "_persist_panel_reviews", noop)
    monkeypatch.setattr(SwarmDispatcher, "_post_missing_panel_comments", noop)

    async def load(self, repo, issue_number):
        return SimpleNamespace(found=True, gate_status={}, gate_status_unreadable=False)

    monkeypatch.setattr(swarm_dispatch.IssueGateStore, "load", load)
    asyncio.run(_dispatcher()._redispatch_missing_lens("owner/repo", _pr_dict(), lens))
    return rec


def test_missing_lens_rerun_passes_the_lens_class(monkeypatch, world):
    rec = _run_missing_lens(monkeypatch, "security")
    falco = rec.only("falco")
    assert falco["action_class"] == "lens_review:security"
    assert tier_of(falco).tier == "top"


def test_missing_lens_rerun_of_a_mid_lens_stays_mid_on_a_small_repeat_round(
    monkeypatch, world
):
    world.fix_rounds = 1
    rec = _run_missing_lens(monkeypatch, "qa")
    assert tier_of(rec.only("phoenicurus")).tier == "mid"


def test_missing_lens_rerun_of_a_mid_lens_escalates_on_a_new_blocking_finding(
    monkeypatch, world
):
    world.fix_rounds = 1
    world.new_blocking_finding = True
    rec = _run_missing_lens(monkeypatch, "qa")
    assert tier_of(rec.only("phoenicurus")).tier == "top"


def test_missing_lens_rerun_of_arch_on_a_repeat_round_stays_top(monkeypatch, world):
    world.fix_rounds = 1
    rec = _run_missing_lens(monkeypatch, "arch")
    assert tier_of(rec.only("waxwing")).tier == "top"


# ── resume_unactioned_revisions ──────────────────────────────────────────────


REVIEW_BODY_UX = "[BLOCKING] naming: the flag is undiscoverable\ndetail"
REVIEW_BODY_SECURITY = (
    "<!-- review:security commit=" + "b" * 40 + " -->\n"
    "**Falco, security lens panelist**\n**REQUEST_CHANGES**\n"
    "[BLOCKING] injection: unsanitised input reaches the shell\ndetail"
)


def _run_revision_sweep(
    monkeypatch,
    *,
    attempts_before: int = 0,
    review_body: str = REVIEW_BODY_UX,
    comment_lenses: tuple[str, ...] = (),
) -> _Recorder:
    d = _dispatcher()
    rec = _Recorder(monkeypatch)
    # Lenses with a standing blocking finding in the PR's own lens comments.
    monkeypatch.setattr(
        SwarmDispatcher,
        "_blocking_findings_from_reviewed_head_comments",
        lambda self, t, h: _async_return({lens: [object()] for lens in comment_lenses}),
    )

    async def scan(repo, now, stale):
        return [
            {
                "number": 163,
                "title": "t",
                "body": "Closes #80.",
                "html_url": "https://github.com/o/r/pull/163",
                "user": {"login": "someone"},
                "head": {"ref": "f", "sha": "b" * 40},
                "base": {"ref": "main"},
                "labels": [],
                "_blocking_review_at": "x",
                "_revision_pushed_at": "y",
                "_blocking_review_body": review_body,
            }
        ]

    monkeypatch.setattr(d, "_prs_with_unactioned_revisions", scan)
    if attempts_before:
        # The key the sweep actually reads: f"{ref}@{head_sha}". Seeding any
        # other shape is a silent no-op and leaves the retry escalation
        # untested (qa review of ateles#1358).
        d._revision_retries[f"o/r#163@{'b' * 40}"] = attempts_before
    asyncio.run(d.resume_unactioned_revisions(["o/r"]))
    return rec


def test_revision_sweep_repair_is_a_diagnosed_repair_on_mid(monkeypatch, world):
    rec = _run_revision_sweep(monkeypatch)
    cicada = rec.only("cicada")
    assert cicada["action_class"] == "repair_diagnosed"
    assert tier_of(cicada).tier == "mid"


def test_revision_sweep_repair_does_not_treat_its_own_input_as_escalation(
    monkeypatch, world
):
    """The blocking finding IS the repair's input; counting it as a prior
    blocking finding would pin every repair to top and defeat the class."""
    rec = _run_revision_sweep(monkeypatch)
    signals = rec.only("cicada")["escalation_signals"]
    assert signals.prior_blocking_finding is False
    assert signals.review_round == 1


def test_revision_sweep_retry_escalates_on_the_attempt_alone(monkeypatch, world):
    """The `attempts > 0` half: no recorded fix round, only a prior sweep
    attempt on this head. Must fail if that half of the condition is removed."""
    world.fix_rounds = 0
    rec = _run_revision_sweep(monkeypatch, attempts_before=1)
    resolved = tier_of(rec.only("cicada"))
    assert resolved.tier == "top"
    assert "prior_attempt_failed" in resolved.escalation_reasons


def test_revision_sweep_first_attempt_does_not_escalate(monkeypatch, world):
    world.fix_rounds = 0
    rec = _run_revision_sweep(monkeypatch, attempts_before=0)
    assert tier_of(rec.only("cicada")).tier == "mid"


def test_revision_sweep_security_review_body_is_a_security_fix_on_top(
    monkeypatch, world
):
    """Security review of ateles#1358: the sweep repaired a security finding
    as `repair_diagnosed` (mid). It must classify like the fix-round path."""
    rec = _run_revision_sweep(monkeypatch, review_body=REVIEW_BODY_SECURITY)
    cicada = rec.only("cicada")
    assert cicada["action_class"] == "security_fix"
    assert tier_of(cicada).tier == "top"


def test_revision_sweep_security_named_only_in_an_aggregate_body_is_top(
    monkeypatch, world
):
    """A formal aggregate review may name the finding without a lens marker.
    The bare word is enough: a false positive costs a top run, a miss runs a
    security fix on mid."""
    rec = _run_revision_sweep(
        monkeypatch,
        review_body="**REQUEST_CHANGES**\n[BLOCKING] Security: path traversal in upload",
    )
    assert rec.only("cicada")["action_class"] == "security_fix"


def test_revision_sweep_security_finding_in_the_lens_comments_is_top(
    monkeypatch, world
):
    """Attribution from the PR's own lens comments, not only the review body."""
    rec = _run_revision_sweep(monkeypatch, comment_lenses=("security",))
    cicada = rec.only("cicada")
    assert cicada["action_class"] == "security_fix"
    assert tier_of(cicada).tier == "top"


def test_revision_sweep_non_security_review_stays_a_mid_repair(monkeypatch, world):
    rec = _run_revision_sweep(monkeypatch, comment_lenses=("ux",))
    cicada = rec.only("cicada")
    assert cicada["action_class"] == "repair_diagnosed"
    assert tier_of(cicada).tier == "mid"


def test_both_repair_sites_share_one_classifier():
    assert model_tiering.repair_action_class({"ux", "security"}) == "security_fix"
    assert model_tiering.repair_action_class({"ux", "qa"}) == "repair_diagnosed"
    assert model_tiering.repair_action_class(set()) == "repair_diagnosed"
    src = (Path(__file__).parent / "swarm_dispatch.py").read_text()
    assert src.count("model_tiering.repair_action_class(") == 2, (
        "the fix-round and sweep repair sites must both call the shared classifier"
    )


def test_revision_sweep_retry_escalates_as_a_failed_prior_attempt(
    monkeypatch, world
):
    world.fix_rounds = 1
    rec = _run_revision_sweep(monkeypatch)
    resolved = tier_of(rec.only("cicada"))
    assert resolved.tier == "top"
    assert "prior_attempt_failed" in resolved.escalation_reasons


# ── issue pipeline: Lanius triage, spec sections, the build ──────────────────


def test_issue_pipeline_lanius_triage_runs_mid(monkeypatch, world):
    rec = _Recorder(
        monkeypatch, stdout={"lanius": "GATE_INHERITANCE: clear"}
    )
    _install_pipeline_stubs(
        monkeypatch,
        swarm_dispatch.run_skill,
        select_agents=lambda *a, **kw: [],
    )
    asyncio.run(_dispatcher()._handle_issue_opened(_issue_trigger()))
    lanius = rec.only("lanius")
    assert lanius["action_class"] == "issue_triage"
    resolved = tier_of(lanius)
    assert (resolved.tier, resolved.source) == ("mid", "policy")


def test_issue_pipeline_sections_run_at_their_lens_tier(monkeypatch, world):
    rec = _Recorder(
        monkeypatch,
        stdout={
            "lanius": "GATE_INHERITANCE: clear",
            **{
                a: "<<<SPEC_SECTION>>>text<<<END_SPEC_SECTION>>>"
                for a in ("pavo", "cicada", "phoenicurus", "waxwing")
            },
        },
    )

    class _Lens:
        def __init__(self, agent):
            self.agent = agent

    _install_pipeline_stubs(
        monkeypatch,
        swarm_dispatch.run_skill,
        select_agents=lambda *a, **kw: [_Lens("waxwing")],
    )
    asyncio.run(_dispatcher()._handle_issue_opened(_issue_trigger()))

    assert rec.only("pavo")["action_class"] == "lens_review:pm"
    assert tier_of(rec.only("pavo")).tier == "mid"
    assert rec.only("phoenicurus")["action_class"] == "lens_review:qa"
    assert tier_of(rec.only("phoenicurus")).tier == "mid"
    # The Security / Arch section is the arch lens: stays top.
    assert rec.only("waxwing")["action_class"] == "lens_review:arch"
    assert tier_of(rec.only("waxwing")).tier == "top"
    # The Engineering section is not a class the ruling names: fails to top.
    assert rec.only("cicada")["action_class"] == "lens_review:eng"
    assert tier_of(rec.only("cicada")).source == "unresolved_class"


def test_open_implementation_pr_is_a_build_on_top(monkeypatch, world):
    rec = _Recorder(
        monkeypatch,
        stdout={"cicada": "Built https://github.com/owner/repo/pull/1910"},
    )
    asyncio.run(
        _dispatcher()._open_implementation_pr(_issue_trigger(), _empty_spec_state())
    )
    cicada = rec.only("cicada")
    assert cicada["action_class"] == "build"
    assert tier_of(cicada).tier == "top"


# ── _route_blocking_findings: fix guidance + Cicada's repair ─────────────────


def _run_route_findings(monkeypatch, reviews) -> _Recorder:
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed"})
    asyncio.run(
        _dispatcher()._route_blocking_findings(
            _trigger(), parent=80, reviews=reviews, verdict="request_changes"
        )
    )
    return rec


UX_FINDING = [("ux", "[BLOCKING] naming: the flag is undiscoverable\ndetail")]
SECURITY_FINDING = [("security", "[BLOCKING] injection: unsanitised input\ndetail")]


def test_fix_round_repair_of_a_non_security_finding_is_mid(monkeypatch, world):
    rec = _run_route_findings(monkeypatch, UX_FINDING)
    cicada = rec.only("cicada")
    assert cicada["action_class"] == "repair_diagnosed"
    assert tier_of(cicada).tier == "mid"
    # And the guidance the ux lens authors runs at the ux tier.
    guidance = rec.only("accipiter")
    assert guidance["action_class"] == "lens_review:ux"
    assert tier_of(guidance).tier == "mid"


def test_fix_round_repair_of_a_security_finding_is_a_security_fix_on_top(
    monkeypatch, world
):
    rec = _run_route_findings(monkeypatch, SECURITY_FINDING)
    cicada = rec.only("cicada")
    assert cicada["action_class"] == "security_fix"
    assert tier_of(cicada).tier == "top"
    assert tier_of(rec.only("falco")).tier == "top"


def test_second_fix_round_escalates_the_repair_as_a_failed_attempt(
    monkeypatch, world
):
    world.fix_rounds = 1  # this is fix round 2: round 1 did not clear review
    rec = _run_route_findings(monkeypatch, UX_FINDING)
    resolved = tier_of(rec.only("cicada"))
    assert resolved.tier == "top"
    assert "prior_attempt_failed" in resolved.escalation_reasons
    assert "prior_blocking_finding" not in resolved.escalation_reasons


def test_fix_round_repair_on_a_security_path_escalates(monkeypatch, world):
    world.files = ["execution/hooks/gate.py"]
    rec = _run_route_findings(monkeypatch, UX_FINDING)
    resolved = tier_of(rec.only("cicada"))
    assert resolved.tier == "top"
    assert "touches_security_sensitive_path" in resolved.escalation_reasons


# ── _route_ci_failure ────────────────────────────────────────────────────────


def test_ci_fix_is_ci_log_triage_on_the_mechanical_tier(monkeypatch, world):
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed"})
    asyncio.run(_dispatcher()._route_ci_failure(_trigger(), parent=80))
    cicada = rec.only("cicada")
    assert cicada["action_class"] == "ci_log_triage"
    assert cicada["work_class"] == "ci_log_triage"  # the local-first tag stays
    assert tier_of(cicada).tier == "mechanical"


def test_ci_fix_second_round_escalates(monkeypatch, world):
    world.fix_rounds = 1
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed"})
    asyncio.run(_dispatcher()._route_ci_failure(_trigger(), parent=80))
    resolved = tier_of(rec.only("cicada"))
    assert resolved.tier == "top"
    assert "prior_attempt_failed" in resolved.escalation_reasons


# ── ensure_issue_entity ──────────────────────────────────────────────────────


def test_entity_backfill_triage_runs_mid(monkeypatch, world):
    rec = _Recorder(monkeypatch, stdout={"lanius": ""})

    class _State:
        def __init__(self, found):
            self.found = found
            self.triaged = found

    seen = {"n": 0}

    async def fake_load(self, repo, issue_number):
        seen["n"] += 1
        return _State(seen["n"] > 1)

    async def fake_fetch(self, repo, n):
        return {"title": "t", "body": "b", "user": {"login": "u"}, "html_url": ""}

    monkeypatch.setattr(swarm_dispatch.IssueGateStore, "load", fake_load)
    monkeypatch.setattr(SwarmDispatcher, "_fetch_issue", fake_fetch)
    asyncio.run(_dispatcher().ensure_issue_entity("o/r", 414))
    lanius = rec.only("lanius")
    assert lanius["action_class"] == "issue_triage"
    resolved = tier_of(lanius)
    assert (resolved.tier, resolved.source) == ("mid", "policy")


# ── apis.py: queue task dispatch ─────────────────────────────────────────────


class _Notifier:
    def send(self, *a, **k):
        return None


def _spawn(monkeypatch, skill, snapshot) -> dict:
    captured: dict = {}

    async def fake_run_skill(s, prompt, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(apis, "run_skill", fake_run_skill)
    asyncio.run(
        apis._spawn_harness_skill(skill, "ent_task", snapshot, "created", _Notifier())
    )
    return captured


def test_queue_task_passes_its_declared_action_type_as_the_class(
    monkeypatch, world
):
    kwargs = _spawn(
        monkeypatch, "cicada", {"title": "t", "body": "b", "action_type": "rebase"}
    )
    assert kwargs["action_class"] == "rebase"
    assert tier_of(kwargs).tier == "mechanical"


def test_queue_task_falls_back_to_the_agents_inferred_class(monkeypatch, world):
    kwargs = _spawn(monkeypatch, "monedula", {"title": "t", "body": "b"})
    assert kwargs["action_class"] == "payment"
    # `payment` is not in the example policy: unmapped fails to top.
    resolved = tier_of(kwargs)
    assert (resolved.tier, resolved.source) == ("top", "unresolved_class")


def test_queue_task_with_no_recognizable_class_fails_to_top(monkeypatch, world):
    kwargs = _spawn(monkeypatch, "someagent", {"title": "t", "body": "b"})
    assert kwargs["action_class"] == "task_dispatch"
    assert tier_of(kwargs).tier == "top"


# ── coverage guard: no run_skill site may omit action_class ──────────────────


def test_no_dispatch_site_calls_run_skill_without_an_action_class():
    """Structural guard: a NEW `run_skill(` call added to swarm_dispatch.py or
    apis.py without `action_class=` would silently run un-tiered. Aquila (a
    monthly report outside the dispatcher) is deliberately not covered."""
    import ast

    missing: list[str] = []
    for name in ("swarm_dispatch.py", "apis.py"):
        tree = ast.parse((Path(__file__).parent / name).read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "run_skill"
                and "action_class" not in {k.arg for k in node.keywords}
            ):
                missing.append(f"{name}:{node.lineno}")
    assert missing == [], f"run_skill calls with no action_class: {missing}"


# ── unmapped class fails to top ──────────────────────────────────────────────


def test_a_class_the_policy_does_not_map_resolves_to_top_never_lower(world):
    resolved = model_tiering.resolve_tier("a_class_nobody_wrote_down")
    assert (resolved.tier, resolved.source) == ("top", "unresolved_class")


def test_every_class_a_site_passes_is_either_mapped_or_knowingly_top(world):
    policy = model_tiering.configured_action_policy()
    for klass in model_tiering.DISPATCH_ACTION_CLASSES:
        tier = model_tiering.resolve_tier(klass, policy=policy)
        if klass in policy:
            assert tier.source == "policy"
        else:
            assert (tier.tier, tier.source) == ("top", "unresolved_class")


# ── example config ───────────────────────────────────────────────────────────


def test_example_config_validates():
    rc = model_tiering.main(["--check", str(EXAMPLE_POLICY), str(EXAMPLE_BINDING)])
    assert rc == 0


def test_example_policy_matches_the_ruling_seed_hint():
    """The committed example and `DEFAULT_ACTION_POLICY_HINT` must not drift:
    the hint is what the module's own tests pin, the example is what an
    operator installs."""
    assert json.loads(EXAMPLE_POLICY.read_text()) == model_tiering.DEFAULT_ACTION_POLICY_HINT


def test_check_rejects_a_typoed_tier(tmp_path):
    bad = tmp_path / "policy.json"
    bad.write_text(json.dumps({"build": "topp"}))
    assert model_tiering.main(["--check", str(bad)]) == 1


def test_check_rejects_a_binding_with_no_top_model(tmp_path):
    bad = tmp_path / "binding.json"
    bad.write_text(json.dumps({"claude": {"mid": "sonnet"}}))
    assert model_tiering.main(["--check", str(bad)]) == 1


def test_check_rejects_a_binding_for_an_unknown_provider(tmp_path):
    bad = tmp_path / "binding.json"
    bad.write_text(json.dumps({"gemini": {"top": "x"}}))
    assert model_tiering.main(["--check", str(bad)]) == 1


def test_check_cross_rejects_a_policy_tier_no_provider_binds(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"build": "top", "rebase": "mechanical"}))
    binding = tmp_path / "binding.json"
    binding.write_text(json.dumps({"claude": {"top": "opus"}}))
    assert model_tiering.main(["--check", str(policy), str(binding)]) == 1


def test_check_rejects_unreadable_and_non_json_files(tmp_path):
    assert model_tiering.main(["--check", str(tmp_path / "missing.json")]) == 1
    junk = tmp_path / "junk.json"
    junk.write_text("not json")
    assert model_tiering.main(["--check", str(junk)]) == 1


def test_check_cli_runs_as_a_script():
    proc = subprocess.run(
        [sys.executable, str(Path(model_tiering.__file__)), "--check",
         str(EXAMPLE_POLICY), str(EXAMPLE_BINDING)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ── observability ────────────────────────────────────────────────────────────


def test_record_dispatch_logs_the_tier_source_and_model_and_fills_the_ledger():
    resolved = model_tiering.ResolvedTier(
        tier="top", source="escalated", action_class="lens_review:pm",
        escalation_reasons=("review_round=2",),
    )
    marker = model_tiering.record_dispatch(
        skill="pavo", provider="claude", resolved=resolved, model="opus"
    )
    assert marker == "tiering=top(escalated) model=opus"
    counts = model_tiering.tier_counts()
    assert counts["by_tier"] == {"top": 1}
    assert counts["by_class"] == {"lens_review:pm": {"top": 1}}


def test_an_untiered_dispatch_is_counted_under_its_own_name():
    """A call site that forgot its action class must show up as `untiered`,
    not hide inside a real tier."""
    marker = model_tiering.record_dispatch(
        skill="x", provider="claude", resolved=None, model=None
    )
    assert marker == "tiering=untiered(no_action_class) model=default"
    assert model_tiering.tier_counts()["by_tier"] == {"untiered": 1}


def test_tier_counts_since_hours_and_torn_lines(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("APIS_TIER_LEDGER_FILE", str(ledger))
    old = {"ts": "2020-01-01T00:00:00+00:00", "tier": "top", "action_class": "a"}
    new = {
        "ts": model_tiering.datetime.now(model_tiering.timezone.utc).isoformat(),
        "tier": "mid", "action_class": "b",
    }
    ledger.write_text(
        json.dumps(old) + "\n{torn\n" + json.dumps(new) + "\n"
    )
    assert model_tiering.tier_counts()["by_tier"] == {"top": 1, "mid": 1}
    assert model_tiering.tier_counts(since_hours=1)["by_tier"] == {"mid": 1}


def test_ledger_write_failure_never_raises(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("APIS_TIER_LEDGER_FILE", str(blocker / "sub" / "l.jsonl"))
    model_tiering.record_dispatch(
        skill="x", provider="claude", resolved=None, model=None
    )


# ── new blocking finding (ruling `small_rereview_rounds_run_mid`) ────────────


def _lens_comment(lens: str, sha_char: str, when: str, findings: list[str]) -> dict:
    body = f"<!-- review:{lens} commit={sha_char * 40} -->\n**REQUEST_CHANGES**\n" + "\n".join(
        f"[BLOCKING] {f}\ndetail" for f in findings
    )
    return {"body": body, "created_at": when}


def test_a_finding_repeated_on_the_next_head_is_not_new():
    comments = [
        _lens_comment("qa", "1", "2026-09-29T01:00:00Z", ["coverage: no test for X"]),
        _lens_comment("qa", "2", "2026-09-29T02:00:00Z", ["coverage: no test for X"]),
    ]
    assert swarm_dispatch.has_new_blocking_finding(comments) is False


def test_a_finding_absent_from_every_earlier_head_is_new():
    comments = [
        _lens_comment("qa", "1", "2026-09-29T01:00:00Z", ["coverage: no test for X"]),
        _lens_comment("qa", "2", "2026-09-29T02:00:00Z", ["coverage: no test for Y"]),
    ]
    assert swarm_dispatch.has_new_blocking_finding(comments) is True


def test_a_finding_that_returns_after_being_fixed_is_not_new_only_if_seen_before():
    comments = [
        _lens_comment("qa", "1", "2026-09-29T01:00:00Z", ["a: one"]),
        _lens_comment("qa", "2", "2026-09-29T02:00:00Z", ["b: two"]),
        _lens_comment("qa", "3", "2026-09-29T03:00:00Z", ["a: one"]),
    ]
    # head 3's blocker was raised at head 1, so it is not new.
    assert swarm_dispatch.has_new_blocking_finding(comments) is False


def test_a_single_reviewed_head_has_nothing_to_be_new_against():
    comments = [_lens_comment("qa", "1", "2026-09-29T01:00:00Z", ["a: one"])]
    assert swarm_dispatch.has_new_blocking_finding(comments) is False
    assert swarm_dispatch.has_new_blocking_finding([]) is False


def test_non_blocking_findings_and_unmarked_comments_are_ignored():
    comments = [
        _lens_comment("qa", "1", "2026-09-29T01:00:00Z", ["a: one"]),
        {"body": "[BLOCKING] x: not a lens comment", "created_at": "2026-09-29T01:30:00Z"},
        {
            "body": "<!-- review:qa commit=" + "2" * 40 + " -->\n[NON-BLOCKING] nit: z\nd",
            "created_at": "2026-09-29T02:00:00Z",
        },
    ]
    assert swarm_dispatch.has_new_blocking_finding(comments) is False


def test_unreadable_comments_count_as_a_new_finding(monkeypatch):
    """Fail closed: when the thread cannot be read, escalate."""

    async def boom(self, repository, number, client):
        raise RuntimeError("github down")

    monkeypatch.setattr(SwarmDispatcher, "_all_issue_comments", boom)
    assert asyncio.run(_dispatcher()._new_blocking_finding(_trigger())) is True


def test_round_one_never_reads_the_thread_for_a_new_finding(monkeypatch, world):
    """Nothing can be new on the first round, so no comment scan is spent."""
    scans: list[int] = []

    async def spy(self, trigger):
        scans.append(1)
        return True

    monkeypatch.setattr(SwarmDispatcher, "_new_blocking_finding", spy)
    world.fix_rounds = 0
    signals = asyncio.run(_dispatcher()._review_signals(_trigger()))
    assert scans == [] and signals.new_blocking_finding is False
    world.fix_rounds = 1
    signals = asyncio.run(_dispatcher()._review_signals(_trigger()))
    assert scans == [1] and signals.new_blocking_finding is True


# ── re-round delta sizing (ruling `small_rereview_rounds_run_mid`) ───────────
#
# Measured 2026-09-29: 47 of 53 dispatches ran top and none mid, because a
# re-round was sized by the WHOLE PR (1054/1344/1824 lines) rather than by what
# changed since the last reviewed head, and a merge of the base branch made even
# that delta look huge and security-touching.

import httpx  # noqa: E402

from test_review_delta import _file, _patch  # noqa: E402

PREV_HEAD = "1" * 40
CUR_HEAD = "2" * 40


def _lens_marker(lens: str, sha: str, blocking: tuple[str, ...] = ()) -> dict:
    body = f"<!-- review:{lens} commit={sha} -->\n**REQUEST_CHANGES**\n" + "\n".join(
        f"[BLOCKING] {b}\ndetail" for b in blocking
    )
    return {"body": body, "created_at": f"2026-09-29T0{int(sha[0])}:00:00Z"}


class _Github:
    """A fake GitHub the dispatcher's httpx client talks to; records every path."""

    def __init__(self) -> None:
        self.comments: list[dict] = [_lens_marker("qa", PREV_HEAD)]
        self.compare: dict[str, object] = {}  # sha -> files list | int status
        self.paths: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if path.endswith("/issues/87/comments"):
            return httpx.Response(200, json=self.comments)
        if "/compare/" in path:
            spec = path.rsplit("/compare/", 1)[1]
            sha = spec.split("...", 1)[1]
            result = self.compare.get(sha, 404)
            if isinstance(result, int):
                return httpx.Response(result, json={"message": "nope"})
            return httpx.Response(200, json={"files": result})
        if path.endswith("/pulls/87"):
            return httpx.Response(200, json={"base": {"ref": "main"}})
        return httpx.Response(404, json={})


@pytest.fixture
def github(monkeypatch, world):
    """Real `_review_delta` / `_new_blocking_finding` against a fake GitHub,
    with the whole-PR facts of today's case: 1054 lines touching a hook."""
    gh = _Github()
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(gh.handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(swarm_dispatch.httpx, "AsyncClient", factory)
    # The world fixture stubbed these two; the point here is the real ones.
    monkeypatch.setattr(
        SwarmDispatcher, "_review_delta", _REAL_REVIEW_DELTA, raising=False
    )
    monkeypatch.setattr(
        SwarmDispatcher, "_new_blocking_finding", _REAL_NEW_BLOCKING, raising=False
    )
    world.lines = 1054
    world.files = ["src/feature.py", ".claude/hooks/gate.py", "docs/notes.md"]
    world.fix_rounds = 1
    return gh


_REAL_REVIEW_DELTA = getattr(SwarmDispatcher, "_review_delta", None)
_REAL_NEW_BLOCKING = SwarmDispatcher._new_blocking_finding


def _small_delta_after_merging_main(gh: _Github) -> None:
    """The PR's own diff at both heads: same two files, its own lines unchanged
    bar one added line, while hunks moved because main changed around them. The
    unchanged file keeps the same blob: a file whose blob differs is a changed
    file even when its +/- lines match (`review_delta._unmeasured_change`)."""
    gh.compare[PREV_HEAD] = [
        _file("src/feature.py", _patch("+one", "-two", start=10)),
        _file(".claude/hooks/gate.py", _patch("+guard", start=5), sha="gate-blob"),
    ]
    gh.compare[CUR_HEAD] = [
        _file("src/feature.py", _patch("+one", "-two", "+fix from review", start=430)),
        _file(".claude/hooks/gate.py", _patch("+guard", start=77), sha="gate-blob"),
    ]


def _signals(github_world, **trigger_overrides):
    trigger = _trigger(head_sha=CUR_HEAD, base_ref="main", **trigger_overrides)
    return asyncio.run(_dispatcher()._review_signals(trigger))


def _tier_for(lens: str, signals) -> str:
    return model_tiering.resolve_tier(
        model_tiering.lens_review_class(lens), signals=signals
    ).tier


def test_a_small_rereview_delta_runs_pm_qa_ux_mid_and_arch_security_top(github):
    """Today's ledger case: a 1054-line PR whose re-round delta is one line."""
    _small_delta_after_merging_main(github)
    signals = _signals(github)
    assert signals.diff_lines_changed == 1
    assert signals.changed_files == ("src/feature.py",)
    assert signals.review_round == 2 and signals.prior_blocking_finding
    assert not signals.new_blocking_finding and not signals.diff_unreadable
    assert {l: _tier_for(l, signals) for l in ("pm", "qa", "ux")} == {
        "pm": "mid", "qa": "mid", "ux": "mid",
    }
    assert _tier_for("arch", signals) == "top"
    assert _tier_for("security", signals) == "top"


def test_a_branch_that_merged_main_is_sized_by_its_own_change_not_mains(github):
    """Everything main brought in sits between the two heads. Only the
    merge-base compares (`main...<sha>`) are read, so it cannot be counted."""
    _small_delta_after_merging_main(github)
    github.compare["raw"] = [
        _file(f".claude/hooks/from_main_{i}.py", _patch("+x" * 9)) for i in range(40)
    ]
    signals = _signals(github)
    assert signals.diff_lines_changed == 1
    assert not signals.touches_security_sensitive_path()
    compares = [p for p in github.paths if "/compare/" in p]
    assert compares == [
        f"/repos/owner/repo/compare/main...{PREV_HEAD}",
        f"/repos/owner/repo/compare/main...{CUR_HEAD}",
    ]


def test_a_delta_that_touches_a_security_path_goes_top(github):
    _small_delta_after_merging_main(github)
    github.compare[CUR_HEAD].append(_file("execution/hooks/new_gate.py", _patch("+x")))
    signals = _signals(github)
    assert signals.touches_security_sensitive_path()
    assert _tier_for("pm", signals) == "top"


def test_a_large_delta_goes_top_even_though_it_is_a_reround(github):
    _small_delta_after_merging_main(github)
    github.compare[CUR_HEAD].append(
        _file("src/big.py", _patch(*[f"+line {i}" for i in range(450)]))
    )
    signals = _signals(github)
    assert signals.diff_lines_changed == 1 + 450
    assert _tier_for("qa", signals) == "top"


def test_round_one_measures_the_whole_pr_and_reads_no_delta(github, world):
    _small_delta_after_merging_main(github)
    world.fix_rounds = 0
    signals = _signals(github)
    assert signals.review_round == 1
    assert signals.diff_lines_changed == 1054
    assert set(signals.changed_files) == set(world.files)
    assert _tier_for("pm", signals) == "top"
    assert github.paths == []  # neither the thread nor a compare was read


@pytest.mark.parametrize(
    "break_delta",
    [
        pytest.param(lambda gh: gh.compare.pop(PREV_HEAD), id="old-head-compare-404"),
        pytest.param(lambda gh: gh.compare.pop(CUR_HEAD), id="current-compare-404"),
        pytest.param(
            lambda gh: gh.compare.__setitem__(
                PREV_HEAD, [_file(f"f{i}.py", _patch("+x")) for i in range(300)]
            ),
            id="compare-may-be-truncated",
        ),
        pytest.param(
            lambda gh: setattr(gh, "comments", [_lens_marker("qa", CUR_HEAD)]),
            id="no-earlier-reviewed-head",
        ),
        pytest.param(lambda gh: setattr(gh, "comments", []), id="no-review-comments"),
    ],
)
def test_an_unreadable_delta_falls_back_to_the_whole_diff_never_a_smaller_one(
    github, break_delta
):
    _small_delta_after_merging_main(github)
    break_delta(github)
    signals = _signals(github)
    assert signals.diff_lines_changed == 1054  # the whole PR, not 0 and not less
    assert set(signals.changed_files) == {
        "src/feature.py", ".claude/hooks/gate.py", "docs/notes.md",
    }
    assert _tier_for("pm", signals) == "top"


def test_the_base_branch_is_read_from_the_pr_when_the_trigger_has_none(github):
    _small_delta_after_merging_main(github)
    signals = asyncio.run(
        _dispatcher()._review_signals(_trigger(head_sha=CUR_HEAD, base_ref=""))
    )
    assert signals.diff_lines_changed == 1
    assert any(p.endswith("/pulls/87") for p in github.paths)


def test_a_delta_is_not_used_by_a_dispatch_that_does_not_read_the_diff(github):
    """Lanius's gate check measures nothing, so it must not spend the reads."""
    _small_delta_after_merging_main(github)
    trigger = _trigger(head_sha=CUR_HEAD, base_ref="main")
    signals = asyncio.run(
        _dispatcher()._review_signals(
            trigger, measure_diff=False, detect_new_finding=False
        )
    )
    assert signals.diff_lines_changed == 0 and not signals.diff_unreadable
    assert github.paths == []


# ── a NEW blocking finding needs two reviewed heads before the current one ───


def test_the_head_being_reviewed_is_not_an_earlier_round():
    """The first re-round has one earlier round (the one it verifies). Lens
    comments already posted against the head under review, from a previous
    attempt or another lens, must not be mistaken for a second earlier round."""
    comments = [
        _lens_marker("qa", "1" * 40, ("coverage: no test for X",)),
        _lens_marker("qa", "2" * 40, ("coverage: no test for Y",)),
    ]
    # Read as two reviewed heads, head 2's Y is "new" ...
    assert swarm_dispatch.has_new_blocking_finding(comments) is True
    # ... but when head 2 is the one under review, only head 1 is reviewed.
    assert swarm_dispatch.has_new_blocking_finding(
        comments, current_head="2" * 40
    ) is False


def test_the_latest_reviewed_round_is_compared_with_every_earlier_one():
    comments = [
        _lens_marker("qa", "1" * 40, ("a: one",)),
        _lens_marker("qa", "2" * 40, ("b: two",)),
        _lens_marker("qa", "3" * 40, ("c: three",)),
    ]
    # Head 4 is under review; head 3 is the latest reviewed round and raised
    # a blocker neither head 1 nor head 2 had.
    assert swarm_dispatch.has_new_blocking_finding(
        comments, current_head="4" * 40
    ) is True
    comments.append(_lens_marker("qa", "4" * 40, ("z: partial run of the head under review",)))
    assert swarm_dispatch.has_new_blocking_finding(
        comments, current_head="4" * 40
    ) is True


def test_the_first_rereview_is_never_new_even_with_the_current_head_partly_reviewed(
    github,
):
    github.comments = [
        _lens_marker("qa", PREV_HEAD, ("coverage: no test for X",)),
        _lens_marker("pm", CUR_HEAD, ("scope: something else entirely",)),
    ]
    _small_delta_after_merging_main(github)
    signals = _signals(github)
    assert signals.new_blocking_finding is False
    assert _tier_for("pm", signals) == "mid"


def test_a_genuinely_new_blocker_on_the_latest_reviewed_head_still_goes_top(github):
    prev, older = PREV_HEAD, "0" * 40
    github.comments = [
        _lens_marker("qa", older, ("coverage: no test for X",)),
        _lens_marker("qa", prev, ("coverage: no test for Y",)),
    ]
    _small_delta_after_merging_main(github)
    signals = _signals(github)
    assert signals.new_blocking_finding is True
    assert _tier_for("pm", signals) == "top"


def test_an_unreadable_thread_still_fails_toward_top(github):
    _small_delta_after_merging_main(github)
    real_handler = github.handler
    github.handler = lambda request: (
        httpx.Response(500, json={})
        if request.url.path.endswith("/comments")
        else real_handler(request)
    )
    signals = _signals(github)
    assert signals.new_blocking_finding is True
    # The delta is unreadable too (it needs the thread): whole diff, top.
    assert signals.diff_lines_changed == 1054
    assert _tier_for("pm", signals) == "top"


def test_panel_rereview_of_a_big_pr_with_a_small_delta_runs_mid_for_pm_and_qa(
    monkeypatch, world
):
    """The whole panel path, today's case: 1054 lines and a security-path file
    in the PR, a three-line delta in the re-round."""
    world.fix_rounds = 1
    world.lines = 1054
    world.files = ["src/feature.py", ".claude/hooks/gate.py"]
    world.delta = review_delta.Delta(3, ("src/feature.py",))
    rec = _run_handle_pr(monkeypatch, world)
    for agent in ("pavo", "phoenicurus"):
        resolved = tier_of(rec.only(agent))
        assert (resolved.tier, resolved.source) == ("mid", "policy"), agent
        assert resolved.escalation_reasons == ()
    assert tier_of(rec.only("waxwing")).tier == "top"  # arch re-rounds stay top


def test_panel_rereview_with_an_unreadable_delta_stays_top(monkeypatch, world):
    world.fix_rounds = 1
    world.lines = 1054
    world.delta = None  # unreadable: the whole PR is measured instead
    rec = _run_handle_pr(monkeypatch, world)
    resolved = tier_of(rec.only("pavo"))
    assert resolved.tier == "top"
    assert "diff_lines_changed=1054>400" in resolved.escalation_reasons


def test_missing_lens_rerun_of_a_big_pr_with_a_small_delta_runs_mid(
    monkeypatch, world
):
    world.fix_rounds = 1
    world.lines = 1054
    world.delta = review_delta.Delta(3, ("src/feature.py",))
    rec = _run_missing_lens(monkeypatch, "qa")
    assert tier_of(rec.only("phoenicurus")).tier == "mid"
    rec = _run_missing_lens(monkeypatch, "security")
    assert tier_of(rec.only("falco")).tier == "top"


def test_panel_narrowed_reround_dispatches_only_the_lenses_it_selected(
    monkeypatch, world
):
    """Ruling `rereview_only_blockers_and_touched_areas`: the panel dispatch
    applies the selection, so a carried lens is not dispatched at all."""
    import review_carry

    world.fix_rounds = 1
    d = _pr_dispatcher(monkeypatch, world)
    d.config = dataclasses.replace(
        d.config, combined_review_pass=False, narrow_rereview=True
    )
    seen: dict[str, list[str]] = {}

    async def fake_selection(self, trigger, panel, review_head, *, forced):
        seen["panel"] = [item.lens for item in panel]
        carried = {
            lens: review_carry.Carried(lens, "b" * 40, "signed_off", "u")
            for lens in seen["panel"]
            if lens != "arch"
        }
        return review_carry.RerunSelection(frozenset({"arch"}), carried)

    async def no_note(self, *a, **k):
        return None

    monkeypatch.setattr(SwarmDispatcher, "_rereview_selection", fake_selection)
    monkeypatch.setattr(SwarmDispatcher, "_post_carry_note", no_note)
    rec = _Recorder(
        monkeypatch,
        stdout={"lanius": "GATE_INHERITANCE: clear", "vanellus": "**APPROVE**\nlgtm"},
    )
    asyncio.run(d._handle_pr(_trigger(body="Closes #80.")))
    lens_agents = {"pavo", "waxwing", "phoenicurus", "falco", "accipiter"}
    dispatched = {skill for skill, _ in rec.calls} & lens_agents
    assert "arch" in seen["panel"] and len(seen["panel"]) > 1
    assert dispatched == {"waxwing"}


# ── default-on wiring, end to end through `_handle_pr` (qa review of #1368) ──


class _Wire:
    """Everything a wiring test records: ordered events, prompts, posted bodies."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.prompts: dict[str, str] = {}
        self.posts: list[str] = []
        self.worktrees: list[str] = []


def _wire(monkeypatch, world, *, combined: bool, narrow: bool) -> tuple[SwarmDispatcher, _Wire]:
    import httpx

    from test_review_carry import _combined_reply

    w = _Wire()
    d = _pr_dispatcher(monkeypatch, world)
    d.config = dataclasses.replace(
        d.config, combined_review_pass=combined, narrow_rereview=narrow
    )

    async def fake_run_skill(skill, prompt, **kwargs):
        w.events.append(f"skill:{skill}")
        w.prompts[skill] = prompt
        if skill == "lanius":
            out = "GATE_INHERITANCE: clear"
        elif skill == "vanellus":
            out = "**APPROVE**\nlgtm"
        elif skill == "pavo" and "IN ONE PASS" in prompt:
            out = _combined_reply({"pm": "SIGNED_OFF", "qa": "SIGNED_OFF"})
        else:
            out = "**COMMENT**\nlgtm"
        return SkillResult(skill, True, 0, out, "")

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            class _R:
                status_code = 200

                def raise_for_status(s):
                    pass

                def json(s):
                    return []

            return _R()

        async def post(self, url, json=None, headers=None):
            body = json["body"]
            w.posts.append(body)
            marker = swarm_dispatch._LENS_MARKER_RE.search(body)
            w.events.append(f"post:{marker.group('lens')}" if marker else "post:note")

            class _R:
                def raise_for_status(s):
                    pass

            return _R()

    async def fake_worktree(repo, number, agent):
        w.worktrees.append(agent)
        return None

    async def expectations(self, repo, number):
        return {"pavo": "- [ ] the scope matches the issue"}

    monkeypatch.setattr(SwarmDispatcher, "_preregistered_expectations", expectations)
    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(swarm_dispatch, "prepare_pr_worktree", fake_worktree)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client())
    monkeypatch.setattr(swarm_dispatch, "usable_providers", lambda: set())
    return d, w


def test_handle_pr_runs_pm_and_qa_in_one_pass_and_posts_their_comments_before_aggregating(
    monkeypatch, world
):
    # A path keyed to a foundation document, so the reading list differs from the
    # kernel-only list a call with no changed files would give.
    world.files = ["execution/daemons/apis/swarm_dispatch.py"]
    d, w = _wire(monkeypatch, world, combined=True, narrow=False)
    asyncio.run(d._handle_pr(_trigger(body="Closes #80.")))
    assert w.events.count("skill:pavo") == 1, "one dispatch for the combined lenses"
    assert "skill:phoenicurus" not in w.events, "qa has no separate dispatch"
    assert "phoenicurus" not in w.worktrees, "no qa checkout for the combined lens"
    assert "post:pm" in w.events and "post:qa" in w.events
    # `changed_files` and `parent` reach the composed prompt through both hops
    # (`_handle_pr` -> `_run_combined_pass` -> `_combined_prompt`): the foundation
    # reading list keyed to the changed paths, the pm design-basis check, and the
    # check-off on the parent issue all vanish if either hop drops them.
    prompt = w.prompts["pavo"]
    readings = swarm_dispatch.reading_block(list(world.files))
    assert readings and readings != swarm_dispatch.reading_block([])
    assert readings in prompt
    assert "Design basis" in prompt
    assert "issues/80/comments" in prompt
    assert "the scope matches the issue" in prompt
    vanellus_at = w.events.index("skill:vanellus")
    assert w.events.index("post:pm") < vanellus_at
    assert w.events.index("post:qa") < vanellus_at


def _carried_selection():
    import review_carry

    carried = {
        lens: review_carry.Carried(lens, "b" * 40, "signed_off", f"https://c/{lens}")
        for lens in ("pm", "qa")
    }
    return carried


def test_handle_pr_names_carried_lenses_to_the_aggregator_and_posts_the_note(
    monkeypatch, world
):
    import review_carry

    world.fix_rounds = 1
    d, w = _wire(monkeypatch, world, combined=False, narrow=True)

    async def selection(self, trigger, panel, head, *, forced):
        carried = _carried_selection()
        return review_carry.RerunSelection(
            frozenset(item.lens for item in panel if item.lens not in carried),
            carried,
            {"arch": "the fix touched its area"},
        )

    monkeypatch.setattr(SwarmDispatcher, "_rereview_selection", selection)
    asyncio.run(d._handle_pr(_trigger(body="Closes #80.")))
    assert "CARRIED FORWARD" in w.prompts["vanellus"]
    assert "b" * 40 in w.prompts["vanellus"]
    note = [p for p in w.posts if "narrowed" in p]
    assert len(note) == 1
    assert "https://c/pm" in note[0], "the note links each carried verdict"
    assert "the fix touched its area" in note[0], "and says why the others re-ran"
    assert "skill:pavo" not in w.events and "skill:phoenicurus" not in w.events


def test_handle_pr_skips_the_aggregator_when_every_lens_is_carried(monkeypatch, world):
    import review_carry

    world.fix_rounds = 1
    d, w = _wire(monkeypatch, world, combined=False, narrow=True)

    async def selection(self, trigger, panel, head, *, forced):
        carried = {
            item.lens: review_carry.Carried(item.lens, "b" * 40, "signed_off", "u")
            for item in panel
        }
        return review_carry.RerunSelection(frozenset(), carried, {})

    monkeypatch.setattr(SwarmDispatcher, "_rereview_selection", selection)
    asyncio.run(d._handle_pr(_trigger(body="Closes #80.")))
    assert "skill:vanellus" not in w.events
    assert any("narrowed" in p for p in w.posts), "the note still says what carried"
