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
      answered five times overnight; others 3, 4 and up to 15 times.
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
      (CI, a PR, a daemon) is still re-checked within the cap.

  (c) IDLE HEARTBEATS  (same task)
      A scheduled heartbeat automation ran 211 turns in a day, 157 with empty
      output, about 500M input tokens. Heartbeats DO run UserPromptSubmit
      (observed), and that hook can block the prompt before the model is
      sampled. Same watermark and back-off as (b), keyed per automation id;
      a heartbeat turn is quiet when it ends with a blank final message and
      made no write.

Modes: ATELES_CODEX_TURN_GUARD = enforce (default) | shadow (decide and log,
never deny/block) | off. Fail-open: any error exits 0 and acts as if the
guard were absent, so this can never trap a turn. A deny is bounded by
design: the back-off window is capped (default 1 hour), and an operator
message always resets it.

State: one JSON file per Codex thread under the checkout's
.claude/.session_state/ (gitignored), written atomically under a file lock
because Codex runs hooks concurrently.

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
from _session_integrity import log, read_hook_input, state_dir  # noqa: E402

MODE_ENV = "ATELES_CODEX_TURN_GUARD"
STATE_DIR_ENV = "ATELES_CODEX_GUARD_STATE_DIR"
CODEX_HOME_ENV = "CODEX_HOME"

GOAL_BASE_S = 300
GOAL_CAP_S = 3600
HEARTBEAT_BASE_S = 600
HEARTBEAT_CAP_S = 6 * 3600
QUIET_MAX_CHARS = 160
PREVIEW_CHARS = 160
ANSWER_PREVIEW_CHARS = 200
KEEP_TURNS = 24
WATCH_TOTAL_BUDGET_S = 12.0
WATCH_ONE_TIMEOUT_S = 6.0
TRANSCRIPT_SCAN_MAX_BYTES = 16 * 1024 * 1024

GOAL_MARKER = '<codex_internal_context source="goal">'
HEARTBEAT_MARKER = "<heartbeat>"
_AUTOMATION_ID_RE = re.compile(
    r"<automation_id>\s*([A-Za-z0-9._-]+)\s*</automation_id>"
)

# turn_trigger values seen in Codex rollouts (task_started.turn_attribution).
TRIGGER_KIND = {
    "goal": "goal",
    "automation_heartbeat_scheduled": "heartbeat",
}


# --------------------------------------------------------------------------
# Mode and small helpers
# --------------------------------------------------------------------------
def mode() -> str:
    value = os.environ.get(MODE_ENV, "enforce").strip().lower()
    return value if value in {"enforce", "shadow", "off"} else "enforce"


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


def codex_home() -> Path:
    return Path(os.environ.get(CODEX_HOME_ENV) or Path.home() / ".codex").expanduser()


# --------------------------------------------------------------------------
# State: one JSON file per thread, atomic and locked
# --------------------------------------------------------------------------
def _state_file(session_id: str) -> Path:
    return _guard_dir() / f"codex_turn_guard.{_safe_id(session_id)}.json"


