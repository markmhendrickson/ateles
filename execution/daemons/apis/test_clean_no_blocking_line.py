"""A clean "No [BLOCKING] findings" line is not a blocking finding.

The review steward ends every approval with the checklist line
`- [x] No [BLOCKING] findings.` `body_has_blocking_findings` used to read the
marker in it as a finding, so a clean APPROVE roll-up was submitted as
REQUEST_CHANGES, stamped `block_kind=content`, paged the operator as an
unparseable verdict and started a fix round for a defect nobody reported.

The exemption is a whole line and nothing more: every case at the bottom of
this file still blocks (docs/foundation/principles.md#5, fail closed on the
field that carries the safety meaning).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from skill_runner import SkillResult
from swarm_dispatch import (
    SwarmDispatcher,
    body_has_blocking_findings,
    is_clean_no_blocking_line,
    parse_review_verdict,
    review_blocks_merge,
    verdict_to_review_event,
)
from test_swarm_dispatch import (
    _FakeHttpxClientForVanellus,
    _StubNotifier,
    _async_return,
    _config,
    _pr_dispatcher_with_stubs,
    _trigger,
)

HEAD = "a" * 40

# The steward's approval roll-up, in the shape it posts it: aggregation marker,
# attribution header, bold verdict token, per-lens roll-up and the checklist
# that carries the clean line.
STEWARD_APPROVAL = (
    f"<!-- vanellus-aggregation commit={HEAD} -->\n"
    "**🤖 Vanellus — Ateles swarm, PR steward**\n"
    "\n"
    "**APPROVE**\n"
    "\n"
    "### Per-lens roll-up\n"
    "- **pm (Pavo):** APPROVE — scope matches the issue.\n"
    "- **arch (Waxwing):** APPROVE — no contract change.\n"
    "- **qa (Phoenicurus):** APPROVE — effect test present and red without the fix.\n"
    "\n"
    "### Checklist\n"
    "- [x] Required checks green on the reviewed head.\n"
    "- [x] No [BLOCKING] findings.\n"
    "- [x] Non-blocking notes recorded for follow-up.\n"
    "\n"
    "Blocking: 0\n"
)


# ── the check itself ─────────────────────────────────────────────────────────


def test_steward_approval_is_not_blocking():
    assert parse_review_verdict(STEWARD_APPROVAL) == "approve"
    assert not body_has_blocking_findings(STEWARD_APPROVAL)
    assert not review_blocks_merge("approve", STEWARD_APPROVAL)
    assert verdict_to_review_event("approve", STEWARD_APPROVAL) == "APPROVE"


@pytest.mark.parametrize(
    "line",
    [
        "No [BLOCKING] findings",
        "No [BLOCKING] findings.",
        "No [BLOCKING] finding.",
        "- [x] No [BLOCKING] findings.",
        "* [X] No [BLOCKING] finding",
        "1. No [BLOCKING] findings.",
        "  - No [BLOCKING] findings.  ",
    ],
)
def test_clean_line_shapes_are_exempt(line):
    assert is_clean_no_blocking_line(line)
    assert not body_has_blocking_findings(f"**APPROVE**\n{line}\n")


# ── the formal review, the content-block stamp, escalation and fix round ─────


def test_steward_approval_submits_approve_and_gates_for_merge(monkeypatch):
    """Drives `_handle_pr` with the steward's approval as the aggregation. The
    formal review is APPROVE, and the routing branch (the only entrance to the
    unparseable-verdict escalation and the fix round) is not taken."""
    calls = []
    d = _pr_dispatcher_with_stubs(monkeypatch, vanellus_stdout=STEWARD_APPROVAL, calls=calls)
    asyncio.run(d._handle_pr(_trigger(body="Closes #80.")))
    assert ("review", "APPROVE") in calls
    assert ("review", "REQUEST_CHANGES") not in calls
    assert not any(c[0] == "route" for c in calls)
    assert ("gate", None) in calls


def test_steward_approval_starts_no_escalation_and_no_fix_round(monkeypatch):
    """The same run with the REAL `_route_blocking_findings`: nothing claims an
    escalation, pages the operator, records a fix round or dispatches a fixer."""
    calls = []
    real_route = SwarmDispatcher._route_blocking_findings
    d = _pr_dispatcher_with_stubs(monkeypatch, vanellus_stdout=STEWARD_APPROVAL, calls=calls)
    monkeypatch.setattr(SwarmDispatcher, "_route_blocking_findings", real_route)

    async def fake_claim(self, trigger, kind):
        calls.append(("escalation", kind))
        return True

    async def fake_record_round(self, trigger, n):
        calls.append(("fix_round", n))

    async def fake_round_count(self, trigger):
        return 0

    monkeypatch.setattr(SwarmDispatcher, "_claim_escalation", fake_claim)
    monkeypatch.setattr(SwarmDispatcher, "_record_fix_round", fake_record_round)
    monkeypatch.setattr(SwarmDispatcher, "_fix_round_count", fake_round_count)
    monkeypatch.setattr(
        SwarmDispatcher,
        "_blocking_findings_from_reviewed_head_comments",
        lambda self, t, h: _async_return({}),
    )
    asyncio.run(d._handle_pr(_trigger(body="Closes #80.")))
    assert not any(c[0] in {"escalation", "fix_round"} for c in calls), calls
    assert not any("blocking findings could be parsed" in m for m in d.notifier.sent)
    assert ("gate", None) in calls


def test_steward_approval_fallback_comment_has_no_content_block_stamp(monkeypatch):
    client = _FakeHttpxClientForVanellus(existing_bodies=[])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")
    asyncio.run(
        SwarmDispatcher(_StubNotifier(), _config())._post_missing_vanellus_comment(
            _trigger(), SkillResult("vanellus", True, 0, STEWARD_APPROVAL, "")
        )
    )
    assert len(client.post_calls) == 1
    posted = client.post_calls[0]["json"]["body"]
    assert "block_kind=" not in posted
    assert f"<!-- vanellus-aggregation commit={HEAD} -->" in posted


# ── what must still block ────────────────────────────────────────────────────

STILL_BLOCKING = {
    "exception clause": "No [BLOCKING] findings except the deadline gap",
    "finding whose summary starts with No": "[BLOCKING] No tests: this path is untested",
    "finding after the clean sentence": "No [BLOCKING] findings. [BLOCKING] deadline: the retry overruns",
    "finding elsewhere in the body": STEWARD_APPROVAL + "\n[BLOCKING] scope: the retry path is untested\n",
    "fullwidth marker": "- [x] No [BLOCKING] findings.\n［ＢＬＯＣＫＩＮＧ］ scope: x",
    "homoglyph marker": "- [x] No [BLOCKING] findings.\n[ВLOCKING] scope: x",  # Cyrillic Ve
    "zero-width split marker": "- [x] No [BLOCKING] findings.\n[BLO‍CKING] scope: x",
    "zero-width joined to the clean line": "No [BLOCKING] findings.​[BLOCKING] scope: x",
    "line separator joined to the clean line": "No [BLOCKING] findings. [BLOCKING] scope: x",
    "unticked checkbox": "- [ ] No [BLOCKING] findings.",
}


@pytest.mark.parametrize("body", list(STILL_BLOCKING.values()), ids=list(STILL_BLOCKING))
def test_real_findings_still_block(body):
    assert body_has_blocking_findings(body)
    assert review_blocks_merge("approve", body)
    assert verdict_to_review_event("approve", body) == "REQUEST_CHANGES"


def test_real_finding_beside_clean_line_still_routes_findings(monkeypatch):
    calls = []
    body = STILL_BLOCKING["finding elsewhere in the body"]
    d = _pr_dispatcher_with_stubs(monkeypatch, vanellus_stdout=body, calls=calls)
    asyncio.run(d._handle_pr(_trigger(body="Closes #80.")))
    assert ("review", "REQUEST_CHANGES") in calls
    assert any(c[0] == "route" for c in calls)
    assert ("gate", None) not in calls

