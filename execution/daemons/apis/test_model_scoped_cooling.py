"""One model's usage cap cools that model, not the whole provider.

2026-11-13: the Sonnet weekly cap was spent while Opus and Fable had room.  The
refusal was classified as capacity and cooled the PROVIDER, so all Claude
dispatch idled.  These tests pin the fix: a refusal that names a model cools
only that model, the same provider then runs the next model the vendor_binding
DECLARES (never one it does not, never one below the dispatch's tier), each
substitution is a harness_event, and with no declared order the behaviour is
what it always was.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import harness_router  # noqa: E402
import limit_reset  # noqa: E402
import model_tiering  # noqa: E402
import skill_runner  # noqa: E402
from dispatch_usage import DispatchUsage  # noqa: E402
from skill_runner import SkillResult  # noqa: E402

SONNET_REFUSAL = "You've hit your Sonnet weekly limit · resets Nov 20 at 2pm (Europe/Madrid)"
OPUS_REFUSAL = "You've hit your Opus weekly limit · resets Nov 20 at 2pm (Europe/Madrid)"
PROVIDER_REFUSAL = "You've hit your weekly limit · resets Nov 20 at 2pm (Europe/Madrid)"

BINDING = {
    "claude": {
        "top": "opus",
        "mid": "sonnet",
        "mechanical": "haiku",
        "fallback_order": ["sonnet", "opus", "haiku"],
    }
}
POLICY = {"lens_review:pm": "mid", "build": "top"}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude")
    monkeypatch.delenv("APIS_HARNESS_HEADROOM", raising=False)
    monkeypatch.delenv("APIS_HARNESS_COOLDOWN_SECONDS", raising=False)
    monkeypatch.setenv("APIS_ACTION_POLICY", json.dumps(POLICY))
    monkeypatch.setenv("APIS_ACTION_POLICY_FILE", str(tmp_path / "none-policy.json"))
    monkeypatch.setenv("APIS_VENDOR_BINDING_FILE", str(tmp_path / "none-binding.json"))
    _binding(monkeypatch, BINDING)
    monkeypatch.setattr(skill_runner, "_provider_binaries", lambda: {"claude": "/bin/claude"})
    monkeypatch.setattr(skill_runner, "_refresh_usage_snapshot", lambda b: None)
    monkeypatch.setattr(skill_runner, "usage_gate", lambda *a, **k: None)
    monkeypatch.setattr(skill_runner, "usage_gate_refusals_all", lambda *a, **k: {})
    monkeypatch.setattr(
        skill_runner, "_load_agent_def", lambda *a, **k: (_ for _ in ()).throw(RuntimeError)
    )
    harness_router.reset_state()
    yield
    harness_router.reset_state()


def _binding(monkeypatch, binding) -> None:
    monkeypatch.setenv("APIS_VENDOR_BINDING", json.dumps(binding))


class Harness:
    """A fake child: records each launch's model and answers from a script."""

    def __init__(self, monkeypatch, refusals: dict[str, str], *, force_model=None,
                 max_launches=50):
        self.launched: list[str] = []
        self.events: list[dict] = []
        self.refusals = refusals  # model -> refusal text; absent model succeeds

        async def fake_once(skill, prompt, *, provider, model=None, precomputed_tier=None, **kw):
            effective = model or (
                model_tiering.model_for_tier(provider, precomputed_tier.tier)
                if precomputed_tier else None
            )
            if force_model is not None:
                effective = force_model  # a runner that ignores the model it is handed
            self.launched.append(effective)
            if len(self.launched) > max_launches:
                raise AssertionError(f"runaway retries: {len(self.launched)} launches")
            usage = DispatchUsage(provider=provider, model=effective, model_source="requested")
            if effective in self.refusals:
                return SkillResult(
                    skill, False, 1, self.refusals[effective], "", provider=provider, usage=usage
                )
            return SkillResult(skill, True, 0, "Verdict: APPROVE", "", provider=provider, usage=usage)

        async def fake_emit(**fields):
            self.events.append(fields)

        monkeypatch.setattr(skill_runner, "_run_skill_once", fake_once)
        monkeypatch.setattr(skill_runner, "_emit_harness_event", fake_emit)

    def run(self, action_class="lens_review:pm", **kw):  # noqa: D102
        return asyncio.run(
            skill_runner.run_skill(
                "pavo", "review", role="pavo", action_class=action_class, **kw
            )
        )


