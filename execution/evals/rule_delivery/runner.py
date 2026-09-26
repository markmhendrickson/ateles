#!/usr/bin/env python3
"""Rule-delivery evals: does a rule bind, by delivery channel x rule type x session length.

Every scenario is one real failure from the 2026-09-26 rule-delivery audit
(Neotoma analysis ent_b66293f0dcc8c887d4fdbeae), rebuilt in a sandbox. The
SAME rule set (three scenario targets plus twelve distractors, from
``fixtures/rules.json``) is delivered through each channel, so a difference
in compliance between channels is an effect of the channel, not of the rule
text. Each target rule has a different type (checkable prohibition, stance or
conduct, judgement or verification), so comparing scenarios within one
channel is the rule-type axis.

Channels (``--channels``):

  none           No rule delivered. The control: what the model does unprompted.
  claude_md      Full rule text in the project CLAUDE.md, as CLAUDE.md bullets.
  index_trigger  SessionStart hook prints the index rendered by the REAL
                 ``policy_skill_renderer`` from rows with no title, so each
                 conditional line is trigger + id only (the shape 35 of 48
                 lines had in the audited session). The full rule is only
                 reachable by fetching it by id from the fixture Neotoma.
  index_summary  Same hook and renderer, rows with titles (tier A: trigger +
                 one-line summary + id).
  index_full     Same hook; each line carries the full rule text inline.
  per_prompt     index_trigger, re-delivered by a UserPromptSubmit hook on
                 every prompt (the mid-session re-delivery fix, task
                 ent_0f6b8b4196a3665548179b25).

Session lengths (``--lengths``): ``short`` puts the scenario prompt in turn 1.
``long`` first runs ``--filler-turns`` turns, each asking for a one-sentence
summary of a ~9k-character chunk of unrelated repo code, in the SAME process
(stream-json, one message per completed turn), so the rule sits that far back
in context when the trigger arrives. The index hook fires on ``startup``
only, as in a live interactive session that never resumes.

Isolation: each run gets its own directory under ``--out``; the harness
loads no user settings, hooks, plugins or MCP servers
(``--setting-sources project,local``, ``--strict-mcp-config``), excludes the
user CLAUDE.md, turns auto-memory off, disallows Bash, subagents and web
tools, and a PreToolUse guard (``sandbox_guard.py``) refuses any file path
outside the run directory. Neotoma is ``fixture_mcp_server.py`` reading the
run directory's own state file. Nothing reaches prod, email, or a remote.

Usage::

    python3 execution/evals/rule_delivery/runner.py \
        --scenarios cursor_stdio,link_ids,grant_admission \
        --channels none,claude_md,index_trigger,index_full \
        --lengths short,long --repeats 3 --model sonnet \
        --out ~/.cache/ateles-rule-evals/<tag> --budget-usd 25

    python3 execution/evals/rule_delivery/runner.py --summarize ~/.cache/ateles-rule-evals/<tag>

``--dry-run`` builds every run directory and prints the channel payloads
without calling a model (this is what CI exercises).
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import selectors
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
FIXTURES = HERE / "fixtures"

sys.path.insert(0, str(HERE))
import checks  # noqa: E402

CHANNELS = (
    "none",
    "claude_md",
    "index_trigger",
    "index_summary",
    "index_full",
    "per_prompt",
)
INDEX_HEADER = "# Agent policy rule index (live from Neotoma, ateles#1261)\n"
FILLER_SOURCES = (
    "execution/scripts/hallucination_filter.py",
    "lib/transcript_clarity.py",
    "execution/scripts/local_whisper.py",
    "execution/scripts/silence_gate.py",
)
FILLER_CHARS = 9000
_FILLER_EXCLUDE = ("ent_", "cursor", "grant", "mcp.json", "neotoma", "claude.md")
DISALLOWED = "Bash,Agent,Task,WebFetch,WebSearch,NotebookEdit,AskUserQuestion"
ALLOWED = "Read,Edit,Write,MultiEdit,Glob,Grep,mcp__neotoma"

_budget_lock = threading.Lock()
_spent = {"usd": 0.0}


# --------------------------------------------------------------------------- fixtures


def load_rules() -> list[dict]:
    return json.loads((FIXTURES / "rules.json").read_text())["rules"]


def load_scenarios() -> tuple[dict, dict]:
    data = json.loads((FIXTURES / "scenarios.json").read_text())
    return {s["id"]: s for s in data["scenarios"]}, data["entities"]


def policy_row(rule: dict, with_title: bool) -> dict:
    row = {
        "_entity_id": rule["entity_id"],
        "entity_id": rule["entity_id"],
        "applies_when": rule["applies_when"],
        "rule": rule["rule"],
        "rule_kind": rule.get("rule_kind", "mandatory"),
        "scope": "global",
        "status": "active",
        "domain": rule["slug"],
    }
    if with_title:
        row["title"] = rule["title"]
    return row


# --------------------------------------------------------------------------- channels


def render_index(rules: list[dict], with_title: bool) -> str:
    """The index exactly as session_rule_index.py prints it, via the real renderer."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from lib.daemon_runtime.policy_skill_renderer import (
        render_index_text,
        render_skills,
    )  # noqa: PLC0415

    skills = render_skills(
        [policy_row(r, with_title) for r in rules], principal="eval@ateles-swarm"
    )
    return INDEX_HEADER + "\n" + render_index_text(skills, 9800) + "\n"


