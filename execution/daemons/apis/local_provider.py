"""The ``claude-local`` harness provider: the claude CLI driving a local model.

The claude CLI runs unchanged, but its Anthropic API calls go to a loopback
proxy (LiteLLM → ``execution/local_llm/ollama_shim.py`` → Ollama) instead of
Anthropic. Running the local model THROUGH the claude CLI, rather than through
a separate agent loop, is what keeps the repo's PreToolUse guards binding: the
stash, sibling-repo, Gmail-send and gh-identity hooks fire on the local path
exactly as they do on the frontier one.

This module owns everything specific to that provider; ``harness_router``
and ``skill_runner`` call into it at their existing provider seam rather than
growing a parallel router.

Configuration is data, never literals. ``APIS_CLAUDE_LOCAL_CONFIG`` (default
``~/.config/ateles/claude-local.json``) holds either the fields below at top
level, or a ``vendor_binding``-shaped record with them under ``config``::

    {
      "enabled": true,
      "base_url": "http://127.0.0.1:4000",
      "model": "qwen3-coder-ollama",
      "context_ceiling_tokens": 32768,
      "eligible_work_classes": ["rebase", "regenerate_generated_files"],
      "chars_per_token": 3.0,
      "output_reserve_tokens": 1024,
      "harness_overhead_tokens": 6000,
      "tool_output_cap_tokens": 3276,
      "default_tools": ["Bash", "Read", "Edit", "Write", "Glob", "Grep"]
    }

A failed local run may fall over only to the cheapest frontier tier: the model the
``vendor_binding`` (model_tiering.py) binds to the ``mechanical`` tier for each
provider. That binding is the single source; this config carries no model
names. A provider with no such binding, or no ``vendor_binding`` at all, is not a
fallback: the run is refused and recorded as a local failure rather than replayed
on a provider's ambient default.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

LOCAL_PROVIDER = "claude-local"

CONFIG_ENV = "APIS_CLAUDE_LOCAL_CONFIG"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "ateles" / "claude-local.json"
GUARDS_PATH_ENV = "APIS_CLAUDE_LOCAL_GUARDS_PATH"
DEFAULT_GUARDS_PATH = Path.home() / ".cache" / "ateles" / "claude-local-guards.json"

# The only work classes that may ever route local-first. Configuration can
# narrow this set, never widen it: judgement work (security and architecture
# lenses, security-fix implementation, any seated reviewer) has no class here,
# so no config value can send it to the local model.
MECHANICAL_WORK_CLASSES = frozenset({
    "rebase",                       # rebase / update-branch onto the base
    "regenerate_generated_files",   # test catalog, decision_state, openapi types, ...
    "carry_forward_check",          # merge-only net-patch comparison
    "ci_log_triage",
    "worktree_hygiene",
})

# Guards that must be present on the local path, by hook filename. Each must
# appear under a PreToolUse matcher covering Bash in .claude/settings.json.
REQUIRED_GUARDS = (
    "git_stash_guard.py",
    "sibling_repo_worktree_guard.py",
    "gmail_send_gate.py",
    "gh_identity_guard.py",
)

# ── Lean local prompt ───────────────────────────────────────────────────────
# The full dispatch prompt (agent_definition.prompt_markdown + the live
# agent_policy rendering, unfiltered) is built for a frontier model with a
# ~200K window and covers every judgement domain the swarm has — Gmail,
# payments, RGPD, correspondence voice, PR review conventions, none of which a
# mechanical local job ever touches. Measured on 2026-09-29 (ateles task
# ent_71387d9c1d1d3d1eef9ecc01): dispatching cicada's full prompt for a
# worktree_hygiene job costs ~44K tokens, of which ~35K (79%, 70 policy rows)
# is `agent_policy` content with zero relevance filter. At a 32K local ceiling
# that prompt cannot fit at all; forcing it to fit at 64K produced a WRONG
# answer that still reported ok=True (7 prunable worktrees counted vs. 56
# true) — the model was not refusing, it was silently dropping the task under
# an oversized, mostly-irrelevant prompt.
#
# `build_lean_prompt` replaces the full prompt for `claude-local` dispatches
# ONLY (see `skill_runner._run_skill_once`'s provider check) with: a one-line
# role statement, the mechanical task's own instructions, and the small set of
# hard rules relevant to THAT work class. Nothing else. Guards enforced by the
# PreToolUse hooks (stash, sibling-repo, Gmail send, gh identity — see
# REQUIRED_GUARDS above) are deliberately NOT restated here: they bind through
# the hook regardless of what the prompt says, so repeating them only spends
# tokens the local window cannot afford.
#
# Frontier dispatches (skill_runner.build_system_prompt) are UNCHANGED by this
# module — this is an additive, local-only prompt path.

LEAN_ROLE_SUMMARY = (
    "You are a swarm worker running a bounded, mechanical task on a local "
    "model with a small context window. Follow the task instructions exactly. "
    "Do only what is asked — no exploration beyond what the task requires, no "
    "extra commentary. Report your result plainly at the end of your reply."
)

# One short paragraph of hard rules per mechanical work class — only the
# rules that class can actually violate. Kept well under what a frontier
# prompt would carry for the same class; guard-enforced rules (never stash,
# never --no-verify, worktree isolation) are omitted here because the
# PreToolUse hooks bind independently of the prompt.
_LEAN_HARD_RULES: dict[str, str] = {
    "rebase": (
        "Integrate the named base into the current branch using the method the "
        "task names: `git rebase <base>` (the default), or a merge commit "
        "(`git merge --no-ff <base>`) when the task asks for one — a merge "
        "commit is the right method for a branch that is already pushed and "
        "reviewed, because a rebase would rewrite its published history. "
        "Resolve conflicts by re-reading the conflicting hunks, never by "
        "discarding either side blindly, then `git add` the resolved files and "
        "continue (`GIT_EDITOR=true git rebase --continue`, or `git commit "
        "--no-edit` for a merge). After integrating, verify the branch "
        "contains the base's HEAD commit and the working tree is clean before "
        "reporting success. Do not force-push unless the task explicitly says "
        "to; if it does, use `--force-with-lease`, never bare `--force`."
    ),
    "regenerate_generated_files": (
        "Run exactly the generator command(s) the task names. Do not hand-"
        "edit a generated file. After regenerating, diff the result against "
        "what was committed; report the file paths that changed. If the "
        "generator reports an error, report the error verbatim rather than "
        "editing the output to make it pass."
    ),
    "carry_forward_check": (
        "Compare the named branch's net patch against the target — report "
        "exactly what differs, with file paths. Do not modify anything; this "
        "is a read-only comparison. State your count or verdict as a single "
        "unambiguous number or line at the end of your reply."
    ),
    "ci_log_triage": (
        "Read the named CI log or run. Report the failing step name, the "
        "first error line, and whether the failure looks environmental or "
        "code-caused. Do not attempt a fix unless the task asks for one. "
        "Quote the exact failing line rather than paraphrasing it."
    ),
    "worktree_hygiene": (
        "Enumerate worktrees with `git worktree list --porcelain` (never "
        "hand-parse `git worktree list`'s human-readable columns — the paths "
        "can contain spaces). A worktree is prunable when its entry has a "
        "line starting with `prunable` (e.g. `prunable gitdir file points to "
        "non-existent location`). Count prunable worktrees by counting those "
        "lines, not by inspecting directories on disk. State the count as a "
        "single number at the end of your reply, e.g. `prunable_count: 7`. "
        "Do not run `git worktree prune` unless the task explicitly asks "
        "for it."
    ),
}


def lean_hard_rules(work_class: str | None) -> str:
    """The hard-rules paragraph for one work class, or '' when none applies."""
    if not work_class:
        return ""
    return _LEAN_HARD_RULES.get(work_class, "")


def build_lean_prompt(work_class: str | None) -> str:
    """The system prompt for a `claude-local` dispatch: role + hard rules.

    Deliberately takes neither ``agent_def.prompt_markdown`` nor a
    ``policy_prompt``, and deliberately does NOT inline the dispatched role's
    SKILL.md either: every role's SKILL.md in this repo is that role's own
    agent_definition mirror (identity, gate protocol, PR/GitHub conventions —
    the same ~14K-character document `render_policy_prompt` was already
    oversized alongside), not a lean, work-class-scoped task description. A
    mechanical dispatch's actual instructions arrive separately, on stdin, as
    the run's own ``prompt`` (see `_run_skill_once`'s
    ``input=stdin_payload if ... else prompt.encode()``) — the model sees
    them as its user turn regardless of what this function returns. This
    system prompt only has to state WHO it is and WHAT class-specific hard
    rules apply; the WHAT-to-do-right-now is the stdin prompt, in full,
    unfiltered, exactly as the caller wrote it.
    """
    rules = lean_hard_rules(work_class)
    parts = [LEAN_ROLE_SUMMARY]
    if rules:
        parts.append(f"## Hard rules for this task class\n\n{rules}")
    return "\n\n---\n\n".join(p for p in parts if p)

# Not a credential: the loopback proxy runs without a master key, but the
# claude CLI needs some API key to skip its login flow.
PLACEHOLDER_API_KEY = "ateles-claude-local-no-credential"  # gitleaks:allow — placeholder, not a credential

# Frontier credentials that must never reach the local proxy.
_FRONTIER_CREDENTIALS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")

DEFAULT_TOOLS = ("Bash", "Read", "Edit", "Write", "Glob", "Grep")

# Failure classes recorded as the failover reason. Only the first two say the
# local path itself is unusable, so only they take claude-local out of routing
# for the cooldown; a too-long prompt or an ordinary task failure does not.
FAILURE_ENDPOINT = "endpoint_unreachable"
FAILURE_GUARDS = "guards_unavailable"
FAILURE_CONTEXT_CEILING = "context_ceiling"
FAILURE_RUN = "local_run_failed"
COOLDOWN_FAILURES = frozenset({FAILURE_ENDPOINT, FAILURE_GUARDS})

_CEILING_SIGNATURES = (
    "ateles-local context ceiling exceeded",  # ollama_shim.CEILING_ERROR_MARKER
    f"{LOCAL_PROVIDER} context ceiling",      # ceiling_refusal(), before launch
    "context_length_exceeded",
    "prompt is too long",
)
_ENDPOINT_SIGNATURES = (
    "connection refused",
    "econnrefused",
    "unable to connect",
    "fetch failed",
    "connection error",
    "api error: 5",
)


class LocalProviderError(RuntimeError):
    """The local path cannot be launched safely; the reason names why."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class LocalProviderConfig:
    base_url: str
    model: str
    context_ceiling_tokens: int
    eligible_work_classes: frozenset[str]
    chars_per_token: float = 3.0
    output_reserve_tokens: int = 1024
    harness_overhead_tokens: int = 6000
    default_tools: tuple[str, ...] = field(default=DEFAULT_TOOLS)
    # Cap, in tokens, on any single tool result the CLI may put in context.
    # 0 means "derive from the ceiling" (see ``tool_output_cap``).
    tool_output_cap_tokens: int = 0


