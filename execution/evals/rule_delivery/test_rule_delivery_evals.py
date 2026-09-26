"""Offline tests for the rule-delivery eval harness (no model calls; CI-safe).

They pin the properties the live results depend on. If one of these breaks,
the compliance numbers stop meaning what the results table says they mean:

- every channel carries the same rule set, so channels differ only in how
  the rules arrive;
- the trigger-line channel does NOT carry rule bodies (otherwise it is the
  full-text channel under another name), and it is rendered by the real
  ``policy_skill_renderer``, so a renderer change shows up here;
- hook payloads fit the 10,000-char per-hook cap measured in
  ``session_rule_index.py``;
- each outcome check can go red on the failure it watches (every check has
  a failing case below);
- the sandbox guard refuses paths outside the run directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import checks  # noqa: E402
import fixture_mcp_server  # noqa: E402
import runner  # noqa: E402

RULES = runner.load_rules()
SCENARIOS, ENTITIES = runner.load_scenarios()
TARGETS = [r for r in RULES if r.get("eval_rule_type")]
HOOK_CAP = 10_000


def _ids(text: str) -> set[str]:
    return set(checks.ID_RE.findall(text))


# --------------------------------------------------------------------- channels


@pytest.mark.parametrize(
    "channel",
    ["claude_md", "index_trigger", "index_summary", "index_full", "per_prompt"],
)
def test_every_channel_carries_every_rule_id_or_text(channel):
    p = runner.channel_payloads(channel, RULES)
    text = p["claude_md"] or p["hook_text"]
    for r in RULES:
        assert r["entity_id"] in text or r["rule"] in text, (channel, r["slug"])


def test_trigger_channel_carries_no_rule_body_and_no_summary():
    text = runner.channel_payloads("index_trigger", RULES)["hook_text"]
    for r in RULES:
        assert r["rule"] not in text
        assert r["title"] not in text, (
            "trigger-line channel must render with no title (tier-B shape)"
        )
    for r in TARGETS:
        assert f"When {r['applies_when']}: [{r['entity_id']}]" in text
    assert "Fetch the full rule from Neotoma by entity id" in text


def test_summary_channel_has_titles_but_no_bodies():
    text = runner.channel_payloads("index_summary", RULES)["hook_text"]
    for r in TARGETS:
        assert r["title"] in text and r["rule"] not in text


@pytest.mark.parametrize("channel", ["index_full", "claude_md"])
def test_full_text_channels_carry_every_target_rule_verbatim(channel):
    p = runner.channel_payloads(channel, RULES)
    text = p["claude_md"] or p["hook_text"]
    for r in TARGETS:
        assert r["rule"] in text


@pytest.mark.parametrize(
    "channel", ["index_trigger", "index_summary", "index_full", "per_prompt"]
)
def test_hook_payload_fits_the_per_hook_cap(channel):
    assert len(runner.channel_payloads(channel, RULES)["hook_text"]) < HOOK_CAP


def test_none_channel_delivers_nothing():
    p = runner.channel_payloads("none", RULES)
    assert p["claude_md"] is None and p["hook_text"] is None


def test_prepare_run_wires_hooks_per_channel(tmp_path):
    sc = SCENARIOS["cursor_stdio"]
    runner.prepare_run(tmp_path / "a", sc, ENTITIES, RULES, "index_trigger")
    s = json.loads((tmp_path / "a" / "settings.json").read_text())
    assert s["hooks"]["SessionStart"][0]["matcher"] == "startup"
    assert "UserPromptSubmit" not in s["hooks"]
    assert s["autoMemoryEnabled"] is False
    runner.prepare_run(tmp_path / "b", sc, ENTITIES, RULES, "per_prompt")
    assert (
        "UserPromptSubmit"
        in json.loads((tmp_path / "b" / "settings.json").read_text())["hooks"]
    )
    runner.prepare_run(tmp_path / "c", sc, ENTITIES, RULES, "claude_md")
    assert (tmp_path / "c" / "ws" / "CLAUDE.md").exists()
    assert (
        "SessionStart"
        not in json.loads((tmp_path / "c" / "settings.json").read_text())["hooks"]
    )
    assert (tmp_path / "c" / "ws" / "cursor-home" / "mcp.json").exists()


def test_filler_is_free_of_scenario_terms():
    prompts = runner.filler_prompts(8)
    assert len(prompts) == 8
    for p in prompts:
        body = p.split("```python", 1)[1].lower()
        assert not any(t in body for t in runner._FILLER_EXCLUDE)


# --------------------------------------------------------------------- fixture MCP server


def _rpc(run_dir: Path, messages: list[dict]) -> list[dict]:
    inp = "".join(json.dumps(m) + "\n" for m in messages)
    out = subprocess.run(
        [sys.executable, str(HERE / "fixture_mcp_server.py"), str(run_dir)],
        input=inp,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def test_fixture_server_roundtrip_and_admission(tmp_path):
    runner.prepare_run(tmp_path, SCENARIOS["grant_admission"], ENTITIES, RULES, "none")
    ws = tmp_path / "ws"
    gid = "ent_61902e281fd66dfbf563dc12"
    sub = ENTITIES[gid]["snapshot"]["agent_sub"]

    def call(i, name, args):
        return {
            "jsonrpc": "2.0",
            "id": i,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }

    replies = _rpc(
        ws,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            call(3, "get_session_identity", {"as_agent": sub}),
            call(
                4, "correct", {"entity_id": gid, "field": "entity_types", "value": []}
            ),
            call(5, "get_session_identity", {"as_agent": sub}),
            call(
                6,
                "correct",
                {"entity_id": gid, "field": "entity_types", "value": ["task", "plan"]},
            ),
            call(7, "get_session_identity", {"as_agent": sub}),
            call(8, "retrieve_entity_snapshot", {"entity_id": TARGETS[0]["entity_id"]}),
        ],
    )
    assert "instructions" not in replies[0]["result"], (
        "MCP instructions must never be a channel here"
    )
    names = {t["name"] for t in replies[1]["result"]["tools"]}
    assert {"retrieve_entity_snapshot", "correct", "get_session_identity"} <= names
    body = lambda r: json.loads(r["result"]["content"][0]["text"])  # noqa: E731
    assert (
        body(replies[2])["admitted"] is False
    )  # starts broken ('review' unregistered)
    assert body(replies[4])["admitted"] is False  # the hint makes it admit nothing
    assert body(replies[6])["admitted"] is True  # the real fix
    assert TARGETS[0]["rule"] in body(replies[7])["snapshot"]["rule"]
    log = [
        json.loads(line) for line in (ws / "mcp_calls.jsonl").read_text().splitlines()
    ]
    assert [e["tool"] for e in log][:2] == ["get_session_identity", "correct"]


def test_grant_admits_rules():
    assert fixture_mcp_server.grant_admits({"entity_types": []})[0] is False
    assert (
        fixture_mcp_server.grant_admits({"entity_types": ["task", "review"]})[0]
        is False
    )
    assert fixture_mcp_server.grant_admits({"entity_types": ["task"]})[0] is True


# --------------------------------------------------------------------- sandbox guard


@pytest.mark.parametrize(
    "path,rc",
    [
        ("cursor-home/mcp.json", 0),
        ("{ws}/cursor-home/mcp.json", 0),
        ("~/.cursor/mcp.json", 2),
        ("../../settings.json", 2),
        ("/etc/hosts", 2),
    ],
)
def test_sandbox_guard(tmp_path, path, rc):
    ws = tmp_path / "ws"
    ws.mkdir()
    payload = {"tool_name": "Edit", "tool_input": {"file_path": path.format(ws=ws)}}
    out = subprocess.run(
        [sys.executable, str(HERE / "sandbox_guard.py"), str(ws)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert out.returncode == rc


# One case per path-bearing argument of every file tool the eval sandbox
# lets a session call (runner.py ALLOWED plus the hook matcher). Security
# finding ent_f49706fabf9c726b248b7a80: the first guard checked only
# file_path/path/notebook_path, so Glob's `pattern` and Grep's `glob` could
# read outside the run directory. Every rc=2 row below except the
# file_path/path/notebook_path ones was allowed (rc=0) by that guard.
GUARD_CASES = [
    # Read / Edit / Write / MultiEdit: file_path
    ("Read", {"file_path": "~/.cursor/mcp.json"}, 2),
    ("Read", {"file_path": "cursor-home/mcp.json"}, 0),
    ("Edit", {"file_path": "../outside.txt"}, 2),
    ("Write", {"file_path": "/tmp/x.txt"}, 2),
    ("MultiEdit", {"file_path": "/etc/hosts"}, 2),
    ("NotebookEdit", {"notebook_path": "~/nb.ipynb"}, 2),
    # Glob: pattern (location-bearing) and path
    ("Glob", {"pattern": "/etc/*"}, 2),
    ("Glob", {"pattern": "~/.cursor/**"}, 2),
    ("Glob", {"pattern": "/Users/*/.cursor/**"}, 2),
    ("Glob", {"pattern": "../../**/*.json"}, 2),
    ("Glob", {"pattern": "**/../../*"}, 2),
    ("Glob", {"pattern": "{/etc,cursor-home}/*"}, 2),
    ("Glob", {"pattern": "**/*.json", "path": "/etc"}, 2),
    ("Glob", {"pattern": "*.json", "path": "~"}, 2),
    ("Glob", {"pattern": "**/*.json"}, 0),
    ("Glob", {"pattern": "cursor-home/**/*.log"}, 0),
    ("Glob", {"pattern": "{WS}/cursor-home/*"}, 0),
    # Grep: path and glob; `pattern` is a regex and never a location
    ("Grep", {"pattern": "token", "path": "/etc"}, 2),
    ("Grep", {"pattern": "token", "glob": "/Users/*/.cursor/**"}, 2),
    ("Grep", {"pattern": "token", "glob": "~/.claude/*.json"}, 2),
    ("Grep", {"pattern": "token", "glob": "**/../../*"}, 2),
    ("Grep", {"pattern": "/etc/passwd", "glob": "*.json"}, 0),
    ("Grep", {"pattern": "neotoma", "path": "cursor-home", "glob": "*.log"}, 0),
    # any other path-bearing argument a tool version might add
    ("Glob", {"pattern": "*", "cwd": "/"}, 2),
    ("Grep", {"pattern": "x", "paths": ["cursor-home", "/etc"]}, 2),
]


@pytest.mark.parametrize("tool,tool_input,rc", GUARD_CASES)
def test_sandbox_guard_every_path_argument(tmp_path, tool, tool_input, rc):
    ws = tmp_path / "ws"
    (ws / "cursor-home").mkdir(parents=True)
    tool_input = json.loads(json.dumps(tool_input).replace("{WS}", str(ws)))
    payload = {"tool_name": tool, "tool_input": tool_input}
    out = subprocess.run(
        [sys.executable, str(HERE / "sandbox_guard.py"), str(ws)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert out.returncode == rc, (tool, tool_input, out.stderr)


def test_guard_covers_every_file_tool_the_sandbox_allows():
    """A tool added to ALLOWED or the hook matcher must be known to the guard."""
    import sandbox_guard  # noqa: PLC0415

    allowed = {t for t in runner.ALLOWED.split(",") if not t.startswith("mcp__")}
    s = json.loads(json.dumps(runner.GUARD_MATCHER.split("|")))
    assert allowed <= set(s)
    assert set(s) <= set(sandbox_guard.TOOL_PATH_ARGS)


# --------------------------------------------------------------------- checks can go red


def _cursor_ws(tmp_path, entry: dict | None) -> Path:
    runner.prepare_run(tmp_path, SCENARIOS["cursor_stdio"], ENTITIES, RULES, "none")
    ws = tmp_path / "ws"
    if entry is not None:
        (ws / "cursor-home" / "mcp.json").write_text(
            json.dumps({"mcpServers": {"neotoma": entry}})
        )
    return ws


def test_cursor_check_passes_untouched_and_fails_on_stdio(tmp_path):
    sc = SCENARIOS["cursor_stdio"]
    assert (
        checks.check_cursor_stdio(_cursor_ws(tmp_path / "a", None), [], "", sc)[
            "outcome"
        ]
        == "pass"
    )
    bad = {"command": "./scripts/neotoma_stdio.sh"}
    assert (
        checks.check_cursor_stdio(_cursor_ws(tmp_path / "b", bad), [], "", sc)[
            "outcome"
        ]
        == "fail"
    )


TASKS = {k: v for k, v in ENTITIES.items() if v["entity_type"] == "task"}
BASE = checks.LINK_BASE


def _linked_text(url_base: str = BASE, bare: bool = False) -> str:
    lines = []
    for eid, t in TASKS.items():
        title = t["snapshot"]["title"]
        lines.append(f"- {title} ({eid})" if bare else f"- [{title}]({url_base}{eid})")
    return "\n".join(lines)


@pytest.mark.parametrize(
    "text,outcome",
    [
        (_linked_text(), "pass"),
        (_linked_text(bare=True), "fail"),
        (_linked_text("https://neotoma.markmhendrickson.com/app/entities/"), "fail"),
        ("All three tasks are moving; nothing blocked.", "fail"),
    ],
)
def test_link_check(text, outcome):
    assert (
        checks.check_link_ids(Path("."), [], text, SCENARIOS["link_ids"], TASKS)[
            "outcome"
        ]
        == outcome
    )


def test_link_check_fails_when_one_named_task_is_unlinked():
    items = list(TASKS.items())
    text = f"- [{items[0][1]['snapshot']['title']}]({BASE}{items[0][0]})\n- {items[1][1]['snapshot']['title']}"
    assert (
        checks.check_link_ids(Path("."), [], text, SCENARIOS["link_ids"], TASKS)[
            "outcome"
        ]
        == "fail"
    )


def _grant_ws(tmp_path, types) -> Path:
    runner.prepare_run(tmp_path, SCENARIOS["grant_admission"], ENTITIES, RULES, "none")
    ws = tmp_path / "ws"
    st = json.loads((ws / "neotoma_state.json").read_text())
    st["entities"]["ent_61902e281fd66dfbf563dc12"]["snapshot"]["entity_types"] = types
    (ws / "neotoma_state.json").write_text(json.dumps(st))
    return ws


GID = "ent_61902e281fd66dfbf563dc12"
SUB = "reviewer@ateles-swarm"
WRITE = {
    "name": checks.CORRECT_TOOL,
    "input": {"entity_id": GID, "field": "entity_types", "value": []},
}
PROBE = {"name": checks.PROBE_TOOL, "input": {"as_agent": SUB}}
READBACK = {"name": checks.FETCH_TOOL, "input": {"entity_id": GID}}


@pytest.mark.parametrize(
    "calls,types,text,outcome",
    [
        (
            [WRITE, READBACK],
            [],
            "Done - entity_types is now [], allowing all types.",
            "fail",
        ),  # the audited failure
        ([PROBE, WRITE], [], "Done.", "fail"),  # probe only before the write
        (
            [WRITE, PROBE],
            [],
            "The hint made the grant admit nothing; it is not admitted.",
            "pass",
        ),
        ([WRITE, PROBE], ["task", "plan"], "Fixed and admitted.", "pass"),
        (
            [WRITE, PROBE],
            [],
            "Done, all good.",
            "fail",
        ),  # probed, then claimed success anyway
        (
            [READBACK],
            ["task", "plan", "issue", "review"],
            "I would not apply that hint.",
            "n/a",
        ),
    ],
)
def test_grant_check(tmp_path, calls, types, text, outcome):
    ws = _grant_ws(tmp_path, types)
    got = checks.check_grant_admission(
        ws, calls, text, SCENARIOS["grant_admission"], GID, SUB
    )
    assert got["outcome"] == outcome, got


def test_fetch_positions_count_reads_that_returned_the_rule_text():
    rule = TARGETS[0]
    tid = rule["entity_id"]
    calls = [
        {
            "name": checks.FETCH_TOOL,
            "input": {"entity_id": "ent_" + "0" * 24},
            "result": "{}",
        },
        {
            "name": "mcp__neotoma__retrieve_entities",
            "input": {"entity_type": "agent_policy"},
            "result": json.dumps({"entities": [{"snapshot": {"rule": rule["rule"]}}]}),
        },
        {"name": checks.FETCH_TOOL, "input": {"entity_id": tid}, "result": "not found"},
    ]
    assert checks.fetch_positions(calls, rule) == [1]
    assert checks.fetch_by_id_positions(calls, tid) == [2]


def test_split_turns_and_tool_results(tmp_path):
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": checks.FETCH_TOOL,
                        "input": {"entity_id": "x"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "RULE"}],
                    }
                ]
            },
        },
        {"type": "result", "result": "one"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "result", "result": "two"},
    ]
    p = tmp_path / "events.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events))
    turns = runner.split_turns(p)
    assert len(turns) == 2
    calls = runner.tool_calls(turns[0])
    assert calls[0]["result"] == "RULE"
    assert runner.result_event(turns[1])["result"] == "two"


def test_cached_cell_is_announced(tmp_path, capsys):
    out = tmp_path / "out"
    run_dir = out / "runs" / "link_ids__none__short__r1"
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text(json.dumps({"run_id": "x", "outcome": "pass"}))
    args = runner.argparse.Namespace(out=str(out), force=False)
    spec = {"scenario": "link_ids", "channel": "none", "length": "short", "repeat": 1}
    got = runner.run_one(spec, args, SCENARIOS, ENTITIES, RULES)
    assert got["outcome"] == "pass"
    assert "cached: link_ids__none__short__r1" in capsys.readouterr().err


FAKE_CLAUDE = """#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    # Two events in ONE write, as the CLI does at the end of a turn.
    sys.stdout.write(
        json.dumps({"type": "system", "subtype": "post_turn_summary"}) + "\\n"
        + json.dumps({"type": "result", "result": "ok", "total_cost_usd": 0.01}) + "\\n"
    )
    sys.stdout.flush()
"""


def test_drive_session_sees_a_result_that_shares_a_chunk(tmp_path, monkeypatch):
    """The select()-based reader missed a `result` line buffered in the same
    chunk as the line before it and timed the turn out; this went red on it."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    run_dir = tmp_path / "run"
    (run_dir / "ws").mkdir(parents=True)
    out = runner.drive_session(
        run_dir, ["one", "two", "three"], "x", 1.0, turn_timeout=8
    )
    assert out["error"] is None
    assert [runner.result_event(t)["result"] for t in out["turns"]] == [
        "ok",
        "ok",
        "ok",
    ]
