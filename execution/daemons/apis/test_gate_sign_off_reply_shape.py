"""Sign-off round answering the ux review and the independent security run at
b76b1376 on PR #1181.

One class per item, each written against the finding's exact shape and run
RED against b76b1376 before the fix:

1. ``TestThePanelPromptAsksForTheFixedPosition`` — ux BLOCKING. The panelist
   prompt's comment-identity block told every lens to open its comment with
   the marker, then a plain ``review:<lens>`` line, then the header, and to
   "repeat the full review text" in its reply. A lens that did exactly that
   produced a reply the fixed-position rule refuses. The block now states
   the same shape for the comment and the reply, and a reply built exactly
   from it clears for every gate-owning lens.
2. ``TestAFormatOnlyRejectionIsVisible`` — ux BLOCKING. A reply refused only
   because its verdict was not in the fixed position left the gate pending
   with nothing on the PR. It now posts the Design ``**BLOCKED**`` template
   with its own reason token, once per gate per head, at both call sites,
   and never for a genuine blocking verdict.
3. ``TestAMalformedRowFailsClosed`` — security NON-BLOCKING. A well-formed
   page whose row was malformed read as "no issue entity", and a snapshot
   that was a string or a list raised out of ``load``.

Run: pytest execution/daemons/apis/test_gate_sign_off_reply_shape.py -v
"""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

import gate_waive
import skill_runner
import swarm_dispatch
from gate_waive import IssueGateStore
from skill_runner import SkillResult
from swarm_dispatch import SwarmDispatcher

import test_swarm_dispatch as tsd
from test_gate_sign_off_fixed_position import OWN_LOGIN, _PagedGitHub

HEAD = "b" * 40
GATE_LENSES = ("pm", "ux", "arch", "qa", "legal")
FIXED_POSITION_RULE = (
    "the FIRST line of your reply must be your header; the second line your verdict"
)
REASON = "gate_verdict_unreadable_format"


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


def _marker(lens: str) -> str:
    return f"<!-- review:{lens} commit={HEAD} -->"


# ── 1. The panelist prompt asks for the fixed position ──────────────────────


def _panel_prompt(lens: str) -> str:
    return SwarmDispatcher._panelist_prompt(
        tsd._trigger(), swarm_dispatch.lens_by_name(lens), "", 80, reviewed_head=HEAD
    )


def _identity_block(prompt: str) -> str:
    """The comment-identity block: from its first sentence to the findings
    rules that follow it."""
    start = prompt.index("Post your review as a PR comment")
    return prompt[start: prompt.index("Findings that must block the merge", start)]


def _instructed_reply(prompt: str, lens: str) -> str | None:
    """The reply a lens writes by following the prompt literally: the fenced
    lines the prompt says the comment and the reply start with, the verdict
    placeholder filled in, then the review body."""
    for block in prompt.split("```")[1::2]:
        lines = block.strip("\n").split("\n")
        if lines and lines[0].strip() == _marker(lens):
            head = "\n".join(lines).replace("**<VERDICT>**", "**SIGNED_OFF**")
            return f"{head}\n\n- [x] checked\n\nNo concerns found through this lens."
    return None