def _is_loopback(url: str) -> bool:
    host = urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_config(record: dict) -> LocalProviderConfig:
    """Validate one config record; raise ValueError naming the defect."""
    if not isinstance(record, dict):
        raise TypeError("config must be a JSON object")
    if isinstance(record.get("config"), dict):
        # vendor_binding-shaped: the slot's fields live under `config`.
        record = {**record["config"], "enabled": record.get("enabled", record["config"].get("enabled", True))}
    if record.get("enabled", True) is not True:
        raise ValueError("disabled")
    base_url = str(record.get("base_url") or "").rstrip("/")
    if urlparse(base_url).scheme not in ("http", "https"):
        raise ValueError(f"base_url must be an http(s) URL, got {base_url!r}")
    if not _is_loopback(base_url):
        # The endpoint receives the full prompt and every file the agent
        # reads. A hosted endpoint needs its own cost/authority policy first.
        raise ValueError(f"base_url must be loopback, got {base_url!r}")
    model = str(record.get("model") or "").strip()
    if not model:
        raise ValueError("model is required")
    ceiling = int(record.get("context_ceiling_tokens") or 0)
    if ceiling <= 0:
        raise ValueError("context_ceiling_tokens must be a positive integer")
    classes = frozenset(str(c) for c in record.get("eligible_work_classes") or ())
    tools = tuple(str(t) for t in record.get("default_tools") or DEFAULT_TOOLS)
    cfg = LocalProviderConfig(
        base_url=base_url,
        model=model,
        context_ceiling_tokens=ceiling,
        # Narrow to the mechanical vocabulary; anything else is ignored.
        eligible_work_classes=classes & MECHANICAL_WORK_CLASSES,
        chars_per_token=float(record.get("chars_per_token") or 3.0),
        output_reserve_tokens=int(record.get("output_reserve_tokens") or 1024),
        harness_overhead_tokens=int(record.get("harness_overhead_tokens") or 6000),
        default_tools=tools,
        tool_output_cap_tokens=int(record.get("tool_output_cap_tokens") or 0),
    )
    if cfg.chars_per_token <= 0 or cfg.output_reserve_tokens < 0 or cfg.harness_overhead_tokens < 0:
        raise ValueError("chars_per_token must be > 0 and token reserves >= 0")
    if cfg.tool_output_cap_tokens < 0:
        raise ValueError("tool_output_cap_tokens must be >= 0")
    if any(t.startswith("mcp__") for t in cfg.default_tools):
        raise ValueError("default_tools must not name MCP tools; the local path has no MCP servers")
    return cfg


