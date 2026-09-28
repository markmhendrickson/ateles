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
      "default_tools": ["Bash", "Read", "Edit", "Write", "Glob", "Grep"]
    }

No file, ``enabled: false`` or an invalid record means the provider does not
exist for this process: every dispatch stays on the frontier providers.

Two safety properties are enforced here and pinned by tests:

* The command NEVER carries ``--bare``. Verified 2026-09-28 against claude
  2.1.x: ``--bare`` skips every hook, including hooks passed via
  ``--settings``, so a stash probe executed under it. ``build_command``
  asserts the flag is absent.
* The guards file is DERIVED from the repo's own ``.claude/settings.json``
  PreToolUse entries, so the local path binds exactly the guards the frontier
  path does and cannot drift into a stale hand-kept copy. A missing required
  guard refuses the launch rather than running unguarded.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
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
    )
    if cfg.chars_per_token <= 0 or cfg.output_reserve_tokens < 0 or cfg.harness_overhead_tokens < 0:
        raise ValueError("chars_per_token must be > 0 and token reserves >= 0")
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
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


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
