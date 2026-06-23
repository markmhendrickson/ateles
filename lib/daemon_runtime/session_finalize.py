"""
lib/daemon_runtime/session_finalize.py — shared session finalize routine.

Task #3 of the task-spine plan (ent_aff87747b49e338790568af6): one routine that
*performs* end-of-session capture — store the turn(s), link them to the bound
plan/task, and record any learning — callable from both worlds so HITL and
autonomous sessions finalize identically:

  * HITL: the `/end` skill (Neotoma ent_af748d985b7bfa4f636eea70) calls it, and
    the Stop hook nudges the agent to run /end when a substantive session
    captured no learning.
  * Autonomous: a daemon that does NOT spawn a `claude` subprocess (so no Stop
    hook fires for it) calls finalize_session() directly at end-of-run. Daemons
    that DO spawn `claude --print` inherit the Stop hook on the child, which is
    the same convergence point.

"Hooks enforce; /end performs; this module is what performs." Pure payload
construction (build_finalize_payload) is unit-tested; the I/O (httpx /store,
/entities query for the skill body) is thin and fail-open, mirroring gating.py.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("daemon_runtime.finalize")

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

# Canonical /end skill (operator is migrating its body into Neotoma; we resolve
# by id with a slug fallback so this keeps working as that lands).
END_SKILL_ID = os.environ.get("END_SKILL_ID", "ent_af748d985b7bfa4f636eea70")
END_SKILL_SLUG = "end"


def build_finalize_payload(
    *,
    trigger_text: str,
    outcome_text: str,
    handler: str,
    conversation_id: str | None = None,
    conversation_title: str | None = None,
    plan_id: str | None = None,
    task_id: str | None = None,
    learning: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Build the /store body that finalizes one (autonomous) turn.

    Stores the trigger + outcome as a user/assistant agent_message pair PART_OF a
    conversation (created here when no conversation_id is supplied), links the
    conversation PART_OF the bound plan and/or task, and — when a learning is
    given — stores it as a `learning` artifact REFERS_TO the conversation and
    PART_OF the plan/task. Pure: returns the dict; does no I/O.
    """
    entities: list[dict] = []
    relationships: list[dict] = []

    conv_index: int | None = None
    if conversation_id is None:
        conv_index = len(entities)
        entities.append({
            "entity_type": "conversation",
            "name": conversation_title or f"{handler} autonomous session",
            "summary": f"Autonomous {handler} turn. Trigger: {trigger_text[:200]}",
        })

    def _conv_ref(rel: dict) -> dict:
        if conv_index is not None:
            rel["target_index"] = conv_index
        else:
            rel["target_entity_id"] = conversation_id
        return rel

    # user (trigger) + assistant (outcome) turn pair
    user_idx = len(entities)
    entities.append({"entity_type": "agent_message", "role": "user", "content": trigger_text})
    asst_idx = len(entities)
    entities.append({"entity_type": "agent_message", "role": "assistant", "content": outcome_text})
    relationships.append(_conv_ref({"source_index": user_idx, "relationship_type": "PART_OF"}))
    relationships.append(_conv_ref({"source_index": asst_idx, "relationship_type": "PART_OF"}))

    # optional learning artifact
    if learning:
        learn_idx = len(entities)
        entities.append({
            "entity_type": "learning",
            "title": f"{handler} learning",
            "content": learning,
        })
        relationships.append(_conv_ref({"source_index": learn_idx, "relationship_type": "REFERS_TO"}))
        for anchor in (plan_id, task_id):
            if anchor:
                relationships.append({
                    "source_index": learn_idx,
                    "target_entity_id": anchor,
                    "relationship_type": "PART_OF",
                })

    # anchor the conversation to the plan and/or task (binding)
    for anchor in (plan_id, task_id):
        if not anchor:
            continue
        rel = {"target_entity_id": anchor, "relationship_type": "PART_OF"}
        if conv_index is not None:
            rel["source_index"] = conv_index
        else:
            rel["source_entity_id"] = conversation_id
        relationships.append(rel)

    body: dict = {
        "entities": entities,
        "relationships": relationships,
        "observation_source": "workflow_state",
        "idempotency_key": idempotency_key or f"finalize-{handler}-{abs(hash(trigger_text)) % (10**10)}",
    }
    return body


def finalize_session(**kwargs) -> bool:
    """Store the finalize payload (see build_finalize_payload). Fail-open."""
    if not NEOTOMA_BEARER_TOKEN:
        log.warning("[finalize] no bearer token — session not finalized")
        return False
    body = build_finalize_payload(**kwargs)
    try:
        resp = httpx.post(
            f"{NEOTOMA_BASE_URL}/store",
            headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
            json=body,
            timeout=20,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 — never crash the caller
        log.warning("[finalize] store failed: %s", exc)
        return False


def load_end_skill() -> str | None:
    """Fetch the canonical /end skill body from Neotoma (fail-open None).

    Resolves by entity id; the operator is migrating the skill into Neotoma, so
    this is the convergence point both the Stop hook path and daemons rely on.
    """
    if not NEOTOMA_BEARER_TOKEN:
        return None
    try:
        resp = httpx.get(
            f"{NEOTOMA_BASE_URL}/entities/{END_SKILL_ID}",
            headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        snap = (data.get("snapshot") or {}).get("snapshot") or data.get("snapshot") or data
        return snap.get("content") if isinstance(snap, dict) else None
    except Exception as exc:  # noqa: BLE001
        log.warning("[finalize] could not load /end skill %s: %s", END_SKILL_ID, exc)
        return None


# ── self-test (pure builder) ─────────────────────────────────────────────────


def _selftest() -> int:
    checks: dict[str, bool] = {}

    # New conversation + plan + task + learning
    b = build_finalize_payload(
        trigger_text="task X due", outcome_text="did X", handler="apis",
        plan_id="ent_plan", task_id="ent_task", learning="X needs retries",
    )
    ents = b["entities"]
    rels = b["relationships"]
    types = [e["entity_type"] for e in ents]
    checks["has_conversation"] = "conversation" in types
    checks["two_messages"] = types.count("agent_message") == 2
    checks["has_learning"] = "learning" in types
    checks["roles"] = {e.get("role") for e in ents if e["entity_type"] == "agent_message"} == {"user", "assistant"}
    # conversation anchored to both plan and task
    conv_anchor_targets = {
        r.get("target_entity_id") for r in rels
        if r.get("source_index") == 0 and r["relationship_type"] == "PART_OF"
    }
    checks["conv_to_plan"] = "ent_plan" in conv_anchor_targets
    checks["conv_to_task"] = "ent_task" in conv_anchor_targets
    checks["idempotency"] = bool(b.get("idempotency_key"))

    # Existing conversation (no new conversation entity), no learning
    b2 = build_finalize_payload(
        trigger_text="t", outcome_text="o", handler="formica",
        conversation_id="ent_conv", task_id="ent_task2",
    )
    types2 = [e["entity_type"] for e in b2["entities"]]
    checks["no_dup_conversation"] = "conversation" not in types2
    msg_part_of = [
        r for r in b2["relationships"]
        if r["relationship_type"] == "PART_OF" and r.get("target_entity_id") == "ent_conv"
    ]
    checks["messages_link_existing_conv"] = len(msg_part_of) == 2
    checks["no_learning_when_absent"] = "learning" not in types2

    ok = all(checks.values())
    for k, v in checks.items():
        print(f"[{'PASS' if v else 'FAIL'}] {k}")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(_selftest())
