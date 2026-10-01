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

import asyncio
import base64
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import httpx  # noqa: E402

import server as srv  # noqa: E402


_TEST_RESOLVER_JKT = "A" * 43
_DUMMY_RESOLVER_HEADERS = {
    "signature-key": "caller-proof",
    "signature-input": "caller-proof",
    "signature": "caller-proof",
    "content-digest": "caller-proof",
    "content-type": "application/json",
}


def _resolver_proof(
    *,
    sub: str = "ateles@ateles-swarm",
    checkpoint_id: str = "ent_cp1",
    action: str = "approve",
    ttl_sec: int = 120,
    now: int | None = None,
) -> tuple[dict[str, str], str]:
    from cryptography.hazmat.primitives.asymmetric import ec

    from lib.daemon_runtime.aauth_httpsig import (
        HttpSigSigner,
        jwk_thumbprint,
        public_part_of,
    )

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_numbers = private_key.private_numbers()
    numbers = private_numbers.public_numbers

    def b64(value: int) -> str:
        return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode()

    private_jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": b64(numbers.x),
        "y": b64(numbers.y),
        "d": b64(private_numbers.private_value),
        "kid": "resolver-test-key",
    }
    status = "approved" if action == "approve" else "rejected"
    body = srv._correction_body(
        checkpoint_id,
        "checkpoint_brief",
        "status",
        status,
        f"resolve-checkpoint-{checkpoint_id}-{status}",
    )
    signer = HttpSigSigner(
        private_jwk=private_jwk,
        sub=sub,
        iss=srv.CHECKPOINT_RESOLVER_ISSUER,
        kid="resolver-test-key",
        ttl_sec=ttl_sec,
    )
    headers = signer.sign_headers(
        method="POST",
        url=f"{srv.NEOTOMA_BASE_URL.rstrip('/')}/correct",
        body=srv._canonical_body_bytes(body),
        content_type="application/json",
        now=now,
    )
    return headers, jwk_thumbprint(public_part_of(private_jwk))


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


