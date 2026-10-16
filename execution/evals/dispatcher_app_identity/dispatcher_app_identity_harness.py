"""Drive the Apis dispatcher's GitHub write paths and report which credential
each request carried.

Each scenario in ``scenarios.json`` runs the REAL ``SwarmDispatcher`` methods
(``_mark_pipeline_inflight``, ``_mirror_spec_to_issue``,
``_post_checkpoint_verdict_comment``, ``_surface_unverified_signed_off_gates``).
Only the edges are stubbed: the HTTP transport and the App token mint, because
an offline lane has neither network nor a private key.

``run_scenario`` returns what was observed; ``check`` compares it to the
scenario's ``expect`` block. ``mutation`` re-runs a scenario with one piece of
the change removed so a test can prove the checks go red.

Token values here are fixtures. The scenarios are synthetic and public-safe.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
APIS_DIR = REPO_ROOT / "execution" / "daemons" / "apis"
for _p in (str(REPO_ROOT), str(APIS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import swarm_dispatch as sd  # noqa: E402

MUTATIONS = ("none", "pat_first", "guard_accepts_instance_token")
REPO = "owner/repo"
APP_TOKEN = "eval-app-installation-token"
PAT = "eval-agent-pat"
BOT = "ateles-agents[bot]"
HEAD = "a" * 40
LABELS = {f"Bearer {APP_TOKEN}": "app", f"Bearer {PAT}": "pat", "": "none"}


def load() -> dict:
    return json.loads((HERE / "scenarios.json").read_text())


class _Notifier:
    def send(self, *a, **k):
        pass

    def clear_dedupe(self, key):
        pass


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


def _kind(path: str) -> str:
    if path == "/user":
        return "user"
    if path.endswith("/comments"):
        return "issue_comment"
    return "issue"


def run_scenario(scenario: dict, mutation: str = "none") -> dict:
    assert mutation in MUTATIONS
    with pytest.MonkeyPatch.context() as mp:
        return asyncio.run(_run(scenario, mutation, mp))


async def _run(scenario: dict, mutation: str, mp: pytest.MonkeyPatch) -> dict:
    sd._clear_app_write_token_cache()
    mp.delenv("NEOTOMA_AGENT_PAT", raising=False)
    mp.delenv("GITHUB_TOKEN", raising=False)
    if scenario["pat"]:
        mp.setenv("ATELES_AGENT_PAT", PAT)
    else:
        mp.delenv("ATELES_AGENT_PAT", raising=False)

    app = scenario["app"]
    mint_calls: list[threading.Thread] = []
    mp.setattr(sd, "_app_write_configured", lambda: app != "unconfigured")
    mp.setattr(sd._github_app_token, "bot_login", lambda prefix=None: BOT)

    def _mint(repo=None, *, prefix=None):
        mint_calls.append(threading.current_thread())
        if app == "fails":
            raise sd._github_app_token.AppTokenError("HTTP 500")
        if app == "slow":
            time.sleep(0.15)
        return sd._github_app_token.InstallationToken(
            token=APP_TOKEN,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            installation_id="1",
            permissions={},
        )

    mp.setattr(sd._github_app_token, "installation_token_info", _mint)
    if scenario["cache"] == "expired":
        sd._APP_WRITE_TOKENS[REPO] = ("expired-fixture", datetime.now(timezone.utc) - timedelta(minutes=1))
    if mutation == "pat_first":  # the pre-change behaviour: the PAT, never the App
        mp.setattr(sd, "_app_write_token_for_sync", lambda repo: None)
        mp.setattr(sd, "_ensure_app_write_token", _no_app)

    if mutation == "guard_accepts_instance_token":  # the round-2 defect
        async def _guard_passes(self, repo):
            return self.config.github_token

        mp.setattr(sd.SwarmDispatcher, "_github_write_token", _guard_passes)

    observed: list[list[str]] = []
    respond = scenario.get("respond") or {}
    comments = [
        {
            "body": sd.gate_cleared_unverified_marker("ux", HEAD) if c["marker"] == "gate_cleared_unverified" else "",
            "user": {"login": c["login"]},
        }
        for c in scenario.get("existing_comments", [])
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        observed.append([request.method, _kind(request.url.path), LABELS.get(auth, "other")])
        if respond.get("status"):
            return httpx.Response(respond["status"], text=respond.get("text", ""))
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "ateles-agent"})
        if request.method == "GET" and request.url.path.endswith("/comments"):
            return httpx.Response(200, json=comments)
        if request.method == "GET":
            return httpx.Response(200, json={"body": "reporter text"})
        return httpx.Response(201, json={})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    mp.setattr(sd.httpx, "AsyncClient", lambda timeout=30: real(transport=transport, timeout=timeout))

    capture = _Capture()
    sd.log.addHandler(capture)
    old_level = sd.log.level
    sd.log.setLevel(logging.DEBUG)
    ticks = 0
    stop = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    tick_task = asyncio.create_task(ticker())
    d = sd.SwarmDispatcher(notifier=_Notifier())
    if scenario.get("instance_token"):
        d.config.github_token = "eval-instance-token"
    trigger = sd.SwarmTrigger(
        kind="issue_opened", repository=REPO, number=11, title="t", body="",
        author="someone", html_url="https://github.com/owner/repo/issues/11",
        delivery_id="eval", action="opened", head_sha=HEAD,
    )
    state = sd.SpecState(repo=REPO, issue_number=11, title="t", sections={"problem": "a problem"})
    try:
        for action in scenario["actions"]:
            if action == "mark_inflight":
                await d._mark_pipeline_inflight(trigger)
            elif action == "mirror_spec":
                await d._mirror_spec_to_issue(trigger, state)
            elif action == "checkpoint_comment":
                await d._post_checkpoint_verdict_comment(trigger, "verdict text")
            elif action == "gate_notice":
                await d._surface_unverified_signed_off_gates(trigger, None, {"ux"}, HEAD)
            else:
                raise AssertionError(f"unknown action {action}")
    finally:
        stop.set()
        await tick_task
        sd.log.removeHandler(capture)
        sd.log.setLevel(old_level)

    messages = [r.getMessage() for r in capture.records]
    return {
        "requests": observed,
        "warnings": [r.getMessage() for r in capture.records if r.levelno >= logging.WARNING],
        "all_logs": messages,
        "loop_ticks": ticks,
        "mint_threads_off_loop": all(t is not threading.main_thread() for t in mint_calls),
    }


async def _no_app(repo):
    return None


def check(scenario: dict, obs: dict) -> list[str]:
    """Why the observation does not meet the scenario's expectation ([] = met)."""
    expect = scenario["expect"]
    problems: list[str] = []
    if "requests" in expect and obs["requests"] != expect["requests"]:
        problems.append(f"requests {obs['requests']} != expected {expect['requests']}")
    warnings = "\n".join(obs["warnings"])
    for needle in expect.get("warnings_contain", []):
        if needle not in warnings:
            problems.append(f"no WARNING containing {needle!r}")
    for needle in expect.get("warnings_absent", []):
        if needle in warnings:
            problems.append(f"unexpected WARNING containing {needle!r}")
    if obs["loop_ticks"] < expect.get("loop_ticks_min", 0):
        problems.append(f"event loop stalled: {obs['loop_ticks']} ticks")
    # Every scenario: a write is authenticated or skipped, never sent bare.
    if any(r[2] != "app" and r[2] != "pat" for r in obs["requests"]):
        problems.append(f"a request was sent without a known credential: {obs['requests']}")
    # Every scenario: no credential value in any log line.
    blob = "\n".join(obs["all_logs"])
    for secret in (APP_TOKEN, PAT, "expired-fixture", "eval-instance-token"):
        if secret in blob:
            problems.append("a credential value appeared in a log line")
    if not obs["mint_threads_off_loop"]:
        problems.append("the App token was minted on the event loop thread")
    return problems