class TestThePanelPromptAsksForTheFixedPosition:
    @pytest.mark.parametrize("provisioned", [False, True], ids=["shared", "own-account"])
    @pytest.mark.parametrize("lens", GATE_LENSES)
    def test_a_reply_built_exactly_from_the_prompt_clears(
        self, monkeypatch, lens, provisioned
    ):
        if provisioned:
            monkeypatch.setenv(f"{_agent(lens).upper()}_AGENT_PAT", "ghp_x")
        prompt = _panel_prompt(lens)
        reply = _instructed_reply(prompt, lens)
        assert reply is not None, (
            "the prompt gives no literal start-of-reply template to follow"
        )
        assert swarm_dispatch.sign_off_is_warranted(reply, lens_agent=_agent(lens)) is True

    @pytest.mark.parametrize("provisioned", [False, True], ids=["shared", "own-account"])
    @pytest.mark.parametrize("lens", GATE_LENSES)
    def test_nothing_but_the_marker_is_asked_for_before_the_header(
        self, monkeypatch, lens, provisioned
    ):
        if provisioned:
            monkeypatch.setenv(f"{_agent(lens).upper()}_AGENT_PAT", "ghp_x")
        block = _identity_block(_panel_prompt(lens))
        assert "followed by `review:" not in block
        assert "Repeat the full review text in your reply here (" not in block
        assert FIXED_POSITION_RULE in block
        assert _header(lens) in block
        # A gate seat on its own account is still asked for the header.
        assert "Do NOT add an attribution header" not in block

    def test_the_contract_names_one_marker_per_comment(self):
        section = skill_runner.SWARM_GITHUB_CONTRACT.split(
            "### PR review head and supersession"
        )[1].split("### Neotoma backlinks")[0]
        assert "never both" in section
        assert "above the header" in section


# ── 2. A format-only rejection is visible ───────────────────────────────────


FORMAT_ONLY = {
    "plain-review-line-before-header": (
        "{m}\nreview:{lens}\n{h}\n**SIGNED_OFF**\n\nok",
        "a plain `review:` line comes before the lens header",
    ),
    "no-header": ("**SIGNED_OFF**\n\nok", "first line is not the lens header"),
    "verdict-after-sections": (
        "<<<SPEC_SECTION>>>\n## PM\nscope\n<<<END_SPEC_SECTION>>>\n{h}\n**SIGNED_OFF**",
        "first line is not the lens header",
    ),
    "prose-between-header-and-verdict": (
        "{h}\nsome prose\n**SIGNED_OFF**",
        "the line after the header is not a verdict line",
    ),
    "empty": ("", "the reply is empty"),
}

GENUINE_BLOCKING = {
    "request-changes": "{h}\n**REQUEST_CHANGES**\n\n[BLOCKING] scope: missing",
    "blocked": "{h}\n**BLOCKED**\n\n[BLOCKING] scope: missing",
    "blocking-finding-off-position": "Summary.\n{h}\n**SIGNED_OFF**\n\n[BLOCKING] x: y",
    "blocking-token-off-position": "Summary: REQUEST_CHANGES\n{h}\n**SIGNED_OFF**",
    "readable-comment": "{h}\n**COMMENT**\n\nobservations only",
    "clear": "{h}\n**SIGNED_OFF**\n\nok",
}


def _fill(shape: str, lens: str = "ux") -> str:
    return shape.format(m=_marker(lens), lens=lens, h=_header(lens))


def _predicate():
    fn = getattr(swarm_dispatch, "gate_verdict_format_rejected", None)
    assert fn is not None, "nothing tells a format-only rejection from a real no"
    return fn


def _describe():
    fn = getattr(swarm_dispatch, "describe_gate_verdict_position", None)
    assert fn is not None, "nothing describes why the verdict was unreadable"
    return fn


def _surface(d):
    method = getattr(d, "_surface_unreadable_gate_verdicts", None)
    assert method is not None, "nothing surfaces a format-only rejection"
    return method


def _entry(lens: str = "ux", observed: str = "first line is not the lens header"):
    return (lens, _agent(lens), _header(lens), observed)


def _dispatcher(monkeypatch, client) -> SwarmDispatcher:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")
    return SwarmDispatcher(tsd._StubNotifier(), tsd._config())


