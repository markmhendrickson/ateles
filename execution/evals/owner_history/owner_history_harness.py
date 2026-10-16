"""Drive the REAL gate sign-off path against a store that models Neotoma's array
reducer, and report what the stored `owner_history` looks like afterwards.

The store (`FakeIssueStore`) is the part that matters. It follows Neotoma's
reducer for an array field (`src/reducers/observation_reducer.ts`, merge_array):
the snapshot value is the UNION, repeats kept, of the arrays carried by the
observations at the HIGHEST priority that carries the field. A `correct` is a
priority-1000 observation; a plain `store()` (a triage write) is lower, so once any
correction carries the field it stops counting. Every observation also moves an
opaque `entity_version` token, and a write that carries a stale `expected_version`
is refused with 409, as `/correct` does.

Scenarios in ``scenarios.json`` run `IssueGateStore.sign_off` (and its settle
paths) unchanged; only the transport and the store are fakes. ``mutation``
reinstates the old writer so a test can show the checks go red.

Synthetic and public-safe: no key, no token, no operator data.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
APIS_DIR = REPO_ROOT / "execution" / "daemons" / "apis"
for _p in (str(REPO_ROOT), str(APIS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gate_waive  # noqa: E402
from gate_waive import GATE_STATUS_FIELD, IssueGateState, IssueGateStore, parse_gate_status  # noqa: E402
from lib.daemon_runtime import neotoma_signed as ns  # noqa: E402

MUTATIONS = ("none", "old_writer", "fixed_window", "suppress_repeated_failure_notes")
LENS_TP = "L" * 43
LATER = "2099-01-01T00:00:00+00:00"
LENSES = {"pm": "pavo", "ux": "accipiter", "arch": "waxwing"}


def load() -> dict:
    return json.loads((HERE / "scenarios.json").read_text())


class FakeIssueStore:
    """One issue entity, with Neotoma's array reducer and CAS version token."""

    def __init__(self, gates: dict, history_seed: list[dict] | None, seed_priority: int):
        self.gate_status = dict(gates)
        self.current_owner = ""
        self.observations: list[dict] = []  # newest first
        self.writes: list[tuple[str, object]] = []
        self.refused = 0
        self.fail_fields: set[str] = set()  # writes to these fields are refused (503)
        self._n = 0
        if history_seed:
            self._add("owner_history", history_seed, seed_priority, signer=None)

    # ── the reducer ──
    @property
    def owner_history(self) -> list[dict]:
        carrying = [o for o in self.observations if "owner_history" in o["fields"]]
        if not carrying:
            return []
        top = max(o["source_priority"] for o in carrying)
        out: list[dict] = []
        for obs in reversed(carrying):  # oldest first
            if obs["source_priority"] == top:
                out.extend(obs["fields"]["owner_history"])
        return out

    @property
    def entity_version(self) -> str:
        return hashlib.sha256(f"obs:{len(self.observations)}".encode()).hexdigest()

    def _add(self, field: str, value, priority: int, signer: str | None) -> str:
        self._n += 1
        obs_id = f"obs-{self._n}"
        self.observations.insert(
            0,
            {
                "id": obs_id,
                "created_at": LATER,
                "source_priority": priority,
                "fields": {field: value},
                "provenance": {
                    "agent_sub": signer,
                    "agent_thumbprint": LENS_TP,
                    "attribution_tier": "software",
                },
            },
        )
        return obs_id

    # ── what the dispatcher calls ──
    def apply(self, body: dict, sub: str | None) -> tuple[int, dict]:
        expected = body.get("expected_version")
        if expected and expected != self.entity_version:
            self.refused += 1
            return 409, {"error_code": "ERR_FIELD_VERSION_CONFLICT"}
        field, value = body["field"], body["value"]
        if field in self.fail_fields:
            return 503, {}
        self.writes.append((field, value))
        if field == GATE_STATUS_FIELD:
            self.gate_status = parse_gate_status(value)
        elif field == "current_owner":
            self.current_owner = str(value)
        obs_id = self._add(field, value, 1000, sub)
        self.provenance = getattr(self, "provenance", {})
        self.provenance[field] = obs_id
        return 200, {"entity_version": self.entity_version}

    async def load(self, repo: str, issue_number: int) -> IssueGateState:
        return IssueGateState(
            repo=repo,
            issue_number=issue_number,
            entity_id="ent_1",
            gate_status=dict(self.gate_status),
            # What the store hands back is the raw union; the reader's guard is
            # `parse_owner_history`, the same call `IssueGateStore.load` makes.
            owner_history=gate_waive.parse_owner_history(self.owner_history),
            current_owner=self.current_owner,
            field_provenance=dict(getattr(self, "provenance", {})),
        )

    async def observations_for(self, entity_id: str, *, limit: int = 100) -> list[dict]:
        return list(self.observations)

    async def observations_page(self, entity_id: str, *, limit: int, offset: int):
        """One page, newest first, like the real endpoint (at most ``limit`` rows) and
        the total: a store that returns everything hides page-boundary bugs."""
        rows = list(self.observations)
        return rows[offset : offset + limit], len(rows)

    async def fetch_observations_limited(self, entity_id: str, *, limit: int = 100) -> list[dict]:
        return list(self.observations)[:limit]


