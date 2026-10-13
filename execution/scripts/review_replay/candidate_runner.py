"""Run one lens child for one provider kind, inside the runner's own sandbox.

This is the "child runner" that ``harness_lens_runner.run_one`` calls in replay
mode. It does NOT replace the runner: the throwaway checkout, the probed
sandbox profile, the lens prompt, the shared brief and the verdict reader all
stay in ``harness_lens_runner``. What it adds is the one step the runner's
production dispatch cannot do, which is to run the CLI in a structured output
mode so that tokens, turns, tool errors and cost are measured rather than
guessed, for these provider kinds:

    claude                the Claude CLI on its subscription login
    codex                 the Codex CLI on its subscription login
    openrouter:<model>    the Claude CLI pointed at OpenRouter's
                          Anthropic-compatible endpoint (through a metering proxy)
    ollama:<model>        the Claude CLI pointed at a local Ollama server
                          (through a metering proxy that keeps thinking off)

The candidate's environment is built from an allowlist (see ``build_child_env``)
and holds no GitHub, Neotoma, or endpoint credential: hosted candidates get a
placeholder token and a loopback URL, and the metering proxy in this process
adds the real key on the way out.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import REPO_ROOT
from .metering_proxy import MeteringProxy, ProxySummary

import harness_lens_runner as hlr  # noqa: E402
import local_provider  # noqa: E402
import skill_runner  # noqa: E402
from dispatch_usage import DispatchUsage  # noqa: E402
from skill_runner import SkillResult  # noqa: E402

OPENROUTER_UPSTREAM = "https://openrouter.ai/api"
OLLAMA_UPSTREAM = "http://127.0.0.1:11434"
PLACEHOLDER_TOKEN = (
    "replay-placeholder-not-a-credential"  # gitleaks:allow - placeholder
)
DEFAULT_TOOLS = "Bash,Read,Grep,Glob,Write"
DEFAULT_OPENROUTER_KEY_REF = "op://Private/OpenRouter/API key"

# Variable names a candidate's environment may carry. Anything else the parent
# holds is dropped. The tests print and check only NAMES, never values.
CHILD_ENV_ALLOWED_NAMES = frozenset(
    {
        "PATH",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "NO_COLOR",
        "HOME",
        "XDG_CONFIG_HOME",
        "GH_CONFIG_DIR",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_TERMINAL_PROMPT",
        "GCM_INTERACTIVE",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_CONFIG_KEY_1",
        "GIT_CONFIG_VALUE_1",
        "ATELES_LOCAL_REVIEW_HOME",
        skill_runner.AGENT_CHILD_MARKER_ENV,
        # provider capability, per kind (see build_child_env)
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CODEX_HOME",
        # endpoint wiring for candidates (placeholder token, loopback URL)
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "DISABLE_AUTOUPDATER",
        "DISABLE_TELEMETRY",
        "DISABLE_AUTO_COMPACT",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
    }
)


class CandidateError(RuntimeError):
    """A candidate could not be launched (missing key, missing CLI, ...)."""


# -- Credentials: the endpoint key lives only in this process ----------------------

_KEY_CACHE: dict[str, str] = {}


def load_openrouter_key(ref: str = DEFAULT_OPENROUTER_KEY_REF) -> str:
    """Read the OpenRouter key from the password manager, in process.

    The value goes to the metering proxy and nowhere else: it is not logged,
    not placed in any child environment or argv, and not written to disk.
    """
    if ref in _KEY_CACHE:
        return _KEY_CACHE[ref]
    try:
        out = subprocess.run(
            ["op", "read", ref], capture_output=True, text=True, timeout=600
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CandidateError(
            f"could not run the password manager: {type(exc).__name__}"
        ) from exc
    key = out.stdout.strip()
    if out.returncode != 0 or not key:
        raise CandidateError(
            "the password manager did not return the OpenRouter key (is it unlocked?)"
        )
    _KEY_CACHE[ref] = key
    return key


def openrouter_generation_lookup(key: str) -> Callable[[str], float | None]:
    """Cost of one finished generation from OpenRouter's own accounting."""

    def lookup(generation_id: str) -> float | None:
        for attempt in range(4):
            req = urllib.request.Request(
                f"https://openrouter.ai/api/v1/generation?id={generation_id}",
                headers={
                    "Authorization": f"Bearer {key}",
                    "User-Agent": "ateles-review-replay/1.0",
                },
            )
            try:
                data = json.loads(urllib.request.urlopen(req, timeout=30).read())[
                    "data"
                ]
                cost = data.get("total_cost")
                return float(cost) if cost is not None else None
            except Exception:
                time.sleep(1.5 * (attempt + 1))
        return None

    return lookup


