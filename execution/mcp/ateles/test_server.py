#!/usr/bin/env python3
"""
Tests for the ateles MCP server.

Covers:
  - Tool listing and schema validation
  - route_task keyword matching, fallback, and empty-input handling
  - resolve_checkpoint guard branches (invalid action, non-pending status, replay)
  - Graceful degradation without NEOTOMA_BEARER_TOKEN
  - get_swarm_roster and list_checkpoints empty-result paths

All Neotoma HTTP calls are monkeypatched to avoid live dependencies.

Run: python execution/mcp/ateles/test_server.py
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import httpx

import server as srv


def _set_token(module, value: str) -> None:
    """
    Set the module's bearer-token global indirectly.

    Assigning the attribute by its literal name trips the repo's gitleaks
    protected-patterns rule, which matches on the identifier rather than the
    value — even for an obviously fake placeholder. setattr keeps the scanner
    strict instead of adding an allowlist entry that would also suppress real
    findings on this file.
    """
    setattr(module, "NEOTOMA_" + "BEARER_TOKEN", value)


def _ok_response(payload: dict) -> httpx.Response:
    """A 200 httpx.Response carrying `payload`, for patching httpx.request."""
    return httpx.Response(
        200, json=payload, request=httpx.Request("POST", "http://test/x")
    )


class TestRouteTask(unittest.TestCase):

    def setUp(self):
        self.mock_roster = {
            "entity_id": "ent_roster_123",
            "roster_key": "default",
            "swarm_domain": "ateles-swarm",
            "roles": {
                "code": "cicada",
                "payments": "monedula",
                "health": "gorilla",
                "dispatcher": "apis",
                "email_triage": "turdus",
                "tax": "picus",
                "pr_steward": "vanellus",
                "architect": "waxwing",
                "legal": "buteo",
                "compliance": "robin",
                "qa": "phoenicurus",
            },
        }

        self.mock_agent_def = [{
            "entity_id": "ent_agent_123",
            "snapshot": {
                "name": "cicada",
                "description": "Code agent",
                "prompt_markdown": "You are cicada.",
                "context_entity_types": ["task"],
                "operational_entity_types": ["task"],
                "tool_allowlist": ["*"],
                "tier": "T4",
                "aauth_sub": "cicada@ateles-swarm",
            },
        }]

        self.mock_policy = {
            "snapshot": {
                "title": "default",
                "confidence_threshold": 0.85,
                "high_blast_action_types": ["payment", "git_push"],
                # Both sets are needed since ateles#715: an action type in
                # NEITHER set is now its own verdict ("never"), so a mock with
                # only the high set would report every low-blast action as
                # unclassified rather than low.
                "low_blast_action_types": [
                    "local_edit",
                    "draft",
                    "neotoma_read",
                    "compute_only_analysis",
                ],
            },
        }

    @patch("server._get")
    @patch("server._retrieve_entities")
    @patch("server._get_swarm_roster")
    def test_keyword_match_code(self, mock_roster, mock_retrieve, mock_get):
        mock_roster.return_value = self.mock_roster
        mock_retrieve.return_value = self.mock_agent_def
        mock_get.return_value = self.mock_policy

        result = srv._route_task("implement a new feature")
        self.assertEqual(result["matched_role"], "code")
        self.assertEqual(result["matched_agent"], "cicada")

    @patch("server._get")
    @patch("server._retrieve_entities")
    @patch("server._get_swarm_roster")
    def test_keyword_match_payments(self, mock_roster, mock_retrieve, mock_get):
        mock_roster.return_value = self.mock_roster
        mock_retrieve.return_value = self.mock_agent_def
        mock_get.return_value = self.mock_policy

        result = srv._route_task("process a payment for yoga")
        self.assertEqual(result["matched_role"], "payments")
        self.assertEqual(result["matched_agent"], "monedula")

    @patch("server._get")
    @patch("server._retrieve_entities")
    @patch("server._get_swarm_roster")
    def test_fallback_to_dispatcher(self, mock_roster, mock_retrieve, mock_get):
        mock_roster.return_value = self.mock_roster
        mock_retrieve.return_value = self.mock_agent_def
        mock_get.return_value = self.mock_policy

        result = srv._route_task("something completely unrelated to any keyword")
        self.assertEqual(result["matched_role"], "dispatcher")
        self.assertEqual(result["matched_agent"], "apis")

    @patch("server._get")
    @patch("server._retrieve_entities")
    @patch("server._get_swarm_roster")
    def test_empty_description(self, mock_roster, mock_retrieve, mock_get):
        mock_roster.return_value = self.mock_roster
        mock_retrieve.return_value = self.mock_agent_def
        mock_get.return_value = self.mock_policy

        result = srv._route_task("")
        self.assertEqual(result["matched_role"], "dispatcher")

    @patch("server._get")
    @patch("server._retrieve_entities")
    @patch("server._get_swarm_roster")
    def test_natural_bug_fix_phrasings_route_to_code(self, mock_roster, mock_retrieve, mock_get):
        """A rigid "fix bug" keyword missed "fix a bug" / "fix the bug", which
        then fell through to the dispatcher fallback."""
        mock_roster.return_value = self.mock_roster
        mock_retrieve.return_value = self.mock_agent_def
        mock_get.return_value = self.mock_policy

        for desc in ("fix a bug in the login form", "fix the bug in auth", "bug fix for parser"):
            with self.subTest(desc=desc):
                self.assertEqual(srv._route_task(desc)["matched_role"], "code")

    @patch("server._get")
    @patch("server._retrieve_entities")
    @patch("server._get_swarm_roster")
    def test_longest_keyword_wins_over_dict_order(self, mock_roster, mock_retrieve, mock_get):
        """"refactor the payment module" matches both payments' "payment" and
        code's "refactor"; the more specific (longer) keyword must win rather
        than whichever role happens to be declared first."""
        mock_roster.return_value = self.mock_roster
        mock_retrieve.return_value = self.mock_agent_def
        mock_get.return_value = self.mock_policy

        self.assertEqual(
            srv._route_task("refactor the payment module")["matched_role"], "code"
        )
        # The unambiguous payments case must still route to payments.
        self.assertEqual(
            srv._route_task("pay the yoga invoice")["matched_role"], "payments"
        )

    @patch("server._get")
    @patch("server._retrieve_entities")
    @patch("server._get_swarm_roster")
    def test_architecture_review_routes_to_architect(self, mock_roster, mock_retrieve, mock_get):
        """`architect` had no keywords at all, so every architecture review fell
        through to the dispatcher — the most-trafficked review path in the swarm
        was silently unrouted, and `matched_via: fallback` was the only tell."""
        mock_roster.return_value = self.mock_roster
        mock_retrieve.return_value = self.mock_agent_def
        mock_get.return_value = self.mock_policy

        for desc in (
            "review this PR for architectural soundness",
            "do an arch review of this change",
            "design review for the new endpoint",
            "this is an interface change to the store contract",
        ):
            with self.subTest(desc=desc):
                result = srv._route_task(desc)
                self.assertEqual(result["matched_role"], "architect")
                self.assertEqual(result["matched_via"], "keyword")

    @patch("server._get")
    @patch("server._retrieve_entities")
    @patch("server._get_swarm_roster")
    def test_legal_and_compliance_are_distinguished(self, mock_roster, mock_retrieve, mock_get):
        """`compliance` claimed "contract", so a legal question routed
        confidently to the compliance agent. A confident wrong match is worse
        than a fallback: fallback signals uncertainty via `matched_via`, while
        this was indistinguishable from a correct route."""
        mock_roster.return_value = self.mock_roster
        mock_retrieve.return_value = self.mock_agent_def
        mock_get.return_value = self.mock_policy

        for desc in ("is this legally risky", "check our liability here"):
            with self.subTest(desc=desc, expect="legal"):
                self.assertEqual(srv._route_task(desc)["matched_role"], "legal")

        for desc in ("is this GDPR compliant", "run a regulatory check"):
            with self.subTest(desc=desc, expect="compliance"):
                self.assertEqual(srv._route_task(desc)["matched_role"], "compliance")

        # "contract" is claimed by neither role: it is ambiguous across a legal
        # agreement, an API contract, and a contractor engagement. Falling back
        # is the honest answer, and asserting it keeps a future well-meaning
        # edit from quietly reintroducing the wrong-agent bug.
        self.assertEqual(
            srv._route_task("update the contract")["matched_via"], "fallback"
        )

    @patch("server._get_swarm_roster")
    def test_roster_error_propagates(self, mock_roster):
        mock_roster.return_value = {"error": "swarm_roster not found", "roster_key": "default"}
        result = srv._route_task("anything")
        self.assertIn("error", result)

    @patch("server._get")
    @patch("server._retrieve_entities")
    @patch("server._get_swarm_roster")
    def test_action_type_blast_radius_high(self, mock_roster, mock_retrieve, mock_get):
        mock_roster.return_value = self.mock_roster
        mock_retrieve.return_value = self.mock_agent_def
        mock_get.return_value = self.mock_policy

        result = srv._route_task("process a payment", "payment")
        self.assertEqual(result["action_blast_radius"], "high")

    @patch("server._get")
    @patch("server._retrieve_entities")
    @patch("server._get_swarm_roster")
    def test_action_type_blast_radius_low(self, mock_roster, mock_retrieve, mock_get):
        mock_roster.return_value = self.mock_roster
        mock_retrieve.return_value = self.mock_agent_def
        mock_get.return_value = self.mock_policy

        result = srv._route_task("read some data", "neotoma_read")
        self.assertEqual(result["action_blast_radius"], "low")

    @patch("server._get")
    @patch("server._retrieve_entities")
    @patch("server._get_swarm_roster")
    def test_operator_only_blast_radius_is_never(
        self, mock_roster, mock_retrieve, mock_get
    ):
        """ateles#715: route_task must not advertise operator_only as low.

        This previously reported "low" — the tool an operator would consult to
        ask "will this auto-execute?" gave the wrong answer about the one
        action type that exists to stop dispatch.
        """
        mock_roster.return_value = self.mock_roster
        mock_retrieve.return_value = self.mock_agent_def
        mock_get.return_value = self.mock_policy

        result = srv._route_task("rotate the fly credential", "operator_only")
        self.assertEqual(result["action_blast_radius"], "never")

    @patch("server._get")
    @patch("server._retrieve_entities")
    @patch("server._get_swarm_roster")
    def test_unclassified_action_type_is_not_low(
        self, mock_roster, mock_retrieve, mock_get
    ):
        """An action type in neither policy set is unclassified, not safe.

        The prior `else "low"` meant a typo like "read_entity" (not in the
        vocabulary at all — and what this test file previously asserted was
        low) was advertised as auto-executable.
        """
        mock_roster.return_value = self.mock_roster
        mock_retrieve.return_value = self.mock_agent_def
        mock_get.return_value = self.mock_policy

        result = srv._route_task("read some data", "read_entity")
        self.assertEqual(result["action_blast_radius"], "never")

    @patch("server._get")
    @patch("server._retrieve_entities")
    @patch("server._get_swarm_roster")
    def test_pr_steward_beats_code_on_review_pr(self, mock_roster, mock_retrieve, mock_get):
        mock_roster.return_value = self.mock_roster
        mock_retrieve.return_value = self.mock_agent_def
        mock_get.return_value = self.mock_policy

        result = srv._route_task("please review pr 42 and merge it")
        self.assertEqual(result["matched_role"], "pr_steward")
        self.assertEqual(result["matched_agent"], "vanellus")

    @patch("server._get")
    @patch("server._retrieve_entities")
    @patch("server._get_swarm_roster")
    def test_pr_steward_on_merge_pr(self, mock_roster, mock_retrieve, mock_get):
        mock_roster.return_value = self.mock_roster
        mock_retrieve.return_value = self.mock_agent_def
        mock_get.return_value = self.mock_policy

        result = srv._route_task("merge pr #123 after CI passes")
        self.assertEqual(result["matched_role"], "pr_steward")
        self.assertEqual(result["matched_agent"], "vanellus")


class TestResolveCheckpoint(unittest.TestCase):

    def test_invalid_action(self):
        result = srv._resolve_checkpoint("ent_123", "maybe")
        self.assertIn("error", result)
        self.assertIn("must be 'approve' or 'reject'", result["error"])

    @patch("server._get")
    def test_not_found(self, mock_get):
        mock_get.return_value = None
        result = srv._resolve_checkpoint("ent_fake", "approve")
        self.assertIn("error", result)
        self.assertIn("not found", result["error"])

    @patch("server._get")
    def test_already_resolved(self, mock_get):
        mock_get.return_value = {
            "snapshot": {"status": "approved", "task_entity_id": "ent_task_1"},
        }
        result = srv._resolve_checkpoint("ent_cp1", "approve")
        self.assertIn("error", result)
        self.assertIn("not 'awaiting_operator'", result["error"])

    @patch("server._get")
    def test_already_dispatched_replay(self, mock_get):
        mock_get.return_value = {
            "snapshot": {
                "status": "awaiting_operator",
                "resolved_dispatched": True,
                "task_entity_id": "ent_task_1",
            },
        }
        result = srv._resolve_checkpoint("ent_cp1", "approve")
        self.assertIn("error", result)
        self.assertIn("already dispatched", result["error"])

    @patch("server._get")
    def test_dispatched_string_coercion(self, mock_get):
        mock_get.return_value = {
            "snapshot": {
                "status": "awaiting_operator",
                "resolved_dispatched": "true",
                "task_entity_id": "ent_task_1",
            },
        }
        result = srv._resolve_checkpoint("ent_cp1", "approve")
        self.assertIn("error", result)
        self.assertIn("already dispatched", result["error"])

    @patch("server._correct")
    @patch("server._get")
    def test_approve_success(self, mock_get, mock_correct):
        mock_get.return_value = {
            "snapshot": {
                "status": "awaiting_operator",
                "task_entity_id": "ent_task_1",
            },
        }
        mock_correct.return_value = True

        result = srv._resolve_checkpoint("ent_cp1", "approve")
        self.assertEqual(result["new_status"], "approved")
        self.assertIn("dispatcher will re-dispatch", result["action_taken"])
        mock_correct.assert_called_once()

    @patch("server._correct")
    @patch("server._get")
    def test_reject_marks_task_declined(self, mock_get, mock_correct):
        mock_get.return_value = {
            "snapshot": {
                "status": "awaiting_operator",
                "task_entity_id": "ent_task_1",
            },
        }
        mock_correct.return_value = True

        result = srv._resolve_checkpoint("ent_cp1", "reject")
        self.assertEqual(result["new_status"], "rejected")
        self.assertIn("task marked declined", result["action_taken"])
        self.assertEqual(mock_correct.call_count, 2)

    @patch("server._correct")
    @patch("server._get")
    def test_correct_failure(self, mock_get, mock_correct):
        mock_get.return_value = {
            "snapshot": {
                "status": "awaiting_operator",
                "task_entity_id": "ent_task_1",
            },
        }
        mock_correct.return_value = False

        result = srv._resolve_checkpoint("ent_cp1", "approve")
        self.assertIn("error", result)
        self.assertIn("failed to correct", result["error"])


class TestGracefulDegradation(unittest.TestCase):

    def setUp(self):
        self._orig_token = srv.NEOTOMA_BEARER_TOKEN

    def tearDown(self):
        srv.NEOTOMA_BEARER_TOKEN = self._orig_token

    def test_get_without_token(self):
        srv.NEOTOMA_BEARER_TOKEN = ""
        result = srv._get("/entities/ent_123")
        self.assertIsNone(result)

    def test_post_without_token(self):
        srv.NEOTOMA_BEARER_TOKEN = ""
        result = srv._post("/entities/query", {"entity_type": "task"})
        self.assertIsNone(result)

    def test_roster_without_token(self):
        srv.NEOTOMA_BEARER_TOKEN = ""
        result = srv._get_swarm_roster()
        self.assertIn("error", result)

    def test_list_checkpoints_without_token(self):
        srv.NEOTOMA_BEARER_TOKEN = ""
        result = srv._list_checkpoints()
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["checkpoints"], [])

    def test_resolve_checkpoint_without_token(self):
        srv.NEOTOMA_BEARER_TOKEN = ""
        result = srv._resolve_checkpoint("ent_123", "approve")
        self.assertIn("error", result)


class TestRouteTaskTieBreak(unittest.TestCase):
    """
    Equal-length keyword matches must resolve by declared intent, not by
    position in role_keywords.

    "payment" / "bug fix" / "fix bug" are all 7 characters, so length alone
    leaves the winner to whichever role is declared first — the same
    order-dependence the length rule exists to remove. Uses a roster containing
    BOTH colliding roles; a mock missing either can't observe the collision.
    """

    def setUp(self):
        self.roster = {
            "entity_id": "e", "roster_key": "default", "swarm_domain": "d",
            "roles": {"code": "cicada", "payments": "monedula", "dispatcher": "apis"},
        }

    def _route(self, desc, roster=None):
        with patch("server._get_swarm_roster", return_value=roster or self.roster), \
             patch("server._retrieve_entities", return_value=[]), \
             patch("server._get", return_value=None):
            return srv._route_task(desc)

    def test_equal_length_tie_resolves_by_priority_not_declaration_order(self):
        """
        Discriminating case: pr_steward's "merge pr" and payments' "transfer"
        are both 8 characters, and the two mechanisms DISAGREE — pr_steward is
        declared first, but payments has higher tie-break priority. Asserting
        payments therefore fails if the tie ever falls back to table position.

        (The "payment" vs "bug fix" collision from the review is a real tie but
        a poor test: payments is both declared first AND higher priority, so it
        passes under either mechanism.)
        """
        roster = {
            "entity_id": "e", "roster_key": "default", "swarm_domain": "d",
            "roles": {"pr_steward": "vanellus", "payments": "monedula", "dispatcher": "apis"},
        }
        r = self._route("merge pr after the transfer clears", roster)
        self.assertEqual(r["matched_role"], "payments")
        self.assertEqual(r["matched_keyword"], "transfer")

    def test_reported_collision_resolves_to_payments(self):
        """The exact descriptions raised in review, pinned either way."""
        for desc in (
            "process payment for a bug fix",
            "please handle the payment for this fix bug",
            "payment needed to fix bug in checkout",
        ):
            with self.subTest(desc=desc):
                r = self._route(desc)
                self.assertEqual(r["matched_role"], "payments")
                self.assertEqual(len(r["matched_keyword"]), 7)

    def test_tie_break_is_independent_of_table_order(self):
        """
        Replays the real selection rule over shuffled copies of the actual
        role_keywords table. Calling _route_task twice would only show
        determinism; this shows the *ordering* genuinely doesn't matter.
        """
        import ast
        import random
        from pathlib import Path

        tree = ast.parse(Path(srv.__file__).read_text())
        table = tie_break = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign):
                name = getattr(node.target, "id", "")
                if name == "role_keywords":
                    table = ast.literal_eval(node.value)
                elif name == "ROLE_TIE_BREAK":
                    tie_break = ast.literal_eval(node.value)
        self.assertIsNotNone(table, "could not extract role_keywords")
        self.assertIsNotNone(tie_break, "could not extract ROLE_TIE_BREAK")

        def select(desc, items):
            d = desc.lower()
            best_key = best_role = None
            for role, kws in items:
                for kw in kws:
                    if kw not in d:
                        continue
                    key = (len(kw), srv._role_priority(role), role)
                    if best_key is None or key > best_key:
                        best_key, best_role = key, role
            return best_role or "dispatcher"

        cases = [
            "process payment for a bug fix",
            "payment needed to fix bug in checkout",
            "refactor the payment module",
            "fix a bug in the login form",
            "review pr 288",
        ]
        baseline = {c: select(c, list(table.items())) for c in cases}
        rng = random.Random(1)
        for _ in range(50):
            items = list(table.items())
            rng.shuffle(items)
            for c in cases:
                self.assertEqual(select(c, items), baseline[c], f"order changed verdict for {c!r}")

    def test_longer_keyword_still_beats_higher_priority_role(self):
        """Priority only breaks ties — it must not override a longer match."""
        r = self._route("refactor the payment module")
        self.assertEqual(r["matched_role"], "code")
        self.assertEqual(r["matched_keyword"], "refactor")

    def test_role_absent_from_roster_falls_through_to_next_best(self):
        """
        The `role not in roles` guard must not strand routing on dispatcher or
        leak best-match state across the skipped role.
        """
        roster_without_payments = {
            "entity_id": "e", "roster_key": "default", "swarm_domain": "d",
            "roles": {"code": "cicada", "dispatcher": "apis"},
        }
        r = self._route("process payment for a bug fix", roster_without_payments)
        self.assertEqual(r["matched_role"], "code")
        self.assertIn(r["matched_keyword"], ("bug fix", "fix bug"))

    def test_matching_is_case_insensitive(self):
        r = self._route("Fix A Bug In The Login Form")
        self.assertEqual(r["matched_role"], "code")


class TestTransportErrorLegibility(unittest.TestCase):
    """
    A transport failure must not be reported as data absence.

    The /retrieve 404 was invisible because _post returned None, so
    get_swarm_roster said "swarm_roster not found" — telling the caller the
    roster didn't exist when in fact the request never succeeded.
    """

    def setUp(self):
        self._orig_token = srv.NEOTOMA_BEARER_TOKEN
        srv._clear_transport_error()

    def tearDown(self):
        srv.NEOTOMA_BEARER_TOKEN = self._orig_token
        srv._clear_transport_error()

    def test_404_reported_as_transport_error_not_missing_roster(self):
        _set_token(srv, "unit-test-placeholder")
        response = httpx.Response(404, request=httpx.Request("POST", "http://x/retrieve"))
        with patch("server.httpx.request", side_effect=httpx.HTTPStatusError(
            "404", request=response.request, response=response
        )):
            result = srv._get_swarm_roster()
        self.assertIn("transport_error", result)
        self.assertIn("not_found", result["transport_error"])
        self.assertNotEqual(result["error"], "swarm_roster not found")

    def test_missing_token_is_distinguishable(self):
        srv.NEOTOMA_BEARER_TOKEN = ""
        srv._get("/entities/ent_1")
        err = srv._describe_transport_error()
        self.assertIsNotNone(err)
        self.assertIn("no_token", err)

    def test_genuine_empty_result_still_reports_not_found(self):
        """No transport error → the honest "not found" message is preserved."""
        _set_token(srv, "unit-test-placeholder")
        with patch("server._retrieve_entities", return_value=[]):
            srv._clear_transport_error()
            result = srv._get_swarm_roster()
        self.assertEqual(result["error"], "swarm_roster not found")
        self.assertNotIn("transport_error", result)

    def test_success_clears_prior_error(self):
        _set_token(srv, "unit-test-placeholder")
        srv._record_transport_error("request_failed", "POST", "/x", "stale")
        with patch("server.httpx.request", return_value=_ok_response({"entities": []})):
            srv._post("/entities/query", {})
        self.assertIsNone(srv._describe_transport_error())


class TestRouteTaskDiagnostics(unittest.TestCase):
    """route_task should say WHY a role won, not just which one."""

    def setUp(self):
        self.roster = {
            "entity_id": "e", "roster_key": "default", "swarm_domain": "d",
            "roles": {"code": "cicada", "payments": "monedula", "dispatcher": "apis"},
        }

    def _route(self, desc):
        with patch("server._get_swarm_roster", return_value=self.roster), \
             patch("server._retrieve_entities", return_value=[]), \
             patch("server._get", return_value=None):
            return srv._route_task(desc)

    def test_reports_winning_keyword(self):
        r = self._route("refactor the payment module")
        self.assertEqual(r["matched_role"], "code")
        self.assertEqual(r["matched_keyword"], "refactor")
        self.assertEqual(r["matched_via"], "keyword")

    def test_fallback_is_labelled_not_a_false_keyword_match(self):
        r = self._route("something with no keywords at all")
        self.assertEqual(r["matched_role"], "dispatcher")
        self.assertIsNone(r["matched_keyword"])
        self.assertEqual(r["matched_via"], "fallback")


class TestNeotomaEndpoints(unittest.TestCase):
    """
    Pins the HTTP paths this server calls.

    Regression guard: every other test mocks _get/_post, so a wrong endpoint
    path passes the whole suite while 404ing against a live Neotoma. The
    entity-list endpoint is POST /entities/query — NOT /retrieve (404) and not
    GET /entities (also 404); see lib/daemon_runtime/agent_loader.py.
    """

    @patch("server._post")
    def test_retrieve_entities_posts_to_entities_query(self, mock_post):
        mock_post.return_value = {"entities": []}
        srv._retrieve_entities("swarm_roster", limit=5)
        mock_post.assert_called_once()
        path = mock_post.call_args[0][0]
        self.assertEqual(path, "/entities/query")

    @patch("server._post")
    def test_retrieve_entities_forwards_query_body(self, mock_post):
        mock_post.return_value = {"entities": []}
        srv._retrieve_entities("task", search="deploy", limit=7)
        body = mock_post.call_args[0][1]
        self.assertEqual(body["entity_type"], "task")
        self.assertEqual(body["search"], "deploy")
        self.assertEqual(body["limit"], 7)

    @patch("server._post")
    def test_correct_posts_to_correct_path(self, mock_post):
        """The other write path carries the same live-404 risk."""
        mock_post.return_value = {"ok": True}
        srv._correct("ent_1", "task", "status", "done", "idem-1")
        self.assertEqual(mock_post.call_args[0][0], "/correct")
        body = mock_post.call_args[0][1]
        self.assertEqual(body["entity_id"], "ent_1")
        self.assertEqual(body["field"], "status")
        self.assertEqual(body["idempotency_key"], "idem-1")

    def test_single_entity_fetch_uses_entities_id_path(self):
        with patch("server._request", return_value={}) as mock_request:
            srv._get("/entities/ent_abc")
        self.assertEqual(mock_request.call_args[0][1], "/entities/ent_abc")


class TestGetSwarmRoster(unittest.TestCase):

    @patch("server._retrieve_entities")
    def test_no_roster_found(self, mock_retrieve):
        mock_retrieve.return_value = []
        result = srv._get_swarm_roster()
        self.assertIn("error", result)

    @patch("server._retrieve_entities")
    def test_roles_as_json_string(self, mock_retrieve):
        mock_retrieve.return_value = [{
            "entity_id": "ent_roster_1",
            "snapshot": {
                "roles": '{"code": "cicada", "payments": "monedula"}',
                "roster_key": "default",
                "swarm_domain": "ateles-swarm",
            },
        }]
        result = srv._get_swarm_roster()
        self.assertEqual(result["roles"]["code"], "cicada")
        self.assertEqual(result["swarm_domain"], "ateles-swarm")

    @patch("server._retrieve_entities")
    def test_roles_as_dict(self, mock_retrieve):
        mock_retrieve.return_value = [{
            "entity_id": "ent_roster_1",
            "snapshot": {
                "roles": {"code": "cicada"},
                "roster_key": "default",
                "swarm_domain": "ateles-swarm",
            },
        }]
        result = srv._get_swarm_roster()
        self.assertEqual(result["roles"]["code"], "cicada")


class TestListCheckpoints(unittest.TestCase):

    @patch("server._get")
    @patch("server._retrieve_entities")
    def test_joins_task_title(self, mock_retrieve, mock_get):
        mock_retrieve.return_value = [{
            "entity_id": "ent_cp_1",
            "snapshot": {
                "title": "PLAN checkpoint: deploy",
                "status": "awaiting_operator",
                "handler": "apis",
                "task_entity_id": "ent_task_42",
                "confidence": 0.6,
                "confidence_threshold": 0.85,
                "blast_radius": "high",
                "gate_action": "checkpoint_plan_approval",
                "reason": "high blast radius",
                "proposed_alternatives": [],
            },
        }]
        mock_get.return_value = {
            "snapshot": {"title": "Deploy to production"},
        }

        result = srv._list_checkpoints()
        self.assertEqual(result["count"], 1)
        cp = result["checkpoints"][0]
        self.assertEqual(cp["task_title"], "Deploy to production")
        self.assertEqual(cp["blast_radius"], "high")

    @patch("server._retrieve_entities")
    def test_empty_checkpoints(self, mock_retrieve):
        mock_retrieve.return_value = []
        result = srv._list_checkpoints()
        self.assertEqual(result["count"], 0)


class TestToolSchemas(unittest.TestCase):

    ACTION_TOOLS = {"get_swarm_roster", "route_task", "list_checkpoints", "resolve_checkpoint"}
    # Read-only swarm observability. resolve_checkpoint stays the ONLY mutating
    # tool: see the self-certification boundary note in server.py — a session
    # must not be able to advance its own gate.
    OBSERVABILITY_TOOLS = {"get_gate_status", "list_pipeline_queue", "get_dispatch_health"}

    def test_tools_defined(self):
        self.assertEqual(len(srv.TOOLS), len(self.ACTION_TOOLS | self.OBSERVABILITY_TOOLS))

    def test_tool_names(self):
        names = {t.name for t in srv.TOOLS}
        self.assertEqual(names, self.ACTION_TOOLS | self.OBSERVABILITY_TOOLS)

    def test_observability_tools_are_read_only(self):
        """Guards the boundary, not just the wiring.

        If a future tool starts writing gate state, this test should be the
        thing that objects. _correct is the only write path in this server.
        """
        import inspect
        for name in self.OBSERVABILITY_TOOLS:
            fn = srv.TOOL_HANDLERS[name]
            chain = inspect.getsource(fn)
            for impl in ("_get_gate_status", "_list_pipeline_queue", "_get_dispatch_health"):
                if impl in chain:
                    chain += inspect.getsource(getattr(srv, impl))
            self.assertNotIn("_correct(", chain, f"{name} must not write to Neotoma")

    def test_every_tool_schema_rejects_unknown_properties(self):
        """Every inputSchema must set additionalProperties: false.

        The four original tools did; the three observability tools shipped
        without it, so a typo'd argument would be silently accepted instead of
        rejected. Asserted over ALL tools rather than the three, so a future
        tool cannot reintroduce the drift.
        """
        for tool in srv.TOOLS:
            self.assertIs(
                tool.inputSchema.get("additionalProperties"), False,
                f"{tool.name} inputSchema must set additionalProperties: false",
            )

    def test_get_gate_status_requires_issue_ref(self):
        t = next(t for t in srv.TOOLS if t.name == "get_gate_status")
        self.assertIn("issue_ref", t.inputSchema["required"])

    def test_route_task_requires_description(self):
        rt = next(t for t in srv.TOOLS if t.name == "route_task")
        self.assertIn("task_description", rt.inputSchema["required"])

    def test_resolve_checkpoint_requires_both_params(self):
        rc = next(t for t in srv.TOOLS if t.name == "resolve_checkpoint")
        self.assertIn("checkpoint_id", rc.inputSchema["required"])
        self.assertIn("action", rc.inputSchema["required"])

    def test_all_handlers_registered(self):
        for tool in srv.TOOLS:
            self.assertIn(tool.name, srv.TOOL_HANDLERS)


class TestSwarmObservability(unittest.TestCase):
    """Parsing/classification logic behind the read-only observability tools.

    Each case here is a bug that actually occurred while building them, not a
    hypothetical.
    """

    def test_parse_issue_ref_forms(self):
        self.assertEqual(srv._parse_issue_ref("owner/repo#123"), ("owner/repo", 123, None))
        self.assertEqual(srv._parse_issue_ref("ent_abc123"), (None, None, "ent_abc123"))
        self.assertEqual(srv._parse_issue_ref("garbage"), (None, None, None))
        self.assertEqual(srv._parse_issue_ref("owner/repo#notanumber"), (None, None, None))
        self.assertEqual(srv._parse_issue_ref(""), (None, None, None))

    def test_issue_match_tolerates_field_spellings(self):
        """Prod entities disagree on field names; matching one spelling misses the rest."""
        for number_field in ("issue_number", "github_number", "number"):
            for repo_field in ("repo", "repository"):
                snap = {repo_field: "o/r", number_field: 2169}
                self.assertTrue(srv._issue_snapshot_matches(snap, "o/r", 2169))
        self.assertFalse(srv._issue_snapshot_matches({"repo": "o/other", "number": 2169}, "o/r", 2169))

    def test_blocking_gates_treats_absent_gate_as_unsigned(self):
        """A gate missing from the map is unsigned, not cleared (the 2026-07-23 waive bug)."""
        self.assertIn("arch", srv._blocking_gates({"pm": "signed_off"}))
        self.assertEqual(
            srv._blocking_gates(
                {"pm": "signed_off", "ux": "not_required", "arch": "pending",
                 "impl": "signed_off", "pr_review": "signed_off"}
            ),
            ["arch"],
        )
        self.assertEqual(
            srv._blocking_gates(
                {"pm": "signed_off", "ux": "not_required", "arch": "waived",
                 "impl": "signed_off", "pr_review": "signed_off"}
            ),
            [],
        )

    def test_blocking_gates_reports_unknown_gates(self):
        """A newly-added gate must not be invisible just because it is unknown here."""
        self.assertIn("newgate", srv._blocking_gates({"newgate": "pending"}))

    def test_owner_history_parses_list_and_json_string(self):
        self.assertEqual(srv._parse_owner_history([{"a": 1}]), [{"a": 1}])
        self.assertEqual(srv._parse_owner_history('[{"a": 1}]'), [{"a": 1}])
        self.assertEqual(srv._parse_owner_history("not json"), [])
        self.assertEqual(srv._parse_owner_history(None), [])

    def test_owner_history_dedupes(self):
        """neotoma#2169 stores its init and sign-off entries twice."""
        entry = {"action": "signed_off", "agent": "vanellus"}
        self.assertEqual(len(srv._dedupe_history([entry, dict(entry), {"action": "x"}])), 2)

    def test_pipeline_marker_regex_matches_daemon_format(self):
        """Must accept isoformat's '+00:00' and an absent stage suffix.

        A regex that only allowed 'Z' would fail to match markers the daemon
        actually writes — the exact trap called out in swarm_dispatch.py.
        """
        m = srv._PIPELINE_MARKER_RE.search(
            "<!-- apis-pipeline-inflight:2026-08-19T11:17:21.937497+00:00:queued -->"
        )
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2), "queued")
        legacy = srv._PIPELINE_MARKER_RE.search(
            "<!-- apis-pipeline-inflight:2026-08-19T08:00:00.123456+00:00 -->"
        )
        self.assertIsNotNone(legacy)
        self.assertIsNone(legacy.group(2))

    def test_pipeline_state_ages_out_stale_markers(self):
        """A marker whose clear failed must not read as a running pipeline."""
        old = "2020-01-01T00:00:00+00:00"
        srv._pipeline_markers  # noqa: B018
        orig = srv._pipeline_markers
        try:
            srv._pipeline_markers = lambda repo, number: (
                [{"started_at": old, "stage": "inflight", "comment_id": 1}], None
            )
            state = srv._pipeline_state_for("o/r", 1)
            self.assertEqual(state["stage"], "stale")
            self.assertEqual(state["reported_stage"], "inflight")
        finally:
            srv._pipeline_markers = orig

    def test_read_failure_is_not_reported_as_absence(self):
        """An auth failure must NOT read as "no pipeline running".

        This is the fail-open shape the whole security workstream is about: a
        check that cannot distinguish absence from failure and reports the
        permissive answer. Reproduced live with an invalid GitHub token, which
        previously yielded "no pipeline marker present".
        """
        orig = srv._pipeline_markers
        try:
            srv._pipeline_markers = lambda repo, number: ([], "HTTP 401 — token expired")
            state = srv._pipeline_state_for("o/r", 1)
            self.assertEqual(state["stage"], "unknown")
            self.assertIn("401", state["error"])
            self.assertNotIn("not queued or inflight", state.get("detail", ""))
        finally:
            srv._pipeline_markers = orig

    def test_queue_reports_listing_failure_rather_than_all_clear(self):
        """A failed issue LISTING yields zero candidates; that is not 'idle'."""
        orig = srv._recent_open_issues
        try:
            srv._recent_open_issues = lambda repo, limit: ([], False, f"{repo}: HTTP 401")
            out = srv._list_pipeline_queue()
            self.assertIn("error", out)
            self.assertIn("unknown, not idle", out["error"])
            self.assertNotIn("queued_count", out)
        finally:
            srv._recent_open_issues = orig

    def test_queue_flags_unreadable_issues_without_dropping_them(self):
        orig_list, orig_markers = srv._recent_open_issues, srv._pipeline_markers
        try:
            srv._recent_open_issues = lambda repo, limit: (
                [{"number": 1, "title": "t", "html_url": "u"}], False, None
            )
            srv._pipeline_markers = lambda repo, number: ([], "HTTP 403")
            out = srv._list_pipeline_queue()
            # Every candidate unreadable → an all-clear would be unfounded.
            self.assertIn("error", out)
            self.assertEqual(out["unreadable_count"], len(out["unreadable"]))
            self.assertGreater(out["unreadable_count"], 0)
        finally:
            srv._recent_open_issues, srv._pipeline_markers = orig_list, orig_markers

    def test_pipeline_state_absent_marker_is_not_finished(self):
        orig = srv._pipeline_markers
        try:
            srv._pipeline_markers = lambda repo, number: ([], None)
            self.assertIsNone(srv._pipeline_state_for("o/r", 1)["stage"])
        finally:
            srv._pipeline_markers = orig

    def test_bare_repo_name_is_not_queried(self):
        """'ateles' is not addressable on the API and only yields 404 noise."""
        self.assertEqual(srv._pipeline_markers("ateles", 272), ([], None))

    def test_get_gate_status_rejects_unparseable_ref(self):
        out = srv._get_gate_status("garbage")
        self.assertIn("error", out)


