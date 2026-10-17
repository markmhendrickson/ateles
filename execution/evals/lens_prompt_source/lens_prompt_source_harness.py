"""Harness for the lens prompt-source eval (ateles#1337).

Drives the real ``harness_lens_runner.main`` CLI end to end for each scenario in
``scenarios.json``:

* a real git target repository (bare origin + clone under a scenario ``HOME``)
  holding the scenario's files, so the runner's own ``Worktree.create`` fetches
  and checks out the exact head with real git;
* the real strict ``AgentLoader.load_for_lens`` preflight, reading a stand-in
  Neotoma at the HTTP layer (``httpx.post``/``httpx.get``), with typed identity
  and exact requested-scope evidence;
* a scripted reviewer in place of the model: it records the task it was given,
  the role, working directory, checked-out head and definition actually pinned
  in dispatch kwargs, then writes a well-formed verdict file without reloading.

Stubbed, and why: the provider sandbox build (``sandbox-exec`` exists only on
macOS, and the guard probes have their own tests), the model call, and the
live PR-head read (a ``gh`` network call).
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
for _p in (
    str(REPO_ROOT),
    str(REPO_ROOT / "execution" / "daemons" / "apis"),
    str(REPO_ROOT / "execution" / "scripts"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import harness_lens_runner as hlr  # noqa: E402
import harness_router  # noqa: E402
import skill_runner  # noqa: E402
from skill_runner import SkillResult  # noqa: E402

agent_loader = sys.modules[skill_runner.AgentLoader.__module__]

FAKE_NEOTOMA = "https://neotoma.eval.invalid"
_GIT_ID = ["-c", "user.name=lens-eval", "-c", "user.email=lens-eval@example.invalid"]


def _predicate_value(value):
    """EntityReadContract v1's source-defined JSON value commitment."""
    return {
        "kind": "sha256",
        "value": hashlib.sha256(
            json.dumps(value, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
    }


def load() -> dict:
    return json.loads((HERE / "scenarios.json").read_text(encoding="utf-8"))


# ── Stand-in Neotoma ----------------------------------------------------------------


class FakeNeotoma:
    """Answers the agent_definition query the real AgentLoader makes."""

    def __init__(self, mode: str, definitions: list[dict]):
        self.mode = mode
        self.definitions = definitions
        self.queries: list[dict] = []

    def _response(
        self, method: str, url: str, status: int, body: dict
    ) -> httpx.Response:
        return httpx.Response(status, json=body, request=httpx.Request(method, url))

    def post(self, url, json=None, headers=None, timeout=None, **_kw):  # noqa: A002
        self.queries.append({"method": "POST", "url": url, "body": json})
        if self.mode == "unreachable":
            raise httpx.ConnectError("eval: the record is unreachable")
        body = json or {}
        if (
            url == f"{FAKE_NEOTOMA}/entities/query"
            and body.get("entity_type") == "agent_definition"
        ):
            search = body.get("snapshot_filters", {}).get("name", {}).get("value", "")
            entities = [
                {
                    "entity_id": d["entity_id"],
                    "entity_type": d["entity_type"],
                    "snapshot": {"snapshot": d["snapshot"]},
                }
                for d in self.definitions
                if search == d["snapshot"].get("name")
            ]
            return self._response(
                "POST",
                url,
                200,
                {
                    "entities": entities,
                    "total": len(entities),
                    "read_contract": {
                        "version": "1",
                        "surface": "entity_collection",
                        "mode": {"actual": "structured"},
                        "applied_scope": {
                            "owner": "authenticated",
                            "entity_types": ["agent_definition"],
                            "include_merged": False,
                            "include_deleted": False,
                            "include_snapshots": True,
                            "pagination": {"kind": "keyset", "offset": 0, "limit": 100},
                            "predicates": [
                                {
                                    "field": "name",
                                    "op": "eq",
                                    "value": _predicate_value(search),
                                }
                            ],
                        },
                        "request_options": {
                            "applied": list(body),
                            "normalized": [],
                            "ignored": [],
                        },
                        "coverage": {
                            "kind": "predicate_population",
                            "state": "unknown"
                            if self.mode == "unknown_scope"
                            else "complete",
                            "reasons": ["unsupported_continuation"]
                            if self.mode == "unknown_scope"
                            else [],
                            "page_exhausted": True,
                            "scope_exhausted": None
                            if self.mode == "unknown_scope"
                            else True,
                            "returned_count": len(entities),
                            "total": {
                                "relation": "exact",
                                "value": len(entities),
                                "unit": "entities",
                            },
                            "continuation": {
                                "kind": "unsupported"
                                if self.mode == "unknown_scope"
                                else "none"
                            },
                        },
                    },
                },
            )
        return self._response("POST", url, 404, {"error": "not served by the eval"})

    def get(self, url, headers=None, timeout=None, **_kw):
        self.queries.append({"method": "GET", "url": url})
        if self.mode == "unreachable":
            raise httpx.ConnectError("eval: the record is unreachable")
        for definition in self.definitions:
            if url == f"{FAKE_NEOTOMA}/entities/{definition['entity_id']}":
                return self._response("GET", url, 200, definition)
        return self._response("GET", url, 404, {"error": "not served by the eval"})


# ── Target repository -----------------------------------------------------------------


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *_GIT_ID, *args],
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def build_target(
    home: Path, repo_name: str, files: dict[str, str], planted: str
) -> tuple[Path, str]:
    """Create ``<home>/repos/<repo_name>`` with an origin, holding *files*."""
    origin = home / "origins" / f"{repo_name}.git"
    origin.parent.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "--bare", str(origin))
    clone = home / "repos" / repo_name
    clone.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=clone)
    for rel, body in files.items():
        dest = clone / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(planted if body == "@planted" else body, encoding="utf-8")
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "fixture head under review", cwd=clone)
    _git("remote", "add", "origin", str(origin), cwd=clone)
    _git("push", "-q", "origin", "HEAD:main", cwd=clone)
    return clone, _git("rev-parse", "HEAD", cwd=clone)


