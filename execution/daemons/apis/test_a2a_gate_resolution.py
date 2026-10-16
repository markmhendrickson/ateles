"""What an A2A-delegated task carries into the execution gate (ateles#682).

An externally delegated request is the one case where the swarm genuinely knows
nothing about the work, written by someone outside it. Two things are pinned:

  * The task declares no `action_type` and no `confidence` (characterization of
    the payload today), so the gate scores it like any other unscored task.
  * Its prose may TIGHTEN its own gate but never loosen it: the entity carries
    `source: "a2a"`, and a report-only statement in the request does not lower
    the Cicada prior the way it would for an operator-filed task.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_DAEMON_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import a2a_executor as ax  # noqa: E402
from lib.daemon_runtime.action_type import (  # noqa: E402
    infer_action_type_from_text,
    resolve_gate_action_type,
)

# Placeholder, written so secret scanners do not flag it.
FAKE_BEARER = "fake.not-a-real-token.for-tests-only"


def _submit(message):
    captured = {}

    def transport(url, data, headers):
        captured["entity"] = json.loads(data)["entities"][0]
        return {"entities": [{"entity_id": "ent_a2a_1"}]}

    def store(title, body, tags, *, caller="", idempotency_key=None):
        return ax.create_neotoma_task(
            title, body, tags, caller=caller, idempotency_key=idempotency_key,
            bearer_token=FAKE_BEARER, transport=transport,
        )

    result = ax.ApisTaskBridge(store_fn=store).submit(message, caller="peer@example.com")
    assert result.ok
    return result, captured["entity"]


def _resolved(result, entity):
    return resolve_gate_action_type(
        result.skill,
        {
            "title": entity["title"],
            "description": entity["description"],
            "source": entity.get("source"),
        },
    )


def test_a2a_task_declares_neither_action_type_nor_confidence_and_is_marked_external():
    _, entity = _submit("Fix the deploy pipeline crash\nThe CI build fails.")
    assert entity["source"] == "a2a"
    assert "action_type" not in entity
    assert "confidence" not in entity


def test_a2a_request_with_no_signal_keeps_the_routed_agents_prior():
    result, entity = _submit("Fix the deploy pipeline crash\nThe CI build fails.")
    assert result.skill == "cicada"
    resolved = _resolved(result, entity)
    assert (resolved.value, resolved.source) == ("open_or_merge_pr", "agent_prior")


def test_a2a_request_that_asks_for_a_pr_is_high_blast():
    result, entity = _submit("Update the docs\nPlease open a PR with the change.")
    assert _resolved(result, entity).value == "open_or_merge_pr"


def test_a2a_prose_cannot_lower_the_gate():
    result, entity = _submit(
        "Look over the pipeline code\nPURE REPORTING. Write NOTHING - the only output is a report."
    )
    assert result.skill == "cicada"
    # Operator-filed, the same text WOULD lower the prior (the control).
    assert infer_action_type_from_text(entity["title"], entity["description"]) == (
        "compute_only_analysis"
    )
    operator_filed = resolve_gate_action_type(
        "cicada", {"title": entity["title"], "description": entity["description"]}
    )
    assert operator_filed.value == "compute_only_analysis"
    resolved = _resolved(result, entity)
    assert (resolved.value, resolved.source) == ("open_or_merge_pr", "agent_prior")