# ── reading the refusal ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line,family",
    [
        (SONNET_REFUSAL, "sonnet"),
        ("Sonnet 5 limit reached ∙ resets 3pm", "sonnet"),
        ("weekly limit for Claude Opus · resets 3pm", "opus"),
        (PROVIDER_REFUSAL, None),
        ("rate limit reached", None),
        ("You've hit your session limit · resets 12:30pm", None),
    ],
)
def test_a_refusal_names_a_model_only_when_it_says_so(line, family) -> None:
    assert limit_reset.limit_model(line) == family
    refusal = limit_reset.parse_refusal(line, "", provider="claude")
    assert refusal is not None and refusal.model == family


# ── the router's model window ───────────────────────────────────────────────


def test_a_model_window_covers_that_models_ids_and_no_other() -> None:
    harness_router.record_model_cooling("claude", "sonnet", time.time() + 3600, reason="weekly_limit")
    assert harness_router.model_cooled("claude", "sonnet")
    assert harness_router.model_cooled("claude", "claude-sonnet-5-5")
    assert harness_router.model_cooled("claude", "opus") is None
    assert harness_router.persisted_cooling("claude") is None  # the provider is untouched
    assert "claude" not in harness_router.cooling_providers()


def test_a_model_window_does_not_cover_a_longer_id_sharing_a_prefix() -> None:
    harness_router.record_model_cooling("claude", "gpt-6", time.time() + 3600, reason="x")
    assert harness_router.model_cooled("claude", "gpt-6-luna") is None


def test_an_expired_model_window_is_no_opinion() -> None:
    harness_router.record_model_cooling("claude", "sonnet", time.time() - 5, reason="x")
    assert harness_router.model_cooled("claude", "sonnet") is None


def test_a_usage_file_written_before_model_windows_still_reads(tmp_path) -> None:
    path = Path(harness_router._usage_path())
    until = harness_router._iso_from_wall(time.time() + 3600)
    path.write_text(json.dumps({"claude": {"cooling": {"until": until, "reason": "session_limit"}}}))
    assert harness_router.persisted_cooling("claude") is not None
    assert harness_router.model_cooled("claude", "sonnet") is None
    # Recording a model window keeps the provider window beside it.
    harness_router.record_model_cooling("claude", "sonnet", time.time() + 3600, reason="x")
    assert harness_router.persisted_cooling("claude") is not None
    assert harness_router.model_cooled("claude", "sonnet")


def test_two_models_windows_do_not_erase_each_other() -> None:
    harness_router.record_model_cooling("claude", "sonnet", time.time() + 3600, reason="x")
    harness_router.record_model_cooling("claude", "opus", time.time() + 3600, reason="x")
    assert harness_router.model_cooled("claude", "sonnet")
    assert harness_router.model_cooled("claude", "opus")


# ── the declared order ─────────────────────────────────────────────────────


def test_fallback_order_is_read_from_the_vendor_binding_only() -> None:
    assert model_tiering.configured_model_fallback_order("claude") == ("sonnet", "opus", "haiku")
    assert model_tiering.configured_vendor_binding()["claude"].keys() == {"top", "mid", "mechanical"}
    assert model_tiering.configured_model_fallback_order("codex") == ()


def test_a_malformed_order_declares_nothing(monkeypatch) -> None:
    for bad in ("sonnet", ["sonnet", "--opus"], ["sonnet", 3]):
        _binding(monkeypatch, {"claude": {"top": "opus", "fallback_order": bad}})
        assert model_tiering.configured_model_fallback_order("claude") == ()