# -- Environment ---------------------------------------------------------------------


def build_child_env(
    kind: str,
    model: str | None,
    sandbox: "hlr.HarnessSandbox",
    *,
    proxy_url: str | None = None,
    local_context_tokens: int | None = None,
) -> dict[str, str]:
    """The complete environment of a candidate child.

    Starts from the runner's own ``local_review`` environment (an allowlist with
    an isolated HOME, no GitHub config, git credential helpers disabled), then
    applies per-kind rules:

    * ``claude``: keeps its subscription capability only.
    * ``codex``: keeps its isolated ``CODEX_HOME`` only.
    * ``openrouter`` / ``ollama``: drops every subscription capability and points
      the Claude CLI at the loopback proxy with a placeholder token.

    A final pass keeps only names in ``CHILD_ENV_ALLOWED_NAMES``.
    """
    env = skill_runner._subscription_only_env(
        sandbox.env_extra, local_review=True, local_review_home=str(sandbox.root)
    )
    if kind in ("openrouter", "ollama"):
        if not proxy_url or not model:
            raise CandidateError(f"{kind} needs a proxy URL and a model")
        for name in ("CLAUDE_CODE_OAUTH_TOKEN", "CODEX_HOME"):
            env.pop(name, None)
        env.update(
            {
                "ANTHROPIC_BASE_URL": proxy_url,
                "ANTHROPIC_AUTH_TOKEN": PLACEHOLDER_TOKEN,
                "ANTHROPIC_API_KEY": "",
                "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
                "ANTHROPIC_SMALL_FAST_MODEL": model,
                "DISABLE_AUTOUPDATER": "1",
                "DISABLE_TELEMETRY": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            }
        )
        if kind == "ollama" and local_context_tokens:
            env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(local_context_tokens)
            env["DISABLE_AUTO_COMPACT"] = "1"
    elif kind == "claude":
        env.pop("CODEX_HOME", None)
    elif kind == "codex":
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    return {k: v for k, v in env.items() if k in CHILD_ENV_ALLOWED_NAMES}


# -- Commands ------------------------------------------------------------------------


def claude_family_command(
    binary: str,
    model: str | None,
    *,
    guards_path: Path,
    empty_mcp_path: Path,
    tools: str = DEFAULT_TOOLS,
) -> list[str]:
    """Claude CLI argv for a review child: no MCP, no ambient settings, only the
    repo's PreToolUse guards, a fixed small tool set, structured output."""
    cmd = [
        binary,
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--strict-mcp-config",
        "--mcp-config",
        str(empty_mcp_path),
        "--setting-sources",
        "",
        "--settings",
        str(guards_path),
        "--tools",
        tools,
        "--allowed-tools",
        tools,
        "--disable-slash-commands",
        "--no-session-persistence",
    ]
    if model:
        cmd += ["--model", model]
    return cmd


def codex_command(
    binary: str, model: str | None, cwd: str, effort: str | None
) -> list[str]:
    """Codex argv from the production builder, plus ``--json`` for usage events."""
    cmd, _ = skill_runner._provider_command(
        "codex", binary, "", "", cwd=cwd, codex_outer_sandboxed=True, model=model
    )
    idx = cmd.index("exec") + 1
    extra = ["--json"]
    if effort:
        extra += ["-c", f'model_reasoning_effort="{effort}"']
    cmd[idx:idx] = extra
    return cmd


# -- Output parsing -------------------------------------------------------------------


@dataclass
class StreamMetrics:
    final_text: str = ""
    turns: int | None = None
    tool_calls: int = 0
    tool_errors: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reported_cost_usd: float | None = None
    model: str | None = None
    is_error: bool = False
    error_text: str = ""


def _iter_json_lines(text: str):
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