def _wire(mp: pytest.MonkeyPatch, store: FakeIssueStore, *, signer_fails: str | None) -> IssueGateStore:
    issue_store = IssueGateStore("http://eval.invalid", "eval-token-not-real")
    mp.setattr(
        gate_waive._ns,
        "agent_identity",
        lambda agent, sub=None: {"key": f"/k/{agent}", "sub": sub or f"{agent}@ateles-swarm", "kid": agent},
    )

    def signed(method, url, body=None, agent_name="", timeout=20, *, sub=None):
        if signer_fails == "no_key":
            raise ns.SignedRequestError("no AAuth key for this agent (private detail)", "no_key")
        if signer_fails == "helper_error":
            raise ns.SignedRequestError("helper said /private/path/key.jwk.json Bearer eval-secret", "helper_error")
        if signer_fails == "payload_too_large":
            return 413, {}
        return store.apply(body, sub)

    mp.setattr(gate_waive._ns, "signed_request", signed)
    mp.setattr(gate_waive, "_lens_key_thumbprint", lambda identity: LENS_TP, raising=False)
    mp.setattr(issue_store, "load", store.load)
    mp.setattr(issue_store, "_observations", store.observations_for)
    mp.setattr(issue_store, "_observations_page", store.observations_page, raising=False)
    mp.setattr(issue_store, "_fetch_observations", store.fetch_observations_limited)
    return issue_store


