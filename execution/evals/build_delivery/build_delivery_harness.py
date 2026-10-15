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

MUTATIONS = (
    "none", "no_grants", "no_network", "no_git_roots", "no_durable_record", "no_startup_sweep",
    "no_continuation", "no_readback", "no_worktree_check", "no_revalidation",
    "silent_unrecorded", "reason_not_required", "drop_intact_registration",
    "skip_pre_clear_read",
)

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

    def __init__(
        self, root: Path, *, origin: str = "match", clone: bool = True, fault: str = ""
    ):
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
        if fault in ("worktree_remote_1", "worktree_remote_2"):
            self._scope_hostile_configuration(fault)
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

    def _scope_hostile_configuration(self, fault: str) -> None:
        """Configuration that applies only inside a worktree made from the clone."""
        include = self.root / "worktree-scoped.gitconfig"
        include.write_text(
            '[url "https://github.com/someone-else/other-repo.git"]\n'
            f"\tpushInsteadOf = https://github.com/{REPO}.git\n"
        )
        common = Path(
            _git(self.clone, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()
        condition = (
            f"gitdir:{common}/worktrees/" if fault == "worktree_remote_1" else "onbranch:cicada/**"
        )
        _git(self.clone, "config", f"includeIf.{condition}.path", str(include))

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


class _TaskStore:
    """The task rows as Neotoma holds them: they outlive any process state."""

    def __init__(self, obs: dict):
        self.rows: dict[str, dict] = {}
        self.obs = obs
        self.unreadable: set[str] = set()
        # field -> [mode, remaining]: a write that SUCCEEDS but is not persisted as
        # sent ("omit": dropped, "mismatch": stored with a different value).
        self.lossy: dict[str, list] = {}
        # field -> fn(value) -> "apply" | "drop" | "reject": faults that depend on
        # WHAT is written (e.g. a write that clears a field fails).
        self.policy: dict[str, object] = {}
        self.fail_next_reads = 0

    def put(self, entity_id: str, snapshot: dict) -> None:
        self.rows[entity_id] = dict(snapshot)

    def _persist(self, entity_id: str, field: str, value) -> bool:
        row = self.rows.setdefault(entity_id, {})
        rule = self.policy.get(field)
        verdict = rule(value) if rule else "apply"
        if verdict == "reject":
            return False
        if verdict == "drop":
            return True
        loss = self.lossy.get(field)
        if loss and loss[1] > 0:
            loss[1] -= 1
            if loss[0] == "mismatch":
                row[field] = "altered-" + str(value)[:8]
            return True
        row[field] = value
        return True

    def field(self, entity_id, field, value, **kw) -> bool:
        return self._persist(entity_id, field, value)

    def status(self, entity_id, status, **kw) -> bool:
        value = getattr(status, "value", status)
        ok = self._persist(entity_id, "status", value)
        if kw.get("reason") is not None:
            ok = self._persist(entity_id, "blocked_reason", kw["reason"]) and ok
        self.obs["statuses"].append((entity_id, value))
        return ok

    def query(self, entity_type, *, snapshot_filters, limit=None, **kw):
        found = [
            {"entity_id": eid, "snapshot": dict(row)}
            for eid, row in self.rows.items()
            if all(row.get(k) == f["value"] for k, f in snapshot_filters.items())
        ]
        return found[:limit] if limit else found

    def read(self, entity_id):
        if self.fail_next_reads > 0:
            self.fail_next_reads -= 1
            return None
        if entity_id in self.unreadable or entity_id not in self.rows:
            return None
        return dict(self.rows[entity_id])


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
    """Let any recovery sweep and the dispatches it started run to completion."""
    for _ in range(5):
        await asyncio.sleep(0)
    pending = list(apis._background_dispatch_tasks)
    while pending:
        await asyncio.gather(*pending, return_exceptions=True)
        pending = [t for t in apis._background_dispatch_tasks if not t.done()]


def run_scenario(scenario: dict, tmp_path: Path, *, mutation: str = "none") -> dict:
    """Run one scenario; return what was observed in the world."""
    assert mutation in MUTATIONS, mutation
    world = World(
        tmp_path,
        origin=(scenario.get("fault") == "origin_mismatch" and "other") or "match",
        clone=scenario.get("fault") != "no_clone",
        fault=scenario.get("fault", ""),
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
    # Production allows no rewrite of a clone's github.com remote. The eval's only
    # one is the local bare repository that stands in for GitHub; it is named here
    # in code (never in the environment) for this process only.
    mp.setattr(
        build_delivery,
        "TEST_ONLY_LOCAL_TRANSPORTS",
        frozenset({str(world.bare)}),
        raising=False,
    )
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
    }.get(mutation, ())
    wrapped = _strip_grants(real, drop)
    mp.setattr(apis, "run_skill", wrapped)
    mp.setattr(swarm_dispatch, "run_skill", wrapped)
    if mutation == "no_durable_record":
        async def _forget(*a, **k):
            return getattr(getattr(apis, "DeferralOutcome", None), "RECORDED", True)

        mp.setattr(apis, "_record_deferral", _forget, raising=False)
    if mutation == "no_startup_sweep":
        async def _hook_only(notifier):
            apis.install_build_release_hook(notifier)
            return {}

        mp.setattr(apis, "start_deferred_recovery", _hook_only, raising=False)
    if mutation == "no_continuation":
        mp.setattr(apis, "_continue_later", lambda *a, **k: None, raising=False)
    if mutation == "no_revalidation":
        async def _stale(entity_id, approved, notifier):
            # Read BEFORE the wait for a slot, dispatch whatever that read said.
            try:
                snap = await asyncio.to_thread(apis.fetch_task_snapshot, entity_id)
                async with apis._recovery_slot():
                    await apis.dispatch_task(
                        entity_id, snap, trigger="build_released",
                        notifier=notifier, snapshot_hydrated=True,
                    )
            finally:
                apis._recovering.discard(entity_id)

        mp.setattr(apis, "_redispatch_deferred", _stale, raising=False)
    if scenario["path"] == "intact_order" and scenario.get("release") in ("before", "during"):
        # Release the build (and let its sweep finish) at the chosen point of the
        # deferral being recorded: before the reason is written, or during rollback.
        target = "_write_deferral" if scenario["release"] == "before" else "_roll_back_deferral"
        real_target = getattr(apis, target)
        fired: list[int] = []

        def _release_then(*a, **k):
            if not fired:
                fired.append(1)
                asyncio.run_coroutine_threadsafe(obs["release_and_settle"](), obs["loop"]).result(10)
            return real_target(*a, **k)

        mp.setattr(apis, target, _release_then)
    if mutation == "skip_pre_clear_read":
        def _blind_rollback(entity_id, snapshot, claim):
            for field in ("blocked_reason", "dispatch_context", "disposition"):
                apis.set_task_field(
                    entity_id, field, snapshot.get(field) or "",
                    handler="apis", key_suffix=f"deferral-rollback-{claim.run_id}",
                )
            return apis._classify_deferral_state(entity_id, claim)

        mp.setattr(apis, "_roll_back_deferral", _blind_rollback, raising=False)
    if mutation == "drop_intact_registration":
        real_record = apis._record_deferral

        async def _unregistered(*a, **k):
            outcome = await real_record(*a, **k)
            if outcome is apis.DeferralOutcome.INTACT_MARKER:
                return apis.DeferralOutcome.NOT_HELD
            return outcome

        mp.setattr(apis, "_record_deferral", _unregistered, raising=False)
    if mutation == "reason_not_required":
        mp.setattr(build_delivery, "reason_matches_context", lambda reason, context: True)
    if mutation == "silent_unrecorded":
        async def _quiet_failure(*a, **k):
            return getattr(getattr(apis, "DeferralOutcome", None), "NOT_HELD", False)

        mp.setattr(apis, "_record_deferral", _quiet_failure, raising=False)
    if mutation == "no_readback":
        mp.setattr(apis, "_deferral_persisted", lambda *a, **k: [], raising=False)
    if mutation == "no_worktree_check":
        real_verify = build_delivery.verify_clone_identity

        async def _source_only(path, repository, *, what="clone"):
            if what == "worktree":
                return None
            return await real_verify(path, repository, what=what)

        mp.setattr(build_delivery, "verify_clone_identity", _source_only)
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
    store = _TaskStore(obs)
    obs["store"] = store
    mp.setattr(apis, "set_task_status", store.status)
    mp.setattr(apis, "set_task_field", store.field, raising=False)

    if scenario["path"] == "rollback_race":
        # The task moves on (to a gate hold, a dispatch, a terminal state, ...) before
        # the first clear, between two clears, or after the last one.
        race = scenario["race"]

        def _move() -> None:
            obs["store"].rows["ent_eval_race_0000"].update(
                status=race["status"], blocked_reason=race.get("reason", "")
            )

        if race["when"] == "before":
            real_roll = apis._roll_back_deferral

            def _roll_after_move(*a, **k):
                _move()
                return real_roll(*a, **k)

            mp.setattr(apis, "_roll_back_deferral", _roll_after_move)
        else:
            trigger = 1 if race["when"] == "between" else 3
            rollback_writes: list[str] = []
            store_field = apis.set_task_field

            def _field(entity_id, field, value, **kw):
                result = store_field(entity_id, field, value, **kw)
                if kw.get("key_suffix", "").startswith("deferral-rollback"):
                    rollback_writes.append(field)
                    if len(rollback_writes) == trigger:
                        _move()
                return result

            mp.setattr(apis, "set_task_field", _field)
            obs["rollback_writes"] = rollback_writes
    mp.setattr(apis, "query_all_entities", store.query)
    mp.setattr(apis, "fetch_task_snapshot", store.read)
    obs["token_requests"] = []
    real_token = apis._token_for_agent_on_repo

    def _token(*args):
        obs["token_requests"].append(args)
        return real_token(*args)

    mp.setattr(apis, "_token_for_agent_on_repo", _token)
    mp.setattr(swarm_dispatch, "_token_for_agent_on_repo", _token)
    mp.setattr(apis, "_RECOVERY_PASS_DELAY_S", 0.001, raising=False)
    mp.setattr(apis, "_RECOVERY_LIST_RETRY_DELAY_S", 0.001, raising=False)
    mp.setattr(apis, "_surfaced", set(), raising=False)
    mp.setattr(apis, "_recovering", set(), raising=False)
    mp.setattr(apis, "_recovery_notifier", None, raising=False)
    mp.setattr(build_delivery, "_release_hook", None, raising=False)
    mp.setattr(apis, "write_checkpoint_brief", lambda **kw: "ent_brief")
    mp.setattr(apis, "_release_lifecycle_proven", lambda *a, **k: True)

    async def _no_hydrate(event):
        return None

    mp.setattr(apis, "hydrate_snapshot", _no_hydrate)
    if scenario["path"] in (
        "mid_build", "restart", "sweep_scale", "slot_wait", "intact_order", "rollback_race"
    ):
        # What gets dispatched for the OTHER tasks is not the subject; the child
        # the build itself launched still goes through the real run_skill.
        mp.setattr(apis, "_spawn_harness_skill", _recording_spawn(obs))
    # Fresh registries per scenario (a name the module does not define is skipped).
    for name in ("_inflight", "_runs", "_deferred"):
        mp.setattr(build_delivery, name, {}, raising=False)


def _recording_spawn(obs: dict):
    async def _spawn(skill, entity_id, snapshot, trigger, *a, **kw):
        obs["spawns"].append(
            {"skill": skill, "task": entity_id, "trigger": trigger, "title": snapshot.get("title")}
        )
        if entity_id.startswith("ent_a_busy") and obs.get("release") is not None:
            await obs["release"].wait()  # holds a recovery slot until the scenario says

        class _R:
            ok = True
            pr_url = None
            detail = "recorded"

        return _R()

    return _spawn


def _dispatcher(notifier):
    return swarm_dispatch.SwarmDispatcher(
        notifier,
        swarm_dispatch.DispatchConfig(
            neotoma_token="", github_token="", auto_merge=False, auto_rereview_on_push=False
        ),
    )


def _spec_state() -> SpecState:
    state = SpecState(repo=REPO, issue_number=ISSUE, title="Build the change")
    state.sections = {"eng": "Add delivered.txt."}
    return state


def _wipe_process_state() -> None:
    """Everything a restart destroys: no claim, hook, dedupe, or in-flight record."""
    build_delivery._release_hook = None
    build_delivery._inflight.clear()
    build_delivery._runs.clear()
    apis._created_seen.clear()
    apis._recovering.clear()
    apis._surfaced.clear()
    apis._recovery_notifier = None
    for task in list(apis._background_dispatch_tasks):
        task.cancel()
    apis._background_dispatch_tasks.clear()


def _marked_row(**over) -> dict:
    row = _task_snapshot(action_type=over.pop("action_type", "local_edit"), **over)
    row.update(
        status="pending",
        disposition=build_delivery.DEFERRED_DISPOSITION,
        blocked_reason=build_delivery.deferral_reason(
            build_delivery.BuildClaim(REPO.lower(), ISSUE, "swarm-build-handoff", "0123456789abcdef")
        ),
    )
    return row


async def _drive_rollback_race(scenario: dict, world: World, obs: dict, notifier) -> None:
    """A deferral that cannot be confirmed is rolled back while the task moves on."""
    store = obs["store"]
    store.fail_next_reads = 2
    obs["notifier"] = notifier
    apis.install_build_release_hook(notifier)
    cm = build_delivery.claim_build(REPO, ISSUE, "swarm-build-handoff")
    cm.__enter__()
    task = _task_snapshot(action_type="local_edit")
    store.put("ent_eval_race_0000", task)
    await apis.handle_event(_created("ent_eval_race_0000", task), notifier)
    obs["row_after_notice"] = dict(store.rows["ent_eval_race_0000"])
    cm.__exit__(None, None, None)
    await _settle()


async def _drive_intact_order(scenario: dict, world: World, obs: dict, notifier) -> None:
    """A deferral that cannot be confirmed AND whose marker cannot be cleared, with the
    build released at a chosen moment relative to it. The notice promises that Apis
    WILL dispatch the task; the task must be dispatched exactly once after the
    build ends, in every ordering."""
    store = obs["store"]
    store.fail_next_reads = 2
    store.policy = {"blocked_reason": lambda v: "reject" if v == "" else "apply"}
    obs["notifier"] = notifier
    obs["loop"] = asyncio.get_running_loop()
    apis.install_build_release_hook(notifier)
    cm = build_delivery.claim_build(REPO, ISSUE, "swarm-build-handoff")
    cm.__enter__()
    released: list[int] = []

    def release() -> None:
        if not released:
            released.append(1)
            cm.__exit__(None, None, None)

    async def release_and_settle() -> None:
        release()
        await _settle()

    obs["release_and_settle"] = release_and_settle
    task = _task_snapshot(action_type="local_edit")
    store.put("ent_eval_ord_0000", task)
    await apis.handle_event(_created("ent_eval_ord_0000", task), notifier)
    release()  # a no-op unless the build was meant to end only after the notice
    await _settle()


async def _drive_slot_wait(scenario: dict, world: World, obs: dict, notifier) -> None:
    """Tasks queued behind busy recovery slots change while they wait.

    Three marked tasks hold the three slots. The queued ones are then completed,
    cancelled, re-marked behind a different build, left alone (control), or made
    unreadable, BEFORE their turn comes; only the control may be dispatched, and
    from the read taken at its turn.
    """
    store = obs["store"]
    obs["release"] = asyncio.Event()
    obs["notifier"] = notifier
    context = f"deferred-behind:{REPO.lower()}#{ISSUE}:0123456789abcdef"
    for i in range(apis._RECOVERY_CONCURRENCY):
        store.put(f"ent_a_busy_{i:03d}", {**_marked_row(), "dispatch_context": context})
    queued = scenario["queued"]
    for kind in queued:
        store.put(
            f"ent_b_{kind}",
            {**_marked_row(title="as first read"), "dispatch_context": context},
        )
    await apis.recover_deferred_tasks(notifier)
    for _ in range(500):  # real time: each step reads the task on a worker thread
        if len(obs["spawns"]) >= apis._RECOVERY_CONCURRENCY:
            break
        await asyncio.sleep(0.01)
    for _ in range(50):
        await asyncio.sleep(0)
    obs["dispatched_while_waiting"] = [s["task"] for s in obs["spawns"]]
    for kind in queued:
        row = store.rows[f"ent_b_{kind}"]
        if kind == "completed":
            row["status"] = "done"
        elif kind == "cancelled":
            row["status"] = "cancelled"
        elif kind == "context_missing":
            row.pop("dispatch_context", None)
        elif kind == "context_garbled":
            row["dispatch_context"] = "garbled"
        elif kind == "remarked":
            row["dispatch_context"] = f"deferred-behind:{REPO.lower()}#{ISSUE}:fedcba9876543210"
            row["blocked_reason"] = build_delivery.deferral_reason(
                build_delivery.BuildClaim(REPO.lower(), ISSUE, "swarm-build-handoff", "fedcba9876543210")
            )
        elif kind == "unreadable":
            store.unreadable.add(f"ent_b_{kind}")
        elif kind == "still_deferred":
            row["title"] = "as re-read at its turn"
    obs["rows_after_change"] = {k: dict(v) for k, v in store.rows.items()}
    obs["release"].set()
    await _settle()
    obs["rows_final"] = {k: dict(v) for k, v in store.rows.items()}
    obs["spawns_at_turn"] = list(obs["spawns"])
    obs["statuses_at_turn"] = list(obs["statuses"])
    obs["sent_at_turn"] = list(notifier.sent)
    await apis.recover_deferred_tasks(notifier)  # a later trigger re-examines them
    await _settle()


async def _drive_scale(scenario: dict, world: World, obs: dict, notifier) -> None:
    """More marked tasks than one pass examines, broken records sorted FIRST.

    Nothing is delivered afterwards: no creation event, no unrelated build release,
    no restart. The single startup sweep (and the continuation it schedules) must
    reach every one of them, then repeated sweeps must change nothing.
    """
    scale = scenario["scale"]
    store = obs["store"]
    context = f"deferred-behind:{REPO.lower()}#{ISSUE}:0123456789abcdef"
    bad: dict[str, str] = {}
    for i in range(scale.get("unreadable", 0)):
        eid = f"ent_a_unreadable_{i:03d}"
        store.put(eid, {**_marked_row(), "dispatch_context": context})
        store.unreadable.add(eid)
        bad[eid] = "unreadable"
    for i in range(scale.get("garbled", 0)):
        eid = f"ent_b_garbled_{i:03d}"
        store.put(eid, {**_marked_row(), "dispatch_context": "garbled"})
        bad[eid] = "garbled"
    for i in range(scale.get("missing_context", 0)):
        eid = f"ent_c_missing_{i:03d}"
        store.put(eid, _marked_row())
        bad[eid] = "missing"
    valid = []
    for i in range(scale["valid_local"]):
        eid = f"ent_d_valid_local_{i:03d}"
        store.put(eid, {**_marked_row(), "dispatch_context": context})
        valid.append(eid)
    for i in range(scale.get("valid_pr", 0)):
        eid = f"ent_e_valid_pr_{i:03d}"
        store.put(eid, {**_marked_row(action_type="open_or_merge_pr"), "dispatch_context": context})
        valid.append(eid)
    for eid in store.rows:
        # Unrelated pending backlog: never read for dispatch.
        pass
    for i in range(scale.get("backlog", 0)):
        store.put(f"ent_f_backlog_{i:03d}", {**_task_snapshot(action_type="local_edit"), "status": "pending"})
    obs["scale"] = {"valid": valid, "bad": bad}

    obs["notifier"] = notifier
    await apis.start_deferred_recovery(notifier)
    await _settle()
    obs["after_first_sweep"] = {
        "spawns": list(obs["spawns"]),
        "sent": list(notifier.sent),
        "statuses": list(obs["statuses"]),
    }
    for _ in range(scale.get("repeat_sweeps", 2)):
        await apis.recover_deferred_tasks(notifier)
        await _settle()
    build_delivery._release_hook and build_delivery._release_hook((REPO.lower(), ISSUE))
    await _settle()


async def _drive_restart(scenario: dict, world: World, obs: dict, notifier) -> None:
    """Defer a task behind a build, lose ALL process state, restart, observe.

    No creation event is delivered after the restart: the only thing that can find
    the task is what was written on it.
    """
    store = obs["store"]
    obs["notifier_before"] = notifier
    store.lossy = {k: list(v) for k, v in (scenario.get("store_faults") or {}).items()}
    rules = {
        "reject_clear": lambda v: "reject" if v == "" else "apply",
        "drop_clear": lambda v: "drop" if v == "" else "apply",
        "drop_write": lambda v: "drop" if v else "apply",
    }
    store.policy = {f: rules[r] for f, r in (scenario.get("store_policy") or {}).items()}
    store.fail_next_reads = scenario.get("fail_next_reads", 0)
    apis.install_build_release_hook(notifier)
    claim_cm = build_delivery.claim_build(REPO, ISSUE, "swarm-build-handoff")
    claim_cm.__enter__()
    for n, event in enumerate(scenario["events"]):
        entity_id = f"ent_eval_rst_{n:04d}"
        task = _task_snapshot(action_type="local_edit", **(event.get("task") or {}))
        store.put(entity_id, task)
        await apis.handle_event(_created(entity_id, task), notifier)
    obs["before_restart"] = {k: dict(v) for k, v in store.rows.items()}
    obs["spawns_before_restart"] = list(obs["spawns"])

    _wipe_process_state()
    claim_cm.__exit__(None, None, None)  # released into a process with no hook
    await _settle()
    obs["spawns_after_wipe"] = list(obs["spawns"])

    for entity_id, change in (scenario.get("while_down") or {}).items():
        if change == "complete":
            store.rows[entity_id]["status"] = "done"
        elif change == "unreadable":
            store.unreadable.add(entity_id)

    fresh = _Notifier()  # a fresh Apis context
    obs["notifier"] = fresh
    obs["restart_events"] = 0
    for _ in range(scenario.get("redelivered_creates", 0)):
        # An SSE redelivery of a creation the dead process had already handled,
        # hydrated from the task as Neotoma now holds it.
        entity_id = "ent_eval_rst_0000"
        await apis.handle_event(_created(entity_id, store.read(entity_id)), fresh)
    runs = scenario.get("startup_sweeps", 1)
    await asyncio.gather(*[apis.start_deferred_recovery(fresh) for _ in range(runs)])
    await _settle()
    obs["after_restart"] = {k: dict(v) for k, v in store.rows.items()}


async def _drive(scenario: dict, world: World, obs: dict) -> None:
    path = scenario["path"]
    notifier = _Notifier()
    obs["notifier"] = notifier

    if path == "handoff":
        dispatcher = _dispatcher(notifier)
        state = _spec_state()
        if scenario.get("fault"):
            # The caller that talks to the operator, not just the launcher.
            await dispatcher._hand_off_to_build(
                _trigger(), state, completed=["pm", "eng"],
                spec_action="Open the issue and approve the build.",
                spec_ready_key="eval-spec-ready",
            )
            obs["result"] = None
            obs["operator_text"] = "\n".join(notifier.sent)
        else:
            obs["result"] = await dispatcher._open_implementation_pr(_trigger(), state)
        return

    if path == "task":
        snap = _task_snapshot(**(scenario.get("task") or {}))
        obs["result"] = await apis._spawn_harness_skill(
            "cicada", "ent_eval_task_0001", snap, "created", notifier, role="cicada"
        )
        obs["operator_text"] = getattr(obs["result"], "error", "") or ""
        return

    if path == "mid_build":
        apis.install_build_release_hook(notifier)
        dispatcher = _dispatcher(notifier)
        state = _spec_state()
        build = asyncio.create_task(dispatcher._open_implementation_pr(_trigger(), state))
        await _wait_for(world.held_file)
        run_id = json.loads(world.held_file.read_text())["run_id"]
        obs["run_id_in_prompt"] = run_id
        store = obs["store"]
        for n, event in enumerate(scenario["events"]):
            entity_id = f"ent_eval_mid_{n:04d}"
            task = _task_snapshot(action_type="local_edit", **(event.get("task") or {}))
            if event.get("marker"):
                task["description"] += f"\nbuild-run: {run_id}"
            if entity_id not in store.rows:
                store.put(entity_id, task)
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
            "rows": {k: dict(v) for k, v in store.rows.items()},
        }
        world.release_file.write_text("go")
        obs["result"] = await build
        await _settle()
        return

    if path == "restart":
        await _drive_restart(scenario, world, obs, notifier)
        return

    if path == "sweep_scale":
        await _drive_scale(scenario, world, obs, notifier)
        return

    if path == "slot_wait":
        await _drive_slot_wait(scenario, world, obs, notifier)
        return

    if path == "intact_order":
        await _drive_intact_order(scenario, world, obs, notifier)
        return

    if path == "rollback_race":
        await _drive_rollback_race(scenario, world, obs, notifier)
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


def _check_scale(exp: dict, obs: dict, bad: list[str]) -> None:
    info = obs["scale"]
    valid, broken = info["valid"], info["bad"]
    statuses = obs["statuses"]
    spawned = [s["task"] for s in obs["spawns"]]
    for entity_id in valid:
        if ("routed" not in [st for eid, st in statuses if eid == entity_id]):
            bad.append(f"{entity_id} never reached the gates")
    if len(spawned) != len(set(spawned)):
        bad.append("a task was dispatched twice")
    if exp.get("spawned") is not None and len(spawned) != exp["spawned"]:
        bad.append(f"expected {exp['spawned']} dispatches, got {len(spawned)}")
    first = obs["after_first_sweep"]
    if len(first["spawns"]) != len(spawned):
        bad.append("repeated sweeps dispatched more tasks")
    if len(first["statuses"]) != len(statuses):
        bad.append("repeated sweeps wrote more status changes")
    sent = obs["notifier"].sent
    for entity_id in broken:
        count = sum(entity_id in m for m in sent)
        if count != 1:
            bad.append(f"{entity_id} announced {count} times, expected exactly once")
    if len(first["sent"]) != len(sent):
        bad.append("repeated sweeps announced more")
    never = [eid for eid in broken if any(eid == s for s in spawned)]
    if never:
        bad.append(f"a broken record was dispatched: {never}")
    backlog = [t for t in spawned if t.startswith("ent_f_backlog")]
    if backlog:
        bad.append("the unrelated backlog was dispatched")


def mid_row(obs: dict, entity_id: str) -> dict | None:
    return (obs.get("mid") or {}).get("rows", {}).get(entity_id)


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

    text = obs.get("operator_text", "")
    for needle in exp.get("operator_text_contains", []):
        if needle not in text:
            bad.append(f"the operator is not told {needle!r}; got: {text[:300]!r}")
    for needle in exp.get("operator_text_excludes", []):
        if needle in text:
            bad.append(f"the operator is wrongly told {needle!r}")

    def _marked(row: dict | None) -> bool:
        ctx = build_delivery.parse_deferral_context((row or {}).get("dispatch_context"))
        return bool(
            row
            and row.get("disposition") == build_delivery.DEFERRED_DISPOSITION
            and row.get("status") == "pending"
            and str(row.get("blocked_reason", "")).startswith("Deferred:")
            and ctx
            and ctx[0] == REPO.lower()
            and ctx[1] == ISSUE
        )

    for entity_id in exp.get("mid_deferred_on_the_task", []):
        want(f"{entity_id} marked deferred on the task", _marked(mid_row(obs, entity_id)), True)
    for entity_id in exp.get("marked_before_restart", []):
        want(
            f"{entity_id} marked deferred on the task before the restart",
            _marked((obs.get("before_restart") or {}).get(entity_id)),
            True,
        )
    if "spawns_after_wipe" in exp:
        want("dispatches while nothing was running", len(obs.get("spawns_after_wipe", [])), exp["spawns_after_wipe"])
    if "restart_statuses" in exp:
        for entity_id, expected in exp["restart_statuses"].items():
            got = [st for eid, st in obs["statuses"] if eid == entity_id]
            for status in expected:
                if status not in got:
                    bad.append(f"{entity_id} never reached {status!r} (saw {got})")
    if "restart_status_not" in exp:
        for entity_id, statuses in exp["restart_status_not"].items():
            got = [st for eid, st in obs["statuses"] if eid == entity_id]
            for status in statuses:
                if status in got:
                    bad.append(f"{entity_id} unexpectedly reached {status!r}")
    if "notification_contains" in exp:
        sent = "\n".join(getattr(obs.get("notifier"), "sent", []))
        for needle in exp["notification_contains"]:
            if needle not in sent:
                bad.append(f"no notification says {needle!r}; got: {sent[:300]!r}")

    if "not_marked_before_restart" in exp:
        for entity_id in exp["not_marked_before_restart"]:
            want(
                f"{entity_id} is not acknowledged as deferred",
                _marked((obs.get("before_restart") or {}).get(entity_id)),
                False,
            )
    if "warning_before_restart_contains" in exp:
        sent = "\n".join(getattr(obs.get("notifier_before"), "sent", []))
        for needle in exp["warning_before_restart_contains"]:
            if needle not in sent:
                bad.append(f"the operator was not warned {needle!r}; got: {sent[:300]!r}")
    if "slot_wait" in exp:
        sw = exp["slot_wait"]
        dispatched = {s["task"]: s for s in obs["spawns_at_turn"]}
        want("queued tasks held back while the slots were busy",
             sorted(t for t in obs["dispatched_while_waiting"] if t.startswith("ent_b_")), [])
        for kind in sw.get("not_dispatched", []):
            eid = f"ent_b_{kind}"
            if eid in dispatched:
                bad.append(f"{eid} was dispatched after it stopped qualifying")
            if [st for e, st in obs["statuses_at_turn"] if e == eid]:
                bad.append(f"{eid} got a status write although it was not dispatched")
            if any(eid in m for m in obs["sent_at_turn"]) and kind not in (
                "unreadable", "context_missing", "context_garbled"
            ):
                bad.append(f"{eid} was announced, but finishing while it waited is the normal case")
        for kind, title in sw.get("dispatched_from_fresh_read", {}).items():
            got = dispatched.get(f"ent_b_{kind}")
            want(f"ent_b_{kind} dispatched", got is not None, True)
            if got:
                want(f"ent_b_{kind} dispatched from the read at its turn", got["title"], title)
        for kind in sw.get("task_untouched_by_apis", []):
            eid = f"ent_b_{kind}"
            want(f"{eid} left as it was", obs["rows_final"][eid], obs["rows_after_change"][eid])
        for kind in sw.get("announced_at_its_turn", []):
            n = sum(f"ent_b_{kind}" in m for m in obs["sent_at_turn"])
            want(f"ent_b_{kind} announced when its turn came", n, 1)
        for kind in sw.get("announced_once", []):
            n = sum(f"ent_b_{kind}" in m for m in obs["notifier"].sent)
            want(f"ent_b_{kind} announced", n, 1)
    if "race_row" in exp:
        row = obs["row_after_notice"]
        for field, value in exp["race_row"].items():
            want(f"the task's {field} after the rollback", row.get(field), value)
        want("dispatches", obs["spawns"], [])
    if "rollback_writes" in exp:
        want("writes made to a task that had moved on", len(obs.get("rollback_writes", [])), exp["rollback_writes"])
    if "notification_excludes" in exp:
        sent = "\n".join(getattr(obs.get("notifier"), "sent", []))
        for needle in exp["notification_excludes"]:
            if needle in sent:
                bad.append(f"a notice wrongly says {needle!r}")
    if "notification_counts" in exp:
        sent = obs.get("notifier").sent if obs.get("notifier") else []
        for needle, count in exp["notification_counts"].items():
            want(f"notices containing {needle!r}", sum(needle in m for m in sent), count)
    if "token_requests" in exp:
        want("token resolutions", len(obs.get("token_requests", [])), exp["token_requests"])
    if "scale" in exp:
        _check_scale(exp["scale"], obs, bad)

    mid = obs.get("mid") or {}
    if "mid_launches" in exp:
        want("launches while the build ran", mid.get("launches"), exp["mid_launches"])
    if "mid_status_writes" in exp:
        want("status writes while the build ran", len(mid.get("statuses", [])), exp["mid_status_writes"])
    if "mid_status_values" in exp:
        want(
            "the only status written while the build ran",
            sorted({st for _, st in mid.get("statuses", [])}),
            sorted(exp["mid_status_values"]),
        )
    if "mid_spawns" in exp:
        want("tasks dispatched while the build ran", len(mid.get("spawns", [])), exp["mid_spawns"])
    if "final_spawns" in exp:
        want(
            "tasks dispatched by the end",
            [(s["task"], s["trigger"]) for s in obs["spawns"]],
            [tuple(x) for x in exp["final_spawns"]],
        )
    return bad