def render_index_full(rules: list[dict]) -> str:
    """Index with each rule's full text inline (the 'inline mandatory rules' proposal)."""
    always = [r for r in rules if r["applies_when"] == "always"]
    cond = sorted(
        (r for r in rules if r["applies_when"] != "always"),
        key=lambda r: r["entity_id"],
    )
    lines = [INDEX_HEADER, "## Always-applies rules"]
    lines += [f"- {r['rule']} [{r['entity_id']}]" for r in always]
    lines += ["", "## Conditional rules"]
    lines += [
        f"- When {r['applies_when']}: {r['rule']} [{r['entity_id']}]" for r in cond
    ]
    lines += ["", "<!-- tier: full -->"]
    return "\n".join(lines) + "\n"


def render_claude_md(rules: list[dict]) -> str:
    lines = [
        "# Project instructions",
        "",
        "These are standing rules for every session in this workspace. Neotoma is the system of record and is connected as the `neotoma` MCP server.",
        "",
        "## Standing rules",
        "",
    ]
    for r in rules:
        when = "" if r["applies_when"] == "always" else f"When {r['applies_when']}: "
        lines.append(f"- **{r['title']}.** {when}{r['rule']}")
    return "\n".join(lines) + "\n"


def channel_payloads(channel: str, rules: list[dict]) -> dict:
    """What each channel puts where: {'claude_md': str|None, 'hook_text': str|None, 'per_prompt': bool}."""
    if channel == "none":
        return {"claude_md": None, "hook_text": None, "per_prompt": False}
    if channel == "claude_md":
        return {
            "claude_md": render_claude_md(rules),
            "hook_text": None,
            "per_prompt": False,
        }
    if channel == "index_trigger":
        return {
            "claude_md": None,
            "hook_text": render_index(rules, with_title=False),
            "per_prompt": False,
        }
    if channel == "index_summary":
        return {
            "claude_md": None,
            "hook_text": render_index(rules, with_title=True),
            "per_prompt": False,
        }
    if channel == "index_full":
        return {
            "claude_md": None,
            "hook_text": render_index_full(rules),
            "per_prompt": False,
        }
    if channel == "per_prompt":
        return {
            "claude_md": None,
            "hook_text": render_index(rules, with_title=False),
            "per_prompt": True,
        }
    raise ValueError(f"unknown channel {channel}")


# --------------------------------------------------------------------------- run setup


