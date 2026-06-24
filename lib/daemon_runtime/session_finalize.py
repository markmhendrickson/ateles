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


# ── E1: conversation-per-execution-run ───────────────────────────────────────
# A task execution RUN gets exactly ONE conversation, opened at dispatch and
# anchored PART_OF the task (and plan when known) — the SAME anchoring
# build_finalize_payload uses, so the session-integrity invariant (conversation
# PART_OF plan OR task) holds and a later finalize_session(conversation_id=…)
# APPENDS to it rather than creating a second conversation. A retry/reopen passes
# a fresh run_key and therefore opens a new run conversation.


def build_run_conversation_payload(
    *,
    task_id: str,
    plan_id: str | None = None,
    agent: str,
    run_key: str,
    title: str | None = None,
    summary: str | None = None,
) -> dict:
    """Build the /store body that OPENS the conversation for one execution run.

    The conversation is linked PART_OF the task and (when given) the plan. The
    idempotency key embeds run_key so SSE replays of the same run reuse the
    conversation while a genuine retry (new run_key) opens a fresh one. Pure.
    """
    entities = [{
        "entity_type": "conversation",
        "name": title or f"{agent} run · task {task_id}",
        "summary": summary
        or f"Execution run for task {task_id} (agent {agent}, run {run_key}).",
    }]
    relationships = [
        {"source_index": 0, "target_entity_id": anchor, "relationship_type": "PART_OF"}
        for anchor in (task_id, plan_id)
        if anchor
    ]
    return {
        "entities": entities,
        "relationships": relationships,
        "observation_source": "workflow_state",
        "idempotency_key": f"run-conv-{task_id}-{run_key}",
    }


def build_turn_payload(
    *,
    conversation_id: str,
    role: str,
    content: str,
    sender_kind: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Build the /store body that APPENDS one agent_message turn to a run
    conversation (a progress update, an emailed operator reply, …). Pure."""
    msg: dict = {"entity_type": "agent_message", "role": role, "content": content}
    if sender_kind:
        msg["sender_kind"] = sender_kind
    return {
        "entities": [msg],
        "relationships": [{
            "source_index": 0,
            "target_entity_id": conversation_id,
            "relationship_type": "PART_OF",
        }],
        "observation_source": "workflow_state",
        "idempotency_key": idempotency_key
        or f"turn-{conversation_id}-{abs(hash(content)) % (10**10)}",
    }


def _post_store(body: dict) -> dict | None:
    """POST a /store body. Returns the parsed JSON, or None. Fail-open."""
    if not NEOTOMA_BEARER_TOKEN:
        log.warning("[finalize] no bearer token — store skipped")
        return None
    try:
        resp = httpx.post(
            f"{NEOTOMA_BASE_URL}/store",
            headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
            json=body,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001 — never crash the caller
        log.warning("[finalize] store failed: %s", exc)
        return None


def create_run_conversation(
    *,
    task_id: str,
    plan_id: str | None = None,
    agent: str,
    run_key: str,
    title: str | None = None,
    summary: str | None = None,
) -> str | None:
    """Open (idempotently) the run conversation; return its entity_id. Fail-open."""
    data = _post_store(build_run_conversation_payload(
        task_id=task_id, plan_id=plan_id, agent=agent,
        run_key=run_key, title=title, summary=summary,
    ))
    if not data:
        return None
    for e in data.get("entities", []):
        if e.get("entity_type") == "conversation":
            return e.get("entity_id")
    return None


def append_turn(
    *,
    conversation_id: str,
    role: str,
    content: str,
    sender_kind: str | None = None,
    idempotency_key: str | None = None,
) -> bool:
    """Append one turn to a run conversation. Fail-open."""
    return _post_store(build_turn_payload(
        conversation_id=conversation_id, role=role, content=content,
        sender_kind=sender_kind, idempotency_key=idempotency_key,
    )) is not None


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

    # Run conversation: one conversation, anchored PART_OF task + plan, run_key in key
    rc = build_run_conversation_payload(
        task_id="ent_task", plan_id="ent_plan", agent="cicada", run_key="created-0",
    )
    rc_types = [e["entity_type"] for e in rc["entities"]]
    checks["run_conv_single_conversation"] = rc_types == ["conversation"]
    rc_anchor_targets = {
        r["target_entity_id"] for r in rc["relationships"]
        if r["relationship_type"] == "PART_OF" and r.get("source_index") == 0
    }
    checks["run_conv_part_of_task"] = "ent_task" in rc_anchor_targets
    checks["run_conv_part_of_plan"] = "ent_plan" in rc_anchor_targets
    checks["run_conv_key_has_run"] = rc["idempotency_key"] == "run-conv-ent_task-created-0"
    # No plan: still PART_OF the task, no stray relationship
    rc2 = build_run_conversation_payload(task_id="ent_task", agent="cicada", run_key="r1")
    checks["run_conv_task_only"] = len(rc2["relationships"]) == 1

    # Turn append: one agent_message PART_OF the conversation
    tp = build_turn_payload(conversation_id="ent_conv", role="user", content="reply",
                            sender_kind="operator")
    checks["turn_single_message"] = [e["entity_type"] for e in tp["entities"]] == ["agent_message"]
    checks["turn_part_of_conv"] = (
        tp["relationships"][0]["target_entity_id"] == "ent_conv"
        and tp["relationships"][0]["relationship_type"] == "PART_OF"
    )
    checks["turn_sender_kind"] = tp["entities"][0].get("sender_kind") == "operator"

    ok = all(checks.values())
    for k, v in checks.items():
        print(f"[{'PASS' if v else 'FAIL'}] {k}")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(_selftest())
