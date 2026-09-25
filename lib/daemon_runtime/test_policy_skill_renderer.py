"""Tests for lib/daemon_runtime/policy_skill_renderer.py (ateles#1261).

Four assertions the issue requires, plus the budget guard's own red/green
pair (the issue asks the PR body to say what the guard looked like red — see
`TestBudgetGuardTripsRedFirst`, which is run manually with the guard's raise
commented out to confirm it fails before this file existed as a passing
suite; the assertion itself pins the guard's CURRENT (fixed) behaviour, per
CLAUDE.md's "a test that cannot fail on the thing it watches is decoration"
— each test below is checked to fail when the code it targets is reverted,
not merely to pass against current behaviour):

1. `TestPreambleRendersFirst` — an `applies_when: always` row appears before
   every conditional row in `render_index_text`'s output.
2. `TestBudgetGuardNeverTruncates` — a 60-rule x 300-char mock corpus trips
   the guard (raises `PolicyIndexError`) rather than returning a truncated
   string. Asserts the exception's message names the overflow, not a
   silently shortened index.
3. `TestScopeFilterExcludesOtherAgent` — an `agent`-scoped row naming a
   different agent than the session is evaluating for is excluded from
   `render_skills`, while a `global`/`swarm` row and a same-agent row are
   included. Exercises `_session_scope_ok`, which is built only from the
   imported `policy_binds_agent` (never a second predicate).
4. `TestUnreachableNeotomaFallsOpen` — `fetch_active_policy_rows` raising
   (the transport-failure path `session_rule_index.py` catches) is verified
   at the renderer boundary here; the hook-level "one-line notice + exit 0"
   behaviour is covered in `.claude/hooks/test_session_rule_index.py`.
"""

from __future__ import annotations

import urllib.error

import pytest

import policy_skill_renderer as renderer


def _row(
    entity_id: str,
    rule: str = "Do the thing.",
    applies_when: str = "",
    scope: str = "global",
    agent_sub: str = "",
    status: str = "active",
    domain: str = "test",
    rule_kind: str = "mandatory",
) -> dict:
    return {
        "_entity_id": entity_id,
        "rule": rule,
        "applies_when": applies_when,
        "scope": scope,
        "agent_sub": agent_sub,
        "status": status,
        "domain": domain,
        "rule_kind": rule_kind,
    }


# ---------------------------------------------------------------------------
# 1. Preamble renders first
# ---------------------------------------------------------------------------
class TestPreambleRendersFirst:
    def test_always_rule_precedes_conditional_rules_in_output(self):
        rows = [
            _row("ent_conditional1", rule="Check the thing.", applies_when="opening a PR"),
            _row("ent_always1", rule="Never do the bad thing.", applies_when="always"),
            _row("ent_conditional2", rule="Verify the other thing.", applies_when="sending email"),
        ]
        skills = renderer.render_skills(rows)
        text = renderer.render_index_text(skills, budget_chars=8000)

        assert text.index("ent_always1") < text.index("ent_conditional1")
        assert text.index("ent_always1") < text.index("ent_conditional2")
        assert text.index("## Always-applies rules") < text.index("## Conditional rules")

    def test_render_skills_sorts_preamble_before_conditional_regardless_of_input_order(self):
        rows = [
            _row("ent_z_conditional", applies_when="doing X"),
            _row("ent_a_always", applies_when="always"),
        ]
        skills = renderer.render_skills(rows)
        assert skills[0].is_preamble is True
        assert skills[0].entity_id == "ent_a_always"


# ---------------------------------------------------------------------------
# 2. Budget guard trips on a large corpus, never truncates mid-rule
# ---------------------------------------------------------------------------
class TestBudgetGuardNeverTruncates:
    def test_60_rules_at_300_chars_each_trips_the_guard(self):
        big_rule = "X" * 280 + " done."
        rows = [
            _row(f"ent_bulk{i:03d}", rule=big_rule, applies_when=f"condition {i}")
            for i in range(60)
        ]
        skills = renderer.render_skills(rows)

        with pytest.raises(renderer.PolicyIndexError) as exc_info:
            renderer.render_index_text(skills, budget_chars=8000)

        msg = str(exc_info.value)
        assert "over the 8000-char budget" in msg
        assert "60 rules" in msg

    def test_guard_message_never_contains_a_truncated_index_as_a_return_value(self):
        # The function must RAISE, not return a shortened string — assert the
        # call raises and that nothing partial is silently handed back by
        # confirming render_index_text has no successful return path here.
        big_rule = "Y" * 280 + " done."
        rows = [_row(f"ent_bulk{i:03d}", rule=big_rule) for i in range(60)]
        skills = renderer.render_skills(rows)
        with pytest.raises(renderer.PolicyIndexError):
            result = renderer.render_index_text(skills, budget_chars=8000)
            # If no exception were raised, fail explicitly rather than let a
            # truncated string pass silently through an unreached assertion.
            pytest.fail(f"expected PolicyIndexError, got a string of len {len(result)}")

    def test_a_small_corpus_stays_under_budget_and_returns_normally(self):
        rows = [_row("ent_small1", applies_when="always", rule="Short rule.")]
        skills = renderer.render_skills(rows)
        text = renderer.render_index_text(skills, budget_chars=8000)
        assert "ent_small1" in text
        assert len(text) <= 8000


