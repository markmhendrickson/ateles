"""Drive the REAL agent_definition loader, and the real dispatch that uses it, against
a scripted Neotoma, and report what ran.

Only three things are fake: the HTTP transport to Neotoma (scripted per request), the
clock the loader sleeps on, and the process spawn (any spawn is recorded and, because a
refused dispatch must never reach one, fails the scenario). `AgentLoader`,
`skill_runner._load_agent_def`, `_run_skill_once` and `load_agent_definition_or_exit`
run unchanged.

Each scenario scripts Neotoma's answers to the definition query, one per request (the
last repeats), each one of: ``real`` (the real definition), ``none`` (no definition),
``timeout``, ``connect``, or an HTTP status number.

``mutation`` reinstates a defect so a test can show the checks go red:
``reinstate_stub`` (a failed load becomes a stand-in again), ``no_retry`` (no
retry), ``retry_rejected`` (a 4xx is retried).

Synthetic and public-safe: no key, no token, no operator data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import httpx
import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
APIS_DIR = REPO_ROOT / "execution" / "daemons" / "apis"
for _p in (str(REPO_ROOT), str(APIS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import skill_runner  # noqa: E402
from lib.daemon_runtime import agent_loader as al  # noqa: E402

MUTATIONS = ("none", "reinstate_stub", "no_retry", "retry_rejected")
REAL_PROMPT = "# the real role prompt"
REAL = {
    "entities": [
        {
            "entity_id": "ent_real",
            "snapshot": {
                "name": "pavo",
                "prompt_markdown": REAL_PROMPT,
                "tool_allowlist": ["Read", "Bash(gh pr:*)"],
                "status": "active",
                "aauth_sub": "pavo@ateles-swarm",
            },
        }
    ]
}


def load() -> dict:
    return json.loads((HERE / "scenarios.json").read_text())


class _Resp:
    def __init__(self, payload=None, status=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "http error", request=httpx.Request("POST", "http://x"), response=self
            )

    def json(self):
        return self._payload


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def run_scenario(scenario: dict, *, mutation: str = "none") -> dict:
    assert mutation in MUTATIONS, mutation
    script = list(scenario["neotoma"])
    obs: dict = {"requests": 0, "sleeps": [], "spawned": [], "cooled": []}
    capture = _Capture()
    loggers = [logging.getLogger(n) for n in ("lib.daemon_runtime.agent_loader", "apis.skill_runner")]
    for lg in loggers:
        lg.addHandler(capture)
    prior_levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.INFO)

    with pytest.MonkeyPatch.context() as mp:
        # Host state must never decide a scenario: the usage reading and its probe
        # belong to the operator's machine.
        import tempfile

        scratch = tempfile.mkdtemp(prefix="agent-definition-eval-")
        mp.setenv("APIS_HARNESS_USAGE_FILE", str(Path(scratch) / "usage.json"))
        mp.setenv("APIS_USAGE_GATE", "off")
        mp.setenv("APIS_USAGE_PROBE", "off")
        mp.setattr(al.ns, "via_cli_enabled", lambda: False)
        mp.setattr(al, "NEOTOMA_BEARER_TOKEN", "eval-token-not-real")
        mp.setattr(al, "_sleep", lambda s: obs["sleeps"].append(s), raising=False)
        skill_runner._agent_def_cache.clear()

        def post(url, **kwargs):
            obs["requests"] += 1
            outcome = str(script.pop(0) if len(script) > 1 else script[0])
            if outcome == "timeout":
                raise httpx.ReadTimeout("timed out (private detail)")
            if outcome == "connect":
                raise httpx.ConnectError("refused (private detail)")
            if outcome == "none":
                return _Resp({"entities": []})
            if outcome == "real":
                return _Resp(REAL)
            return _Resp(status=int(outcome))

        mp.setattr(httpx, "post", post)

        async def no_spawn(*args, **kwargs):
            obs["spawned"].append(1)
            raise AssertionError("spawn")

        mp.setattr(asyncio, "create_subprocess_exec", no_spawn)
        mp.setattr(skill_runner, "_provider_binaries", lambda: {"codex": "/bin/true", "claude": "/bin/true"})
        mp.setattr(skill_runner, "cool_down", lambda p: obs["cooled"].append(p))

        if mutation == "reinstate_stub":
            real_load = al.AgentLoader.load

            def reinstated(self, *, allow_undefined=False):
                try:
                    return real_load(self, allow_undefined=allow_undefined)
                except al.AgentDefinitionUnavailable as exc:
                    return self._undefined_definition(exc.code)

            mp.setattr(al.AgentLoader, "load", reinstated)
        if mutation == "no_retry":
            mp.setattr(al, "LOAD_BACKOFFS_S", ())
        if mutation == "retry_rejected":
            mp.setattr(al, "_RETRYABLE_LOAD_CODES", al._RETRYABLE_LOAD_CODES | {"rejected"})

        kind = scenario["kind"]
        try:
            if kind == "dispatch":
                obs["crash"] = ""
                mp.setattr(skill_runner, "_provider_binaries", lambda: {} if scenario.get("stop_after_definition") else {"codex": "/bin/true", "claude": "/bin/true"})
                try:
                    obs["result"] = asyncio.run(
                        skill_runner.run_skill("pavo", "a task", role="pavo")
                        if scenario.get("through_run_skill")
                        else skill_runner._run_skill_once("pavo", "a task", provider="codex", role="pavo")
                    )
                except Exception as exc:  # noqa: BLE001 — a spawn past a refusal is a finding
                    obs["crash"] = type(exc).__name__
                obs["cached"] = skill_runner._agent_def_cache.get("pavo")
            elif kind == "startup":
                try:
                    obs["definition"] = al.load_agent_definition_or_exit("pavo")
                    obs["exit_code"] = None
                except SystemExit as exc:
                    obs["exit_code"] = exc.code
            else:  # pragma: no cover
                raise AssertionError(kind)
        finally:
            for lg, level in zip(loggers, prior_levels):
                lg.removeHandler(capture)
                lg.setLevel(level)
            skill_runner._agent_def_cache.clear()
    obs["log"] = capture.lines
    return obs


def check(scenario: dict, obs: dict) -> list[str]:
    exp = scenario["expect"]
    bad: list[str] = []

    def want(name, actual, expected):
        if actual != expected:
            bad.append(f"{name}: expected {expected!r}, got {actual!r}")

    result = obs.get("result")
    if obs.get("crash"):
        bad.append(f"the dispatch went on to launch a process ({obs['crash']})")
    if "refused" in exp:
        want("dispatch refused for the definition", bool(result and result.definition_unavailable), exp["refused"])
    if exp.get("error_prefix") and result is not None:
        if not result.error.startswith(exp["error_prefix"]):
            bad.append(f"error does not start with {exp['error_prefix']!r}: {result.error[:80]!r}")
    if "past_definition_stage" in exp:
        want(
            "dispatch got past the definition stage",
            bool(result is not None and not result.definition_unavailable),
            exp["past_definition_stage"],
        )
    if "loaded_prompt" in exp:
        cached = obs.get("cached")
        want("cached definition prompt", cached.prompt_markdown if cached else None, exp["loaded_prompt"])
    if "loaded_tools" in exp:
        cached = obs.get("cached")
        want("cached definition tools", cached.tools if cached else None, exp["loaded_tools"])
    if exp.get("no_stand_in"):
        cached = obs.get("cached")
        if cached is not None and (cached.is_stub or cached.tools == ["*"] or not cached.prompt_markdown):
            bad.append("a stand-in definition (empty prompt / wildcard tools) was used")
    if "requests" in exp:
        want("requests to Neotoma", obs["requests"], exp["requests"])
    if "sleeps" in exp:
        want("backoff waits", obs["sleeps"], exp["sleeps"])
    if exp.get("nothing_spawned"):
        want("processes spawned", obs["spawned"], [])
    if exp.get("no_cooldown"):
        want("providers cooled down", obs["cooled"], [])
    if "attempted_providers" in exp and result is not None:
        want("providers attempted", len(result.attempted_providers), exp["attempted_providers"])
    if "exit_code" in exp:
        want("daemon exit code", obs.get("exit_code"), exp["exit_code"])
    log = "\n".join(obs["log"]) + ("\n" + result.error if result is not None else "")
    for needle in exp.get("log_excludes", []):
        if needle in log:
            bad.append(f"the log or error leaks {needle!r}")
    for needle in exp.get("log_contains", []):
        if needle not in log:
            bad.append(f"the log lacks {needle!r}: {log[:300]!r}")
    return bad