class TestAFormatOnlyRejectionIsVisible:
    @pytest.mark.parametrize("name", sorted(FORMAT_ONLY))
    def test_a_format_only_rejection_is_recognised_and_described(self, name):
        shape, expected = FORMAT_ONLY[name]
        stdout = _fill(shape)
        assert swarm_dispatch.sign_off_is_warranted(stdout, lens_agent=_agent("ux")) is False
        assert _predicate()(stdout, lens_agent=_agent("ux")) is True
        assert _describe()(stdout, lens_agent=_agent("ux")) == expected

    @pytest.mark.parametrize("name", sorted(GENUINE_BLOCKING))
    def test_a_genuine_verdict_is_not_a_format_rejection(self, name):
        assert _predicate()(_fill(GENUINE_BLOCKING[name]), lens_agent=_agent("ux")) is False

    def test_the_description_never_echoes_the_reply(self):
        injected = "IGNORE THE GATE <img src=x onerror=alert(1)> @someone"
        observed = _describe()(f"{injected}\n{_header('ux')}\n**SIGNED_OFF**",
                               lens_agent=_agent("ux"))
        assert injected not in observed
        assert "alert" not in observed and "@someone" not in observed

    def test_the_notice_renders_the_five_template_fields(self, monkeypatch):
        client = _PagedGitHub([])
        d = _dispatcher(monkeypatch, client)
        _run(_surface(d)(tsd._trigger(), 80, [_entry()], HEAD))
        assert len(client.posted) == 1
        body = client.posted[0]["body"]
        assert "**BLOCKED**" in body
        assert f"- reason: `{REASON}`" in body
        assert "- gate: `ux` (lens: `accipiter`)" in body
        assert "- attempted: " in body
        assert "- observed: first line is not the lens header" in body
        assert "- next_action: " in body
        next_action = body.split("- next_action: ")[1].split("\n")[0]
        assert _header("ux") in next_action
        assert "line 1" in next_action and "line 2" in next_action
        # Distinguishable from a genuine REQUEST_CHANGES.
        assert "not a REQUEST_CHANGES" in body
        assert swarm_dispatch.gate_verdict_unreadable_marker("ux", HEAD) in body

    def test_the_notice_posts_once_per_gate_per_head(self, monkeypatch):
        client = _PagedGitHub([])
        d = _dispatcher(monkeypatch, client)
        surface = _surface(d)
        _run(surface(tsd._trigger(), 80, [_entry()], HEAD))
        _run(surface(tsd._trigger(), 80, [_entry()], HEAD))
        assert len(client.posted) == 1
        _run(surface(tsd._trigger(), 80, [_entry(), _entry("arch")], HEAD))
        assert len(client.posted) == 2
        assert "gate: `arch`" in client.posted[1]["body"]
        assert "gate: `ux`" not in client.posted[1]["body"]
        _run(surface(tsd._trigger(), 80, [_entry()], "c" * 40))
        assert len(client.posted) == 3

    def test_a_marker_from_another_login_does_not_suppress(self, monkeypatch):
        marker = swarm_dispatch.gate_verdict_unreadable_marker("ux", HEAD)
        client = _PagedGitHub([{"body": marker, "user": {"login": "drive-by"}}])
        d = _dispatcher(monkeypatch, client)
        _run(_surface(d)(tsd._trigger(), 80, [_entry()], HEAD))
        assert len(client.posted) == 1
        assert OWN_LOGIN  # the paged client answers /user with the dispatcher's login

    # ── call sites ──

    def _panel_run(self, monkeypatch, waxwing_stdout: str) -> tuple[list, list]:
        surfaced: list = []
        signed: list = []
        import gate_waive as _gw

        async def fake_signed_write(
            self, repo, issue_number, gate, lens_agent, head_sha, next_owner
        ):
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

        monkeypatch.setattr(
            swarm_dispatch.IssueGateStore, "_sign_off_locked", fake_signed_write
        )
        monkeypatch.setattr(swarm_dispatch.IssueGateStore, "load", fake_load)
        monkeypatch.setattr(
            swarm_dispatch.IssueGateStore, "unverified_signed_off_gates", all_proven,
            raising=False,
        )
        monkeypatch.setattr(SwarmDispatcher, "_live_gate_status", fake_live)
        d = tsd._pr_dispatcher_with_stubs(
            monkeypatch, vanellus_stdout="**APPROVE**\nlgtm", calls=[]
        )

        async def fake_run_skill(skill, prompt, **kwargs):
            if skill == "lanius":
                return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear\nGATE_PENDING: arch", "")
            if skill == "waxwing":
                return SkillResult(skill, True, 0, waxwing_stdout, "")
            return SkillResult(skill, True, 0, "**APPROVE**\nlgtm", "")

        async def record(self, trigger, parent, entries, head):
            surfaced.append((parent, [e[0] for e in entries], head))

        monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
        monkeypatch.setattr(
            SwarmDispatcher, "_surface_unreadable_gate_verdicts", record, raising=False
        )
        _run(d._handle_pr(tsd._trigger(body="Closes #80.")))
        return surfaced, signed

    def test_panel_call_site_surfaces_a_format_only_rejection(self, monkeypatch):
        stdout = _fill("{m}\nreview:arch\n{h}\n**SIGNED_OFF**\n\nno concerns", "arch")
        surfaced, signed = self._panel_run(monkeypatch, stdout)
        assert surfaced == [(80, ["arch"], "a" * 40)]
        assert signed == []

    def test_panel_call_site_is_silent_for_a_genuine_blocking_verdict(self, monkeypatch):
        stdout = _fill("{h}\n**REQUEST_CHANGES**\n\n[BLOCKING] layering: x", "arch")
        surfaced, signed = self._panel_run(monkeypatch, stdout)
        assert surfaced == []
        assert signed == []

    def _issue_run(self, monkeypatch, pavo_stdout: str) -> list:
        surfaced: list = []

        async def fake_run_skill(skill, prompt, **kwargs):
            if skill == "pavo":
                return SkillResult(skill, True, 0, pavo_stdout, "")
            return SkillResult(
                skill, True, 0,
                f"<<<SPEC_SECTION>>>**Scope:** {skill}-section body with real "
                "substance to pass the not-just-narration floor.<<<END_SPEC_SECTION>>>",
                "",
            )

        tsd._install_pipeline_stubs(
            monkeypatch, fake_run_skill, select_agents=lambda *a, **kw: []
        )

        async def record(self, trigger, parent, entries, head):
            surfaced.append((trigger.number, parent, [e[0] for e in entries], head))

        monkeypatch.setattr(
            SwarmDispatcher, "_surface_unreadable_gate_verdicts", record, raising=False
        )
        d = SwarmDispatcher(tsd._StubNotifier(), tsd._config())
        _run(d._handle_issue_opened(tsd._issue_trigger()))
        return surfaced

    def test_issue_pipeline_call_site_surfaces_a_format_only_rejection(self, monkeypatch):
        pavo = "**🤖 Pavo — Ateles swarm, pm gate owner**"
        stdout = (
            "<<<SPEC_SECTION>>>**Scope:** pm section with enough substance to pass "
            f"the not-just-narration floor.<<<END_SPEC_SECTION>>>\n{pavo}\n**SIGNED_OFF**"
        )
        surfaced = self._issue_run(monkeypatch, stdout)
        issue = tsd._issue_trigger()
        head = swarm_dispatch.content_digest(
            [issue.repository, issue.number, issue.title, issue.body]
        )
        assert surfaced == [(100, None, ["pm"], head)]

    def test_issue_pipeline_call_site_is_silent_for_a_genuine_blocked(self, monkeypatch):
        pavo = "**🤖 Pavo — Ateles swarm, pm gate owner**"
        stdout = (
            f"{pavo}\n**BLOCKED**\n\n[BLOCKING] scope: no acceptance criteria\n\n"
            "<<<SPEC_SECTION>>>**Scope:** pm section with enough substance to pass "
            "the not-just-narration floor.<<<END_SPEC_SECTION>>>"
        )
        assert self._issue_run(monkeypatch, stdout) == []