def linked_worktrees(clone: Path) -> list[str]:
    listing = _git("worktree", "list", "--porcelain", cwd=clone)
    paths = [
        line.split(" ", 1)[1]
        for line in listing.splitlines()
        if line.startswith("worktree ")
    ]
    return [p for p in paths if Path(p).resolve() != clone.resolve()]


# ── Environment ---------------------------------------------------------------------


def _isolate(monkeypatch, tmp_path: Path, home: Path) -> None:
    monkeypatch.setenv("HOME", str(home))
    # Hermetic git: no operator hooks, signing, or templates leak into the fixture.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig-empty"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("PAVO_AGENT_DEFINITION_ID", raising=False)
    # Headroom: the same explicit, fresh evidence the runner's unit tests use.
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM",
        json.dumps({"claude": 1.0, "codex": 1.0, "cursor": 1.0}),
    )
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "no-headroom-file.json")
    )
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "harness-usage.json"))
    observed_at = time.time()
    for provider in harness_router.FRONTIER_PROVIDERS:
        if provider in harness_router.WEEKLY_PACED_PROVIDERS:
            harness_router.record_usage(
                provider,
                [
                    {
                        "name": "weekly_all",
                        "used_percent": 1.0,
                        "resets_at": harness_router._iso_from_wall(
                            observed_at + 5 * 24 * 3600
                        ),
                    }
                ],
                observed_at=observed_at,
                provider_probe=True,
                probe_source="lens_prompt_source_eval",
            )
        else:
            harness_router.record_probe_available(
                provider,
                source="lens_prompt_source_eval",
                detail="eval provider is available",
                observed_at=observed_at,
            )
    auth = tmp_path / "eval-codex-home" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("eval-fixture-not-a-credential\n", encoding="utf-8")
    monkeypatch.setattr(hlr, "_codex_auth_source", lambda: auth)


