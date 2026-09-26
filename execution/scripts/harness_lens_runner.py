#!/usr/bin/env python3
"""
execution/scripts/harness_lens_runner.py — run ONE swarm review lens on ONE PR
head through a chosen harness provider (claude / codex / cursor), in a
dedicated throwaway worktree, and post the verdict only after it passes the
same checks a Claude bootstrap lens run applies by hand.

Neotoma task ent_898998f41372ce24369fb365: get bootstrap-mode lens reviews and
builds running on Codex and Cursor, not only Claude subagents. Extends the
existing multi-provider load-balancing work (ent_8b99e1df6aa2f0e7d54cae24)
rather than paralleling it — see "WHY THIS REUSES, NOT PARALLELS" below.

WHY THIS REUSES, NOT PARALLELS
-------------------------------
Every piece of provider routing, identity, credential stripping, and
harness_event bookkeeping already exists in
``execution/daemons/apis/dispatch_role.py`` and the ``skill_runner`` /
``harness_router`` it wraps. This script adds NOTHING to that layer except two
threaded-through kwargs (``env_extra``, ``seated_reviewer`` — see
``dispatch_role.dispatch``). Its own job, and nothing else:

  1. render the shared lens brief + the lens's agent_definition prompt into
     one task string,
  2. create/point at a throwaway worktree of the target repo at the target
     head,
  3. call ``dispatch_role.dispatch(...)`` with a per-harness sandbox override
     (an isolated ``HOME``/``CODEX_HOME`` — see GUARD BINDING below),
  4. capture the verdict file the lens writes inside that worktree,
  5. validate it with the SAME reader every Claude lens run uses
     (``swarm_dispatch.lens_own_verdict`` / ``sign_off_is_warranted``) before
     ever touching ``gh pr comment``.

GUARD BINDING (per harness) — the critical part
-------------------------------------------------
Claude Code's PreToolUse/Stop hooks (``git_stash_guard``,
``credential_read_guard`` — PR #1302, unmerged at the time this was written,
``refuse_task_chip``, ``gmail_send_gate``, ``sibling_repo_worktree_guard``) are
harness mechanisms. They hook Claude Code's own tool-call lifecycle and do
NOT run when the child process is ``codex`` or ``cursor-agent`` — those
binaries have never heard of ``.claude/hooks/``. Running a lens on either
without an equivalent bound guard would silently drop every one of those
protections. What each harness offers instead, verified from its own
``--help``/``--version`` output (no model call made to reach any of this):

  * codex   — ``CODEX_HOME`` relocates ALL of codex's persistent state
              (``config.toml``, auth, sessions, MCP config). Pointing it at an
              empty directory for the dispatch means the child cannot read the
              operator's real ``~/.codex/config.toml`` (which sets
              ``approval_policy = "never"`` and marks every relevant project
              path "trusted" — exactly the opposite of what an unattended lens
              run needs) or its stored auth. ``--sandbox workspace-write``
              (already the default in ``_provider_command``) plus
              ``--add-dir`` scoped to the worktree's own git roots is codex's
              process-execution sandbox; it does not, on its own, hide
              ``~/.config/neotoma/.env*`` or ``~/.claude.json`` from a child
              that still has the real ``HOME`` — the ``CODEX_HOME`` override
              is what removes codex's OWN credential surface, and running the
              child with ``cwd`` pinned to the throwaway worktree (never the
              shared clone) plus a workspace-write sandbox is what keeps it
              from reading arbitrary paths outside that root. Codex has no
              hook mechanism analogous to Claude Code's; there is nothing to
              wire a stash guard or a credential-read guard INTO. AGENTS.md is
              a prompt convention, not an enforcement point, so it is not
              claimed as a guard here.
  * cursor  — ``HOME`` relocates ``~/.cursor/`` (mcp.json, cli-config.json,
              stored auth) the same way ``CODEX_HOME`` relocates codex's state.
              ``cursor-agent``'s own sandbox flag (``--sandbox
              enabled|disabled``) and its project ``.cursor/rules`` are a
              prompt/execution-scope convention, not a credential-file guard —
              nothing in ``cursor-agent --help`` denies a specific path from
              being read by whatever the model decides to run. Project-level
              `.cursor/rules` is not populated at all in this repo (checked:
              only the operator's user-level `~/.cursor/rules` exists), so
              there is no existing project convention this could piggyback on
              without first authoring one, which is out of scope here.

  Neither harness can be told "refuse to open this file" the way the Claude
  hooks do. The isolated-HOME approach makes the operator's real credential
  files simply ABSENT from that HOME/CODEX_HOME, which is a stronger property
  than a hook that has to recognize a read attempt and block it — but it is a
  different mechanism, and it does NOT stop a dispatched child from reading
  ``~/.config/neotoma/.env`` (a NON-harness path, outside HOME/CODEX_HOME
  scope) if the sandbox's workspace-write policy lets it wander there. The
  belt-and-suspenders layer is scoping the sandbox/add-dir to the worktree
  root only (codex) and refusing cursor entirely for anything gate-owning
  (see below) — this script does not claim more than it can verify.

  git-stash: neither CLI has a subcommand named `stash`, and neither offers a
  policy hook to deny one shell invocation by name. The isolated worktree is
  itself the mitigation that matters here — ``git_stash_guard.py``'s hazard is
  the SHARED stash ref in the common git dir, which every worktree of the same
  clone shares. A run that never has `git stash` in its prompt, in a
  dedicated worktree it deletes afterward, carries the same practical
  exposure as a Claude run that also just doesn't type the command — the
  guard is prose-enforced on both, and this script's own worktree lifecycle
  (create fresh, remove after) at least denies it a place to leave a stash
  entry that outlives the run.

  What this script REFUSES rather than fakes: any run whose lens OWNS a
  pending pre-impl gate (``owns_pending_gate``/``seated_reviewer`` in
  ``skill_runner`` terms) still may not go to codex/cursor — see
  ``skill_runner._run_skill_once``'s own preflight, which already refuses
  that combination because neither adapter can deny a single MCP tool
  (``mcp__mcpsrv_neotoma__correct``) while granting the rest. This script's
  lens dispatch never requests Neotoma MCP tools for codex/cursor at all (only
  ``provider == "claude"`` gets ``--mcp-config`` injected — see
  ``skill_runner._run_skill_once``), so it does not set ``seated_reviewer``;
  it relies on that existing absence rather than re-deciding it. A caller that
  ever adds MCP grants to the codex/cursor path must revisit this.

  Credentials the run legitimately needs — the `gh` token for posting, and a
  Neotoma bearer if the lens brief needs one — are injected as NAMED
  environment variables onto the child's process env (`GH_TOKEN`/`GITHUB_TOKEN`
  via ``dispatch_role``'s existing ``github_token`` plumbing is the model to
  extend; this script injects the reviewer's OWN token, never a raw path to
  the operator's `.env` file, and never prints it).

USAGE
-----
    python3 execution/scripts/harness_lens_runner.py \\
        --repo owner/name --pr 1234 --head <sha> \\
        --lens pm --agent pavo --provider codex \\
        [--post] [--dry-run] [--compare claude,codex]

Exit codes: 0 on success (or a clean, reported refusal under --dry-run),
non-zero on any failure that prevented a verdict or a required post.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_DAEMON_DIR = REPO_ROOT / "execution" / "daemons" / "apis"
for _p in (str(REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# noqa: E402 — path bootstrap above must run first
import dispatch_role  # noqa: E402
from harness_router import configured_headroom  # noqa: E402
from skill_runner import SkillResult  # noqa: E402

try:
    import swarm_dispatch  # noqa: E402
except Exception:  # pragma: no cover — degraded import path, handled at call site
    swarm_dispatch = None  # type: ignore[assignment]


# No canonical, checked-in copy of the bootstrap-mode lens brief exists in
# this repo as of this script's authorship — it has so far lived only as an
# operator-session scratchpad file (see ent_898998f41372ce24369fb365's own
# "Read first" list). This script therefore has NO baked-in default path: the
# caller must always pass --brief pointing at whichever copy is current. A
# wrong guessed default here would silently run a stale or wrong brief with no
# error, which is worse than requiring the flag every time.
LENS_BRIEF_PATH = None

HARNESSES = ("claude", "codex", "cursor")


# ── Headroom -------------------------------------------------------------------


class HeadroomExhausted(RuntimeError):
    """Raised when the requested provider's configured headroom is 0."""


