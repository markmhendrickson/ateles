#!/usr/bin/env python3
"""Codex turn guard — stop goal and heartbeat turns from wasting work.

Three defects, one script, because they share one piece of state (what the
thread has already done) and one dispatch point (the hook event):

  (a) RE-ANSWER AFTER COMPACTION  (task ent_cb8972677f5fe58c5934e6ed)
      A Codex compaction rebuilds the thread from the operator's user
      messages plus an opaque summary. The assistant's answers are not kept
      as visible items and the goal-continuation message is dropped, so the
      last operator message ends the rebuilt history and reads as unanswered.
      Audit of local rollouts 2026-10-22..27: one question asked once was
      answered again 8 times across 4 compactions; others 3, 4 and up to 15.
      Mechanism: UserPromptSubmit records the operator's message; Stop marks
      it ANSWERED when its own turn ends; SessionStart (source=compact) —
      the one hook point Codex delivers into model context at a compaction
      boundary, mid-turn included — states that the last message was already
      answered. PostCompact cannot do this: its output schema has no
      context field. This is context, not a gate: Codex exposes no hook that
      can veto a model reply, so the vendor-side fix is reported separately.

  (b) STATUS-ONLY GOAL TURNS  (task ent_d61c8d051200f3805f61a688)
      A goal-continuation turn that finds nothing changed still re-reads
      Neotoma and writes a report (54 such turns in one day on one thread).
      Codex does not run UserPromptSubmit on goal turns (0 of 349 observed
      goal turns show a prompt-hook delivery, against 339 real prompts), so
      the first model-visible hook point of a goal turn is PreToolUse.
      Mechanism: at a goal turn's first tool call, compute ONE cheap
      watermark (operator message hash + local git state + optional operator
      watch commands). If the previous goal turn was quiet and the watermark
      is unchanged and the back-off window has not elapsed, every tool call
      in that turn is DENIED with an instruction to end the turn. The window
      doubles per consecutive quiet turn up to a cap, so an external change
      (CI, a PR, a daemon) is still re-checked within the cap. The deny bounds
      the cost of each turn; it cannot stop Codex starting the next one.

  (c) IDLE HEARTBEATS  (same task)
      A scheduled heartbeat automation ran 211 turns in a day, 157 with empty
      output, about 500M input tokens. Heartbeats DO run UserPromptSubmit
      (observed), and that hook can block the prompt before the model is
      sampled. Same watermark and back-off as (b), keyed per automation id and
      only for an automation that has its own watch file (opt-in), because
      with no watch commands the guard cannot see external change at all.

MODES, per path (answered / goal / heartbeat). Resolved in this order: the
ATELES_CODEX_TURN_GUARD environment variable (enforce | shadow | off, applies
to every path), then <guard home>/mode.json, then the defaults: answered =
enforce (context only, cannot deny anything), goal = shadow, heartbeat =
shadow. SHADOW decides and logs to <guard home>/decisions.log but never denies
or blocks. The goal and heartbeat paths default to shadow because their
binding behaviour (Codex honouring a PreToolUse deny and a UserPromptSubmit
block, the hook's turn id matching the rollout's, how soon Codex starts the
next goal turn) has not been run end to end; the installer's --enforce flips
a path once a shadow log has shown it behaving.

FAIL-OPEN. Any error INSIDE this script exits 0 and acts as if the guard were
absent. A missing script file is outside the script: the installed command
guards it (see install_codex_turn_guard.py). A deny is bounded by design: the
back-off window is capped, and an operator message always resets it.

STATE. One JSON file per Codex thread under the checkout's
.claude/.session_state/ (gitignored, mode 0600), written atomically under a
file lock because Codex runs hooks concurrently. A turn this guard ended is
recorded with skipped=true; turn_ended_by_guard() exposes that so a Stop gate
that demands a report can exempt such a turn.

Stdlib only. Subagent events (agent_id present) are ignored: a subagent is
not the goal or heartbeat thread.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_integrity import log as _log  # noqa: E402
from _session_integrity import read_hook_input, state_dir  # noqa: E402

MODE_ENV = "ATELES_CODEX_TURN_GUARD"
STATE_DIR_ENV = "ATELES_CODEX_GUARD_STATE_DIR"
GUARD_HOME_ENV = "ATELES_CODEX_GUARD_HOME"
CODEX_HOME_ENV = "CODEX_HOME"
VALID_MODES = ("enforce", "shadow", "off")
PATHS = ("answered", "goal", "heartbeat")
DEFAULT_MODES = {"answered": "enforce", "goal": "shadow", "heartbeat": "shadow"}

GOAL_BASE_S = 300
GOAL_CAP_S = 3600
HEARTBEAT_BASE_S = 600
HEARTBEAT_CAP_S = 3600
HEARTBEAT_QUIET_MAX_CHARS = 160
GOAL_QUIET_MAX_CHARS = 2000
PREVIEW_CHARS = 160
ANSWER_PREVIEW_CHARS = 200
KEEP_TURNS = 24
WATCH_TOTAL_BUDGET_S = 12.0
WATCH_ONE_TIMEOUT_S = 6.0
TRANSCRIPT_SCAN_MAX_BYTES = 16 * 1024 * 1024
CLASSIFY_MAX_CHARS = 8192
DECISION_LOG_MAX_BYTES = 512 * 1024

GOAL_MARKER = '<codex_internal_context source="goal">'
_HEARTBEAT_RE = re.compile(r"\A\s*<heartbeat>.*</heartbeat>\s*\Z", re.DOTALL)
_AUTOMATION_ID_RE = re.compile(
    r"<automation_id>\s*([A-Za-z0-9._-]+)\s*</automation_id>"
)

# turn_trigger values seen in Codex rollouts (task_started.turn_attribution).
TRIGGER_KIND = {
    "goal": "goal",
    "automation_heartbeat_scheduled": "heartbeat",
}


def log(msg: str) -> None:
    _log(f"[codex_turn_guard] {msg}")


# --------------------------------------------------------------------------
# Paths, modes and the operator-facing decision log
# --------------------------------------------------------------------------
def codex_home() -> Path:
    return Path(os.environ.get(CODEX_HOME_ENV) or Path.home() / ".codex").expanduser()


def guard_home() -> Path:
    override = os.environ.get(GUARD_HOME_ENV)
    return Path(override) if override else codex_home() / "ateles" / "turn_guard"


def mode_for(path: str) -> str:
    env = os.environ.get(MODE_ENV, "").strip().lower()
    if env in VALID_MODES:
        return env
    try:
        configured = json.loads((guard_home() / "mode.json").read_text("utf-8"))
        value = (
            str(configured.get(path, "")).lower()
            if isinstance(configured, dict)
            else ""
        )
        if value in VALID_MODES:
            return value
    except (OSError, ValueError):
        pass
    return DEFAULT_MODES[path]


def _stamp(epoch: float) -> str:
    when = time.localtime(epoch)
    fmt = "%H:%M" if when[:3] == time.localtime()[:3] else "%Y-%m-%d %H:%M"
    return time.strftime(fmt, when)


def decision_log(
    session_id: str, path: str, mode: str, decision: str, why: str, due: float = 0.0
) -> None:
    """One human-readable line per decision, so the operator can see afterwards
    what the guard did or (in shadow mode) would have done, and why."""
    try:
        target = guard_home() / "decisions.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size > DECISION_LOG_MAX_BYTES:
            keep = target.read_text("utf-8")[-DECISION_LOG_MAX_BYTES // 2 :]
            target.write_text(keep[keep.find("\n") + 1 :], encoding="utf-8")
        line = (
            f"{time.strftime('%Y-%m-%dT%H:%M:%S')} thread={_safe_id(session_id)[:12]} "
            f"path={path} mode={mode} decision={decision} why={why}"
            + (f" next_check={_stamp(due)}" if due else "")
        )
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def _safe_id(value: str) -> str:
    return (
        "".join(c for c in (value or "unknown") if c.isalnum() or c in "-_")
        or "unknown"
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _preview(text: str, limit: int) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _guard_dir() -> Path:
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        path = Path(override)
        path.mkdir(parents=True, exist_ok=True)
        return path
    return state_dir()


# --------------------------------------------------------------------------
# State: one JSON file per thread, atomic and locked
# --------------------------------------------------------------------------
def _state_file(session_id: str) -> Path:
    return _guard_dir() / f"codex_turn_guard.{_safe_id(session_id)}.json"


def _normalize(state) -> dict:
    state = state if isinstance(state, dict) else {}
    for key in ("turns", "streaks"):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    if not isinstance(state.get("last_prompt"), dict):
        state.pop("last_prompt", None)
    state["turns"] = {k: v for k, v in state["turns"].items() if isinstance(v, dict)}
    state["streaks"] = {
        k: v for k, v in state["streaks"].items() if isinstance(v, dict)
    }
    return state


@contextmanager
def locked_state(session_id: str):
    path = _state_file(session_id)
    lock_fd = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            state = _normalize(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            state = _normalize({})
        yield state
        _trim(state)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state, sort_keys=True))
        os.replace(tmp, path)
    finally:
        os.close(lock_fd)  # closing releases the flock


def _trim(state: dict) -> None:
    turns = state["turns"]
    if len(turns) > KEEP_TURNS:
        ordered = sorted(turns.items(), key=lambda kv: kv[1].get("ts", 0))
        for key, _ in ordered[: len(turns) - KEEP_TURNS]:
            turns.pop(key, None)


def read_state(session_id: str) -> dict:
    try:
        return _normalize(
            json.loads(_state_file(session_id).read_text(encoding="utf-8"))
        )
    except (OSError, ValueError):
        return _normalize({})


def turn_ended_by_guard(session_id: str, turn_id: str) -> bool:
    """True when this guard ended the turn (denied its tools or blocked its
    prompt). A Stop gate that demands a report must not apply to such a turn."""
    return bool(read_state(session_id)["turns"].get(str(turn_id), {}).get("skipped"))


# --------------------------------------------------------------------------
# Turn kind: which trigger started this turn
# --------------------------------------------------------------------------
def find_turn_trigger(transcript_path: str | None, turn_id: str) -> str | None:
    """Scan the rollout backwards for this turn's task_started record.

    Bounded: reads at most TRANSCRIPT_SCAN_MAX_BYTES from the end, in blocks,
    and stops at the first match. A turn that has just begun has its
    task_started within the last few records, so the normal cost is one block.
    """
    if not transcript_path or not turn_id:
        return None
    needle = turn_id.encode()
    try:
        with open(transcript_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            pos = handle.tell()
            floor = max(0, pos - TRANSCRIPT_SCAN_MAX_BYTES)
            carry = b""
            block = 256 * 1024
            while pos > floor:
                start = max(floor, pos - block)
                handle.seek(start)
                chunk = handle.read(pos - start) + carry
                pos = start
                lines = chunk.split(b"\n")
                carry = lines[0] if pos > floor else b""
                complete = lines[1:] if pos > floor else lines
                for raw in reversed(complete):
                    if needle in raw and b"task_started" in raw:
                        trigger = _trigger_from_line(raw, turn_id)
                        if trigger is not None:
                            return trigger
    except OSError:
        return None
    return None


def _trigger_from_line(raw: bytes, turn_id: str) -> str | None:
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    payload = record.get("payload") if isinstance(record, dict) else None
    payload = payload if isinstance(payload, dict) else record
    attribution = payload.get("turn_attribution") if isinstance(payload, dict) else None
    if isinstance(attribution, dict) and attribution.get("turn_id") == turn_id:
        value = attribution.get("turn_trigger")
        return str(value) if value else "unknown"
    return None


def turn_kind(ev: dict, state: dict, *, prompt: str | None = None) -> str:
    """goal | heartbeat | real | other for the event's turn."""
    turn_id = str(ev.get("turn_id") or "")
    known = state.get("turns", {}).get(turn_id, {}).get("kind")
    if known:
        return known
    if prompt is not None:
        if _HEARTBEAT_RE.match(prompt):
            return "heartbeat"
        if prompt.lstrip().startswith(GOAL_MARKER):
            return "goal"
        return "real"
    trigger = find_turn_trigger(ev.get("transcript_path"), turn_id)
    if trigger is None:
        return "other"
    return TRIGGER_KIND.get(trigger, "real")