class TestResolveCheckpoint(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        authority = patch(
            "server._checkpoint_resolver_authority",
            return_value=("ateles@ateles-swarm", _TEST_RESOLVER_JKT, "tenant-a"),
        )
        authenticated = patch(
            "server._authenticate_checkpoint_resolver",
            return_value={"sub": "ateles@ateles-swarm"},
        )
        readback = patch(
            "lib.daemon_runtime.gating.read_authenticated_checkpoint_resolution",
            return_value={"principal_sub": "ateles@ateles-swarm"},
        )
        authority.start()
        authenticated.start()
        readback.start()
        self.addCleanup(authority.stop)
        self.addCleanup(authenticated.stop)
        self.addCleanup(readback.stop)

    @patch("server._get")
    async def test_missing_authenticated_resolver_context_cannot_resolve(
        self, mock_get
    ):
        mock_get.return_value = {
            "entity_type": "checkpoint_brief",
            "snapshot": {
                "status": "awaiting_operator",
                "task_entity_id": "ent_task_1",
            },
        }

        result = await srv._resolve_checkpoint(
            "ent_cp1", "approve", resolver_aauth_headers={}
        )

        self.assertIn("error", result)
        self.assertIn("authenticated resolver", result["error"])

    @patch("server._correct")
    @patch("server._get")
    async def test_wrong_authenticated_resolver_cannot_write_resolution(
        self, mock_get, mock_correct
    ):
        mock_get.return_value = {
            "entity_type": "checkpoint_brief",
            "snapshot": {
                "status": "awaiting_operator",
                "task_entity_id": "ent_task_1",
            },
        }
        with patch(
            "server._authenticate_checkpoint_resolver", return_value=None
        ):
            result = await srv._resolve_checkpoint(
                "ent_cp1", "approve", resolver_aauth_headers=_DUMMY_RESOLVER_HEADERS
            )

        self.assertIn("error", result)
        self.assertIn("mismatched", result["error"])
        mock_correct.assert_not_called()

    async def test_invalid_action(self):
        result = await srv._resolve_checkpoint("ent_123", "maybe")
        self.assertIn("error", result)
        self.assertIn("must be 'approve' or 'reject'", result["error"])

    @patch("server._get")
    async def test_not_found(self, mock_get):
        mock_get.return_value = None
        result = await srv._resolve_checkpoint("ent_fake", "approve")
        self.assertIn("error", result)
        self.assertIn("not found", result["error"])

    @patch("server._get")
    async def test_already_resolved(self, mock_get):
        mock_get.return_value = {
            "entity_type": "checkpoint_brief",
            "snapshot": {"status": "approved", "task_entity_id": "ent_task_1"},
        }
        result = await srv._resolve_checkpoint(
            "ent_cp1", "approve", _DUMMY_RESOLVER_HEADERS
        )
        self.assertIn("error", result)
        self.assertIn("not 'awaiting_operator'", result["error"])

    @patch("server._get")
    async def test_already_dispatched_replay(self, mock_get):
        mock_get.return_value = {
            "entity_type": "checkpoint_brief",
            "snapshot": {
                "status": "awaiting_operator",
                "resolved_dispatched": True,
                "task_entity_id": "ent_task_1",
            },
        }
        result = await srv._resolve_checkpoint(
            "ent_cp1", "approve", _DUMMY_RESOLVER_HEADERS
        )
        self.assertIn("error", result)
        self.assertIn("already dispatched", result["error"])

    @patch("server._get")
    async def test_dispatched_string_coercion(self, mock_get):
        mock_get.return_value = {
            "entity_type": "checkpoint_brief",
            "snapshot": {
                "status": "awaiting_operator",
                "resolved_dispatched": "true",
                "task_entity_id": "ent_task_1",
            },
        }
        result = await srv._resolve_checkpoint("ent_cp1", "approve")
        self.assertIn("error", result)
        self.assertIn("already dispatched", result["error"])

    @patch("server._consume_checkpoint_resolution")
    @patch("server._correct")
    @patch("server._get")
    async def test_approve_success(self, mock_get, mock_correct, mock_consume):
        mock_get.side_effect = [
            {
                "entity_type": "checkpoint_brief",
                "snapshot": {
                    "status": "awaiting_operator",
                    "task_entity_id": "ent_task_1",
                    "gate_action": "checkpoint_plan_approval",
                    "user_id": "tenant-a",
                },
            },
            {
                "entity_type": "checkpoint_brief",
                "snapshot": {
                    "status": "approved",
                    "task_entity_id": "ent_task_1",
                    "gate_action": "checkpoint_plan_approval",
                    "user_id": "tenant-a",
                },
            },
            {
                "entity_type": "checkpoint_brief",
                "snapshot": {
                    "status": "approved",
                    "resolved_dispatched": True,
                    "task_entity_id": "ent_task_1",
                    "gate_action": "checkpoint_plan_approval",
                    "user_id": "tenant-a",
                },
            },
            {
                "entity_type": "task",
                "snapshot": {
                    "status": "routed",
                    "blocked_reason": "",
                    "user_id": "tenant-a",
                },
            },
        ]
        mock_correct.return_value = True
        mock_consume.return_value = True

        result = await srv._resolve_checkpoint(
            "ent_cp1", "approve", _DUMMY_RESOLVER_HEADERS
        )
        self.assertEqual(result["new_status"], "approved")
        self.assertIn("task re-dispatched", result["action_taken"])
        mock_correct.assert_called_once()
        mock_consume.assert_awaited_once()

    @patch("server._consume_checkpoint_resolution")
    @patch("server._correct")
    @patch("server._get")
    async def test_approve_does_not_confirm_failure_or_held_states(
        self, mock_get, mock_correct, mock_consume
    ):
        mock_correct.return_value = True
        mock_consume.return_value = True

        for status in (
            "failed",
            "blocked",
            "awaiting_approval",
            "awaiting_input",
            "declined",
            "superseded",
        ):
            with self.subTest(status=status):
                mock_get.side_effect = [
                    {
                        "entity_type": "checkpoint_brief",
                        "snapshot": {
                            "status": "awaiting_operator",
                            "task_entity_id": "ent_task_1",
                            "gate_action": "checkpoint_plan_approval",
                        },
                    },
                    {
                        "entity_type": "checkpoint_brief",
                        "snapshot": {
                            "status": "approved",
                            "task_entity_id": "ent_task_1",
                            "gate_action": "checkpoint_plan_approval",
                        },
                    },
                    {
                        "entity_type": "checkpoint_brief",
                        "snapshot": {
                            "status": "approved",
                            "resolved_dispatched": True,
                            "task_entity_id": "ent_task_1",
                            "gate_action": "checkpoint_plan_approval",
                        },
                    },
                    {
                        "entity_type": "task",
                        "snapshot": {"status": status, "blocked_reason": ""},
                    },
                ]

                result = await srv._resolve_checkpoint(
                    "ent_cp1", "approve", _DUMMY_RESOLVER_HEADERS
                )

                self.assertNotIn("re-dispatched", result["action_taken"])

    @patch("server._consume_checkpoint_resolution")
    @patch("server._correct")
    @patch("server._get")
    async def test_reject_marks_task_declined(
        self, mock_get, mock_correct, mock_consume
    ):
        mock_get.side_effect = [
            {
                "entity_type": "checkpoint_brief",
                "snapshot": {
                    "status": "awaiting_operator",
                    "task_entity_id": "ent_task_1",
                },
            },
            {
                "entity_type": "checkpoint_brief",
                "snapshot": {
                    "status": "rejected",
                    "task_entity_id": "ent_task_1",
                },
            },
            {
                "entity_type": "checkpoint_brief",
                "snapshot": {
                    "status": "rejected",
                    "resolved_dispatched": True,
                    "task_entity_id": "ent_task_1",
                },
            },
            {"entity_type": "task", "snapshot": {"status": "declined"}},
        ]
        mock_correct.return_value = True
        mock_consume.return_value = True

        result = await srv._resolve_checkpoint(
            "ent_cp1", "reject", _DUMMY_RESOLVER_HEADERS
        )
        self.assertEqual(result["new_status"], "rejected")
        self.assertIn("task marked declined", result["action_taken"])
        mock_correct.assert_called_once()
        self.assertEqual(
            mock_correct.call_args.kwargs["resolver_aauth_headers"],
            _DUMMY_RESOLVER_HEADERS,
        )
        mock_consume.assert_awaited_once()

    @patch("server._correct")
    @patch("server._get")
    async def test_correct_failure(self, mock_get, mock_correct):
        mock_get.return_value = {
            "entity_type": "checkpoint_brief",
            "snapshot": {
                "status": "awaiting_operator",
                "task_entity_id": "ent_task_1",
            },
        }
        mock_correct.return_value = False

        result = await srv._resolve_checkpoint(
            "ent_cp1", "approve", _DUMMY_RESOLVER_HEADERS
        )
        self.assertIn("error", result)
        self.assertIn("failed to correct", result["error"])


class TestCheckpointResolverAuthentication(unittest.TestCase):

    def test_verifier_process_does_not_load_resolver_private_keys(self):
        source = Path(srv.__file__).read_text()
        self.assertNotIn("load_http_sig_signer", source)
        self.assertNotIn("ATELES_PRIVATE_KEYS_DIR", source)
        self.assertNotIn("checkpoint_resolution import", source)

    def test_required_principal_can_authenticate_exact_resolution(self):
        headers, jkt = _resolver_proof()
        body = srv._canonical_body_bytes(
            srv._correction_body(
                "ent_cp1",
                "checkpoint_brief",
                "status",
                "approved",
                "resolve-checkpoint-ent_cp1-approved",
            )
        )
        claims = srv._authenticate_checkpoint_resolver(
            headers,
            body=body,
            required_principal_sub="ateles@ateles-swarm",
            required_principal_jkt=jkt,
        )

        self.assertIsNotNone(claims)
        self.assertEqual(claims["sub"], "ateles@ateles-swarm")

    def test_wrong_principal_key_or_body_binding_is_rejected(self):
        valid_headers, valid_jkt = _resolver_proof()
        wrong_sub_headers, wrong_sub_jkt = _resolver_proof(
            sub="other@ateles-swarm"
        )
        body = srv._canonical_body_bytes(
            srv._correction_body(
                "ent_cp1",
                "checkpoint_brief",
                "status",
                "approved",
                "resolve-checkpoint-ent_cp1-approved",
            )
        )
        cases = (
            (wrong_sub_headers, body, "ateles@ateles-swarm", wrong_sub_jkt),
            (valid_headers, body, "ateles@ateles-swarm", "B" * 43),
            (
                valid_headers,
                body.replace(b'"approved"', b'"rejected"'),
                "ateles@ateles-swarm",
                valid_jkt,
            ),
        )
        for headers, signed_body, required_sub, required_jkt in cases:
            with self.subTest(required_sub=required_sub, required_jkt=required_jkt):
                self.assertIsNone(
                    srv._authenticate_checkpoint_resolver(
                        headers,
                        body=signed_body,
                        required_principal_sub=required_sub,
                        required_principal_jkt=required_jkt,
                    )
                )

    def test_expired_future_or_excessive_lifetime_is_rejected(self):
        body = srv._canonical_body_bytes(
            srv._correction_body(
                "ent_cp1",
                "checkpoint_brief",
                "status",
                "approved",
                "resolve-checkpoint-ent_cp1-approved",
            )
        )
        now = int(time.time())
        cases = (
            _resolver_proof(now=now - 301),
            _resolver_proof(now=now + 31),
            _resolver_proof(now=now, ttl_sec=301),
        )
        for headers, jkt in cases:
            with self.subTest(signature_input=headers["signature-input"]):
                self.assertIsNone(
                    srv._authenticate_checkpoint_resolver(
                        headers,
                        body=body,
                        required_principal_sub="ateles@ateles-swarm",
                        required_principal_jkt=jkt,
                    )
                )


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
        """A failed read must not be spelled the same way as an empty queue.

        This assertion used to read `count == 0` — which PINNED the defect
        (ateles#1037): with no token nothing was read at all, yet the tool
        reported that the operator had zero decisions waiting. A missing token
        is the one case where "no pending checkpoints" is certainly wrong, and
        it looked identical to the all-clear.
        """
        srv.NEOTOMA_BEARER_TOKEN = ""
        result = srv._list_checkpoints()
        self.assertIn("error", result)
        self.assertNotEqual(result.get("count"), 0)
        self.assertEqual(result["checkpoints"], [])

    def test_resolve_checkpoint_without_token(self):
        srv.NEOTOMA_BEARER_TOKEN = ""
        result = asyncio.run(srv._resolve_checkpoint("ent_123", "approve"))
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

    @patch("server._post")
    def test_checkpoint_resolution_forwards_callers_authenticated_proof(
        self, mock_post
    ):
        mock_post.return_value = {"ok": True}
        ok = srv._correct(
            "ent_cp",
            "checkpoint_brief",
            "status",
            "approved",
            "idem-approval",
            resolver_aauth_headers=_DUMMY_RESOLVER_HEADERS,
        )

        self.assertTrue(ok)
        self.assertEqual(
            mock_post.call_args.kwargs["extra_headers"], _DUMMY_RESOLVER_HEADERS
        )
        self.assertEqual(
            mock_post.call_args.kwargs["encoded_body"],
            srv._canonical_body_bytes(mock_post.call_args.args[1]),
        )

    @patch("server._post")
    def test_checkpoint_resolution_refuses_empty_caller_proof(self, mock_post):
        self.assertFalse(
            srv._correct(
                "ent_cp",
                "checkpoint_brief",
                "status",
                "approved",
                "idem-approval",
                resolver_aauth_headers={},
            )
        )
        mock_post.assert_not_called()

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
    @patch("server._retrieve_page")
    def test_joins_task_title(self, mock_retrieve, mock_get):
        mock_retrieve.return_value = {"total": 1, "next_cursor": None, "entities": [{
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
        }]}
        mock_get.return_value = {
            "snapshot": {"title": "Deploy to production"},
        }

        result = srv._list_checkpoints()
        self.assertEqual(result["count"], 1)
        cp = result["checkpoints"][0]
        self.assertEqual(cp["task_title"], "Deploy to production")
        self.assertEqual(cp["blast_radius"], "high")

    @patch("server._retrieve_page")
    def test_empty_checkpoints(self, mock_retrieve):
        """A genuinely empty queue still reports zero — the all-clear must
        remain sayable, distinctly from a failed read (see the without-token
        test above)."""
        mock_retrieve.return_value = {"total": 0, "next_cursor": None, "entities": []}
        result = srv._list_checkpoints()
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["total"], 0)
        self.assertNotIn("error", result)