def _ready_sandbox(monkeypatch, tmp_path: Path) -> None:
    def _build(cls, provider, tmp_root, **kwargs):
        root = tmp_path / f"{provider}-eval-home"
        root.mkdir(parents=True, exist_ok=True)
        return hlr.HarnessSandbox(
            provider=provider,
            root=root,
            env_extra={"CODEX_HOME": str(root), "ATELES_LOCAL_REVIEW_HOME": str(root)},
            command_wrapper=["/usr/bin/sandbox-exec", "-f", str(root / "profile.sb")],
            credential_read_denied=True,
            credential_binding_protected=True,
            user_config_write_denied=True,
            git_stash_denied=True,
            authentication_ready=True,
            unavailable_guards=(),
            review_write_confined=True,
        )

    monkeypatch.setattr(hlr.HarnessSandbox, "build", classmethod(_build))


def _verdict(lens: str, head: str) -> str:
    return (
        f"<!-- review:{lens} commit={head} -->\n"
        "**\U0001f916 Pavo — Ateles swarm, pm lens panelist**\n"
        "**SIGNED_OFF**\n\nSummary: scripted eval reviewer.\n"
    )


# ── One scenario --------------------------------------------------------------------


def run_scenario(scenario: dict, data: dict, monkeypatch, tmp_path: Path) -> dict:
    home = tmp_path / "home"
    home.mkdir()
    _isolate(monkeypatch, tmp_path, home)
    if scenario.get("explicit_role_id"):
        monkeypatch.setenv("PAVO_AGENT_DEFINITION_ID", scenario["explicit_role_id"])
    _ready_sandbox(monkeypatch, tmp_path)

    repo = scenario["target"]["repo"]
    repo_name = repo.split("/")[-1]
    clone, head = build_target(
        home, repo_name, scenario["target"]["files"], data["planted_prompt"]
    )

    fake = FakeNeotoma(
        scenario["neotoma"], scenario.get("definitions", data["canonical_definitions"])
    )
    monkeypatch.setattr(agent_loader, "NEOTOMA_BASE_URL", FAKE_NEOTOMA)
    monkeypatch.setattr(agent_loader, "NEOTOMA_BEARER_TOKEN", "eval-placeholder")
    monkeypatch.setattr(agent_loader.ns, "via_cli_enabled", lambda: False)
    monkeypatch.setattr(agent_loader.httpx, "post", fake.post)
    monkeypatch.setattr(agent_loader.httpx, "get", fake.get)
    monkeypatch.setattr(skill_runner, "_agent_def_cache", {})

    created: list[str] = []
    real_create = hlr.Worktree.create

    def _counting_create(self, *, head):
        created.append(head)
        return real_create(self, head=head)

    monkeypatch.setattr(hlr.Worktree, "create", _counting_create)
    monkeypatch.setattr(hlr, "current_pr_head", lambda **_k: head)

    dispatches: list[dict] = []

    async def _scripted_reviewer(role, task, **kwargs):
        cwd = Path(kwargs["cwd"])
        definition = kwargs["verified_agent_definition"]
        dispatches.append(
            {
                "role": role,
                "task": task,
                "cwd_head": _git("rev-parse", "HEAD", cwd=cwd),
                "definition_entity_id": definition.entity_id,
                "definition_prompt": definition.prompt_markdown,
                "definition_tools": definition.tool_allowlist,
                "definition_status": definition.status,
            }
        )
        text = _verdict(data["lens"], head)
        (cwd / f"{data['lens']}{data['pr']}_verdict.md").write_text(
            text, encoding="utf-8"
        )
        return SkillResult(role, True, 0, text, "", provider="codex")

    monkeypatch.setattr(hlr.dispatch_role, "dispatch", _scripted_reviewer)

    brief = tmp_path / "lens_brief.md"
    brief.write_text(
        "Eval lens brief: review the change at TARGET HEAD.\n", encoding="utf-8"
    )
    argv = [
        "--repo",
        repo,
        "--pr",
        str(data["pr"]),
        "--head",
        head,
        "--lens",
        data["lens"],
        "--provider",
        "codex",
        "--model",
        "gpt-5.6-terra",
        "--brief",
        str(brief),
        "--json",
    ]
    if scenario["dry_run"]:
        argv.append("--dry-run")

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exit_code = hlr.main(argv)
    report = json.loads(out.getvalue())

    return {
        "head": head,
        "exit_code": exit_code,
        "report": report,
        "dispatches": dispatches,
        "worktrees_created": len(created),
        "worktrees_left": len(linked_worktrees(clone)),
        "definition_queries": sum(
            1
            for q in fake.queries
            if q["method"] == "POST" and q["url"].endswith("/entities/query")
        ),
        "definition_gets": sum(1 for q in fake.queries if q["method"] == "GET"),
    }