def check_headroom(provider: str) -> float:
    """Return the provider's configured headroom, raising if it is exactly 0.

    Reads the SAME file/env precedence ``harness_router.configured_headroom``
    already implements (file beats env; see that module's docstring) — this
    function adds no second source of truth, it only turns "0.0" into a loud,
    named refusal instead of a routing failure discovered three steps later.
    A provider sitting above 0 but below ``APIS_HARNESS_MIN_HEADROOM`` is left
    to the router's own eligibility check (``harness_router.provider_candidates``),
    which already refuses it with a specific reason; this function only
    special-cases the exact-zero operator-reset signal named in the task.
    """
    headroom = configured_headroom()
    value = headroom.get(provider, 1.0)
    if value <= 0.0:
        raise HeadroomExhausted(
            f"provider {provider!r} has headroom={value:g} — refusing to "
            "dispatch. This is the operator-reset gate: headroom is restored "
            "by editing ~/.config/ateles/harness-headroom.json (operator- or "
            "session-owned, see the PR body's 'Headroom' section), never by "
            "this script."
        )
    return value


# ── Rendering the lens task -----------------------------------------------------


@dataclass(frozen=True)
class LensTarget:
    repo: str  # "owner/name"
    pr: int
    head: str
    lens: str  # short label, e.g. "pm"
    agent: str  # docs/agents/<agent>.md name, e.g. "pavo"
    focus_notes: str = ""