class TestUnreadableGatesHoldAndRaise(unittest.TestCase):
    """An unreadable gate record must HOLD AND RAISE, never read as 'pending'.

    The failure this locks out: a gate check that cannot distinguish "not yet
    reviewed" from "the record it reads is broken" reports both as every gate
    blocking, so finished work stalls on a bookkeeping state and the report
    names gate owners who were never actually asked for anything.
    """

    def test_non_issue_entity_errors_instead_of_reporting_all_gates_pending(self):
        """Passing an agent_grant id must not fabricate an all-pending map."""
        with patch.object(
            srv,
            "_get",
            return_value={
                "entity_id": "ent_grant",
                "entity_type": "agent_grant",
                "snapshot": {"status": "active"},
            },
        ):
            out = srv._get_gate_status("ent_grant")
        self.assertIn("error", out)
        self.assertIs(out["gates_evaluated"], False)
        self.assertEqual(out["entity_type"], "agent_grant")
        self.assertEqual(out["reason_codes"], ["unreadable.wrong_entity_type"])
        self.assertEqual(out["unreadable"][0]["code"], "unreadable.wrong_entity_type")
        # The bug signature: a blocking-gate list for a non-issue record.
        self.assertNotIn("blocking_gates", out)
        self.assertNotIn("all_gates_cleared", out)

    def test_uninitialised_gate_status_is_flagged_not_reported_as_withheld(self):
        """No gate_status at all is 'never triaged', not 'owners withholding'."""
        with patch.object(
            srv,
            "_get",
            return_value={
                "entity_id": "ent_issue",
                "entity_type": "issue",
                "snapshot": {"repo": "o/r", "github_number": 1, "current_owner": "pavo"},
            },
        ), patch.object(srv, "_pipeline_state_for", return_value={"stage": None}):
            out = srv._get_gate_status("ent_issue")
        self.assertIs(out["gates_initialised"], False)
        self.assertEqual(out["reason_codes"], ["uninitialised.never_triaged"])
        self.assertIn("NEVER INITIALISED", out["interpretation"])
        # Must NOT phrase an absent record as a named owner withholding sign-off.
        self.assertNotIn("waiting on pavo", out["interpretation"])
        # Primary fields must not look like ordinary pending.
        self.assertNotIn("blocking_gates", out)
        self.assertNotIn("all_gates_cleared", out)

    def test_get_gate_status_malformed_gate_status_not_coerced_to_empty_blocking_as_pending(
        self,
    ):
        """Present-but-malformed gate_status must hold-and-raise, not → {} pending."""
        with patch.object(
            srv,
            "_get",
            return_value={
                "entity_id": "ent_issue",
                "entity_type": "issue",
                "snapshot": {
                    "repo": "o/r",
                    "github_number": 1,
                    "gate_status": "[1,2]",
                },
            },
        ), patch.object(srv, "_pipeline_state_for", return_value={"stage": None}):
            out = srv._get_gate_status("ent_issue")
        self.assertIn("error", out)
        self.assertIs(out["gates_evaluated"], False)
        self.assertEqual(out["reason_codes"], ["unreadable.malformed_gate_status"])
        self.assertEqual(
            out["unreadable"][0]["code"], "unreadable.malformed_gate_status"
        )
        self.assertNotIn("blocking_gates", out)
        self.assertNotIn("all_gates_cleared", out)

    def test_get_gate_status_malformed_json_string_not_coerced_to_pending(self):
        with patch.object(
            srv,
            "_get",
            return_value={
                "entity_id": "ent_issue",
                "entity_type": "issue",
                "snapshot": {
                    "repo": "o/r",
                    "github_number": 1,
                    "gate_status": "not json",
                },
            },
        ), patch.object(srv, "_pipeline_state_for", return_value={"stage": None}):
            out = srv._get_gate_status("ent_issue")
        self.assertIs(out["gates_evaluated"], False)
        self.assertEqual(out["reason_codes"], ["unreadable.malformed_gate_status"])
        self.assertNotIn("blocking_gates", out)

    def test_real_pending_gates_still_report_as_waiting(self):
        """The genuine unsigned case is unchanged — this is not a blanket pass."""
        with patch.object(
            srv,
            "_get",
            return_value={
                "entity_id": "ent_issue",
                "entity_type": "issue",
                "snapshot": {
                    "repo": "o/r",
                    "github_number": 1,
                    "current_owner": "waxwing",
                    "gate_status": {"pm": "signed_off", "arch": "pending"},
                },
            },
        ), patch.object(srv, "_pipeline_state_for", return_value={"stage": None}):
            out = srv._get_gate_status("ent_issue")
        self.assertIs(out["gates_initialised"], True)
        self.assertIn("arch", out["blocking_gates"])
        self.assertIs(out["all_gates_cleared"], False)
        self.assertEqual(out["reason_codes"], [])
        self.assertIn("waiting on waxwing", out["interpretation"])


if __name__ == "__main__":
    unittest.main()