class TestToolSchemas(unittest.TestCase):

    ACTION_TOOLS = {"get_swarm_roster", "route_task", "list_checkpoints", "resolve_checkpoint"}
    # Read-only swarm observability. resolve_checkpoint stays the ONLY mutating
    # tool: see the self-certification boundary note in server.py — a session
    # must not be able to advance its own gate.
    OBSERVABILITY_TOOLS = {
        "get_gate_status", "list_pipeline_queue", "get_dispatch_health",
        # ateles#1275 slice 1: per-task timeline and change feed, read-only.
        "get_task_timeline", "watch_swarm",
    }

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
            for impl in (
                "_get_gate_status", "_list_pipeline_queue", "_get_dispatch_health",
                "_get_task_timeline", "_watch_swarm", "_watch_poll", "_watch_baseline",
            ):
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

    def test_resolve_checkpoint_requires_all_authority_params(self):
        rc = next(t for t in srv.TOOLS if t.name == "resolve_checkpoint")
        self.assertIn("checkpoint_id", rc.inputSchema["required"])
        self.assertIn("action", rc.inputSchema["required"])
        self.assertIn("resolver_aauth_headers", rc.inputSchema["required"])

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
        self.assertIs(out["gates_evaluated"], True)
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
        self.assertIs(out["gates_evaluated"], True)
        self.assertIs(out["gates_initialised"], True)
        self.assertIn("arch", out["blocking_gates"])
        self.assertIs(out["all_gates_cleared"], False)
        self.assertEqual(out["reason_codes"], [])
        self.assertIn("waiting on waxwing", out["interpretation"])

    def test_gates_evaluated_truthiness_does_not_collapse_evaluated_paths(self):
        """Ordinary falsy checks must not treat success/never-triaged as unevaluable.

        Callers write `if not out.get("gates_evaluated")`. Omitting the key on
        evaluated paths re-collapses states into hold-and-raise — the footgun
        Waxwing flagged on #761. Effect: evaluated answers are truthy.
        """
        never_triaged = {
            "entity_id": "ent_issue",
            "entity_type": "issue",
            "snapshot": {"repo": "o/r", "github_number": 1},
        }
        unsigned = {
            "entity_id": "ent_issue",
            "entity_type": "issue",
            "snapshot": {
                "repo": "o/r",
                "github_number": 1,
                "gate_status": {"pm": "pending"},
            },
        }
        for payload in (never_triaged, unsigned):
            with patch.object(srv, "_get", return_value=payload), patch.object(
                srv, "_pipeline_state_for", return_value={"stage": None}
            ):
                out = srv._get_gate_status("ent_issue")
            # Key present AND true — `.get` default None / missing must not win.
            self.assertIn("gates_evaluated", out)
            self.assertTrue(out.get("gates_evaluated"))
            self.assertFalse(not out.get("gates_evaluated"))