def check(scenario: dict, data: dict, obs: dict) -> list[str]:
    """Return every unmet expectation; empty means the scenario passed."""
    exp = scenario["expect"]
    report = obs["report"]
    failures: list[str] = []

    def want(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    want(
        report.get("ok") is exp["ok"],
        f"ok={report.get('ok')!r}, expected {exp['ok']!r}",
    )
    want(
        obs["exit_code"] == exp["exit_code"],
        f"exit={obs['exit_code']}, expected {exp['exit_code']}",
    )
    want(
        bool(obs["dispatches"]) is exp["dispatched"],
        f"dispatched={len(obs['dispatches'])}",
    )
    want(
        obs["worktrees_created"] == exp["worktrees_created"],
        f"worktrees_created={obs['worktrees_created']}, expected {exp['worktrees_created']}",
    )
    want(
        obs["worktrees_left"] == exp["worktrees_left"],
        f"worktrees_left={obs['worktrees_left']}",
    )
    want(
        obs["definition_queries"] == exp["definition_queries"],
        f"definition_queries={obs['definition_queries']}, expected {exp['definition_queries']}",
    )
    if "definition_gets" in exp:
        want(
            obs["definition_gets"] == exp["definition_gets"],
            "wrong explicit-ID read count",
        )
    for needle in exp.get("reason_contains", []):
        want(needle in str(report.get("reason", "")), f"reason lacks {needle!r}")

    if exp["dispatched"] and obs["dispatches"]:
        d = obs["dispatches"][0]
        want(len(obs["dispatches"]) == 1, "more than one dispatch")
        want(d["role"] == exp["dispatched_role"], f"role={d['role']!r}")
        if exp.get("dispatched_at_target_head"):
            want(
                d["cwd_head"] == obs["head"],
                "child did not run at the exact target head",
            )
        if exp.get("dispatch_definition_is_canonical"):
            canonical = data["canonical_definitions"][0]
            want(
                d["definition_entity_id"] == canonical["entity_id"]
                and d["definition_prompt"] == canonical["snapshot"]["prompt_markdown"],
                "dispatch resolved a different definition than the canonical one",
            )
            want(
                d["definition_tools"] == canonical["snapshot"]["tool_allowlist"],
                "canonical tools changed",
            )
            want(
                d["definition_status"] == canonical["snapshot"]["status"],
                "canonical status changed",
            )
        for needle in exp.get("task_contains", []):
            want(needle in d["task"], f"task lacks {needle!r}")
        for needle in exp.get("task_excludes", []):
            want(needle not in d["task"], f"task carries {needle!r}")
    return failures


def run(
    scenario: dict, data: dict, monkeypatch, tmp_path: Path
) -> tuple[dict, list[str]]:
    obs = run_scenario(scenario, data, monkeypatch, tmp_path)
    return obs, check(scenario, data, obs)