def test_next_model_is_after_the_current_one_and_within_the_floor() -> None:
    order = ("sonnet", "opus", "haiku")
    assert model_tiering.next_fallback_model("claude", "sonnet", order=order, floor_tier="mid") == "opus"
    # haiku is declared but sits below mid, so it is never a stand-in at mid.
    assert model_tiering.next_fallback_model("claude", "opus", order=order, floor_tier="mid") is None
    # Opus is top: neither declared model after it is strong enough.
    assert model_tiering.next_fallback_model("claude", "opus", order=order, floor_tier="top") is None
    # An unknown floor substitutes nothing.
    assert model_tiering.next_fallback_model("claude", "opus", order=order, floor_tier=None) is None
    # A model the order does not name has no fallback.
    assert model_tiering.next_fallback_model("claude", "fable", order=order, floor_tier="mid") is None


def test_a_pinned_model_implies_the_tier_it_is_bound_to() -> None:
    assert model_tiering.bound_tier("claude", "opus") == "top"
    assert model_tiering.bound_tier("claude", "claude-sonnet-5-5") == "mid"  # family part of the id
    assert model_tiering.bound_tier("claude", "sonnet") == "mid"
    assert model_tiering.bound_tier("claude", "fable") is None


def test_the_config_checker_accepts_a_fallback_order(tmp_path) -> None:
    good = tmp_path / "vb.json"
    good.write_text(json.dumps(BINDING))
    _, _, errors, _ = model_tiering.check_config_file(good)
    assert errors == []
    bad = tmp_path / "vb2.json"
    bad.write_text(json.dumps({"claude": {"top": "opus", "fallback_order": []}}))
    _, _, errors, _ = model_tiering.check_config_file(bad)
    assert any("fallback_order" in e for e in errors)


# ── dispatch behaviour ─────────────────────────────────────────────────────


def test_a_sonnet_cap_cools_only_sonnet_and_the_retry_runs_the_next_declared_model(monkeypatch) -> None:
    harness = Harness(monkeypatch, {"sonnet": SONNET_REFUSAL})

    result = harness.run()

    assert result.ok
    assert harness.launched == ["sonnet", "opus"]
    assert harness_router.persisted_cooling("claude") is None
    assert "claude" not in harness_router.cooling_providers()
    assert harness_router.model_cooled("claude", "sonnet")
    assert harness_router.model_cooled("claude", "opus") is None
    # The next dispatch, in a fresh process, never launches the spent model.
    harness_router.reset_state()
    harness.launched.clear()
    assert harness.run().ok
    assert harness.launched == ["opus"]


def test_each_substitution_is_a_harness_event(monkeypatch) -> None:
    harness = Harness(monkeypatch, {"sonnet": SONNET_REFUSAL})
    harness.run()
    rows = [e for e in harness.events if "from_model=sonnet" in e.get("output_summary", "")]
    assert len(rows) == 1
    assert "to_model=opus" in rows[0]["output_summary"]
    assert "same_provider=true" in rows[0]["output_summary"]
    assert rows[0]["usage"].model == "opus"


def test_a_provider_wide_limit_still_cools_the_provider(monkeypatch) -> None:
    harness = Harness(monkeypatch, {"sonnet": PROVIDER_REFUSAL})

    result = harness.run()

    assert not result.ok
    assert harness.launched == ["sonnet"]
    assert harness_router.persisted_cooling("claude") is not None
    assert harness_router.model_cooled("claude", "sonnet") is None


def test_no_declared_order_keeps_todays_behaviour_and_says_so(monkeypatch, caplog) -> None:
    _binding(monkeypatch, {"claude": {"top": "opus", "mid": "sonnet", "mechanical": "haiku"}})
    harness = Harness(monkeypatch, {"sonnet": SONNET_REFUSAL})

    with caplog.at_level("WARNING"):
        result = harness.run()

    assert not result.ok
    assert harness.launched == ["sonnet"]
    assert harness_router.persisted_cooling("claude") is not None
    assert harness_router.model_cooled("claude", "sonnet") is None
    assert "declares no fallback_order" in caplog.text


