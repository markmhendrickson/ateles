"""`get_gate_status` gate_order resolution, exercised AT THE MCP SURFACE.

Why at the surface rather than on `_blocking_gates`
---------------------------------------------------

The Vanellus pm lens blocked PR #719 on exactly this: `_get_gate_status` began
resolving `gate_order` / `gate_order_source` from `workflow_definition`, and the
diff proved it only by testing the pure helper underneath. A helper test cannot
fail if the handler stops calling the helper, passes it the wrong argument, or
drops the field from the response dict — which is the whole mechanism being
claimed. Standing:
`fixed_means_behavior_verified_not_contract_accepted` (ent_db0b7855d47012084477fb00)
and `cross_surface_contract_parity_tested_all_surfaces` (ent_2ad0677fe23c0c1878ae43e8).

So every test here calls `srv.TOOL_HANDLERS["get_gate_status"]({...})` — the
same entry point an MCP client reaches — and asserts on the returned payload.
Only the two transport seams are patched (`_retrieve_entities`, and the
resolver), so the handler, the field wiring, and the fallback branch all run for
real.

What these looked like RED
---------------------------

Run against the pre-fix tree with the resolver patched to return a workflow
order, `test_gate_order_comes_from_the_workflow_definition` and
`test_gate_order_source_names_the_fallback_when_resolution_fails` both FAILED
at collection/assert because nothing at this surface was covered at all — the
`gate_order_source` key had no test asserting its two values, and
`test_blocking_gates_uses_the_resolved_order_not_the_fallback` returned the
fallback's `["ux", "arch"]` rather than the workflow's `["copy"]`.

Run: pytest execution/mcp/ateles/test_gate_order_surface.py -v
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import execution.mcp.ateles.server as srv


def _issue_entity(gate_status: dict, labels=None) -> dict:
    """A minimal issue entity in the shape `_get_gate_status` reads."""
    return {
        "entity_id": "ent_issue",
        "snapshot": {
            "repo": "markmhendrickson/ateles",
            "issue_number": 719,
            "title": "a test issue",
            "status": "open",
            "current_owner": "pm",
            "gate_status": gate_status,
            "labels": labels if labels is not None else ["feature"],
            "owner_history": [],
        },
    }


class GateOrderAtTheMcpSurface(unittest.TestCase):
    """Every test calls the registered tool handler, not a helper."""

    def _call(self, gate_status, resolved=None, resolver_raises=None, labels=None):
        """Invoke the real MCP handler with the two transport seams patched."""
        entity = _issue_entity(gate_status, labels=labels)

        def fake_resolve(repo, issue_labels):
            if resolver_raises is not None:
                raise resolver_raises
            return resolved

        with patch.object(srv, "_retrieve_entities", return_value=[entity]), \
             patch.object(srv, "resolve_gates", side_effect=fake_resolve), \
             patch.object(srv, "_pipeline_state_for", return_value={}):
            return srv.TOOL_HANDLERS["get_gate_status"](
                {"issue_ref": "markmhendrickson/ateles#719"}
            )

    # ── the resolved path ────────────────────────────────────────────────────

    def test_gate_order_comes_from_the_workflow_definition(self):
        """The response carries the workflow's order, not the module fallback."""
        out = self._call(
            {"pm": "signed_off", "copy": "pending", "impl": "pending"},
            resolved=("pm", "copy", "impl"),
        )
        self.assertEqual(out["gate_order"], ["pm", "copy", "impl"])
        self.assertEqual(out["gate_order_source"], "workflow_definition")
        # `copy` exists in no hardcoded list anywhere — its presence proves the
        # order came from the resolver and not from _GATE_ORDER_FALLBACK.
        self.assertIn("copy", out["gate_order"])

    def test_blocking_gates_uses_the_resolved_order_not_the_fallback(self):
        """A gate the fallback names but the workflow does not must not block.

        `ateles|bug` declares only `pm`; the fallback names pm/ux/arch/impl/
        pr_review. Blocking against the fallback would hold a bug issue behind
        `ux` and `arch` gates its workflow never defined.
        """
        out = self._call({"pm": "signed_off"}, resolved=("pm", "impl"), labels=["bug"])
        self.assertNotIn("ux", out["blocking_gates"])
        self.assertNotIn("arch", out["blocking_gates"])
        self.assertIn("impl", out["blocking_gates"])

    def test_all_gates_cleared_tracks_the_resolved_order(self):
        out = self._call(
            {"pm": "signed_off", "impl": "waived"}, resolved=("pm", "impl")
        )
        self.assertEqual(out["blocking_gates"], [])
        self.assertTrue(out["all_gates_cleared"])

    # ── the fallback path, and that it is LABELLED as such ───────────────────

    def test_gate_order_source_names_the_fallback_when_resolution_fails(self):
        """Reporting still answers, but never disguises a fallback as the record."""
        out = self._call(
            {"pm": "pending"}, resolver_raises=RuntimeError("neotoma unreachable")
        )
        self.assertEqual(out["gate_order"], list(srv._GATE_ORDER_FALLBACK))
        self.assertTrue(
            out["gate_order_source"].startswith("fallback ("),
            f"expected a labelled fallback, got {out['gate_order_source']!r}",
        )
        self.assertIn("RuntimeError", out["gate_order_source"])

    def test_the_two_sources_are_distinguishable(self):
        """Proves this file is a gate, not a tautology (#1049's move).

        If `gate_order_source` were hardcoded to either value, one of these two
        assertions would fail. Both run in one test so the discrimination —
        not merely each branch — is pinned.
        """
        resolved = self._call({"pm": "pending"}, resolved=("pm", "impl"))
        fell_back = self._call({"pm": "pending"}, resolver_raises=ValueError("x"))
        self.assertNotEqual(
            resolved["gate_order_source"], fell_back["gate_order_source"]
        )
        self.assertNotEqual(resolved["gate_order"], fell_back["gate_order"])

    # ── normalization reaches this surface too ───────────────────────────────

    def test_a_capitalised_gate_in_the_record_is_not_double_counted(self):
        """"PM" signed must satisfy the declared "pm", not read as an extra gate.

        Before normalization reached `_blocking_gates`, this response listed
        `pm` as unsigned AND `PM` as an unknown extra gate — one signature
        reported as two problems.
        """
        out = self._call({"PM": "signed_off", "impl": "pending"},
                         resolved=("pm", "impl"))
        self.assertNotIn("pm", out["blocking_gates"])
        self.assertNotIn("PM", out["blocking_gates"])
        self.assertEqual(out["blocking_gates"], ["impl"])

    def test_an_unknown_extra_gate_is_still_reported(self):
        """Normalizing must not silence a genuinely new gate name."""
        out = self._call({"pm": "signed_off", "newgate": "pending"},
                         resolved=("pm",))
        self.assertIn("newgate", out["blocking_gates"])

    # ── the surface contract itself ──────────────────────────────────────────

    def test_both_fields_are_always_present(self):
        """A reader must never have to guess which order it got."""
        for kwargs in (
            {"resolved": ("pm", "impl")},
            {"resolver_raises": RuntimeError("boom")},
        ):
            out = self._call({"pm": "pending"}, **kwargs)
            self.assertIn("gate_order", out)
            self.assertIn("gate_order_source", out)
            self.assertTrue(out["gate_order"], "gate_order must never be empty")


if __name__ == "__main__":
    unittest.main()