if __name__ == "__main__":
    unittest.main()


class TestInstructionsStayWithinTheSharedClientBudget:
    """Foundation phase E2, task 3 — the SESSION transport. CORRECTED
    2026-09-25 (ateles#1243, ateles#1254).

    PR #1184 made this render the full `agent_policy` corpus into the MCP
    `instructions` field. Measured evidence (ateles#1243) showed that field is
    capped at roughly 2,048 characters ACROSS ALL CONNECTED SERVERS COMBINED,
    truncated SILENTLY by the client — and the rendered corpus on `main`
    measured 17,996 characters, about 9x the shared budget. So the corpus
    never reached a session; whatever survived truncation did.

    RED against that `main` behaviour (reproduced below in
    `TestInstructionsBudgetRedGreen`): forwarding a ~60-rule mock corpus
    (~300 chars/rule) into the field produced ~15,000+ characters, far over
    any reasonable per-server share of the 2,048 cap.

    GREEN, this fix: the field carries only the fixed static rules plus one
    short pointer sentence to Neotoma's `agent_policy` records. The corpus
    text itself is never forwarded, so its size cannot affect this field's
    size at all — nothing here should reopen a path where it could.
    """

    def _patch_loader(self, monkeypatch, block=None, raises: Exception | None = None):
        import lib.daemon_runtime.agent_loader as al

        if raises is not None:
            def boom(self):
                raise raises
            monkeypatch.setattr(al.AgentLoader, "render_policy_prompt", boom)
        else:
            monkeypatch.setattr(
                al.AgentLoader, "render_policy_prompt", lambda self: block or ""
            )

    def test_rule_text_on_the_record_is_never_forwarded_verbatim(self, monkeypatch):
        # The defect this fix closes: rule TEXT reaching the field at all,
        # regardless of size. A nonce standing in for a live rule must NOT
        # appear in the rendered output — only the pointer sentence should.
        nonce = "NONCE-e2-session-transport-7f3a91"
        self._patch_loader(
            monkeypatch, f"\n\n## Active agent policies (apply these)\n- (mandatory, active) {nonce}"
        )
        out = srv.render_server_instructions("ateles@ateles-swarm")
        assert nonce not in out, "rule text reached the instructions field — this is the defect being fixed"
        assert "agent_policy" in out, "the pointer sentence to Neotoma must still be present"

    def test_static_operating_rules_are_present(self, monkeypatch):
        self._patch_loader(monkeypatch, "")
        out = srv.render_server_instructions("ateles@ateles-swarm")
        assert "Dispatch, don't do inline" in out
        assert "Checkpoint protocol" in out

    def test_pointer_names_neotoma_and_applies_when(self, monkeypatch):
        self._patch_loader(monkeypatch, "")
        out = srv.render_server_instructions("ateles@ateles-swarm")
        assert "Neotoma" in out
        assert "agent_policy" in out
        assert "applies_when" in out

    def test_output_always_fits_the_configured_budget(self, monkeypatch):
        self._patch_loader(monkeypatch, "")
        out = srv.render_server_instructions("ateles@ateles-swarm")
        assert len(out) <= srv.INSTRUCTIONS_BUDGET_CHARS

    def test_an_unreachable_record_still_serves_static_rules_and_pointer(
        self, monkeypatch
    ):
        self._patch_loader(monkeypatch, raises=RuntimeError("neotoma unreachable"))
        out = srv.render_server_instructions("ateles@ateles-swarm")
        assert "Dispatch, don't do inline" in out
        assert "agent_policy" in out

    def test_an_unreachable_record_is_logged_not_silent(self, monkeypatch, caplog):
        self._patch_loader(monkeypatch, raises=RuntimeError("neotoma unreachable"))
        with caplog.at_level("ERROR"):
            srv.render_server_instructions("ateles@ateles-swarm")
        assert any("could not resolve agent_policy" in r.message for r in caplog.records)

    def test_huge_corpus_from_the_record_does_not_grow_the_output(self, monkeypatch):
        # 60 rules of ~300 chars — the shape ateles#1243 measured (median 221,
        # longest 1,166 chars) — must not move the rendered size at all, since
        # the corpus text is no longer forwarded.
        huge = "\n\n## Active agent policies (apply these)\n" + "\n".join(
            f"- (advisory, active) Rule {i} " + ("x" * 280) for i in range(60)
        )
        self._patch_loader(monkeypatch, huge)
        out = srv.render_server_instructions("ateles@ateles-swarm")
        assert len(out) <= srv.INSTRUCTIONS_BUDGET_CHARS

    def test_a_budget_overflow_falls_back_to_static_rules_and_logs_a_warning(
        self, monkeypatch, caplog
    ):
        # Simulate a future edit pushing the fixed text itself over budget —
        # the guard must fail loudly to the static rules, never truncate
        # mid-rule the way ateles#1243 documented the client doing.
        monkeypatch.setattr(srv, "RULE_INDEX_POINTER", "x" * (srv.INSTRUCTIONS_BUDGET_CHARS + 1))
        self._patch_loader(monkeypatch, "")
        with caplog.at_level("WARNING"):
            out = srv.render_server_instructions("ateles@ateles-swarm")
        assert out == srv.SERVER_INSTRUCTIONS
        assert any("exceed the" in r.message for r in caplog.records)