def render_lens_task(target: LensTarget, brief_path: Path) -> str:
    """Render the shared lens brief + the lens's own agent prompt into one
    task string, exactly as the bootstrap-mode operator-run subagent brief
    describes doing it by hand (see the brief's step 2).

    The agent's own ``docs/agents/<agent>.md`` prompt is read from the LOCAL
    worktree the caller is about to dispatch into (passed in via
    ``agent_prompt_source``-equivalent callers use ``read_agent_prompt``
    below) — not fetched again here — so the two reads are guaranteed to be
    against the SAME head the lens will actually review.
    """
    if not brief_path.is_file():
        raise FileNotFoundError(
            f"lens brief not found at {brief_path} — pass --brief explicitly"
        )
    brief = brief_path.read_text(encoding="utf-8")
    launch = textwrap.dedent(
        f"""\
        LAUNCH MESSAGE
        REPO: {target.repo}
        PR: {target.pr}
        TARGET HEAD: {target.head}
        LENS: {target.lens}
        AGENT: {target.agent}
        FOCUS NOTES: {target.focus_notes or "(none)"}
        """
    )
    return f"{launch}\n---\n\n{brief}"


def read_agent_prompt(worktree: Path, agent: str) -> str:
    """Read docs/agents/<agent>.md from the worktree (i.e. from TARGET HEAD's
    own tree state at fetch time — the worktree is created from origin, and
    docs/agents is generated from Neotoma on origin/main, per the lens brief's
    own instruction to read it from ``origin/main`` rather than the PR branch).
    """
    path = worktree / "docs" / "agents" / f"{agent}.md"
    if not path.is_file():
        raise FileNotFoundError(f"{path} does not exist — unknown lens agent {agent!r}")
    return path.read_text(encoding="utf-8")


# ── Throwaway worktree -----------------------------------------------------------


@dataclass
class Worktree:
    repo_name: str
    path: Path
    _created: bool = field(default=False, repr=False)

    def create(self, *, head: str) -> None:
        repo_path = Path.home() / "repos" / self.repo_name
        subprocess.run(
            ["git", "-C", str(repo_path), "fetch", "-q", "origin"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_path),
                "worktree",
                "add",
                str(self.path),
                head,
            ],
            check=True,
        )
        self._created = True
        # If the checkout carried an env file into the new worktree (some
        # post-checkout hooks copy .env/.env.development for local dev
        # convenience), delete it before anything runs here — the lens brief's
        # own instruction, and doubly necessary once a dispatched child's HOME
        # is isolated but its CWD is this worktree.
        for name in (".env", ".env.development", ".env.local"):
            candidate = self.path / name
            if candidate.is_file():
                candidate.unlink()

    def remove(self) -> None:
        if not self._created:
            return
        repo_path = Path.home() / "repos" / self.repo_name
        subprocess.run(
            ["git", "clean", "-fdxq"], cwd=str(self.path), check=False
        )
        subprocess.run(
            ["git", "-C", str(repo_path), "worktree", "remove", str(self.path)],
            check=False,
        )


