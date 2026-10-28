"""Agent-facing credential containment evals: offline, deterministic, no model call.

Three entrypoints are exercised exactly as an agent meets them:

* the PreToolUse read guard (``.claude/hooks/credential_read_guard.py``), run
  as a subprocess with a Claude Code tool-call payload on stdin. A denied call
  must return the structured refusal (exit 2 and a ``permissionDecision:
  deny`` document whose reason opens with the refusal notice and says what was
  protected); an allowed
  call must exit 0 and print nothing; and neither stream may ever carry a
  fake secret value.
* the review launcher (``harness_lens_runner.run_one``). A review worktree
  that holds an env-like file must be refused BEFORE any sandbox is built or
  any model dispatch starts, with a structured report, and the worktree must
  be cleaned up either way. A clean worktree (or one holding only placeholder
  templates) must reach dispatch exactly once.
* the input packager's collection and verification CLI. Only declared synthetic
  files may be read, and observed external content reads must remain zero.

Every credential here is a synthetic fake in a throwaway workspace; ``HOME``
points at that workspace so home-relative credential directories resolve
inside it. Scenario data lives in ``fixtures/scenarios.json``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
HOOK = REPO_ROOT / ".claude" / "hooks" / "credential_read_guard.py"
SCENARIOS = HERE / "fixtures" / "scenarios.json"
PACKAGER = REPO_ROOT / "execution/scripts/package_rule_inventory_inputs.py"
RENDERER = REPO_ROOT / "execution/scripts/render_rule_inventory.py"

# Fake secret values planted in the workspace. They must never appear in any
# output the guard produces, allowed or denied.
FAKE_SECRET_VALUES = (
    "fake_eval_token_value_not_real_0001",
    "fake_eval_client_secret_not_real_0002",
)


def load_scenarios() -> dict:
    return json.loads(SCENARIOS.read_text(encoding="utf-8"))


def build_workspace(root: Path) -> Path:
    """Create the fake credential tree the hook scenarios point at."""
    ws = root / "workspace"
    neotoma = ws / ".config" / "neotoma"
    neotoma.mkdir(parents=True)
    (neotoma / ".env").write_text(
        f"SERVICE_NAME={FAKE_SECRET_VALUES[0]}\n"
        f"client_secret={FAKE_SECRET_VALUES[1]}\n",
        encoding="utf-8",
    )
    (neotoma / ".env.example").write_text("SERVICE_NAME=\n", encoding="utf-8")
    (ws / "notes.txt").write_text("nothing sensitive\n", encoding="utf-8")
    (ws / "somewhere-else").mkdir()
    (ws / "somewhere-else" / "readme.txt").write_text("x\n", encoding="utf-8")
    links = ws / "links"
    links.mkdir()
    (links / "notes-link").symlink_to(neotoma / ".env")
    (links / "dir-link").symlink_to(neotoma)
    (links / "plain-link").symlink_to(ws / "notes.txt")
    return ws


@dataclass
class HookOutcome:
    exit_code: int
    stdout: str
    stderr: str
    decision: str | None = None
    reason: str = ""
    problems: list[str] = field(default_factory=list)


def _substitute(value, ws: Path):
    if isinstance(value, str):
        return value.replace("{ws}", str(ws))
    if isinstance(value, dict):
        return {k: _substitute(v, ws) for k, v in value.items()}
    return value


def run_hook(scenario: dict, ws: Path, hook: Path = HOOK) -> HookOutcome:
    payload = {
        "tool_name": scenario["tool"],
        "tool_input": _substitute(scenario["input"], ws),
    }
    env = dict(os.environ)
    env["HOME"] = str(ws)
    result = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ws),
        timeout=30,
    )
    outcome = HookOutcome(result.returncode, result.stdout, result.stderr)
    if result.stdout.strip():
        try:
            document = json.loads(result.stdout)
            specific = document.get("hookSpecificOutput", {})
            outcome.decision = specific.get("permissionDecision")
            outcome.reason = specific.get("permissionDecisionReason", "")
        except json.JSONDecodeError:
            outcome.problems.append("stdout is not a structured document")
    return outcome


def judge_hook(scenario: dict, outcome: HookOutcome) -> list[str]:
    """Return the ways the outcome departs from the scenario's expectation."""
    problems = list(outcome.problems)
    combined = outcome.stdout + outcome.stderr
    for secret in FAKE_SECRET_VALUES:
        if secret in combined:
            problems.append("a fake secret value reached the guard's output")
    if scenario["expect"] == "deny":
        if outcome.exit_code != 2:
            problems.append(f"expected exit 2, got {outcome.exit_code}")
        if outcome.decision != "deny":
            problems.append("no structured deny decision")
        if not outcome.reason.startswith("Refused:"):
            problems.append("refusal reason does not open with the refusal notice")
        if "credential file" not in outcome.reason:
            problems.append("refusal reason does not say what was protected")
    else:
        if outcome.exit_code != 0:
            problems.append(f"expected exit 0, got {outcome.exit_code}")
        if outcome.stdout.strip():
            problems.append("an allowed call produced output")
    return problems


