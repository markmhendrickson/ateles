"""A gate-owning lens's clean review that the dispatcher did not record
(ateles#1181, PR #1173 arch review, 2026-09-24).

Waxwing posted a well-formed `**SIGNED_OFF**` arch review on PR #1173 at
13:32:14Z. The dispatcher judges the lens's REPLY, never the posted comment,
and the reply it received (recovered verbatim from the lens's session
transcript; 660 characters, matching the daemon's `660B stdout` log line) was a
summary ending in a link to the comment. The fixed-position read correctly
refused it. Two things then went wrong, and each class below is written
against one of them:

1. ``TestTheRecordedArchReply`` pins the recorded reply: it must still NOT
   clear (the fixed position, the blocking veto and the signed write are
   unchanged), it IS a format-only rejection, and the notice now says what it
   is: a pointer at the posted comment, not the review.
2. ``TestTheNoticeIsPostedBeforeTheNextLensRuns``: the unreadable-verdict
   notice was deferred until every later lens had finished, and Apis restarted
   mid-panel (15:35:05 CEST), so nothing reached the PR. The notice is now
   posted as soon as the lens's reply is judged.
3. ``TestThePromptSaysTheReplyIsTheReview``: every panelist reply that day
   (28 of 28 recovered) opened with a posting note, a storage note, or was a
   summary. The prompt now names that failure and ends on the reply shape,
   and the prior-art contract no longer asks for a report "at the top" of a
   reply whose first lines must be the header and verdict.

Run: pytest execution/daemons/apis/test_gate_verdict_reply_is_the_review.py -v
"""

from __future__ import annotations

import asyncio

import pytest

import gate_waive as _gw
import skill_runner
import swarm_dispatch
from skill_runner import SkillResult
from swarm_dispatch import SwarmDispatcher

import test_swarm_dispatch as tsd
from test_gate_sign_off_reply_shape import _PagedGitHub, _dispatcher

HEAD = "b" * 40
GATE_LENSES = ("pm", "ux", "arch", "qa", "legal")

# Waxwing's reply on PR #1173, verbatim, with the trailing newline the CLI
# printed (660 characters, as `waxwing dispatch via claude ok (660B stdout)`).
RECORDED_ARCH_REPLY = (
    "Posted and signed off. Summary: this is a process-internal env-var kill "
    "switch in Apis with no MCP/HTTP/schema surface, so the "
    "interface-consistency gate doesn't apply — layering is respected (gate "
    "sits at the SSE-consumer boundary, doesn't reach into `dispatch_task` "
    "internals or the GitHub pipeline), the \"no design applies\" basis is "
    "correct, and reversibility is high. Two non-blocking notes: the PR body's "
    "README claim is stale relative to its own diff, and the startup-log "
    "observability claim is unverified from diff-only review.\n\n"
    "**arch gate: SIGNED_OFF** — [comment posted]"
    "(https://github.com/markmhendrickson/ateles/pull/1173#issuecomment-5815112080).\n"
)

POINTER_PHRASE = (
    "the reply is a note or summary pointing at the posted comment, not the "
    "review itself"
)