def test_a_top_tier_dispatch_is_never_dropped_to_a_weaker_declared_model(monkeypatch) -> None:
    harness = Harness(monkeypatch, {"opus": OPUS_REFUSAL})
    _binding(monkeypatch, {"claude": {**BINDING["claude"], "fallback_order": ["opus", "sonnet"]}})

    result = harness.run(action_class="build")

    assert not result.ok
    assert harness.launched == ["opus"]  # sonnet is below top: not substituted
    assert harness_router.persisted_cooling("claude") is None  # the provider is healthy
    assert harness_router.model_cooled("claude", "opus")
    # The refusal to substitute is itself recorded, so spend stays explainable.
    assert any("to_model=none" in e.get("output_summary", "") for e in harness.events)


def test_a_refusal_naming_a_model_the_run_did_not_use_cools_the_provider(monkeypatch) -> None:
    harness = Harness(monkeypatch, {"sonnet": OPUS_REFUSAL})

    harness.run()

    assert harness.launched == ["sonnet"]
    assert harness_router.persisted_cooling("claude") is not None


def test_review_prompt_refusals_still_cool_the_provider(monkeypatch) -> None:
    """run_review_prompt launches one separately qualified model: no substitution."""
    scoped = skill_runner._cool_after_capacity_failure(
        "claude",
        SkillResult("pavo", False, 1, SONNET_REFUSAL, "", provider="claude",
                    usage=DispatchUsage(provider="claude", model="sonnet")),
    )
    assert scoped is None
    assert harness_router.persisted_cooling("claude") is not None
    assert harness_router.model_cooled("claude", "sonnet") is None


def test_a_pinned_model_is_never_replaced_by_a_weaker_one(monkeypatch) -> None:
    """The arch finding: a pinned model has no resolved tier, yet must not be
    downgraded; its floor is the tier the vendor_binding binds it to."""
    _binding(monkeypatch, {"claude": {**BINDING["claude"], "fallback_order": ["opus", "sonnet", "haiku"]}})
    harness = Harness(monkeypatch, {"opus": OPUS_REFUSAL})

    result = harness.run(action_class=None, model="opus")

    assert not result.ok
    assert harness.launched == ["opus"]  # neither sonnet (mid) nor haiku (mechanical)
    assert harness_router.persisted_cooling("claude") is None
    assert harness_router.model_cooled("claude", "opus")


def test_a_pinned_model_may_move_to_an_equal_or_stronger_declared_one(monkeypatch) -> None:
    harness = Harness(monkeypatch, {"sonnet": SONNET_REFUSAL})
    assert harness.run(action_class=None, model="sonnet").ok
    assert harness.launched == ["sonnet", "opus"]


def test_a_pinned_model_bound_to_no_tier_is_not_replaced(monkeypatch) -> None:
    _binding(monkeypatch, {"claude": {**BINDING["claude"], "fallback_order": ["fable", "opus"]}})
    harness = Harness(monkeypatch, {"fable": "You've hit your Fable weekly limit · resets Nov 20 at 2pm (Europe/Madrid)"})

    result = harness.run(action_class=None, model="fable")

    assert not result.ok
    assert harness.launched == ["fable"]


def test_an_unpinned_untiered_dispatch_launches_once_then_cools_the_provider(monkeypatch) -> None:
    """No model was named, so there is nothing to substitute: one launch, not a
    string of retries of the same ambient default."""
    harness = Harness(monkeypatch, {None: SONNET_REFUSAL})

    result = harness.run(action_class=None)

    assert not result.ok
    assert harness.launched == [None]
    assert harness_router.persisted_cooling("claude") is not None
    assert harness_router.model_cooled("claude", "sonnet") is None


def test_retries_are_bounded_by_the_declared_order(monkeypatch) -> None:
    """Every declared model refusing ends the dispatch after one launch each."""
    refusals = {"sonnet": SONNET_REFUSAL, "opus": OPUS_REFUSAL}
    _binding(monkeypatch, {"claude": {**BINDING["claude"], "fallback_order": ["sonnet", "opus"]}})
    harness = Harness(monkeypatch, refusals)

    result = harness.run()

    assert not result.ok
    assert harness.launched == ["sonnet", "opus"]