def parse_claude_stream(stdout: str) -> StreamMetrics:
    m = StreamMetrics()
    assistant_ids: set[str] = set()
    for ev in _iter_json_lines(stdout):
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            m.model = m.model or ev.get("model")
        elif t == "assistant":
            msg = ev.get("message") or {}
            if msg.get("id"):
                assistant_ids.add(str(msg["id"]))
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    m.tool_calls += 1
        elif t == "user":
            for block in (ev.get("message") or {}).get("content") or []:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and block.get("is_error")
                ):
                    m.tool_errors += 1
        elif t == "result":
            m.final_text = str(ev.get("result") or "")
            m.is_error = bool(ev.get("is_error"))
            if m.is_error:
                m.error_text = (m.final_text or str(ev.get("subtype") or ""))[:300]
            if isinstance(ev.get("num_turns"), int):
                m.turns = ev["num_turns"]
            usage = ev.get("usage") or {}
            for attr, key in (
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
                ("cache_read_tokens", "cache_read_input_tokens"),
                ("cache_write_tokens", "cache_creation_input_tokens"),
            ):
                if isinstance(usage.get(key), (int, float)):
                    setattr(m, attr, int(usage[key]))
            if isinstance(ev.get("total_cost_usd"), (int, float)):
                m.reported_cost_usd = float(ev["total_cost_usd"])
    if m.turns is None and assistant_ids:
        m.turns = len(assistant_ids)
    return m


def parse_codex_stream(stdout: str) -> StreamMetrics:
    m = StreamMetrics()
    turns = 0
    for ev in _iter_json_lines(stdout):
        t = str(ev.get("type") or "")
        item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        if t == "turn.started":
            turns += 1
        elif t == "item.completed":
            itype = item.get("type")
            if itype == "agent_message":
                m.final_text = str(item.get("text") or m.final_text)
            elif itype in ("command_execution", "mcp_tool_call", "file_change"):
                m.tool_calls += 1
                failed = item.get("status") == "failed" or (
                    isinstance(item.get("exit_code"), int) and item["exit_code"] != 0
                )
                if failed:
                    m.tool_errors += 1
        elif t == "turn.completed":
            u = ev.get("usage") or {}
            for attr, key in (
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
                ("cache_read_tokens", "cached_input_tokens"),
            ):
                if isinstance(u.get(key), (int, float)):
                    setattr(m, attr, (getattr(m, attr) or 0) + int(u[key]))
        elif t in ("error", "turn.failed"):
            m.is_error = True
            m.error_text = str(
                ev.get("message") or (ev.get("error") or {}).get("message") or t
            )[:300]
    m.turns = turns or None
    # Codex counts cached tokens inside input_tokens; report fresh input and
    # cache reads separately, the way the Claude CLI does.
    if m.input_tokens is not None and m.cache_read_tokens:
        m.input_tokens = max(m.input_tokens - m.cache_read_tokens, 0)
    return m


# -- Execution -----------------------------------------------------------------------


@dataclass
class ChildLaunch:
    provider: str  # as given: "claude", "codex:gpt-x", "openrouter:vendor/model", ...
    task_text: str
    worktree: Path
    sandbox: "hlr.HarnessSandbox"
    verdict_path: Path
    timeout: int
    agent: str
    run_cost_limit_usd: float | None = (
        None  # stop the child once the run costs this much
    )
    codex_effort: str | None = None
    ollama_thinking: bool = False
    ollama_context_tokens: int | None = 32768
    ollama_num_ctx: int | None = None  # build/use a model tag with this window
    openrouter_key_ref: str = DEFAULT_OPENROUTER_KEY_REF


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def force_thinking_off(body: dict) -> dict:
    body = dict(body)
    body["thinking"] = {"type": "disabled"}
    return body


LARGE_WINDOW_SUFFIX = "[1m]"


def strip_window_suffix(body: dict) -> dict:
    """The Claude CLI sizes its window from a ``[1m]`` model-name suffix; the
    local server must see the plain tag."""
    name = body.get("model")
    if isinstance(name, str) and name.endswith(LARGE_WINDOW_SUFFIX):
        body = {**body, "model": name[: -len(LARGE_WINDOW_SUFFIX)]}
    return body


