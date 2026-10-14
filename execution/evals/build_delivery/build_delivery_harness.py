"""Drive every launch path of a delivery-bearing Cicada build to a stub child and
report what happened in the world, not what arguments were passed.

Each scenario in ``scenarios.json`` runs through the REAL launch code: the swarm
build handoff (``SwarmDispatcher._open_implementation_pr``) and the task path
(``apis.handle_event`` -> ``dispatch_task`` -> ``_spawn_harness_skill``), both of
which call the REAL ``skill_runner.run_skill`` and build the REAL ``codex``
command. Only the edges are stubbed:

* ``codex`` is a stub executable that stands in for the child. It emulates the
  one thing that matters about ``--sandbox workspace-write``: it may write only
  its working directory and the ``--add-dir`` roots, and it has no network unless
  ``-c sandbox_workspace_write.network_access=true`` was passed. Inside those
  limits it does a REAL ``git commit`` and a REAL ``git push`` to a local bare
  repository standing in for origin, then calls a stub ``gh pr create`` that
  records the PR. When the sandbox would deny it, it prints the same git error a
  denied child prints and exits 0, exactly as the canary child did.
* GitHub is the bare repository plus the PR record; the clone says its origin is
  the real repository URL and only the transport (``url.<bare>.insteadOf``) is
  local.

What is asserted is therefore an EFFECT: the commit is on the remote's branch and
the PR is on record. ``mutation`` re-runs a scenario with one delivery grant
removed so a test can prove the checks go red at that effect.

Synthetic and public-safe: no operator data, no real token, no network.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
APIS_DIR = REPO_ROOT / "execution" / "daemons" / "apis"
for _p in (str(REPO_ROOT), str(APIS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import apis  # noqa: E402
import build_delivery  # noqa: E402
import model_tiering  # noqa: E402
import skill_runner  # noqa: E402
import swarm_dispatch  # noqa: E402
from github_gateway import SwarmTrigger  # noqa: E402
from issue_spec import SpecState  # noqa: E402
from lib.daemon_runtime.agent_loader import AgentDefinition  # noqa: E402
from lib.daemon_runtime.sse_client import NeotomaEvent  # noqa: E402
from unroutable_ledger import UnroutableLedger  # noqa: E402

MUTATIONS = ("none", "no_grants", "no_network", "no_git_roots")

REPO = "eval-owner/eval-repo"
ISSUE = 1448
SCOPED_TOKEN = "eval-scoped-token-not-real"
AMBIENT_TOKEN = "eval-ambient-token-not-real"


def load() -> dict:
    return json.loads((HERE / "scenarios.json").read_text())


# ── the stub child ───────────────────────────────────────────────────────────

_CODEX_STUB = textwrap.dedent(
    '''\
    #!{python}
    import json, os, re, subprocess, sys, time
    from pathlib import Path

    LAUNCHES = Path({launches!r})
    CONFIG = Path({config!r})
    GH = {gh!r}
    REPO = {repo!r}
    SCOPED = {scoped!r}

    argv = sys.argv[1:]
    prompt = sys.stdin.read()
    cfg = json.loads(CONFIG.read_text())

    def values(flag):
        return [argv[i + 1] for i, a in enumerate(argv) if a == flag and i + 1 < len(argv)]

    workdir = (values("--cd") or [os.getcwd()])[0]
    add_dirs = values("--add-dir")
    network = "sandbox_workspace_write.network_access=true" in values("-c")
    sandbox = (values("--sandbox") or [""])[0]
    marker = re.search(r"build-run:\\s*([0-9a-f]{{16}})", prompt)
    seen = {{
        "argv": argv, "cwd": os.getcwd(), "workdir": workdir, "add_dirs": add_dirs,
        "network": network, "sandbox": sandbox,
        "token_scoped": os.environ.get("GITHUB_TOKEN") == SCOPED
        and os.environ.get("GH_TOKEN") == SCOPED,
        "run_id": marker.group(1) if marker else None,
        "in_worktree_prompt": "YOUR WORKTREE" in prompt,
    }}
    with LAUNCHES.open("a") as fh:
        fh.write(json.dumps(seen) + "\\n")

    if cfg.get("hold"):
        Path(cfg["held_file"]).write_text(json.dumps({{"run_id": seen["run_id"]}}))
        deadline = time.time() + 60
        while not Path(cfg["release_file"]).exists() and time.time() < deadline:
            time.sleep(0.05)
    if cfg.get("crash"):
        sys.exit(3)

    def say(text):
        print(text, file=sys.stderr)

    def git(*args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=cwd or workdir, capture_output=True, text=True,
            env={{**os.environ, "GIT_AUTHOR_NAME": "stub", "GIT_AUTHOR_EMAIL": "s@example.invalid",
                  "GIT_COMMITTER_NAME": "stub", "GIT_COMMITTER_EMAIL": "s@example.invalid"}},
        )

    top = git("rev-parse", "--absolute-git-dir")
    if top.returncode != 0:
        say("fatal: not a git repository (or any of the parent directories): .git")
        sys.exit(0)
    gitdir = Path(top.stdout.strip()).resolve()
    common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()).resolve()

    # workspace-write: the working directory and the --add-dir roots, nothing else.
    roots = [Path(workdir).resolve()] + [Path(d).resolve() for d in add_dirs]
    def writable(path):
        return any(path == r or r in path.parents for r in roots)
    if not writable(gitdir):
        say("fatal: Unable to create '%s/index.lock': Operation not permitted" % gitdir)
        sys.exit(0)
    if not writable(common):
        say("error: unable to create temporary file: Operation not permitted")
        sys.exit(0)
    if not writable(Path(workdir).resolve()):
        say("error: cannot write the working directory")
        sys.exit(0)

    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    (Path(workdir) / "delivered.txt").write_text("the build\\n")
    git("add", "delivered.txt")
    commit = git("commit", "-m", "Build the change")
    if commit.returncode != 0:
        say(commit.stderr)
        sys.exit(0)
    sha = git("rev-parse", "HEAD").stdout.strip()
    with LAUNCHES.open("a") as fh:
        fh.write(json.dumps({{"committed": sha, "branch": branch}}) + "\\n")

    if not network:
        say("fatal: unable to access 'https://github.com/%s.git/': Could not resolve host: github.com" % REPO)
        sys.exit(0)

    push = git("push", "origin", "HEAD:refs/heads/%s" % branch)
    if push.returncode != 0:
        say(push.stderr)
        sys.exit(0)
    pr = subprocess.run(
        [sys.executable, GH, "pr", "create", "--repo", REPO, "--head", branch,
         "--title", "Build the change", "--body", "Closes #{issue}"],
        capture_output=True, text=True, env=os.environ,
    )
    print(pr.stdout)
    '''
)

_GH_STUB = textwrap.dedent(
    '''\
    #!{python}
    import json, sys
    from pathlib import Path

    RECORD = Path({record!r})
    args = sys.argv[1:]
    if args[:2] != ["pr", "create"]:
        sys.exit(2)
    def opt(flag):
        return args[args.index(flag) + 1] if flag in args else ""
    existing = RECORD.read_text().splitlines() if RECORD.exists() else []
    number = len(existing) + 1
    entry = {{"number": number, "repo": opt("--repo"), "head": opt("--head"),
              "title": opt("--title"), "body": opt("--body")}}
    with RECORD.open("a") as fh:
        fh.write(json.dumps(entry) + "\\n")
    print("https://github.com/%s/pull/%d" % (entry["repo"], number))
    '''
)


# ── the world ────────────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    out = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "eval",
            "GIT_AUTHOR_EMAIL": "eval@example.invalid",
            "GIT_COMMITTER_NAME": "eval",
            "GIT_COMMITTER_EMAIL": "eval@example.invalid",
        },
    )
    if check and out.returncode != 0:
        raise RuntimeError(f"git {args}: {out.stderr}")
    return out.stdout.strip()


class World:
    """A bare 'origin', a clone that says it is the real repository, and the stubs."""

    def __init__(self, root: Path, *, origin: str = "match", clone: bool = True):
        self.root = root
        self.bare = root / "origin.git"
        self.clone = root / "repos" / "eval-repo"
        self.worktrees = self.clone.parent / "eval-repo-worktrees"
        self.daemon_cwd = root / "daemon-cwd"
        self.launches_file = root / "launches.jsonl"
        self.config_file = root / "child-config.json"
        self.held_file = root / "held.json"
        self.release_file = root / "release"
        self.pr_file = root / "prs.jsonl"
        self.bin = root / "bin"
        for d in (self.bin, self.daemon_cwd, self.clone.parent):
            d.mkdir(parents=True, exist_ok=True)
        self.bare.mkdir()
        _git(self.bare, "init", "--bare", "-b", "main")
        seed = root / "seed"
        seed.mkdir()
        _git(seed, "init", "-b", "main")
        (seed / "README.md").write_text("seed\n")
        _git(seed, "add", "README.md")
        _git(seed, "commit", "-m", "seed")
        _git(seed, "remote", "add", "origin", str(self.bare))
        _git(seed, "push", "origin", "main")
        self.base_sha = _git(seed, "rev-parse", "HEAD")
        if clone:
            subprocess.run(
                ["git", "clone", "-q", str(self.bare), str(self.clone)],
                check=True,
                capture_output=True,
            )
            url = (
                f"https://github.com/{REPO}.git"
                if origin == "match"
                else "https://github.com/someone-else/other-repo.git"
            )
            _git(self.clone, "remote", "set-url", "origin", url)
            # Only the transport is local: the clone still says it is REPO.
            _git(self.clone, "config", f"url.{self.bare}.insteadOf", url)
        self.codex = self.bin / "codex"
        self.gh = self.bin / "gh"
        self.codex.write_text(
            _CODEX_STUB.format(
                python=sys.executable,
                launches=str(self.launches_file),
                config=str(self.config_file),
                gh=str(self.gh),
                repo=REPO,
                scoped=SCOPED_TOKEN,
                issue=ISSUE,
            )
        )
        self.gh.write_text(_GH_STUB.format(python=sys.executable, record=str(self.pr_file)))
        self.codex.chmod(0o755)
        self.gh.chmod(0o755)
        self.configure_child()

    def configure_child(self, **cfg) -> None:
        cfg.setdefault("held_file", str(self.held_file))
        cfg.setdefault("release_file", str(self.release_file))
        self.config_file.write_text(json.dumps(cfg))

    # observations
    def launches(self) -> list[dict]:
        if not self.launches_file.exists():
            return []
        rows = [json.loads(line) for line in self.launches_file.read_text().splitlines()]
        return [r for r in rows if "argv" in r]

    def commits(self) -> list[dict]:
        if not self.launches_file.exists():
            return []
        rows = [json.loads(line) for line in self.launches_file.read_text().splitlines()]
        return [r for r in rows if "committed" in r]

    def prs(self) -> list[dict]:
        if not self.pr_file.exists():
            return []
        return [json.loads(line) for line in self.pr_file.read_text().splitlines()]

    def remote_branches(self) -> dict[str, str]:
        out = _git(self.bare, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/")
        rows = [line.split() for line in out.splitlines()]
        return {name: sha for name, sha in rows if name != "main"}

    def worktree_dirs(self) -> list[str]:
        return sorted(p.name for p in self.worktrees.iterdir()) if self.worktrees.exists() else []


# ── launching ────────────────────────────────────────────────────────────────


class _Notifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, priority=None, handler=None, **kwargs):
        self.sent.append(message)

    def clear_dedupe(self, key):
        pass


def _trigger() -> SwarmTrigger:
    return SwarmTrigger(
        kind="issue_opened",
        repository=REPO,
        number=ISSUE,
        title="Build the change",
        body="Please build the change.",
        author="reporter",
        html_url=f"https://github.com/{REPO}/issues/{ISSUE}",
        delivery_id="eval-delivery",
        action="opened",
        labels=[],
    )


def _task_snapshot(**over) -> dict:
    snap = {
        "title": "Build the change for the eval repository",
        "description": f"Implement the approved spec in {REPO}#{ISSUE}. Open a PR.",
        "assigned_to": "cicada",
        "repo": REPO,
        "issue_number": ISSUE,
        "action_type": "open_or_merge_pr",
        "confidence": 0.95,
        "status": "pending",
    }
    snap.update(over)
    return snap


def _created(entity_id: str, snapshot: dict) -> NeotomaEvent:
    return NeotomaEvent(
        event_type="entity_created",
        entity_type="task",
        entity_id=entity_id,
        action="created",
        snapshot=snapshot,
        hydrated=True,
    )


def _strip_grants(real, drop):
    async def _run(skill, prompt, **kw):
        for key in drop:
            kw.pop(key, None)
        return await real(skill, prompt, **kw)

    return _run


async def _wait_for(path: Path, timeout: float = 30.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not path.exists():
        if loop.time() > end:
            raise TimeoutError(f"{path.name} never appeared")
        await asyncio.sleep(0.02)


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)
    background = list(getattr(build_delivery, "_background", ()))
    if background:
        await asyncio.gather(*background)


def run_scenario(scenario: dict, tmp_path: Path, *, mutation: str = "none") -> dict:
    """Run one scenario; return what was observed in the world."""
    assert mutation in MUTATIONS, mutation
    world = World(
        tmp_path,
        origin=(scenario.get("fault") == "origin_mismatch" and "other") or "match",
        clone=scenario.get("fault") != "no_clone",
    )
    world.configure_child(**(scenario.get("child") or {}))
    obs: dict = {"world": world, "statuses": [], "spawns": [], "mid": {}, "notifications": []}

    with pytest.MonkeyPatch.context() as mp:
        _install(mp, world, obs, mutation, scenario)
        asyncio.run(_drive(scenario, world, obs))
    _collect(world, obs)
    return obs


def _install(mp, world: World, obs: dict, mutation: str, scenario: dict) -> None:
    root = world.root
    mp.chdir(world.daemon_cwd)
    mp.setenv(build_delivery.BUILD_CLONES_ENV, f"{REPO}={world.clone}")
    mp.setenv("PATH", f"{world.bin}{os.pathsep}{os.environ.get('PATH', '')}")
    mp.setenv("CICADA_AGENT_PAT", SCOPED_TOKEN)
    mp.setenv("GITHUB_TOKEN", AMBIENT_TOKEN)
    mp.setenv("APIS_HARNESS_PROVIDERS", "codex")
    mp.setenv("APIS_HARNESS_HEADROOM", '{"codex": 1.0}')
    mp.setenv("APIS_HARNESS_HEADROOM_FILE", str(root / "no-headroom.json"))
    mp.setenv("APIS_HARNESS_USAGE_FILE", str(root / "no-usage.json"))
    mp.setenv("APIS_USAGE_GATE", "off")
    mp.setenv("APIS_USAGE_PROBE", "off")
    mp.setenv("APIS_TIER_LEDGER_FILE", str(root / "tier-ledger.jsonl"))
    mp.setenv("NEOTOMA_BASE_URL", "http://eval.invalid")
    mp.setattr(skill_runner, "CODEX_BIN", str(world.codex))
    # The SKILL.md it loads is this checkout's, not whatever ~/repos/ateles holds
    # on the machine running the eval.
    mp.setattr(skill_runner, "ATELES_REPO", REPO_ROOT)
    mp.setattr(skill_runner, "DISPATCH_FAILURE_LOG_DIR", root / "dispatch-failures")
    mp.setattr(skill_runner, "_write_harness_event", lambda **kw: None)
    mp.setattr(model_tiering, "configured_action_policy", lambda: {})
    mp.setattr(model_tiering, "configured_vendor_binding", lambda: {})

    definition = AgentDefinition(
        entity_id="ent_eval_cicada",
        name="cicada",
        prompt_markdown="You are Cicada, the issue worker.",
        tool_allowlist="*",
        aauth_sub="cicada@ateles-swarm",
    )
    loader = MagicMock()
    loader.load.return_value = definition
    mp.setattr(skill_runner, "AgentLoader", lambda *a, **k: loader)

    real = skill_runner.run_skill
    drop = {
        "none": (),
        "no_grants": ("cwd", "include_github_contract", "github_token"),
        "no_network": ("include_github_contract", "github_token"),
        "no_git_roots": (),
    }[mutation]
    wrapped = _strip_grants(real, drop)
    mp.setattr(apis, "run_skill", wrapped)
    mp.setattr(swarm_dispatch, "run_skill", wrapped)
    if mutation == "no_git_roots":
        mp.setattr(skill_runner, "_git_roots_for_sandbox", lambda cwd: [])

    # GitHub is the PR record: the only thing the handoff's PR confirmation sees.
    async def _find_open_pr(self, trigger):
        prs = world.prs()
        return f"https://github.com/{REPO}/pull/{prs[-1]['number']}" if prs else None

    mp.setattr(swarm_dispatch.SwarmDispatcher, "_find_open_pr_for_issue", _find_open_pr)

    # task path edges: Neotoma reads/writes and the gate's policy lookup
    mp.setattr(apis, "_unroutable", UnroutableLedger(path=root / "unroutable.json"))
    mp.setattr(apis, "_created_seen", {})
    mp.setattr(apis, "READINESS_GATE", False)
    mp.setattr(apis, "RUN_CONVERSATIONS", False)
    mp.setattr(
        apis,
        "set_task_status",
        lambda eid, status, **kw: obs["statuses"].append((eid, getattr(status, "value", status))) or True,
    )
    mp.setattr(apis, "write_checkpoint_brief", lambda **kw: "ent_brief")
    mp.setattr(apis, "_release_lifecycle_proven", lambda *a, **k: True)

    async def _no_hydrate(event):
        return None

    mp.setattr(apis, "hydrate_snapshot", _no_hydrate)
    mp.setattr(apis, "fetch_task_snapshot", lambda _id: {"status": "pending"})
    if scenario["path"] == "mid_build":
        # What gets dispatched for the OTHER tasks is not the subject; the child
        # the build itself launched still goes through the real run_skill.
        mp.setattr(apis, "_spawn_harness_skill", _recording_spawn(obs))
    # Fresh registries per scenario (a name the module does not define is skipped).
    for name in ("_inflight", "_runs", "_deferred"):
        mp.setattr(build_delivery, name, {}, raising=False)


def _recording_spawn(obs: dict):
    async def _spawn(skill, entity_id, snapshot, trigger, *a, **kw):
        obs["spawns"].append({"skill": skill, "task": entity_id, "trigger": trigger})

        class _R:
            ok = True
            pr_url = None
            detail = "recorded"

        return _R()

    return _spawn


async def _drive(scenario: dict, world: World, obs: dict) -> None:
    path = scenario["path"]
    notifier = _Notifier()
    obs["notifier"] = notifier

    if path == "handoff":
        dispatcher = swarm_dispatch.SwarmDispatcher(
            notifier,
            swarm_dispatch.DispatchConfig(
                neotoma_token="", github_token="", auto_merge=False, auto_rereview_on_push=False
            ),
        )
        state = SpecState(repo=REPO, issue_number=ISSUE, title="Build the change")
        state.sections = {"eng": "Add delivered.txt."}
        obs["result"] = await dispatcher._open_implementation_pr(_trigger(), state)
        return

    if path == "task":
        snap = _task_snapshot(**(scenario.get("task") or {}))
        obs["result"] = await apis._spawn_harness_skill(
            "cicada", "ent_eval_task_0001", snap, "created", notifier, role="cicada"
        )
        return

    if path == "mid_build":
        dispatcher = swarm_dispatch.SwarmDispatcher(
            notifier,
            swarm_dispatch.DispatchConfig(
                neotoma_token="", github_token="", auto_merge=False, auto_rereview_on_push=False
            ),
        )
        state = SpecState(repo=REPO, issue_number=ISSUE, title="Build the change")
        state.sections = {"eng": "Add delivered.txt."}
        build = asyncio.create_task(dispatcher._open_implementation_pr(_trigger(), state))
        await _wait_for(world.held_file)
        run_id = json.loads(world.held_file.read_text())["run_id"]
        obs["run_id_in_prompt"] = run_id
        for n, event in enumerate(scenario["events"]):
            entity_id = f"ent_eval_mid_{n:04d}"
            task = _task_snapshot(action_type="local_edit", **(event.get("task") or {}))
            if event.get("marker"):
                task["description"] += f"\nbuild-run: {run_id}"
            if event.get("override"):
                await apis.dispatch_task(
                    entity_id, task, trigger="approved", notifier=notifier,
                    snapshot_hydrated=True, gate_override=True,
                )
            else:
                for _ in range(event.get("deliveries", 1)):
                    await apis.handle_event(_created(entity_id, task), notifier)
        obs["mid"] = {
            "launches": len(world.launches()),
            "statuses": list(obs["statuses"]),
            "spawns": list(obs["spawns"]),
        }
        world.release_file.write_text("go")
        obs["result"] = await build
        await _settle()
        return

    raise AssertionError(f"unknown path {path!r}")


def _collect(world: World, obs: dict) -> None:
    launches = world.launches()
    commits = world.commits()
    branches = world.remote_branches()
    prs = world.prs()
    obs.update(
        launches=launches,
        commits=commits,
        remote_branches=branches,
        prs=prs,
        worktree_dirs=world.worktree_dirs(),
        base_sha=world.base_sha,
        active_after=build_delivery.active_build(REPO, ISSUE),
    )


# ── checks ───────────────────────────────────────────────────────────────────


def _result_ok(result) -> bool | None:
    if result is None:
        return False
    if isinstance(result, str):
        return True  # the handoff returns the PR URL
    return bool(getattr(result, "ok", False))


def check(scenario: dict, obs: dict) -> list[str]:
    """Compare what happened in the world with ``scenario['expect']``."""
    exp = scenario["expect"]
    world: World = obs["world"]
    bad: list[str] = []
    child = obs["launches"][0] if obs["launches"] else {}

    def want(name, actual, expected):
        if actual != expected:
            bad.append(f"{name}: expected {expected!r}, got {actual!r}")

    if "launches" in exp:
        want("launches", len(obs["launches"]), exp["launches"])
    if "child_in_worktree" in exp:
        in_wt = bool(child) and Path(child["cwd"]).resolve().parent == world.worktrees.resolve()
        want("child cwd is a worktree of the clone", in_wt, exp["child_in_worktree"])
    if "child_in_daemon_cwd" in exp:
        in_cwd = bool(child) and Path(child["cwd"]).resolve() == world.daemon_cwd.resolve()
        want("child ran in the daemon's cwd", in_cwd, exp["child_in_daemon_cwd"])
    if "network" in exp:
        want("network granted", child.get("network"), exp["network"])
    if "add_dirs" in exp:
        want("--add-dir count", len(child.get("add_dirs", [])), exp["add_dirs"])
        if exp["add_dirs"]:
            common = str((world.clone / ".git").resolve())
            want(
                "git roots are the worktree's gitdir and the common dir",
                sorted(child.get("add_dirs", []))
                == sorted([common, str(Path(common, "worktrees", Path(child.get("cwd", "")).name).resolve())]),
                True,
            )
    if "sandbox" in exp:
        want("sandbox", child.get("sandbox"), exp["sandbox"])
    if "token_scoped" in exp:
        want("scoped identity", child.get("token_scoped"), exp["token_scoped"])
    if "worktree_dirs" in exp:
        want("worktrees created", len(obs["worktree_dirs"]), exp["worktree_dirs"])
    if "commit_on_remote" in exp:
        committed = obs["commits"][-1]["committed"] if obs["commits"] else None
        branch = obs["commits"][-1]["branch"] if obs["commits"] else None
        on_remote = bool(committed) and obs["remote_branches"].get(branch) == committed
        want("the commit is on the remote's branch", on_remote, exp["commit_on_remote"])
        if exp["commit_on_remote"] and on_remote:
            parent = _git(world.bare, "rev-parse", f"{committed}^")
            want("the commit is built on origin/main", parent, obs["base_sha"])
    if "prs" in exp:
        want("PRs on record", len(obs["prs"]), exp["prs"])
        if exp["prs"] and obs["prs"] and obs["commits"]:
            want("the PR is for the pushed branch", obs["prs"][-1]["head"], obs["commits"][-1]["branch"])
            want("the PR names the repository", obs["prs"][-1]["repo"], REPO)
    if "result_ok" in exp:
        want("result ok", _result_ok(obs.get("result")), exp["result_ok"])
    if exp.get("result_names_recorded_pr") and obs["prs"]:
        want(
            "the handoff reports the recorded PR",
            str(obs.get("result", "")).rsplit("/", 1)[-1],
            str(obs["prs"][-1]["number"]),
        )
    if "result_error_contains" in exp:
        err = getattr(obs.get("result"), "error", "") or ""
        want("refusal names the reason", exp["result_error_contains"] in err, True)
    if "claim_released" in exp:
        want("claim released", obs["active_after"] is None, exp["claim_released"])
    if "run_id_in_prompt" in exp:
        want("child told its run marker", bool(obs.get("run_id_in_prompt")), exp["run_id_in_prompt"])

    mid = obs.get("mid") or {}
    if "mid_launches" in exp:
        want("launches while the build ran", mid.get("launches"), exp["mid_launches"])
    if "mid_status_writes" in exp:
        want("status writes while the build ran", len(mid.get("statuses", [])), exp["mid_status_writes"])
    if "mid_spawns" in exp:
        want("tasks dispatched while the build ran", len(mid.get("spawns", [])), exp["mid_spawns"])
    if "final_spawns" in exp:
        want(
            "tasks dispatched by the end",
            [(s["task"], s["trigger"]) for s in obs["spawns"]],
            [tuple(x) for x in exp["final_spawns"]],
        )
    return bad