@contextmanager
def locked_state(session_id: str):
    path = _state_file(session_id)
    lock = path.with_suffix(".lock")
    with open(lock, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(state, dict):
                    state = {}
            except (OSError, ValueError):
                state = {}
            state.setdefault("turns", {})
            state.setdefault("streaks", {})
            yield state
            _trim(state)
            tmp = path.with_suffix(f".tmp{os.getpid()}")
            tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _trim(state: dict) -> None:
    turns = state.get("turns", {})
    if len(turns) > KEEP_TURNS:
        ordered = sorted(turns.items(), key=lambda kv: kv[1].get("ts", 0))
        for key, _ in ordered[: len(turns) - KEEP_TURNS]:
            turns.pop(key, None)


def read_state(session_id: str) -> dict:
    try:
        return json.loads(_state_file(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


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
                        trigger = _trigger_from_line(raw)
                        if trigger is not None:
                            return trigger
    except OSError:
        return None
    return None


def _trigger_from_line(raw: bytes) -> str | None:
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    payload = record.get("payload") if isinstance(record, dict) else None
    payload = payload if isinstance(payload, dict) else record
    attribution = payload.get("turn_attribution") if isinstance(payload, dict) else None
    if isinstance(attribution, dict):
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
        if prompt.lstrip().startswith(HEARTBEAT_MARKER):
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
        )
        return f"{done.returncode}:{done.stdout}"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return f"error:{type(exc).__name__}"


def git_watermark(cwd: str | None) -> str:
    if not cwd or not os.path.isdir(cwd):
        return "nogit"
    top = (
        _run(["git", "rev-parse", "--show-toplevel"], cwd, 5).partition(":")[2].strip()
    )
    if not top:
        return "nogit"
    head = _run(["git", "rev-parse", "HEAD"], top, 5)
    status = _run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=normal"], top, 8
    )
    return _sha(f"{top}\0{head}\0{status}")


def load_watch_spec(name: str) -> dict:
    """Operator watch commands: goal -> turn_guard/goal_watch.json, heartbeat ->
    turn_guard/heartbeats/<id>.json under the Codex home (or the env override)."""
    override = os.environ.get("ATELES_CODEX_GUARD_WATCH_DIR")
    base = Path(override) if override else codex_home() / "ateles" / "turn_guard"
    path = base / (
        f"{name[len('heartbeat:') :]}.json"
        if name.startswith("heartbeat:")
        else "goal_watch.json"
    )
    if name.startswith("heartbeat:"):
        path = base / "heartbeats" / path.name
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
        return spec if isinstance(spec, dict) else {}
    except (OSError, ValueError):
        return {}


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
    spec = load_watch_spec(name)
    return _sha(f"{last}\0{git_watermark(cwd)}\0{watch_watermark(spec, cwd)}")


def back_off(name: str, spec: dict) -> tuple[int, int]:
    heartbeat = name.startswith("heartbeat:")
    base = int(spec.get("base_s") or (HEARTBEAT_BASE_S if heartbeat else GOAL_BASE_S))
    cap = int(spec.get("cap_s") or (HEARTBEAT_CAP_S if heartbeat else GOAL_CAP_S))
    return max(30, base), max(base, cap)


def skip_decision(streak: dict, wm: str, name: str, now: float) -> tuple[bool, float]:
    """(skip?, next_check_epoch). Skip only while the last completed turn was
    quiet, nothing changed, and the window has not elapsed."""
    quiet = int(streak.get("quiet", 0))
    if quiet < 1 or streak.get("wm") != wm:
        return False, 0.0
    base, cap = back_off(name, load_watch_spec(name))
    window = min(cap, base * (2 ** (quiet - 1)))
    due = float(streak.get("ended_ts", 0)) + window
    return now < due, due


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
_WRITE_MARKERS = re.compile(
    r"(apply_patch|\bgit\s+(-C\s+\S+\s+)?(commit|push|add|rm|mv|merge|rebase|reset|checkout|switch|tag|cherry-pick|worktree\s+(add|remove)|branch\s+-[dDmM])"
    r"|\bgh\s+(pr|issue|release|repo|run)\s+(create|merge|comment|edit|close|reopen|review|rerun|cancel|delete)"
    r"|\bgh\s+api\b[^\n]*(-X\s*(POST|PUT|PATCH|DELETE)|--method\s*(POST|PUT|PATCH|DELETE)|\s-[fF]\s|--field|--raw-field|--input)"
    r"|\bcurl\b[^\n]*(-X\s*(POST|PUT|PATCH|DELETE)|\s-d\s|--data|\s-F\s|-T\s)"
    r"|\bsed\s+-i|\btee\b|\brm\s|\bmv\s|\bcp\s|\bmkdir\b|\btouch\b|\blaunchctl\b|\bkill\b|\bnpm\s+(install|publish)|\bpip3?\s+install"
    r"|(?<![>&0-9])>{1,2}\s*[^&\s/]|>{1,2}\s*/(?!dev/null)"
    r"|writeFile|appendFile|\bfs\.(write|append|rename|unlink|mkdir)"
    r"|\b(update_goal|spawn_agent|send_message|followup_task|request_user_input)\b"
    r"|\bneotoma\s+(store|correct|create|delete|merge|submit|submit_issue)\b"
    r"|\b(store|correct|create_relationship|create_relationships|delete_entity|merge_entities|submit_entity|submit_issue|add_entity_message|resolve_checkpoint|route_task)\s*\()",
    re.IGNORECASE,
)


def is_mutating(tool_name: str, tool_input) -> bool:
    """Heuristic: did this tool call write something? Errs toward "read-only"
    for unrecognized writes, which costs at most one back-off window of delay
    (bounded by the cap) and is calibrated with ATELES_CODEX_TURN_GUARD=shadow.
    """
    name = tool_name or ""
    if name.startswith("mcp__"):
        tail = name.rsplit("__", 1)[-1]
        return not tail.startswith(_MCP_READ_PREFIXES)
    if name in {"apply_patch", "Edit", "Write", "update_goal", "NotebookEdit"}:
        return True
    text = (
        tool_input
        if isinstance(tool_input, str)
        else json.dumps(tool_input, default=str)
    )
    return bool(_WRITE_MARKERS.search(text or ""))


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------
def _deny(reason: str) -> int:
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


def _stamp(epoch: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


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
        streak = state["streaks"].setdefault(name, {})
        skip, due = skip_decision(streak, wm, name, now)
        if not skip:
            return 0
        streak["suppressed"] = int(streak.get("suppressed", 0)) + 1
        log(
            f"{name}: suppressed (quiet x{streak.get('quiet')}, next check {_stamp(due)})"
        )
        if mode() != "enforce":
            return 0
        record["skipped"] = True
        return _block_prompt(
            f"Heartbeat {name.partition(':')[2]} skipped: nothing changed since the last "
            f"{streak.get('quiet')} quiet run(s). Next check is due after {_stamp(due)}. "
            "(Ateles codex_turn_guard)"
        )


def on_pre_tool(ev: dict) -> int:
    if ev.get("agent_id"):
        return 0
    session_id = ev.get("session_id") or ""
    turn_id = str(ev.get("turn_id") or "")
    now = time.time()
    peek = read_state(session_id)
    seen = peek.get("turns", {}).get(turn_id)
    kind = seen["kind"] if seen else turn_kind(ev, peek)
    wm = None
    if kind == "goal" and not (seen and "wm_start" in seen):
        wm = watermark(peek, ev.get("cwd"), "goal")
    with locked_state(session_id) as state:
        record = state["turns"].setdefault(
            turn_id, {"kind": kind, "ts": now, "mutated": False}
        )
        if record.get("skipped"):
            return _deny_for(record)
        if record["kind"] == "goal" and "wm_start" not in record and wm is not None:
            record["wm_start"] = wm
            streak = state["streaks"].setdefault("goal", {})
            skip, due = skip_decision(streak, wm, "goal", now)
            if skip:
                streak["suppressed"] = int(streak.get("suppressed", 0)) + 1
                log(
                    f"goal turn suppressed (quiet x{streak.get('quiet')}, next check {_stamp(due)})"
                )
                if mode() == "enforce":
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
        "or the repository changes. (Ateles codex_turn_guard)"
    )


def on_stop(ev: dict) -> int:
    if ev.get("agent_id") or ev.get("hook_event_name") == "SubagentStop":
        return 0
    session_id = ev.get("session_id") or ""
    turn_id = str(ev.get("turn_id") or "")
    message = ev.get("last_assistant_message") or ""
    now = time.time()
    peek = read_state(session_id)
    seen = peek.get("turns", {}).get(turn_id)
    kind = seen["kind"] if seen else turn_kind(ev, peek)
    wm_end = None
    name = (seen or {}).get("automation") or "goal"
    if kind in {"goal", "heartbeat"} and not (seen or {}).get("skipped"):
        wm_end = watermark(peek, ev.get("cwd"), name)
    with locked_state(session_id) as state:
        record = state["turns"].setdefault(
            turn_id, {"kind": kind, "ts": now, "mutated": False}
        )
        if kind == "real":
            prompt = state.get("last_prompt") or {}
            if prompt.get("turn_id") == turn_id and message.strip():
                prompt["answered"] = True
                prompt["answered_ts"] = now
                prompt["answer_preview"] = _preview(message, ANSWER_PREVIEW_CHARS)
            return 0
        if wm_end is None or record.get("skipped") or record.get("stopped"):
            return 0
        record["stopped"] = True
        streak = state["streaks"].setdefault(name, {})
        wm_start = record.get("wm_start")
        unchanged = wm_start is None or wm_start == wm_end
        quiet_output = (
            len(message.strip()) <= QUIET_MAX_CHARS if kind == "heartbeat" else True
        )
        if unchanged and quiet_output and not record.get("mutated"):
            streak.update(
                quiet=int(streak.get("quiet", 0)) + 1, wm=wm_end, ended_ts=now
            )
        else:
            streak.clear()
            streak.update(quiet=0, wm=wm_end, ended_ts=now)
    return 0


def on_session_start(ev: dict) -> int:
    if ev.get("source") != "compact":
        return 0
    state = read_state(ev.get("session_id") or "")
    prompt = state.get("last_prompt") or {}
    if not prompt.get("answered"):
        return 0
    print(
        "# Already-answered operator message (Ateles codex_turn_guard)\n"
        "Compaction keeps the operator's last message as the final item of this thread, so it\n"
        "can look unanswered. It was answered before this compaction:\n"
        f'  message: "{prompt.get("preview", "")}"\n'
        f'  answered {_stamp(float(prompt.get("answered_ts", 0)))}, the answer began: "{prompt.get("answer_preview", "")}"\n'
        "Do NOT answer it again, do not restate the answer, and do not open with a paraphrase of\n"
        "it. Continue the active goal or task from the current state and report only what changed\n"
        "since that answer. If the operator has sent nothing newer, there is nothing to reply to."
    )
    return 0


def dispatch(ev: dict) -> int:
    if mode() == "off" or not ev.get("session_id"):
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
        log(f"codex_turn_guard error (ignored): {exc}")
        sys.exit(0)