def filler_prompts(n: int) -> list[str]:
    chunks: list[tuple[str, str]] = []
    for rel in FILLER_SOURCES:
        text = (REPO_ROOT / rel).read_text()
        for i in range(0, len(text), FILLER_CHARS):
            chunk = text[i : i + FILLER_CHARS]
            if len(chunk) < FILLER_CHARS // 2 or any(
                t in chunk.lower() for t in _FILLER_EXCLUDE
            ):
                continue
            chunks.append((rel, chunk))
    if n > len(chunks):
        raise ValueError(f"only {len(chunks)} filler chunks available, asked for {n}")
    return [
        f"Here's part of a file I'm reviewing ({Path(rel).name}). In one sentence, what does this part do? No need to use any tools.\n\n```python\n{chunk}\n```"
        for rel, chunk in chunks[:n]
    ]


def prepare_run(
    run_dir: Path, scenario: dict, entities: dict, rules: list[dict], channel: str
) -> dict:
    ws = run_dir / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    for rel, content in scenario.get("files", {}).items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    state = {"entities": json.loads(json.dumps(entities))}
    for r in rules:
        state["entities"][r["entity_id"]] = {
            "entity_type": "agent_policy",
            "snapshot": policy_row(r, True),
        }
    (ws / "neotoma_state.json").write_text(json.dumps(state, indent=2))

    payload = channel_payloads(channel, rules)
    if payload["claude_md"]:
        (ws / "CLAUDE.md").write_text(payload["claude_md"])
    hooks: dict = {
        "PreToolUse": [
            {
                "matcher": "Read|Edit|Write|MultiEdit|Glob|Grep|NotebookEdit",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"python3 '{HERE / 'sandbox_guard.py'}' '{ws}'",
                    }
                ],
            }
        ]
    }
    if payload["hook_text"]:
        idx = run_dir / "rule_index.txt"
        idx.write_text(payload["hook_text"])
        hook = [{"type": "command", "command": f"cat '{idx}'"}]
        hooks["SessionStart"] = [{"matcher": "startup", "hooks": hook}]
        if payload["per_prompt"]:
            hooks["UserPromptSubmit"] = [{"hooks": hook}]
    settings = {
        "claudeMdExcludes": [
            str(Path.home() / ".claude" / "CLAUDE.md"),
            "**/.claude/CLAUDE.md",
        ],
        "autoMemoryEnabled": False,
        "hooks": hooks,
    }
    (run_dir / "settings.json").write_text(json.dumps(settings, indent=2))
    mcp = {
        "mcpServers": {
            "neotoma": {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(HERE / "fixture_mcp_server.py"), str(ws)],
            }
        }
    }
    (run_dir / "mcp.json").write_text(json.dumps(mcp, indent=2))
    return payload


# --------------------------------------------------------------------------- driving claude


def _user_msg(text: str) -> str:
    return (
        json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                },
            }
        )
        + "\n"
    )


def drive_session(
    run_dir: Path,
    prompts: list[str],
    model: str,
    max_budget: float,
    turn_timeout: float,
) -> dict:
    """One Claude Code process, one prompt per completed turn. Returns events per turn."""
    ws = run_dir / "ws"
    cmd = [
        "claude",
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-hook-events",
        "--model",
        model,
        "--setting-sources",
        "project,local",
        "--settings",
        str(run_dir / "settings.json"),
        "--strict-mcp-config",
        "--mcp-config",
        str(run_dir / "mcp.json"),
        "--no-session-persistence",
        "--max-budget-usd",
        str(max_budget),
        "--allowedTools",
        ALLOWED,
        "--disallowedTools",
        DISALLOWED,
    ]
    events_log = (run_dir / "events.jsonl").open("w")
    stderr_log = (run_dir / "stderr.txt").open("w")
    proc = subprocess.Popen(
        cmd,
        cwd=ws,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr_log,
        text=True,
        bufsize=1,
    )
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    turns: list[list[dict]] = []
    error = None
    try:
        for prompt in prompts:
            proc.stdin.write(_user_msg(prompt))
            proc.stdin.flush()
            turn: list[dict] = []
            deadline = time.time() + turn_timeout
            done = False
            while not done:
                if time.time() > deadline:
                    error = "turn timeout"
                    break
                if not sel.select(timeout=5):
                    if proc.poll() is not None:
                        error = f"claude exited rc={proc.returncode}"
                        break
                    continue
                line = proc.stdout.readline()
                if not line:
                    error = f"claude closed stdout rc={proc.poll()}"
                    break
                events_log.write(line)
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                turn.append(ev)
                if ev.get("type") == "result":
                    done = True
            turns.append(turn)
            if error:
                break
    finally:
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        events_log.close()
        stderr_log.close()
    return {"turns": turns, "error": error}


