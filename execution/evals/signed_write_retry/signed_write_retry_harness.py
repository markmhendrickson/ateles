"""Drive the REAL gate sign-off path through a flaky transport and report what the
stored record looks like afterwards.

The store is the owner_history eval's `FakeIssueStore` (Neotoma's array reducer,
CAS version token) with the one behaviour the retry depends on added: `/correct`
honours `idempotency_key`, so a repeated key is answered as a replay and nothing is
applied twice. The transport is scripted per field: a list of outcomes consumed one
per attempt, each one of

    ok            the write lands and the response arrives
    transport     the call fails before the write lands (a transport error)
    timeout       the signing helper times out before the write lands
    hang          the helper never answers: it runs until the timeout it was given
    lost          the write LANDS, then the response is lost (a transport error)
    http:<n>      the server answers with status n and applies nothing

Scenarios in ``scenarios.json`` run `IssueGateStore.sign_off` unchanged; only the
transport, the clock and the store are fakes. ``mutation`` reinstates a defect so a
test can show the checks go red.

Synthetic and public-safe: no key, no token, no operator data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "owner_history"))

import owner_history_harness as base  # noqa: E402

gate_waive = base.gate_waive
ns = base.ns
GATE_STATUS_FIELD = base.GATE_STATUS_FIELD

MUTATIONS = ("none", "no_retry", "retry_4xx", "fresh_key_per_attempt", "old_cap")


def load() -> dict:
    return json.loads((HERE / "scenarios.json").read_text())


class FlakyStore(base.FakeIssueStore):
    """The reducer store, plus idempotent replay and a scripted unreliable transport."""

    def __init__(self, gates: dict, script: dict[str, list[str]]):
        super().__init__(gates, None, 1000)
        self.script = {k: list(v) for k, v in script.items()}
        self.seen_keys: set[str] = set()
        self.attempts: dict[str, list[str]] = {}  # field -> idempotency key per attempt
        self.timeouts: list[float] = []  # the timeout each hanging attempt was given

    def transport(self, body: dict, sub: str | None, timeout: float = 20.0, clock=None) -> tuple[int, dict]:
        field, key = body["field"], body.get("idempotency_key", "")
        self.attempts.setdefault(field, []).append(key)
        outcome = (self.script.get(field) or ["ok"]).pop(0) if self.script.get(field) else "ok"
        if outcome == "transport":
            raise ns.SignedRequestError("fetch failed (private detail)", "transport_error")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired("signed_fetch", 20)
        if outcome == "hang":
            if clock is not None:
                clock["now"] += timeout  # the request consumed all it was given
            self.timeouts.append(timeout)
            raise subprocess.TimeoutExpired("signed_fetch", timeout)
        if outcome.startswith("http:"):
            return int(outcome[5:]), {}
        replay = key in self.seen_keys
        self.seen_keys.add(key)
        if replay:
            status, resp = 200, {"replayed": True}
        else:
            status, resp = self.apply(body, sub)
        if outcome == "lost" and status < 300:
            raise ns.SignedRequestError("response lost (private detail)", "transport_error")
        return status, resp


def run_scenario(scenario: dict, *, mutation: str = "none") -> dict:
    assert mutation in MUTATIONS, mutation
    store = FlakyStore(
        scenario.get("gates") or {"pm": "pending"}, scenario.get("script") or {}
    )
    capture = base._Capture()
    capture.setLevel(logging.INFO)
    logger = logging.getLogger("apis.gate_waive")
    logger.addHandler(capture)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    delays: list[float] = []
    clock = {"now": 0.0}
    obs: dict = {"store": store, "outcomes": [], "log": capture.lines, "delays": delays,
                 "clock": clock}

    async def sleep(delay: float) -> None:
        delays.append(delay)
        clock["now"] += delay

    with pytest.MonkeyPatch.context() as mp:
        issue_store = base._wire(mp, store, signer_fails=None)

        def signed(method, url, body=None, agent_name="", timeout=20, *, sub=None):
            return store.transport(body, sub, timeout, clock)

        mp.setattr(gate_waive._ns, "signed_request", signed)
        mp.setattr(gate_waive, "_retry_sleep", sleep, raising=False)
        mp.setattr(gate_waive, "_monotonic", lambda: clock["now"], raising=False)
        if mutation == "old_cap":
            # The previous check: elapsed + next wait only; every attempt launched
            # with its full default timeout.
            mp.setattr(
                gate_waive, "_retry_fits",
                lambda started, done: (
                    gate_waive._monotonic() - started + gate_waive._next_backoff(done)
                    <= gate_waive.SIGNED_WRITE_TOTAL_CAP_S
                ),
                raising=False,
            )
            mp.setattr(gate_waive, "_attempt_timeout", lambda default, started: default, raising=False)
        if mutation == "no_retry":
            mp.setattr(gate_waive, "retries_for_reason", lambda code: 0)
            mp.setattr(gate_waive, "retries_for_status", lambda status: 0)
        if mutation == "retry_4xx":
            mp.setattr(gate_waive, "retries_for_status", lambda status: 2)
        if mutation == "fresh_key_per_attempt":
            real = IssueGateStore = gate_waive.IssueGateStore
            counter = {"n": 0}
            original = real._signed_correct

            async def _fresh(self, entity_id, field_name, value, key, **kw):
                counter["n"] += 1
                return await original(self, entity_id, field_name, value, f"{key}#{counter['n']}", **kw)

            mp.setattr(IssueGateStore, "_signed_correct", _fresh)

        async def _go():
            for step in scenario["steps"]:
                gate = step["gate"]
                store.fail_fields = {GATE_STATUS_FIELD} if step.get("fail_gate") else set()
                outcome = await issue_store.sign_off(
                    "o/r", 795, gate, step.get("lens") or base.LENSES[gate], step.get("head", "a" * 40)
                )
                obs["outcomes"].append(outcome)

        try:
            asyncio.run(_go())
        finally:
            logger.removeHandler(capture)
            logger.setLevel(previous_level)
    return obs


def check(scenario: dict, obs: dict) -> list[str]:
    exp = scenario["expect"]
    store: FlakyStore = obs["store"]
    bad: list[str] = []

    def want(name, actual, expected):
        if actual != expected:
            bad.append(f"{name}: expected {expected!r}, got {actual!r}")

    if "ok" in exp:
        want("sign-off ok", [o.ok for o in obs["outcomes"]], exp["ok"])
    if "verified" in exp:
        want("sign-off verified", [o.verified for o in obs["outcomes"]], exp["verified"])
    if "gates" in exp:
        want("gate_status", store.gate_status, exp["gates"])
    if "attempts" in exp:
        want("attempts per field", {f: len(k) for f, k in store.attempts.items()}, exp["attempts"])
    if exp.get("one_key_per_field"):
        for field, keys in store.attempts.items():
            if len(set(keys)) != 1:
                bad.append(f"{field}: retries changed the idempotency key: {len(set(keys))} keys")
    if "delays" in exp:
        want("backoff delays", obs["delays"], exp["delays"])
    if "gate_writes_applied" in exp:
        want("gate writes applied", sum(1 for f, _ in store.writes if f == GATE_STATUS_FIELD),
             exp["gate_writes_applied"])
    if "history_actions" in exp:
        want("stored history actions", [e.get("action") for e in store.owner_history],
             exp["history_actions"])
    if "max_seconds" in exp and obs["clock"]["now"] > exp["max_seconds"]:
        bad.append(f"took {obs['clock']['now']}s of waits and attempts, over {exp['max_seconds']}s")
    log = "\n".join(obs["log"])
    for needle in exp.get("log_contains", []):
        if needle not in log:
            bad.append(f"the log lacks {needle!r}: {log[:400]!r}")
    for needle in exp.get("log_excludes", []):
        if needle in log:
            bad.append(f"the log leaks {needle!r}")
    return bad