def config_path() -> Path:
    raw = os.environ.get(CONFIG_ENV, "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_CONFIG_PATH


def load_config() -> LocalProviderConfig | None:
    """Return the active config, or None when the provider is not configured.

    Read on every call so an operator can enable or disable the provider
    without restarting Apis. Any defect disables the provider (fail closed to
    the frontier providers) rather than raising into dispatch.
    """
    path = config_path()
    if not path.is_file():
        return None
    try:
        return parse_config(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def is_eligible(work_class: str | None, config: LocalProviderConfig | None) -> bool:
    """True only for a configured mechanical work class."""
    return bool(config and work_class and work_class in config.eligible_work_classes)


def ceiling_refusal(system_prompt: str, work_prompt: str, config: LocalProviderConfig) -> str | None:
    """Refuse before launch when the prompt cannot fit the local window.

    The shim refuses per request as well; this catches the common case before
    a subprocess is spent. ``harness_overhead_tokens`` covers the CLI's own
    system prompt and tool schemas (measured ~2-5K tokens in isolated mode).
    """
    estimate = math.ceil((len(system_prompt) + len(work_prompt)) / config.chars_per_token)
    need = estimate + config.harness_overhead_tokens + config.output_reserve_tokens
    if need <= config.context_ceiling_tokens:
        return None
    return (
        f"{LOCAL_PROVIDER} context ceiling: estimated {estimate} prompt tokens + "
        f"{config.harness_overhead_tokens} harness overhead + "
        f"{config.output_reserve_tokens} output reserve exceeds "
        f"{config.context_ceiling_tokens}"
    )


def render_guards_settings(repo_root: Path) -> dict:
    """Build a guards-only settings object from the repo's own hook wiring.

    Keeps only ``hooks.PreToolUse`` (no SessionStart/UserPromptSubmit context
    injection, which would crowd the small local window) and pins each hook
    command to ``repo_root``, because the child may run in a worktree of
    another repo that has no ``.claude/hooks``.
    """
    root = Path(repo_root).resolve()
    if any(ch in str(root) for ch in '"$`\\'):
        raise LocalProviderError(FAILURE_GUARDS, f"repo path {root} cannot be quoted into a hook command")
    settings_file = root / ".claude" / "settings.json"
    try:
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LocalProviderError(FAILURE_GUARDS, f"cannot read {settings_file}: {exc}") from exc

    entries = []
    bash_guards: set[str] = set()
    for entry in (settings.get("hooks") or {}).get("PreToolUse") or []:
        hooks = []
        for hook in entry.get("hooks") or []:
            command = str(hook.get("command") or "").replace("$CLAUDE_PROJECT_DIR", str(root))
            hooks.append({**hook, "command": command})
            name = next((part for part in command.replace('"', " ").split() if part.endswith(".py")), "")
            if name and not Path(name).is_file():
                raise LocalProviderError(FAILURE_GUARDS, f"guard hook missing on disk: {name}")
            if name and "Bash" in str(entry.get("matcher") or "").split("|"):
                bash_guards.add(Path(name).name)
        entries.append({**entry, "hooks": hooks})

    missing = [g for g in REQUIRED_GUARDS if g not in bash_guards]
    if missing:
        raise LocalProviderError(
            FAILURE_GUARDS,
            f"required PreToolUse Bash guards not wired in {settings_file}: {', '.join(missing)}",
        )
    return {"hooks": {"PreToolUse": entries}}


def write_guards_file(repo_root: Path) -> str:
    """Render the guards settings to disk (atomically) and return the path."""
    rendered = render_guards_settings(repo_root)
    raw = os.environ.get(GUARDS_PATH_ENV, "").strip()
    target = Path(raw).expanduser() if raw else DEFAULT_GUARDS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".claude-local-guards-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(rendered, fh, indent=2)
    os.replace(tmp, target)
    return str(target)


def local_tools(agent_tools: list[str], config: LocalProviderConfig) -> list[str]:
    """The built-in tools the local child may use.

    MCP tools are dropped (the local path runs with no MCP servers); an
    unrestricted agent (``['*']``) gets the configured default set rather than
    every built-in tool, whose schemas would not fit the local window.
    """
    tools = [t for t in agent_tools if t != "*" and not t.startswith("mcp__")]
    return tools or list(config.default_tools)


def build_command(
    binary: str,
    system_prompt: str,
    agent_tools: list[str],
    config: LocalProviderConfig,
    guards_path: str,
) -> list[str]:
    """argv for one noninteractive claude-local run; the prompt goes on stdin."""
    tools = ",".join(local_tools(agent_tools, config))
    cmd = [
        binary,
        "--print",
        "--model", config.model,
        # Isolation: no ambient MCP servers and no user, project or local
        # settings — their SessionStart hooks inject rule context the local
        # window cannot hold, and a target worktree's settings.local.json
        # could override the endpoint or credential via `env`. Only the
        # derived guards file loads. Never --bare: it skips these hooks.
        "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}',
        "--setting-sources", "",
        "--settings", guards_path,
        "--tools", tools,
        "--allowed-tools", tools,
        "--system-prompt", system_prompt,
    ]
    if "--bare" in cmd:  # pragma: no cover - structural guarantee, pinned by tests
        raise LocalProviderError(FAILURE_GUARDS, "--bare would skip every guard hook")
    return cmd


def tool_output_cap(config: LocalProviderConfig) -> int:
    """Tokens one tool result may occupy: configured, else a tenth of the window."""
    return config.tool_output_cap_tokens or max(512, config.context_ceiling_tokens // 10)


def apply_env(env: dict[str, str], config: LocalProviderConfig) -> dict[str, str]:
    """Point the child's claude CLI at the local proxy, with no frontier credential."""
    for key in _FRONTIER_CREDENTIALS:
        env.pop(key, None)
    env["ANTHROPIC_BASE_URL"] = config.base_url
    env["ANTHROPIC_API_KEY"] = PLACEHOLDER_API_KEY
    # Background calls (titles, summaries) use the small-model slot; send them
    # to the local model too rather than to an id the proxy does not serve.
    env["ANTHROPIC_SMALL_FAST_MODEL"] = config.model
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = config.model
    env.pop("ANTHROPIC_MODEL", None)
    # The CLI does not know a local model's window and assumes 200K, so its
    # auto-compaction would never fire before the local ceiling. Tell it.
    env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(config.context_ceiling_tokens)
    # Never compact on the local path. Verified 2026-09-29 against claude
    # 2.1.283 on a 32K local window: the CLI's own system prompt and tool
    # schemas plus its compaction reserve leave so little headroom that every
    # compact refills within 3 turns, and the run burns ~80s of local compute
    # before aborting with "Autocompact is thrashing". With compaction off the
    # same overflow surfaces as a plain "Prompt is too long" (a classified
    # context ceiling), which is a failure this module already names.
    env["DISABLE_AUTO_COMPACT"] = "1"
    # Bound what one tool result may add to context, so a large `git log` or
    # file read cannot alone fill the window. The same probe with these caps
    # (compaction left on) completed instead of thrashing.
    cap = tool_output_cap(config)
    env["BASH_MAX_OUTPUT_LENGTH"] = str(int(cap * config.chars_per_token))
    env["CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS"] = str(cap)
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


# Specific reasons under ``local_run_failed``. Each is a signature the claude
# CLI prints when the local run dies for a reason of its own, observed on
# 2026-09-29 (a rebase dispatch: "Autocompact is thrashing" after 158s).
REASON_AUTOCOMPACT_THRASH = "autocompact_thrash"
REASON_UNRECOGNIZED_MODEL = "unrecognized_model"

_THRASH_SIGNATURES = ("autocompact is thrashing", "context refilled to the limit")
# The CLI prints this on stderr for ANY model id it has no catalog entry for,
# on a run that succeeds as well as one that fails (verified 2026-09-29), so
# it is a note about a failed run, never a failure by itself.
_UNRECOGNIZED_MODEL_SIGNATURE = "[claude-code:unrecognized_model]"


def classify_failure(*texts: str) -> str:
    """Name why a local run failed, for the failover record."""
    blob = " ".join(t for t in texts if t).lower()
    if any(sig in blob for sig in _CEILING_SIGNATURES):
        return FAILURE_CONTEXT_CEILING
    if FAILURE_GUARDS in blob:
        return FAILURE_GUARDS
    if any(sig in blob for sig in _ENDPOINT_SIGNATURES):
        return FAILURE_ENDPOINT
    return FAILURE_RUN


def failure_reason(*texts: str) -> str:
    """The specific reason behind a failed local run, or '' when none is known.

    Only meaningful for a run that already failed. ``unrecognized_model`` is
    reported only when nothing more specific explains the failure, since the
    CLI prints it on healthy runs too.
    """
    blob = " ".join(t for t in texts if t).lower()
    if any(sig in blob for sig in _THRASH_SIGNATURES):
        return REASON_AUTOCOMPACT_THRASH
    if classify_failure(blob) == FAILURE_RUN and _UNRECOGNIZED_MODEL_SIGNATURE in blob:
        return REASON_UNRECOGNIZED_MODEL
    return ""


def describe_failure(*texts: str) -> str:
    """``kind`` or ``kind:reason``, the string recorded for a local failure."""
    kind, reason = classify_failure(*texts), failure_reason(*texts)
    return f"{kind}:{reason}" if reason else kind


# ── Post-condition checks ────────────────────────────────────────────────────
# The regression this module exists to fix (ent_71387d9c1d1d3d1eef9ecc01) was
# not a crash: the local model returned a syntactically fine, confidently
# wrong answer (7 prunable worktrees against a true 56) and the dispatch
# recorded `ok: true`. Exit code and "the child replied" are necessary but not
# sufficient for a mechanical task with a checkable ground truth. Each
# function below re-derives that ground truth independently of the model's
# own words and either confirms or refutes the model's stated answer; a
# refutation is surfaced as a failure so `_run_provider_attempts` fails over
# to a frontier provider instead of accepting a wrong local result.
#
# Deliberately narrow: only work classes with an OBJECTIVE, cheaply-computed
# ground truth get a check here. A class with no entry is not verified beyond
# the exit code — same as before this module existed — because inventing a
# check with no real ground truth would just be a second guess with more
# code around it.

_PRUNABLE_COUNT_RE = re.compile(r"prunable_count\s*:\s*(\d+)", re.IGNORECASE)


def _git_porcelain_prunable_count(cwd: str | None) -> int | None:
    """The ground-truth prunable-worktree count, or None if git could not run."""
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    # A prunable worktree's line is `prunable <reason>` (e.g. "prunable
    # gitdir file points to non-existent location"), not the bare word —
    # match the leading token, not the whole line.
    return sum(
        1 for line in proc.stdout.splitlines() if line.strip().startswith("prunable ")
        or line.strip() == "prunable"
    )


def verify_worktree_hygiene(stdout: str, *, cwd: str | None) -> str | None:
    """Refute a `worktree_hygiene` result against the real prunable count.

    Returns a failure reason naming both numbers when the model's stated
    `prunable_count:` disagrees with `git worktree list --porcelain`, or when
    it stated no parseable count at all. Returns None (pass) when the stated
    count matches, or when git itself could not be queried here (a checker
    that cannot establish ground truth must not manufacture a failure).
    """
    truth = _git_porcelain_prunable_count(cwd)
    if truth is None:
        return None
    # The LAST match, not the first: a model that reasons out loud and
    # self-corrects ("prunable_count: 3 ... wait, recounting: prunable_count:
    # 9") states its actual final answer last. Taking the first match would
    # validate against an interim number the model itself abandoned.
    matches = list(_PRUNABLE_COUNT_RE.finditer(stdout or ""))
    if not matches:
        return (
            "worktree_hygiene post-condition: reply carried no parseable "
            "`prunable_count: N` line to check against the true count "
            f"({truth})"
        )
    claimed = int(matches[-1].group(1))
    if claimed != truth:
        return (
            f"worktree_hygiene post-condition: claimed prunable_count="
            f"{claimed} but `git worktree list --porcelain` shows {truth}"
        )
    return None


# work_class -> checker(stdout, cwd=...) -> failure reason | None
_POSTCONDITION_CHECKS = {
    "worktree_hygiene": verify_worktree_hygiene,
}

# Every OTHER mechanical work class, named explicitly rather than left
# implicit. A class with no checker gets no ground-truth verification beyond
# its exit code — same exposure the worktree_hygiene regression had before
# this module existed. Listing them here (checked by
# test_local_provider.py::test_every_mechanical_class_is_accounted_for_in_
# postcondition_checks) makes that a visible, deliberate gap instead of a
# silent one: adding a 6th mechanical work class without adding it to EITHER
# this set or `_POSTCONDITION_CHECKS` fails that test, so the next class
# forces the same "does this need a checker" decision this one did.
_MECHANICAL_CLASSES_WITHOUT_POSTCONDITION_CHECK = frozenset({
    "rebase",
    "regenerate_generated_files",
    "carry_forward_check",
    "ci_log_triage",
})


def verify_postcondition(
    work_class: str | None, stdout: str, *, cwd: str | None
) -> str | None:
    """Run the work class's post-condition check, or None when it has none.

    Called only for `claude-local` results (see `skill_runner`'s use of this
    function) — a frontier dispatch is unaffected. A checker raising is
    treated as "could not verify" rather than a failure: a broken checker
    must not fail a run that may in fact be correct.
    """
    checker = _POSTCONDITION_CHECKS.get(work_class or "")
    if checker is None:
        return None
    try:
        return checker(stdout, cwd=cwd)
    except Exception:  # noqa: BLE001 — a checker defect must not fail the run
        return None
