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
    not_json      the write LANDS, then the server's body is not JSON (response_not_json)
    lost_rotate   the write LANDS, the response is lost, and the lens's signing key is
                  rotated before the next attempt (a different signing principal)

Scenarios in ``scenarios.json`` run `IssueGateStore.sign_off` unchanged; only the
transport, the clock and the store are fakes. ``mutation`` reinstates a defect so a
test can show the checks go red.

Synthetic and public-safe: no key, no token, no operator data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import concurrent.futures
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

MUTATIONS = ("none", "no_retry", "retry_4xx", "fresh_key_per_attempt", "old_cap", "timeout_before_thread_only",
             "no_principal_check")


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
        self.principal = ("lens@ateles-swarm", "thumbprint-before-rotation")

    def transport(self, body: dict, sub: str | None, timeout: float = 20.0, clock=None,
                  principal_pin=None) -> tuple[int, dict]:
        # The real preparer reports the principal it resolved, and the shared helper
        # refuses to authorize transport when it differs from the pin. The pin class
        # is the real one; only the key resolution is scripted.
        if principal_pin is not None and not principal_pin.admit(self.principal):
            raise ns.SignedRequestError("signing principal changed (private detail)", "identity_changed")
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
        if outcome == "lost_rotate" and status < 300:
            self.principal = ("lens@ateles-swarm", "thumbprint-after-rotation")
            raise ns.SignedRequestError("response lost (private detail)", "transport_error")
        if outcome == "not_json" and status < 300:
            raise ns.SignedRequestError("body was <html>private detail</html>", "response_not_json")
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

        def signed(method, url, body=None, agent_name="", timeout=20, *, sub=None,
                   principal_pin=None, **request_options):
            return store.transport(body, sub, timeout, clock, principal_pin)

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
        if mutation == "timeout_before_thread_only":
            # The previous design: compute the helper timeout, hand the call to the
            # executor and await it with no deadline of its own.
            async def old(self, entity_id, field_name, value, key, *, lens_agent, lens_sub, deadline=None,
                          principal_pin=None):
                timeout = min(gate_waive.SIGNED_ATTEMPT_TIMEOUT_S, deadline - gate_waive._monotonic())
                return await asyncio.to_thread(
                    gate_waive._ns.signed_request, "POST", "u", {"field": field_name, "value": value,
                    "idempotency_key": key}, agent_name=lens_agent, timeout=timeout, sub=lens_sub)

            mp.setattr(gate_waive.IssueGateStore, "_signed_correct", old)
        if mutation == "no_principal_check":
            mp.setattr(gate_waive._ns.PrincipalPin, "admit", lambda self, principal: True)
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
            delay = scenario.get("queue_delay_s", 0)
            if delay:
                # Every attempt waits in the worker pool's queue for `delay` seconds of the
                # virtual clock before a worker begins it.
                class _Queueing(concurrent.futures.ThreadPoolExecutor):
                    def submit(self, fn, *args, **kwargs):
                        def late(*a, **k):
                            clock["now"] += delay
                            return fn(*a, **k)

                        return super().submit(late, *args, **kwargs)

                mp.setattr(gate_waive, "_signed_pool", _Queueing(max_workers=2))
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
    if "max_attempts_per_key" in exp:
        sent = [k for keys in store.attempts.values() for k in keys]
        want("most times one idempotency key was sent", max((sent.count(k) for k in set(sent)), default=0),
             exp["max_attempts_per_key"])
    if "delays" in exp:
        want("backoff delays", obs["delays"], exp["delays"])
    if "writes_issued" in exp:
        want("writes issued to the store", len(store.writes), exp["writes_issued"])
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


def delayed_preparation_identity(agent_name, *, sub=None, keys_dir=None):
    """Synthetic, spawn-safe resolver used only by the offline timing eval."""
    import time
    (Path(keys_dir) / "started").write_text("started")
    time.sleep(2)
    return {"key": "synthetic-unused", "sub": sub or "synthetic", "kid": "synthetic"}