def test_refusing_to_substitute_is_a_provider_failover_row(monkeypatch) -> None:
    harness = Harness(monkeypatch, {"opus": OPUS_REFUSAL})
    _binding(monkeypatch, {"claude": {**BINDING["claude"], "fallback_order": ["opus", "sonnet"]}})

    harness.run(action_class="build")

    rows = [e for e in harness.events if "to_model=none" in e.get("output_summary", "")]
    assert len(rows) == 1
    assert rows[0]["event_type"] == "provider_failover"
    assert "from_model=opus" in rows[0]["output_summary"]
    assert "failover_reason=model_capacity" in rows[0]["output_summary"]
    assert rows[0]["usage"].model == "opus"


# ── floor arithmetic: each operand and the choice of max is pinned ──────────


def test_a_lower_tier_pinned_model_is_held_to_the_dispatch_tier(monkeypatch) -> None:
    """Dispatch tier top, pinned haiku (mechanical): sonnet (mid) is declared and
    above the pinned model, but below the dispatch tier, so only opus may follow."""
    _binding(monkeypatch, {"claude": {**BINDING["claude"], "fallback_order": ["haiku", "sonnet", "opus"]}})
    harness = Harness(monkeypatch, {"haiku": "You've hit your Haiku weekly limit · resets Nov 20 at 2pm (Europe/Madrid)"})

    result = harness.run(
        action_class=None, model="haiku", resolved_tier=model_tiering.resolve_tier("build")
    )

    assert result.ok
    assert harness.launched == ["haiku", "opus"]


def test_a_higher_tier_pinned_model_is_held_to_its_own_tier(monkeypatch) -> None:
    """Dispatch tier mid, pinned opus (top): sonnet (mid) meets the dispatch tier
    but is weaker than the model that was asked for, so nothing replaces it."""
    _binding(monkeypatch, {"claude": {**BINDING["claude"], "fallback_order": ["opus", "sonnet", "haiku"]}})
    harness = Harness(monkeypatch, {"opus": OPUS_REFUSAL})

    result = harness.run(
        action_class=None, model="opus", resolved_tier=model_tiering.resolve_tier("lens_review:pm")
    )

    assert not result.ok
    assert harness.launched == ["opus"]


def test_no_known_floor_means_no_substitution_even_to_the_strongest_model(monkeypatch) -> None:
    """A pinned model bound to no tier on an untiered dispatch: opus is declared
    and is the strongest model there is, and still is not substituted."""
    _binding(monkeypatch, {"claude": {**BINDING["claude"], "fallback_order": ["fable", "haiku", "sonnet", "opus"]}})
    harness = Harness(monkeypatch, {"fable": "You've hit your Fable weekly limit · resets Nov 20 at 2pm (Europe/Madrid)"})

    result = harness.run(action_class=None, model="fable")

    assert not result.ok
    assert harness.launched == ["fable"]


def test_model_meets_floor_is_false_without_a_floor() -> None:
    for model in ("haiku", "sonnet", "opus"):
        assert model_tiering.model_meets_floor("claude", model, None) is False
    assert model_tiering.model_meets_floor("claude", "opus", "top") is True
    assert model_tiering.model_meets_floor("claude", "sonnet", "top") is False


# ── the retry bound ─────────────────────────────────────────────────────────


def test_a_refusal_that_keeps_naming_an_unused_model_is_retried_exactly_as_often_as_the_order_allows(
    monkeypatch,
) -> None:
    """The runner keeps launching sonnet whatever it is handed, and sonnet keeps
    refusing, so the model windows never end the loop: only the bound does. The
    first launch plus one retry per declared model is the whole budget."""
    order = ["sonnet", "opus", "haiku"]
    _binding(monkeypatch, {"claude": {**BINDING["claude"], "fallback_order": order}})
    harness = Harness(monkeypatch, {"sonnet": SONNET_REFUSAL}, force_model="sonnet", max_launches=20)

    result = harness.run()

    assert not result.ok
    assert len(harness.launched) == 1 + len(order)