def ensure_ollama_context_model(base: str, num_ctx: int) -> str:
    """Name of a local model tag that is *base* loaded with a *num_ctx* window.

    A local server fixes the window per model, and the Anthropic-compatible route
    cannot set it per request, so a larger window needs a model tag that carries
    it. The tag shares the base model's weights (no copy) and is created once.
    Remove it with ``ollama rm <name>``.
    """
    name = f"replay-{base.replace(':', '-').replace('/', '-')}-ctx{num_ctx}"
    try:
        tags = json.loads(
            urllib.request.urlopen(f"{OLLAMA_UPSTREAM}/api/tags", timeout=10).read()
        )
    except Exception as exc:
        raise CandidateError(
            f"local model server unreachable: {type(exc).__name__}"
        ) from exc
    if any(
        m.get("name", "").split(":latest")[0] == name for m in tags.get("models") or []
    ):
        return name
    body = json.dumps(
        {
            "model": name,
            "from": base,
            "parameters": {"num_ctx": num_ctx},
            "stream": False,
        }
    ).encode()
    req = urllib.request.Request(
        f"{OLLAMA_UPSTREAM}/api/create",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=120).read()
    except Exception as exc:
        raise CandidateError(
            f"could not create {name} from {base}: {type(exc).__name__}"
        ) from exc
    return name


def ollama_loaded_context(model: str | None) -> int | None:
    """Context window the local server actually loaded the model with."""
    try:
        data = json.loads(
            urllib.request.urlopen(f"{OLLAMA_UPSTREAM}/api/ps", timeout=5).read()
        )
    except Exception:
        return None
    for entry in data.get("models") or []:
        if model and entry.get("name", "").split(":latest")[0] == model:
            value = entry.get("context_length")
            return int(value) if isinstance(value, int) else None
    return None