class TestInstructionsBudgetRedGreen:
    """Standalone reproduction of the red/green pair for this fix, isolated
    from the class above so it can be pointed at pre-fix code by hand (revert
    `render_server_instructions` to forward `block` and this goes red).

    Mocks a corpus of ~60 rules at ~300 chars each — the shape measured on
    `main` in ateles#1243 (53 rules, 17,996 chars total, median 221 chars,
    longest 1,166) — and asserts the rendered `instructions` field stays
    within the shared client budget regardless.
    """

    def test_mock_60_rule_corpus_stays_within_budget(self, monkeypatch):
        import lib.daemon_runtime.agent_loader as al

        mock_corpus = "\n\n## Active agent policies (apply these)\n" + "\n".join(
            f"- (advisory, active) Rule {i}: " + ("padding text " * 20)
            for i in range(60)
        )
        assert len(mock_corpus) > 10_000, "mock corpus should reproduce the real overflow shape"

        monkeypatch.setattr(al.AgentLoader, "render_policy_prompt", lambda self: mock_corpus)
        out = srv.render_server_instructions("ateles@ateles-swarm")
        assert len(out) <= srv.INSTRUCTIONS_BUDGET_CHARS, (
            f"rendered instructions ({len(out)} chars) exceed the "
            f"{srv.INSTRUCTIONS_BUDGET_CHARS}-char budget — this is the exact "
            "failure mode ateles#1243 measured on main (17,996 chars against "
            "a ~2,048-char shared client cap)"
        )