def tool_calls(turn: list[dict]) -> list[dict]:
    """Ordered tool calls in one turn, each with the text of its result."""
    calls: list[dict] = []
    by_id: dict[str, dict] = {}
    for ev in turn:
        content = ev.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if ev.get("type") == "assistant" and block.get("type") == "tool_use":
                call = {
                    "name": block.get("name"),
                    "input": block.get("input") or {},
                    "result": "",
                }
                calls.append(call)
                by_id[block.get("id")] = call
            elif ev.get("type") == "user" and block.get("type") == "tool_result":
                call = by_id.get(block.get("tool_use_id"))
                if call is not None:
                    body = block.get("content")
                    if isinstance(body, list):
                        body = "".join(
                            b.get("text", "") for b in body if isinstance(b, dict)
                        )
                    call["result"] = str(body or "")
    return calls


def split_turns(events_path: Path) -> list[list[dict]]:
    """Rebuild per-turn event lists from a stored events.jsonl."""
    turns: list[list[dict]] = [[]]
    for line in events_path.read_text().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        turns[-1].append(ev)
        if ev.get("type") == "result":
            turns.append([])
    return [t for t in turns if t]


def result_event(turn: list[dict]) -> dict:
    for ev in reversed(turn):
        if ev.get("type") == "result":
            return ev
    return {}


def context_tokens(turn: list[dict]) -> int:
    """Prompt size of the FIRST model call in the trigger turn: how much context
    sat in front of the model when the trigger prompt arrived. (The result
    event's usage sums every call in the turn, so it is not this.)"""
    for ev in turn:
        if ev.get("type") == "assistant":
            u = ev.get("message", {}).get("usage") or {}
            return int(
                u.get("input_tokens", 0)
                + u.get("cache_read_input_tokens", 0)
                + u.get("cache_creation_input_tokens", 0)
            )
    return 0


def hook_delivered(turns: list[list[dict]]) -> bool:
    for turn in turns:
        for ev in turn:
            if (
                ev.get("type") == "system"
                and "hook" in str(ev.get("subtype", ""))
                and "rule index" in json.dumps(ev)
            ):
                return True
    return False


# --------------------------------------------------------------------------- one run


