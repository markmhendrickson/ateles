"""Tests for plan-binding detection in `_session_integrity.py` (ateles#1130).

`DEFAULT_PLAN_ID` was hardcoded to the swarm architecture plan until
2026-09-21, binding every session to that one workstream regardless of what
it was actually doing. The fix sets `DEFAULT_PLAN_ID = None` and keeps
`_LEGACY_SWARM_PLAN_ID` as a read-only pattern the Stop hook still recognizes,
so a transcript that already bound the old id still reads as bound rather
than retroactively flagged as a violation.

These cases were verified by hand in the PR body ("What red looked like")
but never landed as committed assertions — this file promotes them at both
levels: the `_mentions_plan` helper in isolation, AND `scan_transcript`, the
caller that actually decides whether a session counts as plan-bound. A test
of the helper alone cannot catch a regression in the wiring between them
(e.g. the `bound_plan` gate on `_session_integrity.py:167` moving inside a
narrower conditional) — the review that flagged this PR's first draft named
that gap explicitly, so both levels are covered here.

The suite also includes a genuine revert of the actual regression this PR
fixes — `DEFAULT_PLAN_ID` hardcoded back to the swarm plan id — via
monkeypatching the imported module's attribute, not a hand-reimplemented
copy of its logic. Without that, the suite could pass while no longer
watching the thing it claims to watch (`docs/foundation/principles.md`
invariant 4).
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _session_integrity as si  # noqa: E402

LEGACY_SWARM_PLAN_ID = "ent_99ace4dd6673aa36ed08b1fe"


def store_event(entity_type: str | None = None, plan_id: str | None = None, extra: dict | None = None) -> dict:
    """Build a transcript event shaped like a real Neotoma `store` tool_use,
    the shape `_inspect_event`/`scan_transcript` actually walk — not a bare
    dict handed straight to `_mentions_plan`."""
    payload: dict = {}
    if entity_type is not None:
        payload["entity_type"] = entity_type
    if plan_id is not None:
        payload["plan_id"] = plan_id
    if extra:
        payload.update(extra)
    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "name": "store", "input": payload}],
    }


class TestDefaultPlanRemoved(unittest.TestCase):
    def test_default_plan_id_is_none(self) -> None:
        self.assertIsNone(si.DEFAULT_PLAN_ID)


class TestMentionsPlan(unittest.TestCase):
    """The pure helper, in isolation."""

    def test_legacy_swarm_plan_id_is_detected(self) -> None:
        self.assertTrue(si._mentions_plan({"x": f"{LEGACY_SWARM_PLAN_ID} bound"}))

    def test_entity_type_plan_is_detected(self) -> None:
        self.assertTrue(si._mentions_plan({"entity_type": "plan"}))

    def test_plan_id_field_is_detected(self) -> None:
        """A separate branch of the `or` chain from `"plan"`/legacy-id — a
        payload that names `plan_id` but never spells out the literal
        `"plan"` substring or the legacy id."""
        self.assertTrue(si._mentions_plan({"plan_id": "ent_81aadb43caf2fa493361e8ed"}))

    def test_unrelated_entity_type_is_not_detected(self) -> None:
        self.assertFalse(si._mentions_plan({"entity_type": "contact"}))


class TestScanTranscriptBindsPlan(unittest.TestCase):
    """The caller `_inspect_event`/`scan_transcript` actually consult to set
    `summary["bound_plan"]` — where the real regression risk lives, since
    `_mentions_plan` alone can stay correct while the gate around its call
    (`_session_integrity.py:167`) silently narrows or is bypassed."""

    def _scan(self, events: list[dict]) -> dict:
        summary = {
            "turns": 0, "wrote_domain": False, "bound_plan": False,
            "bound_task": False, "captured_learning": False, "write_types": set(),
        }
        for ev in events:
            si._inspect_event(ev, summary)
        return summary

    def test_legacy_plan_id_in_a_real_store_event_binds_plan(self) -> None:
        summary = self._scan([store_event(entity_type="task", extra={"note": LEGACY_SWARM_PLAN_ID})])
        self.assertTrue(summary["bound_plan"])

    def test_plan_entity_type_in_a_real_store_event_binds_plan(self) -> None:
        summary = self._scan([store_event(entity_type="plan")])
        self.assertTrue(summary["bound_plan"])

    def test_domain_write_with_no_plan_mention_does_not_bind_plan(self) -> None:
        """The mirror case: a plausible non-bookkeeping write that never
        touches a plan must NOT flip `bound_plan` — proving detection isn't
        defaulting true, which was the actual shape of the original bug."""
        summary = self._scan([store_event(entity_type="contact")])
        self.assertFalse(summary["bound_plan"])
        self.assertTrue(summary["wrote_domain"])

    def test_read_only_tool_use_is_never_inspected_for_plan_binding(self) -> None:
        """`_inspect_event` only looks at writes (`store`/`correct`/
        `create_relationship`/`submit_`). A read tool naming the legacy plan
        id must not itself bind the session — binding comes from a write."""
        ev = {
            "role": "assistant",
            "content": [{
                "type": "tool_use", "name": "retrieve_entities",
                "input": {"entity_type": "plan", "search": LEGACY_SWARM_PLAN_ID},
            }],
        }
        summary = self._scan([ev])
        self.assertFalse(summary["bound_plan"])


class TestRegressionRevert(unittest.TestCase):
    """Reverts the ACTUAL fix — `DEFAULT_PLAN_ID` hardcoded back to the swarm
    plan id — via monkeypatch on the imported module, not a hand-copied
    reimplementation of its logic. Confirms `test_default_plan_id_is_none`
    would itself go red under that revert, by running it as a real
    `unittest` case against the mutated module state and checking its
    result, rather than asserting on `assertIsNone` directly (which would be
    tautological — of course a mismatched value fails an equality check)."""

    def test_default_plan_id_is_none_test_goes_red_under_revert(self) -> None:
        with mock.patch.object(si, "DEFAULT_PLAN_ID", LEGACY_SWARM_PLAN_ID):
            result = unittest.TestResult()
            TestDefaultPlanRemoved("test_default_plan_id_is_none").run(result)
            self.assertEqual(len(result.failures), 1, "reverted case must fail, not error or pass")
            self.assertTrue(result.wasSuccessful() is False)


class TestLegacyBranchIsLoadBearing(unittest.TestCase):
    """Revert-style mutation check on `_mentions_plan` itself: monkeypatches
    the module's live function with a version that has the legacy-id branch
    removed, and confirms detection actually goes red — not a hand-copied
    reimplementation of the removed branch, but the module's own code with
    one line deleted via source transformation."""

    def test_removing_legacy_branch_from_the_real_function_goes_red(self) -> None:
        mutated_source = self._source_without_legacy_branch()
        namespace: dict = {"json": json}
        exec(mutated_source, namespace)  # noqa: S102 — controlled, test-only mutation of a copy
        mutated_mentions_plan = namespace["_mentions_plan"]

        payload = {"x": f"{LEGACY_SWARM_PLAN_ID} bound"}
        self.assertTrue(si._mentions_plan(payload), "sanity: the real function must detect it")
        self.assertFalse(
            mutated_mentions_plan(payload),
            "the mutated function (legacy branch removed) must NOT detect it, "
            "or this check cannot prove the branch is load-bearing",
        )

    @staticmethod
    def _source_without_legacy_branch() -> str:
        import inspect

        src = inspect.getsource(si._mentions_plan)
        mutated = src.replace('or _LEGACY_SWARM_PLAN_ID in blob', '')
        assert mutated != src, "source pattern not found — _mentions_plan body changed shape"
        return mutated


if __name__ == "__main__":
    unittest.main()
