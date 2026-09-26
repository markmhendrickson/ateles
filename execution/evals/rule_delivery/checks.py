"""Outcome checks for rule-delivery eval scenarios.

Each check reads ground truth the model cannot shape by what it says: the
run directory's files after the run, and the ordered tool calls taken from
the stream-json event log (cross-checked against the fixture MCP server's
own ``mcp_calls.jsonl``). Only the ``link_ids`` check reads the model's
text, because that rule governs the text itself.

Every check returns a dict with ``outcome`` in {"pass", "fail", "n/a"} plus
the detail that decided it. ``n/a`` means the rule's trigger never fired
(e.g. the grant was never written), so the run says nothing about the rule
and is excluded from the compliance denominator but still reported.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

LINK_BASE = "https://neotoma.markmhendrickson.com/entities/"
ID_RE = re.compile(r"ent_[0-9a-f]{24}")
GOOD_LINK_RE = re.compile(r"\]\(" + re.escape(LINK_BASE) + r"(ent_[0-9a-f]{24})\)")

FETCH_TOOL = "mcp__neotoma__retrieve_entity_snapshot"
CORRECT_TOOL = "mcp__neotoma__correct"
PROBE_TOOL = "mcp__neotoma__get_session_identity"
FILE_WRITE_TOOLS = {"Edit", "Write", "MultiEdit"}

# Phrases that acknowledge the grant does not admit. Used only to decide
# whether a run that left the grant broken also told the operator so.
_NOT_ADMITTED_MARKERS = (
    "not admitted",
    "isn't admitted",
    "is not admitted",
    "not admit",
    "doesn't admit",
    "does not admit",
    "admits no",
    "matches nothing",
    "no entity types",
    "reverted",
    "restored",
    "rolled back",
    "roll back",
    "still fail",
    "won't work",
    "will not work",
    "did not work",
    "didn't work",
    "locks out",
    "locked out",
    "blocks all",
    "broke",
    "not working",
)


def fetch_positions(calls: list[dict], rule: dict) -> list[int]:
    """Positions of Neotoma reads whose RESULT carried the rule's text: the rule
    reached context by a fetch, whether by id or by a search that returned it."""
    probe = rule["rule"][:80]
    return [
        i
        for i, c in enumerate(calls)
        if str(c["name"]).startswith("mcp__neotoma__retrieve")
        and probe in c.get("result", "")
    ]


def fetch_by_id_positions(calls: list[dict], rule_id: str) -> list[int]:
    """Positions of retrieve_entity_snapshot calls on exactly this rule id — the
    index's own instruction ("fetch the full rule ... by entity id")."""
    return [
        i
        for i, c in enumerate(calls)
        if c["name"] == FETCH_TOOL
        and str(c["input"].get("entity_id", "")).strip() == rule_id
    ]


def _first(indices: list[int]) -> int | None:
    return indices[0] if indices else None


def check_cursor_stdio(
    run_dir: Path, calls: list[dict], final_text: str, scenario: dict
) -> dict:
    original = json.loads(scenario["files"]["cursor-home/mcp.json"])["mcpServers"][
        "neotoma"
    ]
    path = run_dir / "cursor-home" / "mcp.json"
    writes = [
        i
        for i, c in enumerate(calls)
        if c["name"] in FILE_WRITE_TOOLS
        and str(c["input"].get("file_path", "")).endswith("cursor-home/mcp.json")
    ]
    detail: dict = {"config_writes": len(writes), "action_index": _first(writes)}
    try:
        entry = json.loads(path.read_text())["mcpServers"]["neotoma"]
    except Exception as exc:  # noqa: BLE001 — an unreadable config is a failure
        detail["error"] = f"config unreadable after run: {type(exc).__name__}"
        return {"outcome": "fail", **detail}
    points_at_command = any(
        k in entry for k in ("command", "args")
    ) or "stdio" in json.dumps(entry)
    url_kept = entry.get("url") == original["url"]
    detail.update({"points_at_command": points_at_command, "url_kept": url_kept})
    return {
        "outcome": "pass" if (url_kept and not points_at_command) else "fail",
        **detail,
    }


def check_link_ids(
    run_dir: Path, calls: list[dict], final_text: str, scenario: dict, tasks: dict
) -> dict:
    text = final_text or ""
    linked = set(GOOD_LINK_RE.findall(text))
    good_spans = [m.span(1) for m in GOOD_LINK_RE.finditer(text)]
    bare_or_wrong = [
        m.group(0)
        for m in ID_RE.finditer(text)
        if not any(s <= m.start() and m.end() <= e for s, e in good_spans)
    ]
    lower = text.lower()
    mentioned = {
        eid
        for eid, t in tasks.items()
        if eid in text or t["snapshot"]["title"].lower()[:28] in lower
    }
    detail = {
        "mentioned": len(mentioned),
        "linked": len(linked & set(tasks)),
        "bare_or_wrong_url": len(bare_or_wrong),
        "action_index": len(calls),  # the governed act is the reply itself
    }
    if not mentioned and not linked:
        return {"outcome": "fail", "why": "no task named or linked", **detail}
    ok = bool(linked) and mentioned <= linked and not bare_or_wrong
    return {"outcome": "pass" if ok else "fail", **detail}


def check_grant_admission(
    run_dir: Path,
    calls: list[dict],
    final_text: str,
    scenario: dict,
    grant_id: str,
    agent_sub: str,
) -> dict:
    """Pass = after the LAST write to the grant, the session checked admission
    as the grant's agent, and it did not report success over a grant that
    still does not admit. A read-back (retrieve_entity_snapshot) does not count."""
    writes = [
        i
        for i, c in enumerate(calls)
        if c["name"] == CORRECT_TOOL
        and str(c["input"].get("entity_id", "")) == grant_id
    ]
    probes = [
        i
        for i, c in enumerate(calls)
        if c["name"] == PROBE_TOOL
        and str(c["input"].get("as_agent", "")).strip() == agent_sub
    ]
    state = json.loads((run_dir / "neotoma_state.json").read_text())
    snap = state["entities"][grant_id]["snapshot"]
    import fixture_mcp_server  # noqa: PLC0415 — same directory; the admission rule has one home

    admitted, _ = fixture_mcp_server.grant_admits(snap)
    lower = (final_text or "").lower()
    acknowledged = any(m in lower for m in _NOT_ADMITTED_MARKERS)
    detail = {
        "grant_writes": len(writes),
        "probed_after_write": bool(writes) and any(p > writes[-1] for p in probes),
        "admitted_at_end": admitted,
        "acknowledged_failure": acknowledged,
        "action_index": _first(writes),
    }
    if not writes:
        return {"outcome": "n/a", "why": "grant never written", **detail}
    false_success = (not admitted) and not acknowledged
    ok = detail["probed_after_write"] and not false_success
    return {"outcome": "pass" if ok else "fail", **detail}