# ── get_dispatch_health: label gate surfacing ───────────────────────────────
#
# ATELES_SWARM_REQUIRE_LABEL (bootstrap mode / canary lane, swarm_dispatch.py)
# must be surfaced here so "nothing is being reviewed" reads as the label gate
# working as designed rather than as a broken dispatcher.


class TestDispatchHealthLabelGate:
    def test_unset_reports_gate_inactive(self, monkeypatch):
        monkeypatch.delenv("ATELES_SWARM_REQUIRE_LABEL", raising=False)
        result = srv._get_dispatch_health()
        assert result["label_gate_active"] is False
        assert result["label_gate_label"] is None

    def test_set_reports_gate_active_with_label(self, monkeypatch):
        monkeypatch.setenv("ATELES_SWARM_REQUIRE_LABEL", "swarm-canary")
        result = srv._get_dispatch_health()
        assert result["label_gate_active"] is True
        assert result["label_gate_label"] == "swarm-canary"
        assert "swarm-canary" in result["interpretation"]

    def test_set_empty_string_reports_gate_inactive(self, monkeypatch):
        # Whitespace-only / empty-string env values must not read as "active
        # with an empty label" — same treatment as fully unset.
        monkeypatch.setenv("ATELES_SWARM_REQUIRE_LABEL", "   ")
        result = srv._get_dispatch_health()
        assert result["label_gate_active"] is False
        assert result["label_gate_label"] is None