def _require_binary(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise CandidateError(f"{name} CLI not found on PATH")
    return found


def _running_cost(proxy: MeteringProxy) -> float:
    return sum(
        r.usage["cost"]
        for r in list(proxy.records)
        if isinstance(r.usage.get("cost"), (int, float))
    )


async def run_candidate_child(launch: ChildLaunch, metrics: dict) -> SkillResult:
    """Run the child and fill *metrics*; return a ``SkillResult`` for ``run_one``."""
    kind, model = hlr.parse_provider(launch.provider)
    sandbox = launch.sandbox
    root = sandbox.root
    proxy: MeteringProxy | None = None
    started = time.time()
    metrics.update({"kind": kind, "model_requested": model})
    timed_out = budget_abort = False
    try:
        if kind in ("claude", "openrouter", "ollama"):
            binary = _require_binary("claude")
            guards = root / "guards-settings.json"
            guards.write_text(
                json.dumps(local_provider.render_guards_settings(REPO_ROOT))
            )
            empty_mcp = root / "empty-mcp.json"
            empty_mcp.write_text('{"mcpServers":{}}')
            cmd = claude_family_command(
                binary, model, guards_path=guards, empty_mcp_path=empty_mcp
            )
            if kind == "openrouter":
                key = load_openrouter_key(launch.openrouter_key_ref)
                proxy = MeteringProxy(
                    OPENROUTER_UPSTREAM,
                    api_key=key,
                    generation_lookup=openrouter_generation_lookup(key),
                )
            elif kind == "ollama":
                if launch.ollama_num_ctx:
                    model = ensure_ollama_context_model(model, launch.ollama_num_ctx)
                    metrics["ollama_num_ctx"] = launch.ollama_num_ctx
                    metrics["model_requested"] = model
                    cmd = claude_family_command(
                        binary,
                        model + LARGE_WINDOW_SUFFIX,
                        guards_path=guards,
                        empty_mcp_path=empty_mcp,
                    )

                def rewrite(body: dict) -> dict:
                    if not launch.ollama_thinking:
                        body = force_thinking_off(body)
                    return strip_window_suffix(body)

                proxy = MeteringProxy(OLLAMA_UPSTREAM, rewrite_request=rewrite)
            if proxy is not None:
                proxy.start()
            env = build_child_env(
                kind,
                model,
                sandbox,
                proxy_url=proxy.base_url if proxy else None,
                local_context_tokens=launch.ollama_context_tokens
                if kind == "ollama"
                else None,
            )
            parse = parse_claude_stream
        elif kind == "codex":
            binary = _require_binary("codex")
            cmd = codex_command(
                binary, model, str(launch.worktree), launch.codex_effort
            )
            env = build_child_env(kind, model, sandbox)
            parse = parse_codex_stream
        else:
            raise CandidateError(f"unsupported provider kind {kind!r}")
        cmd = [*sandbox.command_wrapper, *cmd]
        metrics["env_names"] = sorted(env)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(launch.worktree),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        async def watchdog() -> None:
            nonlocal budget_abort
            while proc.returncode is None:
                await asyncio.sleep(3)
                if (
                    proxy is not None
                    and launch.run_cost_limit_usd is not None
                    and _running_cost(proxy) >= launch.run_cost_limit_usd
                ):
                    budget_abort = True
                    _kill_group(proc)
                    return

        wd = asyncio.create_task(watchdog())
        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(launch.task_text.encode()), timeout=launch.timeout
            )
        except asyncio.TimeoutError:
            timed_out = True
            _kill_group(proc)
            out_b, err_b = b"", b""
            try:
                out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=10)
            except Exception:
                pass
        finally:
            wd.cancel()
        stdout = out_b.decode("utf-8", "replace")
        stderr = err_b.decode("utf-8", "replace")
        sm = parse(stdout)
        summary: ProxySummary | None = proxy.summary() if proxy else None
        if kind == "ollama":
            metrics["ollama_context_length"] = ollama_loaded_context(model)
    except CandidateError as exc:
        metrics.update(
            {"wall_seconds": round(time.time() - started, 1), "launch_error": str(exc)}
        )
        return SkillResult(
            launch.agent, False, None, "", "", error=str(exc), provider=launch.provider
        )
    finally:
        if proxy is not None:
            proxy.stop()

    metrics.update(
        {
            "wall_seconds": round(time.time() - started, 1),
            "timeout": timed_out,
            "budget_abort": budget_abort,
            "exit_status": proc.returncode,
            "turns": sm.turns,
            "tool_calls": sm.tool_calls,
            "tool_errors": sm.tool_errors,
            "model_reported": sm.model,
        }
    )
    # Tokens and cost: the provider's own numbers when a proxy saw the traffic,
    # else the CLI's reported figures. Cost is never derived from a price list.
    if summary is not None:
        metrics.update(
            {
                "tokens_in": summary.input_tokens,
                "tokens_out": summary.output_tokens,
                "tokens_cache_read": summary.cache_read_tokens,
                "tokens_cache_write": summary.cache_write_tokens,
                "cost_usd": summary.cost_usd,
                "cost_source": "provider_usage_cost"
                if summary.cost_usd is not None
                else "not_reported",
                "model_reported": summary.models[0] if summary.models else sm.model,
                "endpoint_requests": summary.requests,
                "endpoint_errors": summary.errors,
            }
        )
    else:
        metrics.update(
            {
                "tokens_in": sm.input_tokens,
                "tokens_out": sm.output_tokens,
                "tokens_cache_read": sm.cache_read_tokens,
                "tokens_cache_write": sm.cache_write_tokens,
                # A subscription login has no per-call charge; the CLI's own
                # figure is an API-price equivalent and is kept apart.
                "cost_usd": None,
                "cost_source": "subscription_not_charged_per_call",
                "cost_api_equivalent_usd": sm.reported_cost_usd,
            }
        )
    metrics["stderr_tail"] = stderr[-400:]
    if proc.returncode != 0 or sm.is_error:
        metrics["stdout_tail"] = stdout[-1500:]
    ok = proc.returncode == 0 and not timed_out and not budget_abort and not sm.is_error
    error = ""
    if timed_out:
        error = f"timeout after {launch.timeout}s"
    elif budget_abort:
        error = "run cost limit reached; child stopped"
    elif not ok:
        error = sm.error_text or f"exit {proc.returncode}: {stderr[-200:].strip()}"
    usage = DispatchUsage(
        provider=kind,
        model=metrics.get("model_reported") or model,
        model_source="reported" if metrics.get("model_reported") else "requested",
        input_tokens=metrics.get("tokens_in"),
        output_tokens=metrics.get("tokens_out"),
        total_cost_usd=metrics.get("cost_usd"),
    )
    return SkillResult(
        launch.agent,
        ok,
        proc.returncode,
        sm.final_text,
        stderr,
        error=error,
        provider=launch.provider,
        usage=usage,
    )