# --------------------------------------------------------------------------
# Launch scenarios
# --------------------------------------------------------------------------


class LaunchTrace:
    """What the launcher did, recorded by the stand-ins it was run against."""

    def __init__(self) -> None:
        self.dispatch_calls = 0
        self.role_loads = 0
        self.role_fingerprint = None
        self.sandbox_builds = 0
        self.worktree_removed = False
        self.report: dict | None = None
        self.reached_dispatch_then_stopped = False


class StopAtDispatch(Exception):
    """Raised by the dispatch stand-in so a launching scenario ends there."""


def run_launch(scenario: dict, tmp_root: Path, monkeypatch) -> LaunchTrace:
    """Drive ``run_one`` against a fake worktree holding the scenario's files."""
    import asyncio

    scripts = REPO_ROOT / "execution" / "scripts"
    daemon_dir = REPO_ROOT / "execution" / "daemons" / "apis"
    for entry in (str(REPO_ROOT), str(daemon_dir), str(scripts)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    import harness_lens_runner as hlr
    from lib.daemon_runtime import agent_loader

    trace = LaunchTrace()
    files = list(scenario["files"])

    def fake_create(self, *, head):
        self._created = True
        self.path.mkdir(parents=True, exist_ok=True)
        for relative in files:
            target = self.path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("SYNTHETIC=1\n", encoding="utf-8")

    def fake_remove(self):
        trace.worktree_removed = True

    def tracking_build(cls, provider, tmp_root_arg, **kwargs):
        trace.sandbox_builds += 1
        root = tmp_root / f"{provider}-eval-home"
        root.mkdir(parents=True, exist_ok=True)
        return hlr.HarnessSandbox(
            provider=provider,
            root=root,
            env_extra={"ATELES_LOCAL_REVIEW_HOME": str(root)},
            command_wrapper=["/usr/bin/sandbox-exec", "-f", str(root / "profile.sb")],
            credential_read_denied=True,
            credential_binding_protected=True,
            user_config_write_denied=True,
            git_stash_denied=True,
            authentication_ready=True,
            unavailable_guards=(),
            review_write_confined=True,
        )

    async def fake_dispatch(*args, **kwargs):
        trace.dispatch_calls += 1
        trace.role_fingerprint = kwargs.get("expected_role_fingerprint")
        raise StopAtDispatch()

    monkeypatch.setattr(hlr.Worktree, "create", fake_create)
    monkeypatch.setattr(hlr.Worktree, "remove", fake_remove)
    monkeypatch.setattr(hlr.HarnessSandbox, "build", classmethod(tracking_build))
    monkeypatch.setattr(hlr.dispatch_role, "dispatch", fake_dispatch)
    # The checkout under review is source only. Supply a complete, inert
    # canonical role at the loader's external read edge; admission and the
    # wrapper's fingerprint calculation remain real.
    def canonical_role(self):
        assert self.agent_name == "pavo"
        trace.role_loads += 1
        return agent_loader.AgentDefinition(
            entity_id="ent_eval_pavo", name="pavo", status="active",
            prompt_markdown="Synthetic canonical review role.", tool_allowlist=["Read"],
            raw={"snapshot": {"revision": "eval-1"}},
        )

    monkeypatch.setattr(agent_loader.AgentLoader, "load", canonical_role)
    monkeypatch.setattr(hlr, "check_headroom", lambda provider: 1.0)
    # The scenario controls credential files in the checkout, not remote CI.
    # Feed the real seating gate a synthetic green head at its external edge;
    # no fixture may consult GitHub credentials before the containment probe.
    monkeypatch.setattr(hlr, "current_pr_metadata", lambda **kw: {
        "state": "open", "head": {"sha": "a" * 40}, "base": {"ref": "main"},
    })

    async def green_checks(repository, head_sha, base_ref):
        assert repository == "example/repo" and head_sha == "a" * 40
        return hlr.panel_check_gate.CheckGate(head=head_sha, green=True)

    monkeypatch.setattr(hlr.panel_check_gate, "read_check_gate", green_checks)
    monkeypatch.setattr(
        hlr,
        "capture_stash_ref_state",
        lambda worktree: hlr.StashRefState(resolved_oid=None, packed_oid=None),
    )
    monkeypatch.setattr(
        hlr, "verify_stash_ref_unchanged_after_dispatch", lambda *a, **k: None
    )

    brief = tmp_root / "lens_brief.md"
    brief.write_text("BRIEF BODY\n", encoding="utf-8")
    target = hlr.LensTarget(
        repo="example/repo", pr=1, head="a" * 40, lens="pm", agent="pavo"
    )
    try:
        trace.report = asyncio.run(
            hlr.run_one(
                target,
                provider="codex",
                post=False,
                dry_run=bool(scenario["dry_run"]),
                repo_worktree_name="repo",
                scratch_root=tmp_root,
                brief_path=brief,
                timeout=None,
            )
        )
    except StopAtDispatch:
        trace.reached_dispatch_then_stopped = True
    return trace


def judge_launch(scenario: dict, trace: LaunchTrace) -> list[str]:
    problems: list[str] = []
    if not trace.worktree_removed:
        problems.append("the review worktree was not cleaned up")
    if scenario["expect"] == "refuse":
        if trace.dispatch_calls != 0:
            problems.append(f"{trace.dispatch_calls} dispatch call(s) after a refusal")
        report = trace.report or {}
        if report.get("ok") is not False:
            problems.append("refusal did not return ok: false")
        reason = report.get("refusal_reason") or report.get("would_refuse") or ""
        if not reason:
            problems.append("refusal carries no reason")
        if report.get("posted") not in (False, None):
            problems.append("a refused launch posted something")
        if not scenario["dry_run"] and trace.sandbox_builds != 0:
            problems.append("a sandbox was built before the refusal")
        if trace.role_loads != (1 if scenario["dry_run"] else 0):
            problems.append("refusal crossed the canonical role read boundary")
    else:
        if trace.dispatch_calls != 1:
            problems.append(f"expected exactly one dispatch, got {trace.dispatch_calls}")
        if trace.role_loads != 1 or not isinstance(trace.role_fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{64}", trace.role_fingerprint
        ):
            problems.append("launch missed the fresh canonical role/fingerprint")
    return problems


_PACKET_CLI_OBSERVER = '''import errno,json,os,runpy,sys
from pathlib import Path
context=json.loads(Path(sys.argv[1]).read_text())
external=Path(context["external"])
identity=(external.stat().st_dev, external.stat().st_ino)
reads=[];swaps=[];owned=set()
original_read,original_open,original_close=os.read,os.open,os.close
original_path_read,original_text_read=Path.read_bytes,Path.read_text
def read_fd(fd,size):
    info=os.fstat(fd)
    if (info.st_dev,info.st_ino)==identity: reads.append("descriptor")
    return original_read(fd,size)
def read_path(path):
    info=path.stat()
    if (info.st_dev,info.st_ino)==identity: reads.append("path")
    return original_path_read(path)
def read_text(path,*args,**kwargs):
    info=path.stat()
    if (info.st_dev,info.st_ino)==identity: reads.append("text")
    return original_text_read(path,*args,**kwargs)
def open_fd(path,flags,*args,**kwargs):
    if context.get("swap") and path==context.get("leaf","a.txt") and kwargs.get("dir_fd") is not None and not swaps:
        leaf=Path(context["root"])/context.get("leaf","a.txt")
        leaf.unlink();leaf.symlink_to(external);swaps.append(True)
    fd=original_open(path,flags,*args,**kwargs);owned.add(fd);return fd
def close_fd(fd):
    owned.discard(fd);return original_close(fd)
os.read=read_fd;os.open=open_fd;os.close=close_fd;Path.read_bytes=read_path;Path.read_text=read_text
if context.get("wrong_owner"):
    current=os.geteuid();os.geteuid=lambda: current+1
sys.path.insert(0,str(Path(context["script"]).parent))
sys.argv=[context["script"],*context["args"]]
rc=0
try:
    runpy.run_path(context["script"],run_name="__main__")
except SystemExit as exc:
    rc=exc.code
finally:
    # fdopen and file objects close without calling the Python os.close wrapper.
    remaining=[]
    for fd in owned:
        try: os.fstat(fd)
        except OSError as exc:
            if exc.errno != errno.EBADF: raise
        else: remaining.append(fd)
    Path(context["observation"]).write_text(json.dumps({"external_reads":reads,"swaps":swaps,"owned_fds_remaining":len(remaining)}))
raise SystemExit(rc)
'''


def run_packet_cli(root, tmp_path, outside, operation, *, swap=False, wrong_owner=False,
                   script=None, args=None, leaf="a.txt"):
    script = PACKAGER if script is None else script
    observation = tmp_path / "observation.json"
    context = tmp_path / "context.json"
    context.write_text(json.dumps({
        "script": str(script), "root": str(root), "external": str(outside),
        "swap": swap, "wrong_owner": wrong_owner, "observation": str(observation), "leaf": leaf,
        "args": args if args is not None else ["--inventory-root", str(root), "--inventory", "inputs.json",
                 "--" + operation + "-manifest", "manifest.json"],
    }))
    result = subprocess.run([sys.executable, "-I", "-c", _PACKET_CLI_OBSERVER, str(context)],
                            capture_output=True, text=True, timeout=10,
                            env={"PATH": os.defpath, "HOME": str(tmp_path)})
    evidence = json.loads(observation.read_text())
    return result, evidence



def run_packet(scenario: dict, tmp_root: Path, script: Path = PACKAGER) -> dict:
    """Execute a declared CLI action and judge observable IO, without a model."""
    tmp_root.mkdir(parents=True)
    root, source = tmp_root / "packet", tmp_root / "source"
    root.mkdir()
    source.mkdir()
    (root / "a.txt").write_text("ordinary declared fixture")
    (source / "b.txt").write_text("ordinary second-root fixture")
    outside = tmp_root / "outside.txt"
    outside.write_text("synthetic outside sentinel")
    inventory = {"roots": {"packet": str(root), "source": str(source)},
                 "files": [{"root": "packet", "path": "a.txt"},
                           {"root": "source", "path": "b.txt"}]}
    if scenario["plant"] == "parent":
        (root / "nested").mkdir()
        (root / "a.txt").rename(root / "nested/a.txt")
        inventory["files"][0]["path"] = "nested/a.txt"
    if scenario["plant"] == "overlap_output":
        nested = root / "nested"
        nested.mkdir()
        (root / "a.txt").rename(nested / "a.txt")
        inventory["roots"]["packet"] = str(nested)
    (root / "inputs.json").write_text(json.dumps(inventory))
    operation, plant = scenario["operation"], scenario["plant"]
    preparation = None
    if operation == "verify":
        prepared, observation = run_packet_cli(root, tmp_root, outside, "collect", script=script)
        preparation = {"exit_code": prepared.returncode, **observation}
    if plant == "leaf":
        (root / "a.txt").unlink()
        (root / "a.txt").symlink_to(outside)
    elif plant == "parent":
        (source / "a.txt").write_bytes(outside.read_bytes())
        outside = source / "a.txt"
        (root / "nested").rename(root / "saved")
        (root / "nested").symlink_to(source, target_is_directory=True)
    elif plant == "hardlink":
        (root / "a.txt").unlink()
        os.link(outside, root / "a.txt")
    elif plant == "fifo":
        (root / "a.txt").unlink()
        os.mkfifo(root / "a.txt")
        outside = root / "a.txt"
    elif plant == "manifest_scope":
        manifest = root / "manifest.json"
        value = json.loads(manifest.read_text())
        value["files"][0]["path"] = str(outside)
        manifest.write_text(json.dumps(value))
    args = None
    if plant in {"inventory_output", "overlap_output"}:
        destination = "inputs.json" if plant == "inventory_output" else "nested/a.txt"
        args = ["--inventory-root", str(root), "--inventory", "inputs.json",
                "--collect-manifest", destination]
        outside = root / ("nested/a.txt" if plant == "overlap_output" else "a.txt")
        # Admission must precede even declared content reads.
    before = (root / "inputs.json").read_bytes()
    result, observation = run_packet_cli(root, tmp_root, outside, operation,
                                        swap=plant == "swap", wrong_owner=plant == "ownership", script=script, args=args)
    if plant == "inventory_output":
        observation["authority_unchanged"] = (root / "inputs.json").read_bytes() == before
    return {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
            "preparation": preparation, "root": str(root), **observation}


def judge_packet(scenario: dict, trace: dict) -> list[str]:
    problems = []
    expected = 0 if scenario["expect"] == "allow" else 2
    if trace["exit_code"] != expected:
        problems.append(f"expected exit {expected}, got {trace['exit_code']}")
    if trace["external_reads"]:
        problems.append("content outside the declared inventory was read")
    if trace["owned_fds_remaining"]:
        problems.append("input descriptors remained open")
    if trace.get("authority_unchanged") is False:
        problems.append("inventory authority was changed")
    prepared = trace["preparation"]
    if prepared and (prepared["exit_code"] or prepared["external_reads"] or prepared["owned_fds_remaining"]):
        problems.append("ordinary preparation did not complete safely")
    if scenario["expect"] == "allow":
        if "explicit input boundary verified" not in trace["stdout"]:
            problems.append("no successful CLI verification evidence")
    elif "verified" in trace["stdout"] or trace["root"] in trace["stderr"]:
        problems.append("a refused CLI action reported success or exposed its root")
    if scenario["plant"] == "swap" and trace["swaps"] != [True]:
        problems.append("the controlled file swap was not reached")
    return problems



def run_renderer(scenario: dict, tmp_root: Path, script: Path = RENDERER) -> dict:
    """Exercise the existing offline renderer CLI with owned selected inputs."""
    tmp_root.mkdir(parents=True)
    root, cache = tmp_root / "source", tmp_root / "cache"
    root.mkdir()
    cache.mkdir()
    # These are the renderer's existing offline cache inputs, not role substitutes.
    for kind in ("standing_rule", "agent_policy", "task_policy"):
        (cache / (kind + ".json")).write_text(json.dumps({"entities": []}))
    selected = scenario.get("selected", "CLAUDE.md")
    leaf = root / selected
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text("- **Always** preserve an ordinary synthetic rule.\n")
    outside = tmp_root / "outside.txt"
    outside.write_text("- **Never** acquire the unselected synthetic rule.\n")
    plant = scenario["plant"]
    if plant in {"leaf", "hardlink", "fifo"}:
        leaf.unlink()
        if plant == "leaf":
            leaf.symlink_to(outside)
        elif plant == "hardlink":
            os.link(outside, leaf)
        else:
            os.mkfifo(leaf)
            outside = leaf
    elif plant == "parent":
        leaf.parent.rename(root / "saved")
        outside = root / "saved" / leaf.name
        leaf.parent.symlink_to(root / "saved", target_is_directory=True)
    output = tmp_root / "render.md"
    args = ["--repository-input-root", str(root), "--cache", str(cache), "--output", str(output)]
    result, observation = run_packet_cli(root, tmp_root, outside, "render", script=script,
                                        args=args, leaf=selected, swap=plant == "swap",
                                        wrong_owner=plant == "ownership")
    return {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
            "root": str(root), "output_exists": output.is_file(), **observation}


def judge_renderer(scenario: dict, trace: dict) -> list[str]:
    problems = []
    allowed = scenario["expect"] == "allow"
    expected = 0 if allowed else 3
    if trace["exit_code"] != expected:
        problems.append(f"expected exit {expected}, got {trace['exit_code']}")
    if trace["external_reads"]:
        problems.append("unselected content was read")
    if trace["owned_fds_remaining"]:
        problems.append("input descriptors remained open")
    if trace["output_exists"] != allowed:
        problems.append("render publication did not match admission")
    if not allowed and (trace["root"] in trace["stderr"] or "boundary rejected" not in trace["stderr"]):
        problems.append("refusal diagnostic was missing or exposed its root")
    if scenario["plant"] == "swap" and trace["swaps"] != [True]:
        problems.append("controlled file swap was not reached")
    return problems


def main() -> int:
    """Run every hook scenario and print the result (launch scenarios need pytest)."""
    import tempfile

    scenarios = load_scenarios()
    failed = 0
    with tempfile.TemporaryDirectory(prefix="cred_eval_") as tmp:
        ws = build_workspace(Path(tmp))
        for scenario in scenarios["hook"]:
            problems = judge_hook(scenario, run_hook(scenario, ws))
            print(("FAIL " if problems else "ok   ") + scenario["id"], *problems)
            failed += bool(problems)
        for scenario in scenarios["packet"]:
            problems = judge_packet(scenario, run_packet(scenario, Path(tmp).resolve() / scenario["id"]))
            print(("FAIL " if problems else "ok   ") + scenario["id"], *problems)
            failed += bool(problems)
        for scenario in scenarios["renderer"]:
            problems = judge_renderer(scenario, run_renderer(scenario, Path(tmp).resolve() / scenario["id"]))
            print(("FAIL " if problems else "ok   ") + scenario["id"], *problems)
            failed += bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