def run_preparation_boundary(directory, *, mutation=False, cancel=False):
    """Exercise real signed_request preparation, expiry/cancellation and cleanup.

    The planted control drops the caller's budget/cancellation at the wrapper
    boundary. The helper remains a recorder: no signer, credentials or network.
    """
    import threading
    import time
    _write_helper_fixture(directory)
    permits = threading.BoundedSemaphore(1)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    observation = {}
    real_request = ns.signed_request
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gate_waive, "_signed_pool", pool)
        mp.setattr(gate_waive, "_signed_permits", permits)
        mp.setattr(gate_waive, "_monotonic", time.monotonic)
        mp.setattr(gate_waive, "SIGNED_WRITE_TOTAL_CAP_S", 0.8)
        mp.setattr(gate_waive, "MIN_ATTEMPT_TIMEOUT_S", 0.01)
        mp.setattr(gate_waive, "SIGNED_ATTEMPT_TIMEOUT_S", 5)
        mp.setattr(ns, "AAUTH_KEYS_DIR", str(directory))
        mp.setattr(ns, "NEOTOMA_RC_DIR", str(directory))
        mp.setattr(ns, "agent_identity", delayed_preparation_identity)
        mp.setenv("ATELES_SIGNED_EVAL_EFFECT", str(directory / "effect"))
        if mutation:
            def detached(*args, **kwargs):
                kwargs.pop("deadline", None)
                kwargs.pop("cancelled", None)
                kwargs["timeout"] = 5
                return real_request(*args, **kwargs)
            mp.setattr(ns, "signed_request", detached)

        async def exercise():
            started = time.monotonic()
            call = asyncio.create_task(gate_waive.IssueGateStore("http://unused.invalid", "")._sign_off_write(
                "synthetic", "gate_status", {}, "synthetic-key", lens_agent="synthetic",
                lens_sub="synthetic", context=("synthetic", 1, "pm")))
            if cancel:
                while not (directory / "started").exists() and time.monotonic() - started < 0.6:
                    await asyncio.sleep(0.01)
                call.cancel()
            try:
                observation["failed"] = bool(await call)
            except asyncio.CancelledError:
                observation["failed"] = True
            observation["caller_seconds"] = time.monotonic() - started
            observation["preparation_started"] = (directory / "started").exists()
            await asyncio.sleep(0.3)
            observation["permit_reclaimed"] = permits.acquire(blocking=False)
            if observation["permit_reclaimed"]:
                permits.release()

        try:
            asyncio.run(exercise())
        finally:
            # Join while all patches still hold, including in the planted control.
            pool.shutdown(wait=True)
    observation["helper_launches"] = int((directory / "effect").exists())
    return observation


def check_preparation_boundary(observation):
    expected = {"failed": True, "preparation_started": True,
                "permit_reclaimed": True, "helper_launches": 0}
    bad = [f"{key}: {observation.get(key)!r}" for key, value in expected.items()
           if observation.get(key) != value]
    if observation["caller_seconds"] > 1.2:
        bad.append("caller exceeded deadline allowance")
    return bad


_REAL_POPEN = subprocess.Popen


def _helper_creation_barrier(*args, **kwargs):
    """Spawn-safe seam: release leads to the actual original helper process."""
    import os
    ready = os.open(os.environ["ATELES_SIGNED_EVAL_READY"], os.O_WRONLY)
    os.write(ready, str(os.getpid()).encode())
    os.close(ready)
    release = os.open(os.environ["ATELES_SIGNED_EVAL_RELEASE"], os.O_RDONLY)
    try:
        os.read(release, 1)
    finally:
        os.close(release)
    return _REAL_POPEN(*args, **kwargs)


def _write_helper_fixture(directory):
    module = directory / "dist" / "cli" / "aauth_signer.js"
    module.parent.mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text('{"type":"module"}')
    module.write_text('''import fs from "node:fs";
import {spawn} from "node:child_process";
export async function cliSignedFetch(url, options) {
  if (process.env.ATELES_SIGNED_EVAL_DESCENDANT === "1") {
    const script = `const fs=require("node:fs");
      const ready=fs.openSync(process.env.ATELES_SIGNED_EVAL_READY,"w");
      fs.writeSync(ready,String(process.pid));fs.closeSync(ready);
      const release=fs.openSync(process.env.ATELES_SIGNED_EVAL_RELEASE,"r");
      fs.readSync(release,Buffer.alloc(1),0,1,null);fs.closeSync(release);
      fs.writeFileSync(process.env.ATELES_SIGNED_EVAL_EFFECT,"late");
      const done=fs.openSync(process.env.ATELES_SIGNED_EVAL_READY,"w");
      fs.writeSync(done,"effect");fs.closeSync(done);`;
    const child=spawn(process.execPath,["-e",script],{stdio:["ignore","inherit","inherit"]});
    fs.writeFileSync(process.env.ATELES_SIGNED_EVAL_PIDS,JSON.stringify({helper:process.pid,descendant:child.pid}));
    await new Promise(resolve=>child.on("exit",resolve));
  } else {
    fs.writeFileSync(process.env.ATELES_SIGNED_EVAL_EFFECT,JSON.stringify({pid:process.pid,body:options.body}));
  }
  return {status:200,ok:true,text:async()=>"{}"};
}
''')