def run_one(
    spec: dict,
    args: argparse.Namespace,
    scenarios: dict,
    entities: dict,
    rules: list[dict],
) -> dict:
    scenario = scenarios[spec["scenario"]]
    run_id = (
        f"{spec['scenario']}__{spec['channel']}__{spec['length']}__r{spec['repeat']}"
    )
    run_dir = Path(args.out).expanduser() / "runs" / run_id
    if (run_dir / "result.json").exists() and not args.force:
        return json.loads((run_dir / "result.json").read_text())
    if run_dir.exists():
        shutil.rmtree(run_dir)
    payload = prepare_run(run_dir, scenario, entities, rules, spec["channel"])
    prompts = (
        filler_prompts(args.filler_turns) if spec["length"] == "long" else []
    ) + [scenario["prompt"]]
    target = next(r for r in rules if r["slug"] == scenario["target_rule"])

    base = {
        "run_id": run_id,
        **spec,
        "model": args.model,
        "rule_type": scenario["rule_type"],
        "target_rule": target["entity_id"],
        "n_turns": len(prompts),
    }
    if args.dry_run:
        return {
            **base,
            "outcome": "dry-run",
            "payload_chars": {
                k: len(v) for k, v in payload.items() if isinstance(v, str)
            },
        }

    with _budget_lock:
        if _spent["usd"] >= args.budget_usd:
            return {**base, "outcome": "skipped", "why": "budget exhausted"}

    t0 = time.time()
    session = drive_session(
        run_dir, prompts, args.model, args.max_run_usd, args.turn_timeout
    )
    turns = session["turns"]
    # total_cost_usd in a result event is CUMULATIVE for the process, so the
    # run's cost is the last one and a turn's cost is the difference.
    cumulative = [result_event(t).get("total_cost_usd") or 0.0 for t in turns]
    cost = max(cumulative) if cumulative else 0.0
    trigger_cost = (
        (cumulative[-1] - (cumulative[-2] if len(cumulative) > 1 else 0.0))
        if cumulative
        else None
    )
    with _budget_lock:
        _spent["usd"] += cost

    meta = {
        "cost_usd": round(cost, 4),
        "trigger_turn_cost_usd": round(trigger_cost, 4)
        if trigger_cost is not None
        else None,
        "duration_s": round(time.time() - t0, 1),
        "error": session["error"],
    }
    result = score_run(run_dir, base, scenario, entities, rules, turns, meta)
    (run_dir / "result.json").write_text(json.dumps(result, indent=2))
    return result


def score_run(
    run_dir: Path,
    base: dict,
    scenario: dict,
    entities: dict,
    rules: list[dict],
    turns: list[list[dict]],
    meta: dict,
) -> dict:
    """Judge one finished run from its events and its run directory. Pure given
    those inputs, so --rescore can re-judge stored runs when a check changes."""
    target = next(r for r in rules if r["slug"] == scenario["target_rule"])
    trigger = turns[-1] if len(turns) == base["n_turns"] else []
    calls = tool_calls(trigger)
    res = result_event(trigger)
    final_text = res.get("result") or ""
    ws = run_dir / "ws"
    if not trigger or not res:
        outcome = {"outcome": "error", "why": meta.get("error") or "no result"}
    elif scenario["id"] == "cursor_stdio":
        outcome = checks.check_cursor_stdio(ws, calls, final_text, scenario)
    elif scenario["id"] == "link_ids":
        tasks = {k: v for k, v in entities.items() if v["entity_type"] == "task"}
        outcome = checks.check_link_ids(ws, calls, final_text, scenario, tasks)
    elif scenario["id"] == "grant_admission":
        grant_id = next(
            k for k, v in entities.items() if v["entity_type"] == "agent_grant"
        )
        outcome = checks.check_grant_admission(
            ws,
            calls,
            final_text,
            scenario,
            grant_id,
            entities[grant_id]["snapshot"]["agent_sub"],
        )
    else:
        raise ValueError(scenario["id"])

    fetches = checks.fetch_positions(calls, target)
    by_id = checks.fetch_by_id_positions(calls, target["entity_id"])
    action = outcome.get("action_index")
    guard_path = ws / "guard_blocks.jsonl"
    hook_channel = (run_dir / "rule_index.txt").exists()
    return {
        **base,
        **outcome,
        "fetched_target": bool(fetches),
        "fetched_before_action": bool(fetches)
        and (action is None or fetches[0] < action),
        "fetched_by_id_before_action": bool(by_id)
        and (action is None or by_id[0] < action),
        "n_tool_calls": len(calls),
        "tools": [c["name"] for c in calls],
        "guard_blocks": len(guard_path.read_text().splitlines())
        if guard_path.exists()
        else 0,
        "hook_delivered": hook_delivered(turns) if hook_channel else None,
        "context_tokens_at_trigger": context_tokens(trigger),
        "final_text": final_text[:4000],
        **meta,
    }