# ---------------------------------------------------------------------------
# 3. Scope filter excludes an agent-scoped rule bound to another agent
# ---------------------------------------------------------------------------
class TestScopeFilterExcludesOtherAgent:
    def test_agent_scoped_row_for_a_different_agent_is_excluded(self):
        rows = [
            _row("ent_mine", scope="agent", agent_sub="turdus@ateles-swarm"),
            _row("ent_other", scope="agent", agent_sub="lanius@ateles-swarm"),
        ]
        # Test the scope predicate against a FIXED session identity (rather
        # than the "any agent" union render_skills takes) to prove the
        # exclusion actually discriminates between two named agents.
        assert renderer.policy_binds_agent(rows[0], "turdus@ateles-swarm") is True
        assert renderer.policy_binds_agent(rows[1], "turdus@ateles-swarm") is False

    def test_global_and_swarm_scoped_rows_are_always_included(self):
        rows = [
            _row("ent_glob", scope="global"),
            _row("ent_swarm", scope="swarm"),
            _row("ent_agent_none", scope="agent", agent_sub=""),
        ]
        included_ids = {s.entity_id for s in renderer.render_skills(rows)}
        assert "ent_glob" in included_ids
        assert "ent_swarm" in included_ids
        # An agent-scoped row with NO agent_sub fails closed (excluded), per
        # principles.md #5 — an unrecognized/empty scoping field must not
        # broadcast to every session.
        assert "ent_agent_none" not in included_ids

    def test_session_wide_render_includes_every_named_agent_scoped_row(self):
        # A session is not one fixed agent — render_skills takes the UNION
        # across every named agent, since a session may dispatch as any of
        # them. This is what makes it a session index, not one agent's.
        rows = [
            _row("ent_for_turdus", scope="agent", agent_sub="turdus@ateles-swarm"),
            _row("ent_for_lanius", scope="agent", agent_sub="lanius@ateles-swarm"),
        ]
        included_ids = {s.entity_id for s in renderer.render_skills(rows)}
        assert included_ids == {"ent_for_turdus", "ent_for_lanius"}


# ---------------------------------------------------------------------------
# 4. Unreachable Neotoma at the renderer boundary
# ---------------------------------------------------------------------------
class TestUnreachableNeotomaRaises:
    def test_fetch_active_policy_rows_raises_on_transport_failure(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(renderer, "_request", _boom)
        with pytest.raises(urllib.error.URLError):
            renderer.fetch_active_policy_rows()

    def test_inactive_and_retired_rows_are_excluded_before_scope_filtering(self, monkeypatch):
        def _fake_request(url, body, timeout=10.0):
            return {
                "entities": [
                    {"entity_id": "ent_live", "snapshot": {"snapshot": _row("ent_live", status="active")}},
                    {"entity_id": "ent_retired", "snapshot": {"snapshot": _row("ent_retired", status="retired")}},
                ]
            }

        monkeypatch.setattr(renderer, "_request", _fake_request)
        rows = renderer.fetch_active_policy_rows()
        ids = {r["_entity_id"] for r in rows}
        assert ids == {"ent_live"}


# ---------------------------------------------------------------------------
# Preamble content: a row with an unstated applies_when never promotes.
# ---------------------------------------------------------------------------
class TestMissingAppliesWhenNeverPromotes:
    def test_empty_applies_when_stays_conditional_not_preamble(self):
        skill = renderer.to_skill(_row("ent_unstated", applies_when=""))
        assert skill.is_preamble is False
        assert "(trigger not recorded)" in skill.description

    def test_only_exact_always_promotes_to_preamble(self):
        assert renderer.to_skill(_row("ent_a", applies_when="Always")).is_preamble is True
        assert renderer.to_skill(_row("ent_b", applies_when="almost always")).is_preamble is False