def _terminal_pid(pid):
    with _REAL_POPEN(["ps", "-p", str(pid), "-o", "stat="],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
        stdout, _ = proc.communicate(timeout=2)
    return proc.returncode == 1 or stdout.strip().startswith("Z")


def run_complete_boundary(directory, *, caller="gate", cancel=False,
                          descendant=False, mutation=False, start=False):
    """Synthetic agent request -> actual gate/shared call -> original Node helper.

    External key metadata and signer effects are synthetic. Real OS startup,
    pipes, cancellation, descendants and the SAME worker permit are observed.
    """
    import os
    import contextlib
    import select
    import threading
    import time
    _write_helper_fixture(directory)
    (directory / "synthetic.jwk.json").write_text('{"kid":"synthetic"}')
    ready, release = directory / "ready", directory / "release"
    os.mkfifo(ready)
    os.mkfifo(release)
    ready_fd = os.open(ready, os.O_RDWR | os.O_NONBLOCK)
    release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    permits = threading.BoundedSemaphore(1)
    abandoned = threading.Event()
    state, control = {}, {}
    real_request = ns.signed_request
    original_killpg = os.killpg
    context = ns.multiprocessing.get_context("spawn")
    real_process = context.Process
    created = []

    def controlled_process(*args, **kwargs):
        child = real_process(*args, **kwargs)
        launch = child.start

        def controlled_start():
            os.write(ready_fd, str(os.getpid()).encode())
            # A real OS barrier before the original Process.start; releasing
            # it still creates the actual child, even after caller abandonment.
            import select
            select.select([release_fd], [], [], 6)
            os.read(release_fd, 1)
            del child.start
            launch()
            created.append(child.pid)

        child.start = controlled_start
        return child
    # This is the synthetic agent's selected request, including a fresh key.
    request = {"entity_id": "synthetic", "field": "gate_status", "value": {},
               "idempotency_key": "synthetic-unique-attempt"}

    def detached(*args, **kwargs):
        kwargs.pop("deadline", None)
        kwargs.pop("cancelled", None)
        kwargs["timeout"] = 5
        return real_request(*args, **kwargs)

    def run():
        began = time.monotonic()
        try:
            if caller == "direct":
                def call():
                    return ns.signed_request("POST", "http://fixture.invalid", request,
                                             agent_name="synthetic", timeout=0.8, cancelled=abandoned)
                if mutation == "unbound_start":
                    ns._run_in_signing_worker(call)
                else:
                    call()
            else:
                async def call():
                    control["loop"] = asyncio.get_running_loop()
                    control["task"] = asyncio.current_task()
                    return await gate_waive.IssueGateStore("http://fixture.invalid", "")._signed_correct(
                        "synthetic", "gate_status", {}, request["idempotency_key"],
                        lens_agent="synthetic", lens_sub="synthetic@ateles-swarm",
                        deadline=time.monotonic() + 0.8)
                asyncio.run(call())
            state["failed"] = False
        except (ns.SignedRequestError, subprocess.TimeoutExpired, asyncio.CancelledError):
            state["failed"] = True
        finally:
            state["caller_seconds"] = time.monotonic() - began

    cleanup = contextlib.ExitStack()
    cleanup.callback(os.close, ready_fd)
    cleanup.callback(os.close, release_fd)
    cleanup.callback(pool.shutdown, wait=True)

    def stop_control_group():
        if mutation == "no_group" and state.get("owned_group"):
            try:
                original_killpg(state["owned_group"], __import__("signal").SIGKILL)
            except ProcessLookupError:
                pass

    cleanup.callback(stop_control_group)
    with cleanup, pytest.MonkeyPatch.context() as mp:
        mp.setattr(ns, "AAUTH_KEYS_DIR", str(directory))
        mp.setattr(ns, "NEOTOMA_RC_DIR", str(directory))
        mp.setattr(ns, "_signed_pool", pool)
        mp.setattr(ns, "_signed_permits", permits)
        mp.setattr(gate_waive, "_signed_pool", pool)
        mp.setattr(gate_waive, "_signed_permits", permits)
        mp.setattr(gate_waive, "_monotonic", time.monotonic)
        mp.setattr(gate_waive, "MIN_ATTEMPT_TIMEOUT_S", 0.01)
        mp.setenv("ATELES_SIGNED_EVAL_EFFECT", str(directory / "effect"))
        mp.setenv("ATELES_SIGNED_EVAL_READY", str(ready))
        mp.setenv("ATELES_SIGNED_EVAL_RELEASE", str(release))
        mp.setenv("ATELES_SIGNED_EVAL_PIDS", str(directory / "pids"))
        mp.setenv("ATELES_SIGNED_EVAL_DESCENDANT", "1" if descendant else "0")
        if start:
            mp.setattr(context, "Process", controlled_process)
            mp.setattr(ns.multiprocessing, "get_context", lambda method: context)
        elif not descendant:
            mp.setattr(ns.subprocess, "Popen", _helper_creation_barrier)
        if mutation is True:
            mp.setattr(ns, "signed_request", detached)
        if mutation == "no_group":
            mp.setattr(ns.os, "killpg", lambda pid, sig: None)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            state["boundary_reached"] = bool(select.select([ready_fd], [], [], 2)[0])
            if state["boundary_reached"]:
                state["boundary_pid"] = int(os.read(ready_fd, 100))
            if descendant and (directory / "pids").exists():
                owned = json.loads((directory / "pids").read_text())
                state["owned_group"] = os.getpgid(owned["helper"])
                assert state["owned_group"] == os.getpgid(owned["descendant"])
            if cancel:
                abandoned.set()
                if caller == "gate" and "task" in control:
                    control["loop"].call_soon_threadsafe(control["task"].cancel)
            thread.join(1.5)
            state["caller_bounded"] = not thread.is_alive()
            if start:
                state["permit_held_during_start"] = not permits.acquire(blocking=False)
                if not state["permit_held_during_start"]:
                    permits.release()
            if (directory / "pids").exists():
                state["owned_pids"] = json.loads((directory / "pids").read_text())
                state["owned_pids_terminal_at_return"] = all(
                    _terminal_pid(pid) for pid in state["owned_pids"].values())
        finally:
            os.write(release_fd, b"1")
            thread.join(6)
            # Drain actual work without replacing either the pool or permit.
            pool.submit(lambda: None).result(timeout=6)
        if mutation == "no_group":
            state["effect_signal"] = bool(select.select([ready_fd], [], [], 2)[0])
            if state["effect_signal"]:
                assert os.read(ready_fd, 100) == b"effect"
        state["late_effect"] = (directory / "effect").exists()
        if start:
            state["actual_created_pids"] = created
            state["created_pids_terminal"] = bool(created) and all(_terminal_pid(pid) for pid in created)
        state["permit_recovered"] = permits.acquire(blocking=False)
        if state["permit_recovered"]:
            permits.release()
        mp.setattr(ns.subprocess, "Popen", _REAL_POPEN)
        mp.setattr(context, "Process", real_process)
        mp.setattr(ns.os, "killpg", original_killpg)
        mp.setattr(ns, "signed_request", real_request)
        mp.setenv("ATELES_SIGNED_EVAL_DESCENDANT", "0")
        if (directory / "pids").exists():
            state["owned_pids"] = json.loads((directory / "pids").read_text())
            state["owned_pids_terminal"] = all(_terminal_pid(pid) for pid in state["owned_pids"].values())
        state["healthy_result"] = real_request("POST", "http://fixture.invalid", request,
                                                agent_name="synthetic", timeout=3) == (200, {})
        state["healthy_effect"] = (directory / "effect").exists()
    return state


def check_complete_boundary(observation):
    expected = {"boundary_reached": True, "caller_bounded": True, "failed": True,
                "late_effect": False, "permit_recovered": True,
                "healthy_result": True, "healthy_effect": True}
    if "owned_pids" in observation:
        expected["owned_pids_terminal"] = True
        expected["owned_pids_terminal_at_return"] = True
    if "actual_created_pids" in observation:
        expected["created_pids_terminal"] = True
        expected["permit_held_during_start"] = True
    bad = [f"{key}: {observation.get(key)!r}" for key, value in expected.items()
           if observation.get(key) != value]
    if observation.get("caller_seconds", float("inf")) > 1.2:
        bad.append("caller exceeded complete-attempt allowance")
    return bad


def _write_rotating_helper(directory, *, rotate):
    """A Node helper that records each launch. Its first launch loses the response
    (a transport error after the write 'landed') and, when ``rotate``, replaces the
    lens's key file, so the next attempt would be signed by another principal."""
    module = directory / "dist" / "cli" / "aauth_signer.js"
    module.parent.mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text('{"type":"module"}')
    module.write_text('''import fs from "node:fs";
export async function cliSignedFetch(url, options) {
  const launches = process.env.ATELES_SIGNED_EVAL_LAUNCHES;
  const first = !fs.existsSync(launches);
  fs.appendFileSync(launches, "launch\\n");
  if (first) {
    if (process.env.ATELES_SIGNED_EVAL_ROTATE === "1") {
      fs.writeFileSync(process.env.NEOTOMA_AAUTH_PRIVATE_JWK_PATH,
        JSON.stringify({kty: "EC", crv: "P-256", x: "rotated-x", y: "synthetic-y", kid: "synthetic"}));
    }
    throw new Error("fetch failed");
  }
  return {status: 200, ok: true, text: async () => "{}"};
}
''')
    (directory / "synthetic.jwk.json").write_text(
        json.dumps({"kty": "EC", "crv": "P-256", "x": "original-x", "y": "synthetic-y", "kid": "synthetic"}))


def run_identity_boundary(directory, *, rotate=True, mutation=False):
    """The REAL gate write -> real spawned preparer -> real Node helper, where the
    lens's signing key changes between a first attempt (which lost its response)
    and the retry. Offline: the key is a synthetic public JWK, the helper an inert
    recorder. ``mutation`` removes the principal check (the planted control)."""
    import time
    _write_rotating_helper(directory, rotate=rotate)
    capture = base._Capture()
    logger = logging.getLogger("apis.gate_waive")
    logger.addHandler(capture)
    observation = {}

    async def instant(delay):
        return None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ns, "AAUTH_KEYS_DIR", str(directory))
        mp.setattr(ns, "NEOTOMA_RC_DIR", str(directory))
        mp.setattr(gate_waive, "_retry_sleep", instant)
        mp.setattr(gate_waive, "_monotonic", time.monotonic)
        mp.setenv("ATELES_SIGNED_EVAL_LAUNCHES", str(directory / "launches"))
        mp.setenv("ATELES_SIGNED_EVAL_ROTATE", "1" if rotate else "0")
        if mutation:
            mp.setattr(ns.PrincipalPin, "admit", lambda self, principal: True)
        try:
            observation["result"] = asyncio.run(
                gate_waive.IssueGateStore("http://unused.invalid", "")._sign_off_write(
                    "synthetic", "gate_status", {}, "synthetic-key", lens_agent="synthetic",
                    lens_sub="synthetic@ateles-swarm", context=("synthetic", 1, "pm")))
        finally:
            logger.removeHandler(capture)
    launches = directory / "launches"
    observation["launches"] = len(launches.read_text().split()) if launches.exists() else 0
    observation["log"] = "\n".join(capture.lines)
    return observation


def check_identity_boundary(observation, *, rotate):
    if rotate:
        expected = {"result": gate_waive.SIGN_OFF_SIGNING_FAILED, "launches": 1}
    else:
        expected = {"result": "", "launches": 2}
    bad = [f"{key}: {observation.get(key)!r}" for key, value in expected.items()
           if observation.get(key) != value]
    if rotate and "reason=identity_changed" not in observation["log"]:
        bad.append("the failure was not reported as identity_changed")
    if "rotated-x" in observation["log"] or "original-x" in observation["log"]:
        bad.append("the log carries key material")
    return bad