# ── Per-harness sandbox env -----------------------------------------------------


@dataclass
class HarnessSandbox:
    """An isolated HOME/CODEX_HOME for one dispatch, and what it does and does
    not guarantee. See the module docstring's GUARD BINDING section — this
    class only builds the env_extra mapping; the reasoning lives there.
    """

    provider: str
    root: Path
    env_extra: dict[str, str]
    unavailable_guards: tuple[str, ...]

    @classmethod
    def build(cls, provider: str, tmp_root: Path) -> "HarnessSandbox":
        sandbox_home = tmp_root / f"{provider}-home"
        sandbox_home.mkdir(parents=True, exist_ok=True)
        if provider == "codex":
            return cls(
                provider=provider,
                root=sandbox_home,
                env_extra={"CODEX_HOME": str(sandbox_home)},
                unavailable_guards=(
                    "git_stash_guard (no hook mechanism in codex; mitigated "
                    "only by the throwaway worktree's own lifecycle)",
                    "sibling_repo_worktree_guard (same)",
                    "gmail_send_gate (not applicable — this runner never "
                    "invokes gws/gmail)",
                    "refuse_task_chip (not applicable — no harness task-chip "
                    "surface exists outside Claude Code)",
                ),
            )
        if provider == "cursor":
            return cls(
                provider=provider,
                root=sandbox_home,
                env_extra={"HOME": str(sandbox_home)},
                unavailable_guards=(
                    "git_stash_guard (no hook mechanism in cursor-agent; "
                    "mitigated only by the throwaway worktree's own lifecycle)",
                    "sibling_repo_worktree_guard (same)",
                    "gmail_send_gate (not applicable)",
                    "refuse_task_chip (not applicable)",
                    "credential_read_guard (cursor-agent has no per-path deny; "
                    "HOME isolation removes cursor's OWN credential store only)",
                ),
            )
        # claude: runs under this session's own hook set already (or, when
        # dispatched headless via `claude --print`, under whatever hooks its
        # own settings.json wires — unaffected by this script).
        return cls(provider=provider, root=sandbox_home, env_extra={}, unavailable_guards=())


def refuse_if_guard_required(provider: str, *, needs_full_guards: bool) -> str | None:
    """Return a refusal reason when *provider* cannot mechanically enforce a
    guard this run needs, or None to proceed.

    ``needs_full_guards`` is True for any run this script cannot itself scope
    to a throwaway worktree with an isolated HOME (i.e. would rely on the
    harness's own credential/stash protections) — the task's own hard rule:
    "make the runner refuse that harness for work that needs the guard rather
    than silently running unguarded." Today every lens review this script
    dispatches DOES get a throwaway worktree + isolated HOME, so this refusal
    exists for callers/future extensions that skip that setup, not for the
    normal path.
    """
    if provider == "claude":
        return None
    if needs_full_guards:
        return (
            f"provider {provider!r} has no PreToolUse/Stop hook mechanism "
            "equivalent to Claude Code's, and this run was requested WITHOUT "
            "the throwaway-worktree + isolated-HOME sandbox this script "
            "normally applies — refusing rather than running unguarded."
        )
    return None


# ── dry-run rendering -------------------------------------------------------------


