"""Drive the Apis execution gate through scenarios and report what it did.

Each scenario in ``scenarios.json`` is run through the REAL
``apis.dispatch_task``: the gate, ``_resolve_confidence``, ``evaluate_gate``,
the producer scorer and its reply validation are all production code. Only the
edges are stubbed: Neotoma reads and writes, the agent spawn, and the model
call (the scorer's injected runner), because an offline lane has no model.

``run_scenario`` returns what was observed; ``check`` compares it to the
scenario's ``expect`` block. ``mutation`` re-runs a scenario with one piece of
the change removed so a test can prove the checks go red (an eval whose checks
cannot fail is not an eval).

The scenarios are synthetic and public-safe: no operator data of any kind.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
APIS_DIR = REPO_ROOT / "execution" / "daemons" / "apis"
for _p in (str(REPO_ROOT), str(APIS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import apis  # noqa: E402
import model_tiering  # noqa: E402
import producer_confidence  # noqa: E402
from lib.daemon_runtime import gating  # noqa: E402
from lib.daemon_runtime.gating import ExecutionPolicy  # noqa: E402
from unroutable_ledger import UnroutableLedger  # noqa: E402

MUTATIONS = ("none", "no_producer", "round_before_gate")
BINDING = {"claude": {"local": "m-local", "mechanical": "m-mech", "mid": "m-mid", "top": "m-top"}}


def load() -> dict:
    return json.loads((HERE / "scenarios.json").read_text())


class _Notifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, priority=None, handler=None, **kwargs):
        self.sent.append(message)

    def clear_dedupe(self, key):
        pass


class _Spawned:
    ok = True
    pr_url = None
    detail = "stub"


class _Posted:
    def __init__(self, store: list):
        self._store = store

    def raise_for_status(self):
        pass

    def json(self):
        return {"entities": [{"entity_id": "ent_brief"}]}


def build_task(base: dict, overrides: dict | None) -> dict:
    task = dict(base)
    over = dict(overrides or {})
    pad = over.pop("body_pad_chars", None)
    suffix = over.pop("body_suffix", "")
    task.update(over)
    if pad:
        task["body"] = ("Tidy the local notes index. " * (pad // 28 + 1))[:pad] + suffix
    return task


def run_scenario(scenario: dict, base_task: dict, *, mutation: str = "none") -> dict:
    """Run one scenario; return the observation. Never touches the network."""
    assert mutation in MUTATIONS, mutation
    obs: dict = {
        "briefs": [], "spawns": [], "statuses": [], "runner_calls": [],
        "notifications": [], "persisted": [], "scorer_tier": None,
    }
    config = scenario.get("config") or {}
    scorer = scenario.get("scorer") or {"kind": "none"}
    task = build_task(base_task, scenario.get("task"))
    gate_override = bool((scenario.get("dispatch") or {}).get("gate_override"))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(apis, "_unroutable", UnroutableLedger(path=_tmp_ledger()))
        mp.setattr(apis, "_created_seen", {})
        mp.setattr(apis, "READINESS_GATE", False)
        mp.setattr(apis, "RUN_CONVERSATIONS", False)

        def _set_status(entity_id, status, **kw):
            obs["statuses"].append((entity_id, status, kw.get("reason")))
            return True

        mp.setattr(apis, "set_task_status", _set_status)
        mp.setattr(apis, "fetch_task_record", lambda _id: {"entity_id": _id})
        mp.setattr(apis, "fetch_entity_user_id", lambda _id: "usr_eval")
        mp.setattr(
            apis, "_record_snapshot",
            lambda record: {
                "status": "awaiting_approval",
                "blocked_reason": next(
                    (r for _i, s, r in reversed(obs["statuses"]) if r), None
                ),
            },
        )
        mp.setattr(apis, "_release_lifecycle_proven", lambda *a, **k: True)

        # The REAL checkpoint writer, with only its HTTP call captured, so the
        # persisted record (not just the in-memory decision) is what is checked.
        mp.setattr(gating, "NEOTOMA_BEARER_TOKEN", "eval-token")
        mp.setattr(gating, "NEOTOMA_BASE_URL", "http://eval.invalid")

        def _post(url, headers=None, json=None, content=None, timeout=None):
            obs["persisted"].append((json or {})["entities"][0])
            return _Posted(obs["persisted"])

        mp.setattr(gating.httpx, "post", _post)
        real_writer = gating.write_checkpoint_brief

        def _writer(**kw):
            obs["briefs"].append(kw)
            # No authority envelope offline: drop the pieces that demand keys.
            kw = {k: v for k, v in kw.items() if k not in ("task_record", "policy")}
            return real_writer(**kw)

        mp.setattr(apis, "write_checkpoint_brief", _writer)

        async def _spawn(skill, entity_id, *a, **kw):
            obs["spawns"].append((skill, entity_id))
            return _Spawned()

        mp.setattr(apis, "_spawn_harness_skill", _spawn)
        mp.setattr(
            apis, "resolve_policy_for_agent",
            lambda _skill: ExecutionPolicy(entity_id="fallback", loaded=False),
        )
        policy_map = (
            {model_tiering.ACTION_CONFIDENCE_SCORING: config["tier_policy"]}
            if config.get("tier_policy") else {}
        )
        mp.setattr(model_tiering, "configured_action_policy", lambda: policy_map)
        mp.setattr(
            model_tiering, "configured_vendor_binding",
            lambda: BINDING if config.get("vendor_binding", True) else {},
        )
        if config.get("kill_switch"):
            mp.setenv("APIS_PRODUCER_SCORER", "0")
        else:
            mp.delenv("APIS_PRODUCER_SCORER", raising=False)

        async def _runner(prompt, tier=None, task_entity_id=""):
            obs["runner_calls"].append(prompt)
            obs["scorer_tier"] = getattr(tier, "tier", None)
            kind = scorer["kind"]
            if kind == "raise":
                raise RuntimeError("scorer exploded")
            if kind == "hang":
                await asyncio.sleep(30)
            if kind == "not_ok":
                return False, "", ""
            return True, scorer.get("reply", ""), "claude"

        mp.setattr(producer_confidence, "_default_runner", _runner)
        if scorer["kind"] == "hang":
            mp.setattr(producer_confidence, "SCORER_TIMEOUT_SECONDS", 0)
            mp.setattr(producer_confidence, "TIMEOUT_GRACE_SECONDS", 0.05)

        if mutation == "no_producer":
            async def _nothing(*a, **k):
                return producer_confidence.ScoreAttempt(None)

            mp.setattr(producer_confidence, "score_unscored_task", _nothing)
        elif mutation == "round_before_gate":
            real_gate = apis.evaluate_gate

            def _rounded(**kw):
                kw["confidence"] = round(kw["confidence"], 2)
                return real_gate(**kw)

            mp.setattr(apis, "evaluate_gate", _rounded)

        notifier = _Notifier()
        asyncio.run(
            apis.dispatch_task(
                "ent_eval_task", task, trigger="approved" if gate_override else "created",
                notifier=notifier, snapshot_hydrated=True, gate_override=gate_override,
            )
        )
        obs["notifications"] = notifier.sent
    return obs


_LEDGERS: list = []


def _tmp_ledger() -> Path:
    import tempfile

    d = Path(tempfile.mkdtemp(prefix="dispatch_gate_eval_"))
    _LEDGERS.append(d)
    return d / "ledger.json"


def check(scenario: dict, obs: dict) -> list[str]:
    """Compare an observation to ``scenario['expect']``; return failures."""
    exp = scenario["expect"]
    fails: list[str] = []
    held = bool(obs["briefs"])
    executed = bool(obs["spawns"])
    brief = obs["briefs"][0] if held else None
    decision = brief["decision"] if brief else None

    outcome = "held" if held and not executed else "executed" if executed and not held else "other"
    if outcome != exp["outcome"]:
        fails.append(f"outcome {outcome!r}, expected {exp['outcome']!r}")
    if bool(obs["runner_calls"]) != exp["scorer_called"]:
        fails.append(f"scorer_called {bool(obs['runner_calls'])}, expected {exp['scorer_called']}")
    if held:
        if "confidence" in exp and decision.confidence != exp["confidence"]:
            fails.append(f"confidence {decision.confidence!r}, expected {exp['confidence']!r}")
        if "confidence_unscored" in exp and decision.confidence_unscored != exp["confidence_unscored"]:
            fails.append(f"confidence_unscored {decision.confidence_unscored}")
        if "confidence_source" in exp and decision.confidence_source != exp["confidence_source"]:
            fails.append(f"confidence_source {decision.confidence_source!r}")
        if "reason" in exp and exp["reason"] not in decision.reason:
            fails.append(f"reason {decision.reason!r} lacks {exp['reason']!r}")
        summary = brief["plan_summary"]
        for needle in exp.get("summary_contains", []):
            if needle not in summary:
                fails.append(f"summary lacks {needle!r}: {summary!r}")
        note = obs["notifications"][-1] if obs["notifications"] else ""
        for needle in exp.get("notification_contains", []):
            if needle not in note:
                fails.append(f"notification lacks {needle!r}: {note!r}")
        if "persisted_confidence_source" in exp:
            got = (obs["persisted"][0].get("confidence_source") if obs["persisted"] else None)
            if got != exp["persisted_confidence_source"]:
                fails.append(f"persisted confidence_source {got!r}")
        if exp.get("confidence_source") == "" and obs["persisted"]:
            if "confidence_source" in obs["persisted"][0]:
                fails.append("a mechanical estimate was persisted with a producer source")
    elif exp.get("summary_contains") or exp.get("notification_contains"):
        fails.append("no checkpoint was written to carry the expected text")
    if "scorer_tier" in exp and obs["scorer_tier"] != exp["scorer_tier"]:
        fails.append(f"scorer ran at {obs['scorer_tier']!r}, expected {exp['scorer_tier']!r}")
    if obs["scorer_tier"] == "top":
        fails.append("the scorer ran on the top tier")
    return fails