def doubled_history(n: int) -> list[dict]:
    """The shape the canary entity reached: half 'triaged', the rest sign-offs."""
    triaged = {"agent": "lanius", "action": "triaged", "at": "2026-10-14T10:00:00Z"}
    other = {"actor": "pavo", "gate": "pm", "action": "signed_off", "commit": "a" * 40}
    return [dict(triaged) if i % 2 == 0 else dict(other) for i in range(n)]


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def run_scenario(scenario: dict, *, mutation: str = "none") -> dict:
    assert mutation in MUTATIONS, mutation
    seed = scenario.get("history_seed") or {}
    history = (
        doubled_history(seed["entries"])
        if seed.get("kind") == "doubled"
        else seed.get("list")
    )
    store = FakeIssueStore(
        scenario.get("gates") or {"pm": "pending", "ux": "pending", "arch": "pending"},
        history,
        seed.get("priority", 1000),
    )
    capture = _Capture()
    logger = logging.getLogger("apis.gate_waive")
    logger.addHandler(capture)
    obs: dict = {"store": store, "outcomes": [], "log": capture.lines}
    with pytest.MonkeyPatch.context() as mp:
        issue_store = _wire(mp, store, signer_fails=scenario.get("signer_fails"))
        if mutation == "old_writer":
            async def _old(self, entity_id, current, entries):
                return list(current) + list(entries)

            mp.setattr(IssueGateStore, "_history_write_value", _old)
            # And the old reader, which kept exact duplicates.
            mp.setattr(gate_waive, "dedupe_exact", lambda entries: list(entries))

        if mutation == "fixed_window":
            async def _windowed(self, entity_id):
                rows = await self._fetch_observations(entity_id, limit=500)
                return any(
                    isinstance(o.get("fields"), dict) and "owner_history" in o["fields"]
                    for o in rows
                )

            mp.setattr(IssueGateStore, "_owner_history_in_correction_tier", _windowed)
        if mutation == "suppress_repeated_failure_notes":
            real_new = gate_waive.new_history_entries

            def _suppress(current, entries):
                out = real_new(current, entries)
                known = {
                    (e.get("gate"), e.get("reason"), e.get("gate_after"))
                    for e in current
                    if e.get("action") == "sign_off_failed"
                }
                return [
                    e for e in out
                    if not (
                        e.get("action") == "sign_off_failed"
                        and (e.get("gate"), e.get("reason"), e.get("gate_after")) in known
                    )
                ]

            mp.setattr(gate_waive, "new_history_entries", _suppress)

        async def _go():
            for step in scenario["steps"]:
                gate = step["gate"]
                store.fail_fields = {GATE_STATUS_FIELD} if step.get("fail_gate") else set()
                for _ in range(step.get("repeat", 1)):
                    outcome = await issue_store.sign_off(
                        "o/r", 795, gate, step.get("lens") or LENSES[gate], step.get("head", "a" * 40)
                    )
                    obs["outcomes"].append(outcome)
            obs["read_back"] = (await store.load("o/r", 795)).owner_history

        try:
            asyncio.run(_go())
        finally:
            logger.removeHandler(capture)
    return obs


def check(scenario: dict, obs: dict) -> list[str]:
    exp = scenario["expect"]
    store: FakeIssueStore = obs["store"]
    bad: list[str] = []

    def want(name, actual, expected):
        if actual != expected:
            bad.append(f"{name}: expected {expected!r}, got {actual!r}")

    history_writes = [v for f, v in store.writes if f == "owner_history"]
    if "stored_length" in exp:
        want("stored owner_history length", len(store.owner_history), exp["stored_length"])
    if "max_entries_per_write" in exp:
        biggest = max((len(v) for v in history_writes), default=0)
        want("largest owner_history write", biggest, exp["max_entries_per_write"])
    if "history_writes" in exp:
        want("owner_history writes made", len(history_writes), exp["history_writes"])
    if "read_back_length" in exp:
        want("history as the dispatcher reads it", len(obs.get("read_back", [])), exp["read_back_length"])
    if "triaged_entries" in exp:
        want(
            "'triaged' entries stored",
            sum(1 for e in store.owner_history if e.get("action") == "triaged"),
            exp["triaged_entries"],
        )
    if "history_actions" in exp:
        got = [[e.get("action"), (e.get("commit") or "")[:1]] for e in store.owner_history]
        want("stored history (action, first char of commit)", got, exp["history_actions"])
    if "last_action" in exp:
        want("the history ends on", store.owner_history[-1].get("action"), exp["last_action"])
    if "all_signed_off" in exp:
        want("every sign-off ok", all(o.ok for o in obs["outcomes"]), exp["all_signed_off"])
    if "gates" in exp:
        want("gate_status", store.gate_status, exp["gates"])
    log = "\n".join(obs["log"])
    for needle in exp.get("log_contains", []):
        if needle not in log:
            bad.append(f"the log lacks {needle!r}: {log[:300]!r}")
    for needle in exp.get("log_excludes", []):
        if needle in log:
            bad.append(f"the log leaks {needle!r}")
    return bad