def dry_run_report(
    target: LensTarget,
    *,
    provider: str,
    sandbox: HarnessSandbox,
    task_text: str,
    worktree_path: Path,
) -> dict:
    """Everything --dry-run prints: the exact command shape, prompt size, and
    guard configuration — and NO model call. This mirrors, rather than calls,
    the argv `_provider_command` in skill_runner would build, because that
    function requires a resolved binary path and network is not the point of
    a dry run; it is derived from the SAME flags skill_runner uses today so a
    drift between this preview and the real dispatch is visible as a diff
    against `_provider_command` in review, not a silent divergence.
    """
    if provider == "codex":
        example_cmd = [
            "codex", "exec", "--sandbox", "workspace-write",
            "--add-dir", str(worktree_path),
            "--ephemeral", "--skip-git-repo-check", "--color", "never",
            "--cd", str(worktree_path), "-",
        ]
    elif provider == "cursor":
        example_cmd = [
            "cursor-agent", "--print", "--force", "--trust", "--approve-mcps",
            "--output-format", "text", "--workspace", str(worktree_path),
            "<prompt omitted — see prompt_chars>",
        ]
    else:
        example_cmd = ["claude", "--print", "--append-system-prompt", "<system prompt>"]

    return {
        "dry_run": True,
        "provider": provider,
        "repo": target.repo,
        "pr": target.pr,
        "head": target.head,
        "lens": target.lens,
        "agent": target.agent,
        "worktree": str(worktree_path),
        "sandbox_env_extra": sandbox.env_extra,
        "unavailable_guards": list(sandbox.unavailable_guards),
        "example_command": example_cmd,
        "prompt_chars": len(task_text),
        "no_model_call_made": True,
    }


# ── Posting gate -----------------------------------------------------------------


@dataclass
class VerdictCheck:
    """Result of validating a captured verdict file before any post."""

    ok: bool
    reason: str
    lens_verdict: str | None = None
    sign_off_warranted: bool | None = None
    pre_post: dict = field(default_factory=dict)


def validate_verdict(verdict_text: str, *, lens_agent: str) -> VerdictCheck:
    """Run the SAME reader a Claude bootstrap lens run is instructed to run
    by hand (the lens brief's PRE-POST CHECK step) — swarm_dispatch's own
    ``lens_own_verdict`` / ``sign_off_is_warranted`` — so a codex/cursor
    verdict is held to the identical bar a Claude verdict is, not a
    weaker parallel check this script invents.
    """
    lines = verdict_text.splitlines()
    pre_post = {
        "line1": lines[0] if len(lines) > 0 else "",
        "line2": lines[1] if len(lines) > 1 else "",
        "line3": lines[2] if len(lines) > 2 else "",
        "blocking_count": verdict_text.count("[BLOCKING]"),
    }
    if swarm_dispatch is None:
        return VerdictCheck(
            ok=False,
            reason="swarm_dispatch could not be imported — cannot validate a "
            "verdict without the real reader; refusing to post rather than "
            "invent a weaker check",
            pre_post=pre_post,
        )
    verdict = swarm_dispatch.lens_own_verdict(verdict_text, lens_agent=lens_agent)
    warranted = swarm_dispatch.sign_off_is_warranted(verdict_text, lens_agent=lens_agent)
    if verdict is None:
        return VerdictCheck(
            ok=False,
            reason=(
                "lens_own_verdict returned None — the verdict is not readable "
                "at the fixed position (second verdict-like line, missing "
                "header, or quoted header are the usual causes; see the lens "
                "brief's PRE-POST CHECK step)"
            ),
            lens_verdict=verdict,
            sign_off_warranted=warranted,
            pre_post=pre_post,
        )
    return VerdictCheck(
        ok=True,
        reason="verdict readable at fixed position",
        lens_verdict=verdict,
        sign_off_warranted=warranted,
        pre_post=pre_post,
    )