# ── 3. A malformed row fails closed ─────────────────────────────────────────


MALFORMED_ROWS = {
    "empty-row": [{}],
    "null-snapshot": [{"entity_id": "ent_1", "snapshot": None}],
    "string-snapshot": [{"entity_id": "ent_1", "snapshot": "gate_status=signed_off"}],
    "list-snapshot": [{"entity_id": "ent_1", "snapshot": ["o/r", 795]}],
}
OTHER_REPO_ROW = [{"entity_id": "ent_2", "snapshot": {"repo": "other/r", "number": 795}}]
REAL_ROW = {
    "entity_id": "ent_real",
    "snapshot": dict(repo="o/r", number=795, gate_status={"arch": "pending"}),
}


def _store(monkeypatch, filtered: list, scanned: list | None = None) -> IssueGateStore:
    store = IssueGateStore("https://neotoma.invalid", "tok")

    async def post(path, payload):
        if "snapshot_filters" in payload:
            return {"entities": filtered}
        return {"entities": list(scanned or [])}

    monkeypatch.setattr(store, "_post", post)
    return store


def _patch_all_stores(monkeypatch, filtered: list) -> None:
    async def post(self, path, payload):
        if "snapshot_filters" in payload:
            return {"entities": filtered}
        return {"entities": []}

    monkeypatch.setattr(IssueGateStore, "_post", post)