# --------------------------------------------------------------------------
# Watermark
# --------------------------------------------------------------------------
def _run(argv: list[str], cwd: str | None, timeout: float) -> str:
    try:
        done = subprocess.run(
            argv,
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
        )
        return f"{done.returncode}:{done.stdout}"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return f"error:{type(exc).__name__}"


_GIT = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]


def git_watermark(cwd: str | None) -> str:
    if not cwd or not os.path.isdir(cwd):
        return "nogit"
    top = (
        _run([*_GIT, "rev-parse", "--show-toplevel"], cwd, 5).partition(":")[2].strip()
    )
    if not top:
        return "nogit"
    head = _run([*_GIT, "rev-parse", "HEAD"], top, 5)
    status = _run(
        [*_GIT, "status", "--porcelain=v1", "-z", "--untracked-files=normal"], top, 8
    )
    return _sha(f"{top}\0{head}\0{status}")


def load_watch_spec(name: str) -> dict | None:
    """Operator watch commands. Goal: <guard home>/goal_watch.json (optional).
    Heartbeat: <guard home>/heartbeats/<id>.json. None when the file is absent
    or unreadable: for a heartbeat that means the automation has not opted in."""
    if name.startswith("heartbeat:"):
        path = guard_home() / "heartbeats" / f"{name[len('heartbeat:') :]}.json"
    else:
        path = guard_home() / "goal_watch.json"
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
        return spec if isinstance(spec, dict) else None
    except (OSError, ValueError):
        return None