def gh_login() -> str:
    out = subprocess.run(
        ["gh", "api", "user", "-q", ".login"],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip()


def post_verdict(*, repo: str, pr: int, verdict_path: Path) -> str:
    """Post the verdict file as a PR comment and return the comment URL.

    Never called unless ``validate_verdict(...).ok`` AND the caller passed
    ``--post`` — the caller (main()) enforces both; this function trusts its
    caller rather than re-checking, so its own tests can drive it directly.
    """
    result = subprocess.run(
        [
            "gh", "pr", "comment", str(pr), "-R", repo,
            "--body-file", str(verdict_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


# ── Single-provider run -----------------------------------------------------------


async def run_one(
    target: LensTarget,
    *,
    provider: str,
    post: bool,
    dry_run: bool,
    repo_worktree_name: str,
    scratch_root: Path,
    brief_path: Path,
    timeout: int | None,
) -> dict:
    """Run the lens once on one provider. Returns a JSON-serializable report.

    Raises HeadroomExhausted before anything else (no worktree, no dispatch)
    when the provider's configured headroom is exactly 0 — the operator-reset
    gate named in the task.
    """
    check_headroom(provider)

    sandbox = HarnessSandbox.build(provider, scratch_root)
    refusal = refuse_if_guard_required(provider, needs_full_guards=False)
    if refusal:
        return {"ok": False, "provider": provider, "reason": refusal}

    worktree_path = scratch_root / f"{repo_worktree_name}-wt-{target.lens}-{target.pr}-{provider}"
    worktree = Worktree(repo_name=repo_worktree_name, path=worktree_path)

    if dry_run:
        # A dry run still creates nothing model-facing, but it DOES need the
        # real agent prompt to report an accurate prompt_chars figure, and
        # that prompt lives in a worktree at TARGET HEAD. Building the
        # worktree is a `git worktree add` (explicitly allowed by every
        # relevant guard) and is removed again before returning.
        worktree.create(head=target.head)
        try:
            agent_prompt = read_agent_prompt(worktree.path, target.agent)
            task_text = render_lens_task(target, brief_path) + "\n\n---\n\n" + agent_prompt
            report = dry_run_report(
                target, provider=provider, sandbox=sandbox,
                task_text=task_text, worktree_path=worktree.path,
            )
        finally:
            worktree.remove()
        return report

    worktree.create(head=target.head)
    try:
        agent_prompt = read_agent_prompt(worktree.path, target.agent)
        task_text = render_lens_task(target, brief_path) + "\n\n---\n\n" + agent_prompt
        verdict_path = worktree.path / f"{target.lens}{target.pr}_verdict.md"

        result: SkillResult = await dispatch_role.dispatch(
            target.agent,
            (
                f"{task_text}\n\n---\n\nWrite your verdict to the file "
                f"{verdict_path} (create it yourself with the exact strict "
                "format the brief describes) and print its contents to "
                "stdout when done."
            ),
            provider=provider,
            cwd=str(worktree.path),
            timeout=timeout,
            env_extra=sandbox.env_extra,
            seated_reviewer=False,  # see module docstring: no MCP grant requested
        )

        if not result.ok:
            return {
                "ok": False,
                "provider": provider,
                "reason": result.error or "dispatch failed",
                "attempted_providers": list(result.attempted_providers),
                "stderr": result.stderr,
            }

        verdict_text = (
            verdict_path.read_text(encoding="utf-8")
            if verdict_path.is_file()
            else result.stdout
        )
        check = validate_verdict(verdict_text, lens_agent=target.agent)

        report = {
            "ok": check.ok,
            "provider": provider,
            "lens": target.lens,
            "agent": target.agent,
            "lens_verdict": check.lens_verdict,
            "sign_off_warranted": check.sign_off_warranted,
            "pre_post": check.pre_post,
            "verdict_text": verdict_text,
            "posted": False,
            "comment_url": "",
        }
        if not check.ok:
            report["refusal_reason"] = check.reason
            return report

        # Re-check the head before posting — the brief's own step 1, applied
        # here rather than trusted from before dispatch (a long-running codex
        # attempt could span a force-push).
        head_now = subprocess.run(
            ["gh", "pr", "view", str(target.pr), "-R", target.repo, "--json", "headRefOid"],
            capture_output=True, text=True, check=False,
        )
        try:
            current_head = json.loads(head_now.stdout or "{}").get("headRefOid", "")
        except json.JSONDecodeError:
            current_head = ""
        if current_head and current_head != target.head:
            report["ok"] = False
            report["refusal_reason"] = (
                f"PR head moved from {target.head} to {current_head} during "
                "the run — refusing to post a verdict against a stale head"
            )
            return report

        if not post:
            report["refusal_reason"] = "--post not set; verdict validated but not posted"
            return report

        login = gh_login()
        if login != "ateles-agent":
            report["ok"] = False
            report["refusal_reason"] = (
                f"gh api user -q .login returned {login!r}, expected "
                "'ateles-agent' — refusing to post under the wrong identity"
            )
            return report

        report["comment_url"] = post_verdict(
            repo=target.repo, pr=target.pr, verdict_path=verdict_path
        )
        report["posted"] = True
        return report
    finally:
        worktree.remove()


async def run_compare(
    target: LensTarget,
    *,
    providers: list[str],
    dry_run: bool,
    post: bool = False,
    repo_worktree_name: str,
    scratch_root: Path,
    brief_path: Path,
    timeout: int | None,
) -> dict:
    """Run the same lens on the same head via each of *providers* and report
    both verdicts side by side.

    Posting is unconditionally OFF in comparison mode — the task's own spec
    ("Posting is off by default" for this mode). ``post`` is accepted only so
    a caller's mistaken ``--post`` produces a documented no-op rather than a
    TypeError; it is deliberately NEVER forwarded to ``run_one`` below,
    whatever value it carries. A caller who wants ONE of the compared
    providers' verdicts posted runs that provider again, alone, via
    ``run_one``/``--provider`` after inspecting the comparison.
    """
    results = {}
    for provider in providers:
        results[provider] = await run_one(
            target,
            provider=provider,
            post=False,
            dry_run=dry_run,
            repo_worktree_name=repo_worktree_name,
            scratch_root=scratch_root,
            brief_path=brief_path,
            timeout=timeout,
        )
    return {"compare": True, "lens": target.lens, "pr": target.pr, "results": results}


# ── CLI ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness_lens_runner",
        description=(
            "Run one swarm review lens on one PR head through a chosen "
            "harness provider, in a throwaway worktree, posting only after "
            "the verdict passes the same reader Claude lens runs use."
        ),
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head", required=True, help="full 40-hex head SHA")
    parser.add_argument("--lens", required=True, help="short lens label, e.g. pm")
    parser.add_argument("--agent", required=True, help="docs/agents/<agent>.md name")
    parser.add_argument("--focus-notes", default="")
    parser.add_argument(
        "--repo-worktree-name",
        default=None,
        help="Local clone dirname under ~/repos (default: last segment of --repo)",
    )
    parser.add_argument(
        "--brief",
        required=True,
        help=(
            "Path to the shared lens brief markdown file. No default: see "
            "the LENS_BRIEF_PATH comment above main()'s definitions for why."
        ),
    )
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument(
        "--provider", choices=list(HARNESSES), help="Single-provider run"
    )
    parser.add_argument(
        "--compare",
        help="Comma-separated providers to run and report side by side, e.g. claude,codex",
    )
    parser.add_argument(
        "--post",
        action="store_true",
        help="Post the validated verdict as a PR comment. Off by default, "
        "and OFF by default in --compare mode regardless of this flag.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the exact command and prompt size; make no model call.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not args.provider and not args.compare:
        parser.error("one of --provider or --compare is required")
    if args.provider and args.compare:
        parser.error("--provider and --compare are mutually exclusive")

    target = LensTarget(
        repo=args.repo,
        pr=args.pr,
        head=args.head,
        lens=args.lens,
        agent=args.agent,
        focus_notes=args.focus_notes,
    )
    repo_worktree_name = args.repo_worktree_name or args.repo.split("/")[-1]
    brief_path = Path(args.brief)

    with tempfile.TemporaryDirectory(prefix="harness-lens-runner-") as tmp:
        scratch_root = Path(tmp)
        try:
            if args.compare:
                providers = [p.strip() for p in args.compare.split(",") if p.strip()]
                for p in providers:
                    if p not in HARNESSES:
                        parser.error(f"unknown provider in --compare: {p!r}")
                # Comparison mode never posts, per the task's own spec —
                # regardless of --post, which is accepted but ignored here so
                # a caller who forgets this rule gets a documented no-op
                # rather than a surprising post.
                report = asyncio.run(
                    run_compare(
                        target, providers=providers, dry_run=args.dry_run,
                        post=False, repo_worktree_name=repo_worktree_name,
                        scratch_root=scratch_root, brief_path=brief_path,
                        timeout=args.timeout,
                    )
                )
            else:
                report = asyncio.run(
                    run_one(
                        target, provider=args.provider, post=args.post,
                        dry_run=args.dry_run, repo_worktree_name=repo_worktree_name,
                        scratch_root=scratch_root, brief_path=brief_path,
                        timeout=args.timeout,
                    )
                )
        except HeadroomExhausted as exc:
            report = {"ok": False, "reason": str(exc)}
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            report = {"ok": False, "reason": str(exc)}

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(report)

    ok = report.get("ok", False) if "compare" not in report else all(
        r.get("ok", False) for r in report["results"].values()
    )
    return 0 if ok or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