def rescore(out: Path) -> int:
    rules = load_rules()
    scenarios, entities = load_scenarios()
    n = 0
    for rp in sorted((out / "runs").glob("*/result.json")):
        old = json.loads(rp.read_text())
        if old.get("outcome") in ("dry-run", "skipped"):
            continue
        base = {
            k: old[k]
            for k in (
                "run_id",
                "scenario",
                "channel",
                "length",
                "repeat",
                "model",
                "rule_type",
                "target_rule",
                "n_turns",
            )
        }
        meta = {
            k: old.get(k)
            for k in ("cost_usd", "trigger_turn_cost_usd", "duration_s", "error")
        }
        turns = split_turns(rp.parent / "events.jsonl")
        new = score_run(
            rp.parent, base, scenarios[base["scenario"]], entities, rules, turns, meta
        )
        rp.write_text(json.dumps(new, indent=2))
        n += 1
    return n


# --------------------------------------------------------------------------- summary


def _rate(rows: list[dict]) -> str:
    scored = [r for r in rows if r.get("outcome") in ("pass", "fail")]
    if not scored:
        return "—"
    k = sum(r["outcome"] == "pass" for r in scored)
    na = sum(r.get("outcome") == "n/a" for r in rows)
    return f"{k}/{len(scored)}" + (f" (+{na} n/a)" if na else "")


def _fetch(rows: list[dict]) -> str:
    """'any/by-id' counts of runs whose rule text reached context by a Neotoma
    read before the governed action (any read / retrieve_entity_snapshot by id)."""
    live = [r for r in rows if r.get("outcome") in ("pass", "fail", "n/a")]
    if not live:
        return "—"
    anyf = sum(bool(r.get("fetched_before_action")) for r in live)
    byid = sum(bool(r.get("fetched_by_id_before_action")) for r in live)
    return f"{anyf}/{len(live)} ({byid} by id)"