@pytest.fixture(autouse=True)
def _no_ambient_tokens(monkeypatch):
    for name in ("ATELES_AGENT_PAT", "NEOTOMA_AGENT_PAT", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    for lens in GATE_LENSES:
        agent = swarm_dispatch.lens_by_name(lens).agent
        monkeypatch.delenv(f"{agent.upper()}_AGENT_PAT", raising=False)
    monkeypatch.delenv("APIS_GATE_SIGNING_CUTOFF", raising=False)


def _run(coro):
    return asyncio.run(coro)


def _agent(lens: str) -> str:
    return swarm_dispatch.lens_by_name(lens).agent


def _header(lens: str) -> str:
    return swarm_dispatch.attribution_header(_agent(lens), f"{lens} lens panelist")


# ── 1. The recorded reply ───────────────────────────────────────────────────


class TestTheRecordedArchReply:
    def test_it_is_the_recorded_length(self):
        assert len(RECORDED_ARCH_REPLY) == 660

    def test_it_still_does_not_clear(self):
        # The fix must not make a summary clear a gate: the verdict is read
        # from the fixed position only.
        assert swarm_dispatch.lens_own_verdict(RECORDED_ARCH_REPLY, lens_agent="waxwing") is None
        assert swarm_dispatch.sign_off_is_warranted(RECORDED_ARCH_REPLY, lens_agent="waxwing") is False

    def test_it_is_a_format_only_rejection(self):
        assert swarm_dispatch.output_has_blocking_verdict(RECORDED_ARCH_REPLY) is False
        assert swarm_dispatch.body_has_blocking_findings(RECORDED_ARCH_REPLY) is False
        assert (
            swarm_dispatch.gate_verdict_format_rejected(RECORDED_ARCH_REPLY, lens_agent="waxwing")
            is True
        )

    def test_the_notice_says_it_is_a_pointer_not_the_review(self):
        assert (
            swarm_dispatch.describe_gate_verdict_position(
                RECORDED_ARCH_REPLY, lens_agent="waxwing"
            )
            == POINTER_PHRASE
        )

    def test_a_posting_note_before_the_header_is_not_called_a_pointer(self):
        # The review is present, only preceded by a note: that is the
        # existing "first line" description, not the pointer one.
        reply = (
            "Comment posted: https://github.com/o/r/pull/1#issuecomment-1\n\n"
            f"<!-- review:arch commit={HEAD} -->\n{_header('arch')}\n"
            "**SIGNED_OFF**\n\nok\n"
        )
        assert swarm_dispatch.sign_off_is_warranted(reply, lens_agent="waxwing") is False
        assert (
            swarm_dispatch.describe_gate_verdict_position(reply, lens_agent="waxwing")
            == "first line is not the lens header"
        )

    def test_the_pointer_phrase_never_echoes_the_reply(self):
        observed = swarm_dispatch.describe_gate_verdict_position(
            RECORDED_ARCH_REPLY, lens_agent="waxwing"
        )
        assert "issuecomment-5815112080" not in observed
        assert "kill switch" not in observed

    def test_the_rendered_notice_carries_the_pointer_phrase(self, monkeypatch):
        client = _PagedGitHub([])
        d = _dispatcher(monkeypatch, client)
        observed = swarm_dispatch.describe_gate_verdict_position(
            RECORDED_ARCH_REPLY, lens_agent="waxwing"
        )
        _run(
            d._surface_unreadable_gate_verdicts(
                tsd._trigger(), 80, [("arch", "waxwing", _header("arch"), observed)], HEAD
            )
        )
        assert len(client.posted) == 1
        body = client.posted[0]["body"]
        assert f"- observed: {POINTER_PHRASE}" in body
        assert "not a REQUEST_CHANGES" in body


# ── 2. The notice is posted before the next lens runs ───────────────────────


class _LaterLensDied(Exception):
    """Stands in for the daemon being restarted while a later lens runs."""


def _panel(monkeypatch, *, waxwing_stdout: str, later_lens_dies: bool):
    events: list[tuple[str, str]] = []
    signed: list[str] = []

    async def fake_signed_write(self, repo, issue_number, gate, lens_agent, head_sha, next_owner):
        signed.append(gate)
        return _gw.SignOffOutcome(ok=True, gate=gate, lens_agent=lens_agent)

    class _Cleared:
        found = True
        read_failed = False
        gate_status = {"pm": "signed_off", "ux": "signed_off", "arch": "signed_off"}
        gate_status_unreadable = False

    async def fake_load(self, repo, issue_number):
        return _Cleared()

    async def fake_live(self, repository, issue_number):
        return {"pm": "signed_off", "ux": "signed_off", "arch": "pending"}

    async def all_proven(self, state, owners):
        return set()

    monkeypatch.setattr(swarm_dispatch.IssueGateStore, "_sign_off_locked", fake_signed_write)
    monkeypatch.setattr(swarm_dispatch.IssueGateStore, "load", fake_load)
    monkeypatch.setattr(
        swarm_dispatch.IssueGateStore, "unverified_signed_off_gates", all_proven, raising=False
    )
    monkeypatch.setattr(SwarmDispatcher, "_live_gate_status", fake_live)
    d = tsd._pr_dispatcher_with_stubs(monkeypatch, vanellus_stdout="**APPROVE**\nlgtm", calls=[])
    seen_waxwing = {"done": False}

    async def fake_run_skill(skill, prompt, **kwargs):
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear\nGATE_PENDING: arch", "")
        events.append(("run", skill))
        if skill == "waxwing":
            seen_waxwing["done"] = True
            return SkillResult(skill, True, 0, waxwing_stdout, "")
        if later_lens_dies and seen_waxwing["done"] and skill != "vanellus":
            raise _LaterLensDied(skill)
        return SkillResult(skill, True, 0, "**APPROVE**\nlgtm", "")

    async def record(self, trigger, parent, entries, head):
        events.append(("notice", ",".join(e[0] for e in entries)))

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(SwarmDispatcher, "_surface_unreadable_gate_verdicts", record)
    return d, events, signed


def _lenses_after_waxwing(events) -> list[str]:
    runs = [skill for kind, skill in events if kind == "run"]
    return runs[runs.index("waxwing") + 1:]


class TestTheNoticeIsPostedBeforeTheNextLensRuns:
    def test_the_notice_precedes_every_later_lens(self, monkeypatch):
        d, events, signed = _panel(
            monkeypatch, waxwing_stdout=RECORDED_ARCH_REPLY, later_lens_dies=False
        )
        _run(d._handle_pr(tsd._trigger(body="Closes #80.")))
        assert ("notice", "arch") in events
        later = _lenses_after_waxwing(events)
        assert later, "the test panel must seat a lens after waxwing"
        assert events.index(("notice", "arch")) == events.index(("run", "waxwing")) + 1
        assert signed == []

    def test_a_later_lens_dying_does_not_lose_the_notice(self, monkeypatch):
        d, events, signed = _panel(
            monkeypatch, waxwing_stdout=RECORDED_ARCH_REPLY, later_lens_dies=True
        )
        with pytest.raises(_LaterLensDied):
            _run(d._handle_pr(tsd._trigger(body="Closes #80.")))
        assert ("notice", "arch") in events
        assert signed == []

    def test_a_genuine_block_posts_no_format_notice(self, monkeypatch):
        blocked = (
            f"{_header('arch')}\n**REQUEST_CHANGES**\n\n[BLOCKING] layering: x\n"
        )
        d, events, signed = _panel(monkeypatch, waxwing_stdout=blocked, later_lens_dies=False)
        _run(d._handle_pr(tsd._trigger(body="Closes #80.")))
        assert not [e for e in events if e[0] == "notice"]
        assert signed == []

    def test_a_clear_reply_still_signs(self, monkeypatch):
        clear = (
            f"<!-- review:arch commit={'a' * 40} -->\n{_header('arch')}\n"
            "**SIGNED_OFF**\n\nno concerns\n"
        )
        d, events, signed = _panel(monkeypatch, waxwing_stdout=clear, later_lens_dies=False)
        _run(d._handle_pr(tsd._trigger(body="Closes #80.")))
        assert not [e for e in events if e[0] == "notice"]
        assert signed == ["arch"]


# ── 3. The prompt says the reply is the review ──────────────────────────────


def _panel_prompt(lens: str, changed_files: list[str] | None = None) -> str:
    return SwarmDispatcher._panelist_prompt(
        tsd._trigger(),
        swarm_dispatch.lens_by_name(lens),
        "",
        80,
        changed_files=changed_files,
        reviewed_head=HEAD,
    )


# A path the foundation reading list keys on, so the long inlined reading
# block is present and the reply shape has to survive coming after it.
_READING_LIST_PATHS = ["execution/daemons/apis/swarm_dispatch.py"]


class TestThePromptSaysTheReplyIsTheReview:
    @pytest.mark.parametrize("lens", GATE_LENSES)
    def test_it_names_the_observed_preambles(self, lens):
        prompt = _panel_prompt(lens).lower()
        for phrase in (
            "a note that the comment was posted",
            "the comment's url",
            "not a summary",
            "in place of the review",
        ):
            assert phrase in prompt, phrase

    @pytest.mark.parametrize("lens", GATE_LENSES)
    def test_the_reply_shape_is_the_last_thing_a_gate_owner_reads(self, lens):
        prompt = _panel_prompt(lens, _READING_LIST_PATHS)
        tail = prompt.rsplit("\n\n", 1)[-1]
        assert tail.startswith("FINAL REPLY FORMAT")
        assert f"<!-- review:{lens} commit={HEAD} -->" in tail
        assert _header(lens) in tail
        assert "not a note that the comment was posted" in tail

    def test_a_seat_that_owns_no_gate_gets_no_reply_reminder(self):
        prompt = _panel_prompt("content", _READING_LIST_PATHS)
        assert "FINAL REPLY FORMAT" not in prompt

    @pytest.mark.parametrize("lens", GATE_LENSES)
    def test_one_start_of_reply_template_only(self, lens):
        # The reminder restates the lines inline; the prompt still carries a
        # single fenced block that opens with the marker.
        prompt = _panel_prompt(lens, _READING_LIST_PATHS)
        marker = f"<!-- review:{lens} commit={HEAD} -->"
        fenced = [
            b for b in prompt.split("```")[1::2] if b.strip("\n").split("\n")[0].strip() == marker
        ]
        assert len(fenced) == 1

    def test_the_prior_art_contract_keeps_the_header_first(self):
        contract = skill_runner.SWARM_PRIOR_ART_CONTRACT
        assert "at the top of your output" in contract
        assert "gate verdict" in contract
        assert "directly after the verdict line" in contract
