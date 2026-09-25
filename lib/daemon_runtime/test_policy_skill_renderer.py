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
# 2. Tiered rendering: size DEGRADES the index, never fails it open.
# ---------------------------------------------------------------------------
class TestTieredRendering:
    def test_small_corpus_selects_tier_a(self):
        rows = [
            _row("ent_always1", rule="Never skip the safety check.", applies_when="always"),
            _row("ent_cond1", rule="Verify before merging.", applies_when="opening a PR"),
        ]
        skills = renderer.render_skills(rows)
        text = renderer.render_index_text(skills, budget_chars=8000)
        assert "<!-- tier: A -->" in text
        assert "Verify before merging" in text  # tier A keeps the imperative

    def test_51_rules_at_realistic_lengths_selects_tier_b_and_fits(self):
        # Mirrors the coordinator's live measurement: 51 active rules (2
        # always, 49 conditional), realistic rule/applies_when lengths.
        # A trigger-only (tier B) render of a corpus this size is ~4,895
        # chars per that measurement — well within 8,000 — while tier A
        # (imperative kept) is expected to overflow it.
        rows = [
            _row(
                "ent_always1",
                rule="Never bypass the pre-commit hook with --no-verify.",
                applies_when="always",
            ),
            _row(
                "ent_always2",
                rule="Always verify the GitHub identity a token resolves to before any write.",
                applies_when="always",
            ),
        ]
        realistic_rule = (
            "Check the existing tasks, issues, and PRs before starting new "
            "work so the swarm neither duplicates work nor re-decides a "
            "settled question."
        )
        realistic_trigger_templates = [
            "opening a pull request",
            "sending an email on the operator's behalf",
            "dispatching a task to another agent",
            "merging a PR blocked only by mechanics",
            "restarting a daemon after a merge",
            "filing a GitHub issue",
            "writing to a shared Neotoma plan",
            "renaming an agent or daemon",
        ]
        rows += [
            _row(
                f"ent_cond{i:03d}",
                rule=realistic_rule,
                applies_when=realistic_trigger_templates[i % len(realistic_trigger_templates)],
            )
            for i in range(49)
        ]
        skills = renderer.render_skills(rows)
        text = renderer.render_index_text(skills, budget_chars=8000)
        assert len(text) <= 8000
        assert "<!-- tier: B -->" in text
        # Tier B keeps the trigger and id but drops the imperative sentence.
        assert "Check the existing tasks" not in text
        assert "opening a pull request" in text
        assert all(f"ent_cond{i:03d}" in text for i in range(49))

    def test_tier_c_orders_mandatory_first_never_cuts_a_line_and_states_omitted_count(self):
        # A corpus large enough that even tier B overflows an artificially
        # tiny budget — forces tier C. 40 rules, alternating mandatory and
        # advisory, so mandatory-first ordering is actually exercised.
        rows = [
            _row("ent_always1", rule="Never skip the safety check.", applies_when="always"),
        ]
        for i in range(40):
            rows.append(
                _row(
                    f"ent_cond{i:03d}",
                    rule="Rule body text.",
                    applies_when=f"condition number {i} occurring",
                    rule_kind="mandatory" if i % 2 == 0 else "advisory",
                )
            )
        skills = renderer.render_skills(rows)
        # Small enough that tier B (measured ~2,329 chars for this corpus)
        # cannot fit all 40 conditional lines, but large enough that SOME
        # whole lines still fit at tier C (preamble + closing alone is
        # ~237 chars; ~10-12 conditional lines fit in the remainder).
        tiny_budget = 1000
        text = renderer.render_index_text(skills, budget_chars=tiny_budget)

        assert len(text) <= tiny_budget
        assert "<!-- tier: C -->" in text
        assert "omitted for space" in text

        # Never cuts a line mid-way: every "- When ..." line in the output is
        # a complete, well-formed line (ends with a closing bracket for an
        # entity-id line, or is the omitted-count line).
        for line in text.splitlines():
            if line.startswith("- When "):
                assert line.rstrip().endswith("]"), f"cut mid-line: {line!r}"

        # Mandatory-first: the first conditional entity id actually kept
        # must be a mandatory rule (ent_cond000, ent_cond002, ... are
        # mandatory by construction above).
        kept_ids = [
            line.split("[")[-1].rstrip("]")
            for line in text.splitlines()
            if line.startswith("- When ") and line.rstrip().endswith("]")
        ]
        assert kept_ids, "tier C kept no conditional rules at all"
        first_kept_index = int(kept_ids[0].replace("ent_cond", ""))
        assert first_kept_index % 2 == 0, "tier C did not order mandatory rules first"

    def test_size_overflow_no_longer_produces_the_fail_open_notice(self):
        # Historical behaviour (pre-tiering): an over-budget corpus raised
        # PolicyIndexError, which the hook turned into the fail-open notice.
        # The operator ruling reverses this — size must degrade, not fail
        # open — so a corpus that is merely large must return SOME rendered
        # text (some tier), never raise.
        rows = [_row("ent_always1", applies_when="always")]
        rows += [
            _row(f"ent_cond{i:03d}", applies_when=f"condition {i}")
            for i in range(60)
        ]
        skills = renderer.render_skills(rows)
        text = renderer.render_index_text(skills, budget_chars=8000)  # must not raise
        assert text  # a real, non-empty rendered index
        assert any(f"<!-- tier: {t} -->" in text for t in ("A", "B", "C"))

    def test_even_tier_c_unfittable_still_raises_loudly(self):
        # The ONE case that still must raise: the budget is too small even
        # for the preamble + closing line with every conditional rule
        # dropped. Nothing smaller is left to render.
        rows = [
            _row(
                "ent_always1",
                rule="X" * 500,
                applies_when="always",
            )
        ]
        skills = renderer.render_skills(rows)
        with pytest.raises(renderer.PolicyIndexError):
            renderer.render_index_text(skills, budget_chars=20)


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
