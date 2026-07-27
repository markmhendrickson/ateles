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

import server as srv


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

        result = srv._route_task("read some data", "read_entity")
        self.assertEqual(result["action_blast_radius"], "low")

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

    def test_four_tools_defined(self):
        self.assertEqual(len(srv.TOOLS), 4)

    def test_tool_names(self):
        names = {t.name for t in srv.TOOLS}
        self.assertEqual(names, {"get_swarm_roster", "route_task", "list_checkpoints", "resolve_checkpoint"})

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


if __name__ == "__main__":
    unittest.main()