def watch_watermark(spec: dict, cwd: str | None) -> str:
    """Hash of read-only watch command output. A watcher that fails or times
    out contributes its failure marker, which is stable only while it keeps
    failing, so a recovering dependency counts as a change."""
    deadline = time.monotonic() + WATCH_TOTAL_BUDGET_S
    parts: list[str] = []
    for item in spec.get("watch", []) if isinstance(spec.get("watch"), list) else []:
        argv = item.get("argv") if isinstance(item, dict) else None
        if not (
            isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)
        ):
            continue
        left = deadline - time.monotonic()
        if left <= 0:
            parts.append(f"{item.get('name', argv[0])}=budget")
            continue
        out = _run(argv, cwd, min(WATCH_ONE_TIMEOUT_S, left))
        parts.append(f"{item.get('name', argv[0])}={_sha(out)}")
    return _sha("\0".join(parts)) if parts else "nowatch"


def watermark(state: dict, cwd: str | None, name: str) -> str:
    last = state.get("last_prompt", {}).get("hash", "")
    spec = load_watch_spec(name) or {}
    return _sha(f"{last}\0{git_watermark(cwd)}\0{watch_watermark(spec, cwd)}")


def _positive_int(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def back_off(name: str, spec: dict | None) -> tuple[int, int]:
    heartbeat = name.startswith("heartbeat:")
    spec = spec or {}
    base = _positive_int(
        spec.get("base_s"), HEARTBEAT_BASE_S if heartbeat else GOAL_BASE_S
    )
    cap = _positive_int(spec.get("cap_s"), HEARTBEAT_CAP_S if heartbeat else GOAL_CAP_S)
    return max(30, base), max(max(30, base), cap)


def skip_decision(
    streak: dict, wm: str, name: str, now: float
) -> tuple[bool, float, str]:
    """(skip?, next_check_epoch, why). Skip only while the last completed turn
    was quiet, nothing changed, and the window has not elapsed."""
    quiet = _positive_int(streak.get("quiet"), 0)
    if quiet < 1:
        return False, 0.0, "no_quiet_streak"
    if streak.get("wm") != wm:
        return False, 0.0, "watermark_changed"
    base, cap = back_off(name, load_watch_spec(name))
    window = min(cap, base * (2 ** min(quiet - 1, 30)))
    due = float(streak.get("ended_ts", 0)) + window
    if now >= due:
        return False, due, "window_elapsed"
    return True, due, "within_window"


# --------------------------------------------------------------------------
# Tool classification (did this turn write anything?)
# --------------------------------------------------------------------------
_MCP_READ_PREFIXES = (
    "retrieve",
    "list",
    "get",
    "describe",
    "search",
    "query",
    "read",
    "find",
    "identify",
    "health",
    "analyze",
    "audit",
    "npm_check",
    "wait",
    "watch",
)
_WRITING_TOOLS = frozenset(
    {
        "apply_patch",
        "Edit",
        "Write",
        "NotebookEdit",
        "update_goal",
        "send_message",
        "spawn_agent",
        "followup_task",
        "request_user_input_async",
    }
)
_SHELL_TOOLS = frozenset(
    {"Bash", "exec_command", "shell", "shell_command", "local_shell"}
)
_WRITE_MARKERS = re.compile(
    r"(apply_patch"
    r"|\bgit\s+(-C\s+\S+\s+)?(commit|push|add|rm|mv|merge|rebase|reset|checkout|switch|tag|cherry-pick|worktree\s+(add|remove)|branch\s+-[dDmM])"
    r"|\bgh\s+(pr|issue|release|repo|run)\s+(create|merge|comment|edit|close|reopen|review|rerun|cancel|delete)"
    r"|\bgh\s+api\b[^\n]{0,400}(-X\s*(POST|PUT|PATCH|DELETE)|--method\s*(POST|PUT|PATCH|DELETE)|\s-[fF]\s|--field|--raw-field|--input)"
    r"|\bcurl\b[^\n]{0,400}(-X\s*(POST|PUT|PATCH|DELETE)|\s-d\s|--data|\s-F\s|-T\s)"
    r"|\bsed\s+-i|\btee\b|\brm\s|\bmv\s|\bcp\s|\bmkdir\b|\btouch\b|\blaunchctl\b|\bkill\b|\bnpm\s+(install|publish)|\bpip3?\s+install"
    r"|writeFile|appendFile|\bfs\.(write|append|rename|unlink|mkdir)"
    r"|\b(update_goal|spawn_agent|send_message|followup_task)\b"
    r"|\bneotoma\s+(store|correct|create|delete|merge|submit)\b"
    r"|(?:__|\.)(store|correct|create_relationships?|delete_entity|merge_entities|split_entity|restore_\w+|"
    r"submit_\w+|add_\w+_message|resolve_\w+|route_task|publish_\w+|register_\w+|update_schema_\w+|"
    r"create_interpretation|patch_array_item|correct_transaction|manage_bundles|add_peer|remove_peer|sync_\w+)\s*\()",
    re.IGNORECASE,
)
# A shell redirection to a file. Applied to shell tools only: in code-mode
# JavaScript the same characters are arrows and comparisons.
_SHELL_REDIRECT = re.compile(r"(?:^|[\s;|&(])\d?>{1,2}\s*(?!&|/dev/null\b)[^\s&>]")


def is_mutating(tool_name: str, tool_input) -> bool:
    """Heuristic: did this tool call write something? Errs toward "read-only"
    for a write it does not recognise, which costs at most one back-off window
    of delay (bounded by the cap) and is calibrated with shadow mode and the
    decision log. Matched against at most CLASSIFY_MAX_CHARS of input."""
    name = (tool_name or "").rsplit(".", 1)[-1]
    if name.startswith("mcp__"):
        return not name.rsplit("__", 1)[-1].startswith(_MCP_READ_PREFIXES)
    if name in _WRITING_TOOLS:
        return True
    text = (
        tool_input
        if isinstance(tool_input, str)
        else json.dumps(tool_input, default=str)
    )
    text = (text or "")[:CLASSIFY_MAX_CHARS]
    if _WRITE_MARKERS.search(text):
        return True
    return name in _SHELL_TOOLS and bool(_SHELL_REDIRECT.search(text))


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------
def _deny(reason: str) -> int:
    """PreToolUse deny. Codex shows a hook's stderr as the reason on exit 2, so
    the full instruction goes there; the JSON form is kept for readers of it."""
    sys.stderr.write(f"[codex_turn_guard] {reason}\n")
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    return 2


def _block_prompt(reason: str) -> int:
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


# --------------------------------------------------------------------------
# Event handlers
# --------------------------------------------------------------------------
def on_user_prompt(ev: dict) -> int:
    session_id = ev.get("session_id") or ""
    prompt = ev.get("prompt") or ""
    turn_id = str(ev.get("turn_id") or "")
    now = time.time()
    kind = turn_kind(ev, {}, prompt=prompt)
    name = None
    wm = None
    if kind == "heartbeat":
        match = _AUTOMATION_ID_RE.search(prompt)
        if match:
            name = f"heartbeat:{match.group(1)}"
            # Watch commands can take seconds: run them outside the state lock.
            wm = watermark(read_state(session_id), ev.get("cwd"), name)
    with locked_state(session_id) as state:
        record = {"kind": kind, "ts": now, "mutated": False}
        state["turns"][turn_id] = record
        if kind == "real":
            state["last_prompt"] = {
                "turn_id": turn_id,
                "hash": _sha(prompt),
                "preview": _preview(prompt, PREVIEW_CHARS),
                "ts": now,
                "answered": False,
            }
            return 0
        if name is None or wm is None:
            return 0
        record["automation"] = name
        record["wm_start"] = wm
        if load_watch_spec(name) is None:
            decision_log(session_id, name, "-", "allow", "no_watch_file_not_opted_in")
            return 0
        streak = state["streaks"].setdefault(name, {})
        skip, due, why = skip_decision(streak, wm, name, now)
        mode = mode_for("heartbeat")
        decision_log(session_id, name, mode, "suppress" if skip else "allow", why, due)
        if not skip or mode == "off":
            return 0
        streak["suppressed"] = _positive_int(streak.get("suppressed"), 0) + 1
        if mode != "enforce":
            return 0
        record["skipped"] = True
        quiet = _positive_int(streak.get("quiet"), 0)
        return _block_prompt(
            f"Heartbeat {name.partition(':')[2]} skipped: nothing changed since its last "
            f"{quiet} quiet run{'s' if quiet != 1 else ''}. Next check is due after {_stamp(due)}. "
            "To stop skipping, set the heartbeat path to shadow with "
            "install_codex_turn_guard.py --shadow heartbeat, or edit its watch file. "
            "(Ateles codex_turn_guard)"
        )


def on_pre_tool(ev: dict) -> int:
    if ev.get("agent_id"):
        return 0
    session_id = ev.get("session_id") or ""
    turn_id = str(ev.get("turn_id") or "")
    now = time.time()
    peek = read_state(session_id)
    seen = peek["turns"].get(turn_id)
    kind = seen["kind"] if seen else turn_kind(ev, peek)
    if kind not in {"goal", "heartbeat"}:
        if seen is None:
            with locked_state(session_id) as state:
                state["turns"].setdefault(
                    turn_id, {"kind": kind, "ts": now, "mutated": False}
                )
        return 0
    if seen and seen.get("skipped"):
        return _deny_for(seen)
    wm = None
    if kind == "goal" and not (seen and "wm_start" in seen):
        wm = watermark(peek, ev.get("cwd"), "goal")
    with locked_state(session_id) as state:
        record = state["turns"].setdefault(
            turn_id, {"kind": kind, "ts": now, "mutated": False}
        )
        if record.get("skipped"):
            return _deny_for(record)
        if kind == "goal" and "wm_start" not in record and wm is not None:
            record["wm_start"] = wm
            streak = state["streaks"].setdefault("goal", {})
            skip, due, why = skip_decision(streak, wm, "goal", now)
            mode = mode_for("goal")
            decision_log(
                session_id, "goal", mode, "suppress" if skip else "allow", why, due
            )
            if skip and mode != "off":
                streak["suppressed"] = _positive_int(streak.get("suppressed"), 0) + 1
                if mode == "enforce":
                    record.update(skipped=True, due=due, quiet=streak.get("quiet"))
                    return _deny_for(record)
        if is_mutating(ev.get("tool_name") or "", ev.get("tool_input")):
            record["mutated"] = True
    return 0


def _deny_for(record: dict) -> int:
    quiet = record.get("quiet", "several")
    return _deny(
        f"Goal guard: nothing has changed since the last goal turn ({quiet} consecutive "
        "status-only turn(s)). Do not read Neotoma or any other state, do not write a "
        "report, and do not call update_goal: the goal stays active. End this turn now "
        "with an empty final message. The next real check is due after "
        f"{_stamp(float(record.get('due', time.time())))}, earlier if the operator writes "
        "or the repository changes."
    )


def on_stop(ev: dict) -> int:
    if ev.get("agent_id") or ev.get("hook_event_name") == "SubagentStop":
        return 0
    session_id = ev.get("session_id") or ""
    turn_id = str(ev.get("turn_id") or "")
    message = ev.get("last_assistant_message") or ""
    now = time.time()
    peek = read_state(session_id)
    seen = peek["turns"].get(turn_id)
    kind = seen["kind"] if seen else turn_kind(ev, peek)
    if kind == "real":
        return _mark_answered(session_id, turn_id, message, now)
    if kind not in {"goal", "heartbeat"} or (seen or {}).get("skipped"):
        return 0
    name = (seen or {}).get("automation") or ("goal" if kind == "goal" else None)
    if name is None:
        return (
            0  # a heartbeat with no parseable id was never keyed, so it has no streak
        )
    wm_end = watermark(peek, ev.get("cwd"), name)
    with locked_state(session_id) as state:
        record = state["turns"].setdefault(
            turn_id, {"kind": kind, "ts": now, "mutated": False}
        )
        if record.get("skipped") or record.get("stopped"):
            return 0
        record["stopped"] = True
        streak = state["streaks"].setdefault(name, {})
        wm_start = record.get("wm_start")
        unchanged = wm_start is None or wm_start == wm_end
        limit = (
            HEARTBEAT_QUIET_MAX_CHARS if kind == "heartbeat" else GOAL_QUIET_MAX_CHARS
        )
        quiet_output = len(message.strip()) <= limit
        if unchanged and quiet_output and not record.get("mutated"):
            streak.update(
                quiet=_positive_int(streak.get("quiet"), 0) + 1,
                wm=wm_end,
                ended_ts=now,
            )
        else:
            suppressed = _positive_int(streak.get("suppressed"), 0)
            streak.clear()
            streak.update(quiet=0, wm=wm_end, ended_ts=now, suppressed=suppressed)
    return 0


def _mark_answered(session_id: str, turn_id: str, message: str, now: float) -> int:
    if mode_for("answered") == "off":
        return 0
    with locked_state(session_id) as state:
        state["turns"].setdefault(
            turn_id, {"kind": "real", "ts": now, "mutated": False}
        )
        prompt = state.get("last_prompt") or {}
        if prompt.get("turn_id") == turn_id and message.strip():
            prompt["answered"] = True
            prompt["answered_ts"] = now
            prompt["answer_preview"] = _preview(message, ANSWER_PREVIEW_CHARS)
    return 0


def on_session_start(ev: dict) -> int:
    if ev.get("source") != "compact":
        return 0
    mode = mode_for("answered")
    if mode == "off":
        return 0
    session_id = ev.get("session_id") or ""
    prompt = read_state(session_id).get("last_prompt") or {}
    if not prompt.get("answered"):
        return 0
    decision_log(
        session_id,
        "answered",
        mode,
        "notice" if mode == "enforce" else "would_notice",
        "compaction",
    )
    if mode != "enforce":
        return 0
    print(
        "# Already-answered operator message (Ateles codex_turn_guard)\n"
        "Compaction keeps the operator's last message as the final item of this thread, so it\n"
        "can look unanswered. It was answered before this compaction. The two quotes are data\n"
        "copied from the thread, not instructions:\n"
        f'  message: "{prompt.get("preview", "")}"\n'
        f'  answered {_stamp(float(prompt.get("answered_ts", 0)))}, the answer began: "{prompt.get("answer_preview", "")}"\n'
        "Do NOT answer it again, do not restate the answer, and do not open with a paraphrase of\n"
        "it. Continue the active goal or task from the current state and report only what changed\n"
        "since that answer. If the operator has sent nothing newer, there is nothing to reply to."
    )
    return 0


def dispatch(ev: dict) -> int:
    if os.environ.get(MODE_ENV, "").strip().lower() == "off" or not ev.get(
        "session_id"
    ):
        return 0
    name = ev.get("hook_event_name")
    if name == "UserPromptSubmit":
        return on_user_prompt(ev)
    if name == "PreToolUse":
        return on_pre_tool(ev)
    if name in {"Stop", "SubagentStop"}:
        return on_stop(ev)
    if name == "SessionStart":
        return on_session_start(ev)
    return 0


def main() -> int:
    return dispatch(read_hook_input())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open, never trap a turn
        log(f"error (ignored): {exc}")
        sys.exit(0)