def summarize(out: Path) -> str:
    rows = [
        json.loads(p.read_text()) for p in sorted((out / "runs").glob("*/result.json"))
    ]
    if not rows:
        return "no results"
    channels = [c for c in CHANNELS if any(r["channel"] == c for r in rows)]
    scen = sorted({r["scenario"] for r in rows})
    lengths = [ln for ln in ("short", "long") if any(r["length"] == ln for r in rows)]
    lines = [
        f"Runs: {len(rows)}; model(s): {', '.join(sorted({r['model'] for r in rows}))}; "
        f"total cost ${sum(r.get('cost_usd') or 0 for r in rows):.2f}",
        "",
    ]
    lines.append("### Compliance (pass/scored) by scenario x channel x length")
    lines.append("")
    lines.append("| Scenario (rule type) | Length | " + " | ".join(channels) + " |")
    lines.append("|---|---|" + "---|" * len(channels))
    for s in scen:
        rt = next(r["rule_type"] for r in rows if r["scenario"] == s)
        for ln in lengths:
            cells = [
                _rate(
                    [
                        r
                        for r in rows
                        if r["scenario"] == s
                        and r["channel"] == c
                        and r["length"] == ln
                    ]
                )
                for c in channels
            ]
            lines.append(f"| {s} ({rt}) | {ln} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "### Marginals",
        "",
        "| Channel | short | long | all |",
        "|---|---|---|---|",
    ]
    for c in channels:
        sub = [r for r in rows if r["channel"] == c]
        lines.append(
            f"| {c} | {_rate([r for r in sub if r['length'] == 'short'])} | {_rate([r for r in sub if r['length'] == 'long'])} | {_rate(sub)} |"
        )
    lines += [
        "",
        "| Rule type (scenario) | none | delivered (any channel) |",
        "|---|---|---|",
    ]
    for s in scen:
        rt = next(r["rule_type"] for r in rows if r["scenario"] == s)
        sub = [r for r in rows if r["scenario"] == s]
        lines.append(
            f"| {rt} ({s}) | {_rate([r for r in sub if r['channel'] == 'none'])} | {_rate([r for r in sub if r['channel'] != 'none'])} |"
        )
    lines += [
        "",
        "### Rule text fetched from Neotoma before the governed action",
        "",
        "| Scenario | " + " | ".join(channels) + " |",
        "|---|" + "---|" * len(channels),
    ]
    for s in scen:
        lines.append(
            f"| {s} | "
            + " | ".join(
                _fetch([r for r in rows if r["scenario"] == s and r["channel"] == c])
                for c in channels
            )
            + " |"
        )
    lines += [
        "",
        "### Cost",
        "",
        "| Length | runs | mean $/run | mean context tokens at trigger |",
        "|---|---|---|---|",
    ]
    for ln in lengths:
        sub = [r for r in rows if r["length"] == ln and r.get("cost_usd") is not None]
        if sub:
            lines.append(
                f"| {ln} | {len(sub)} | {sum(r['cost_usd'] for r in sub) / len(sub):.3f} | "
                f"{int(sum(r.get('context_tokens_at_trigger') or 0 for r in sub) / len(sub))} |"
            )
    errs = [r for r in rows if r.get("outcome") in ("error", "skipped")]
    guard = [r for r in rows if r.get("guard_blocks")]
    if errs or guard:
        lines += [
            "",
            f"Errors/skips: {len(errs)}; runs where the sandbox guard refused a path: {len(guard)}",
        ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--scenarios", default="cursor_stdio,link_ids,grant_admission")
    ap.add_argument("--channels", default="none,claude_md,index_trigger,index_full")
    ap.add_argument("--lengths", default="short,long")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--filler-turns", type=int, default=8)
    ap.add_argument(
        "--out", default=str(Path.home() / ".cache" / "ateles-rule-evals" / "default")
    )
    ap.add_argument(
        "--budget-usd",
        type=float,
        default=25.0,
        help="stop starting runs past this total",
    )
    ap.add_argument(
        "--max-run-usd", type=float, default=2.0, help="per-run cap passed to claude"
    )
    ap.add_argument("--turn-timeout", type=float, default=300.0)
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument(
        "--force", action="store_true", help="re-run cells that already have a result"
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--summarize", metavar="OUT_DIR")
    ap.add_argument(
        "--rescore", metavar="OUT_DIR", help="re-judge stored runs with current checks"
    )
    args = ap.parse_args(argv)

    if args.rescore:
        print(f"rescored {rescore(Path(args.rescore).expanduser())} runs")
        return 0
    if args.summarize:
        print(summarize(Path(args.summarize).expanduser()))
        return 0

    rules = load_rules()
    scenarios, entities = load_scenarios()
    specs = [
        {"scenario": s, "channel": c, "length": ln, "repeat": i}
        for s in args.scenarios.split(",")
        for c in args.channels.split(",")
        for ln in args.lengths.split(",")
        for i in range(1, args.repeats + 1)
    ]
    for sp in specs:
        if (
            sp["scenario"] not in scenarios
            or sp["channel"] not in CHANNELS
            or sp["length"] not in ("short", "long")
        ):
            ap.error(f"bad cell {sp}")
    if not args.dry_run and shutil.which("claude") is None:
        ap.error("claude CLI not on PATH")
    Path(args.out).expanduser().mkdir(parents=True, exist_ok=True)
    with cf.ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        futs = {
            pool.submit(run_one, sp, args, scenarios, entities, rules): sp
            for sp in specs
        }
        for f in cf.as_completed(futs):
            try:
                r = f.result()
            except Exception as exc:  # noqa: BLE001 — one broken run must not lose the rest
                r = {
                    "run_id": str(futs[f]),
                    "outcome": "error",
                    "why": f"{type(exc).__name__}: {exc}",
                }
            print(
                json.dumps(
                    {
                        k: r.get(k)
                        for k in (
                            "run_id",
                            "outcome",
                            "fetched_before_action",
                            "cost_usd",
                            "error",
                            "why",
                        )
                    }
                ),
                flush=True,
            )
    if not args.dry_run:
        print()
        print(summarize(Path(args.out).expanduser()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