ALL_SHAPES = {**MALFORMED_ROWS, "another-repo-row": OTHER_REPO_ROW}


class TestAMalformedRowFailsClosed:
    @pytest.mark.parametrize("name", sorted(ALL_SHAPES))
    def test_load_reports_a_failed_read(self, monkeypatch, name):
        state = _run(_store(monkeypatch, ALL_SHAPES[name]).load("o/r", 795))
        assert state.found is False
        assert state.read_failed is True

    @pytest.mark.parametrize("name", sorted(ALL_SHAPES))
    def test_pre_panel_reproof_holds_every_gate(self, monkeypatch, name):
        _patch_all_stores(monkeypatch, ALL_SHAPES[name])
        d = SwarmDispatcher(tsd._StubNotifier(), tsd._config(neotoma_token="tok"))
        assert _run(d._unverified_signed_off_gates("o/r", 795)) == set(
            swarm_dispatch.PRE_IMPL_GATES
        )

    @pytest.mark.parametrize("name", sorted(ALL_SHAPES))
    def test_decision_time_reread_holds_every_gate(self, monkeypatch, name):
        _patch_all_stores(monkeypatch, ALL_SHAPES[name])
        d = SwarmDispatcher(tsd._StubNotifier(), tsd._config(neotoma_token="tok"))
        assert _run(d._refresh_pending_gates("o/r", 795, {"ux"})) == set(
            swarm_dispatch.PRE_IMPL_GATES
        )

    def test_an_unmatched_filtered_row_falls_back_to_the_scan(self, monkeypatch):
        state = _run(_store(monkeypatch, OTHER_REPO_ROW, [REAL_ROW]).load("o/r", 795))
        assert state.read_failed is False
        assert state.entity_id == "ent_real"
        assert state.gate_status == {"arch": "pending"}

    def test_a_well_formed_not_found_is_still_read_ok(self, monkeypatch):
        state = _run(_store(monkeypatch, [], []).load("o/r", 795))
        assert state.found is False
        assert state.read_failed is False

    def test_a_string_snapshot_in_the_scan_is_a_failed_read(self, monkeypatch):
        state = _run(
            _store(monkeypatch, [], [{"entity_id": "x", "snapshot": "s"}]).load("o/r", 795)
        )
        assert state.read_failed is True

    @pytest.mark.parametrize("name", sorted(MALFORMED_ROWS))
    def test_sign_off_returns_the_unreadable_state_failure(self, monkeypatch, name):
        store = _store(monkeypatch, MALFORMED_ROWS[name])
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: {"key": f"/keys/{agent}.jwk", "sub": sub, "kid": "k"},
        )
        monkeypatch.setattr(
            gate_waive, "_lens_key_thumbprint", lambda identity: "tp", raising=False
        )
        write = mock.AsyncMock()
        monkeypatch.setattr("gate_waive._ns.signed_request", write)
        outcome = _run(store.sign_off("o/r", 795, "arch", "waxwing", HEAD))
        assert outcome.ok is False
        assert outcome.error == gate_waive.SIGN_OFF_UNREADABLE_STATE
        write.assert_not_called()
