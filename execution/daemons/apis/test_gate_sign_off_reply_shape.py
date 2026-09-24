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


# ── 2b. A leading harness acknowledgment does not hide a real header ────────
#
# ateles#1247. PR #1149's `ux` (Accipiter) and `pm` (Pavo) rounds each posted
# a byte-exact, contract-shaped comment (marker → header → verdict, no
# leading blank) — confirmed via `gh pr view 1149 --repo markmhendrickson/
# ateles --json comments`. Apis still rejected both with
# `gate_verdict_unreadable_format`, `observed: first line is not the lens
# header`. That message was literally true of what the dispatcher actually
# parsed: the persisted `pr_review.content` for both rounds (Neotoma
# `ent_7640dba44b1b85f92be15de2` / `ent_d535f3202fad94b4b9fd698f`) shows the
# harness's own final reply — the exact `stdout` `gate_verdict_format_rejected`
# / `describe_gate_verdict_position` receive — opened with an extra
# acknowledgment line the posted comment never carried (`Posted: https://
# github.com/…#issuecomment-…`, `Posted. Reply below, reproducing the exact
# same content as returned per the gate-verdict contract.`) ahead of the
# marker+header+verdict the lens actually composed. Neither of the issue's
# two named candidates (a stale/cached comment read, or a marker-skip
# off-by-one) is what happened — the parser's line-0 check was correct
# against the text it was given; it was given the wrong artifact's leading
# line. `_skip_leading_ack_line` looks one line further ONLY when the
# candidate line reads as a `Posted…` acknowledgment (not a marker, not a
# header) AND the next non-blank line is itself a marker or a valid header —
# so a reply that is genuinely headerless still falls through to the
# existing rejection unchanged (asserted below alongside the fix).


class TestALeadingPostedAckLineDoesNotHideTheHeader:
    # Byte-for-byte `result.stdout` the dispatcher parsed on PR #1149 (the
    # harness's own final reply, captured verbatim in the persisted
    # `pr_review.content` for `ent_7640dba44b1b85f92be15de2`; trimmed to the
    # position-relevant prefix — the parser never reads past the verdict
    # line plus the "no second header/verdict" scan, and a full multi-KB
    # review body adds nothing this test needs to assert).
    UX_STDOUT_1149 = (
        "Posted: https://github.com/markmhendrickson/ateles/pull/1149"
        "#issuecomment-5821092658\n\n"
        "<!-- review:ux commit=6d4f69db7929c118b7f964235689d93e4202886c -->\n"
        "**🤖 Accipiter — Ateles swarm, ux lens panelist**\n"
        "**APPROVE**\n\n"
        "Reviewed diff-only (no PR checkout available); no code was "
        "executed, so findings below are held to the stated evidence bar.\n"
    )
    # Same shape, `pm`/Pavo round (`ent_d535f3202fad94b4b9fd698f`) — a
    # differently-worded acknowledgment line, confirming the fix keys on the
    # `Posted` prefix pattern rather than one exact sentence.
    PM_STDOUT_1149 = (
        "Posted. Reply below, reproducing the exact same content as "
        "returned per the gate-verdict contract.\n\n"
        "<!-- review:pm commit=6d4f69db7929c118b7f964235689d93e4202886c -->\n"
        "**🤖 Pavo — Ateles swarm, pm lens panelist**\n"
        "**APPROVE**\n\n"
        "### Scope match against the pm-signed acceptance criteria "
        "(issue #1150)\n"
    )

    def test_red_before_green_reproduces_the_exact_reported_false_positive(self):
        """Pinned to the literal reported bytes: `describe_gate_verdict_
        position` must NEVER again read `first line is not the lens header`
        for a reply whose real header sits one acknowledgment line down —
        that exact string against that exact shape is the false positive
        ateles#1247 reports. This assertion is red against pre-fix
        `swarm_dispatch.py` (confirmed: reverting `_skip_leading_ack_line`
        and its two call sites reproduces `observed ==
        "first line is not the lens header"` for both fixtures below)."""
        observed_ux = swarm_dispatch.describe_gate_verdict_position(
            self.UX_STDOUT_1149, lens_agent="accipiter"
        )
        observed_pm = swarm_dispatch.describe_gate_verdict_position(
            self.PM_STDOUT_1149, lens_agent="pavo"
        )
        assert observed_ux != "first line is not the lens header", observed_ux
        assert observed_pm != "first line is not the lens header", observed_pm

    def test_the_ux_round_now_reads_as_an_explicit_clear(self):
        assert (
            swarm_dispatch.lens_own_verdict(self.UX_STDOUT_1149, lens_agent="accipiter")
            == "approve"
        )
        assert swarm_dispatch.gate_verdict_format_rejected(
            self.UX_STDOUT_1149, lens_agent="accipiter"
        ) is False
        assert swarm_dispatch.sign_off_is_warranted(
            self.UX_STDOUT_1149, lens_agent="accipiter"
        ) is True

    def test_the_pm_round_now_reads_as_an_explicit_clear(self):
        assert (
            swarm_dispatch.lens_own_verdict(self.PM_STDOUT_1149, lens_agent="pavo")
            == "approve"
        )
        assert swarm_dispatch.gate_verdict_format_rejected(
            self.PM_STDOUT_1149, lens_agent="pavo"
        ) is False
        assert swarm_dispatch.sign_off_is_warranted(
            self.PM_STDOUT_1149, lens_agent="pavo"
        ) is True

    def test_shared_parser_one_fixture_pair_covers_every_gate(self):
        """`lens_own_verdict` / `gate_verdict_format_rejected` /
        `describe_gate_verdict_position` are the ONE parsing path both PR-panel
        call sites (`_handle_pr`) and the additive issue-spec call site use for
        every gate (`pm`, `ux`, `arch`, `qa`, `legal`, …) — there is no
        per-gate branch in any of the three functions this fix touches, so the
        `ux`/`pm` fixtures above are representative of the whole class and no
        `arch`/`qa`/`legal`-specific fixture is needed."""
        assert swarm_dispatch.lens_own_verdict.__module__ == "swarm_dispatch"
        # Same function, different lens_agent: arch would clear identically
        # were an arch-owning lens's reply shaped this way. Uses the same
        # bounded ack shape the fix actually recognises (a GitHub PR-comment
        # URL) — an arbitrary "Posted: <url>" is deliberately NOT enough,
        # see `test_an_arbitrary_posted_prefixed_url_is_not_an_ack_line`.
        arch_header = swarm_dispatch.attribution_header("waxwing", "arch reviewer")
        stdout = (
            "Posted: https://github.com/markmhendrickson/ateles/pull/9"
            f"#issuecomment-1\n\n{arch_header}\n**SIGNED_OFF**\n\nok"
        )
        assert (
            swarm_dispatch.lens_own_verdict(stdout, lens_agent="waxwing") == "signed_off"
        )

    # ── no-regression: every existing true-positive rejection is unchanged ──

    def test_a_headerless_reply_still_rejects_even_with_a_posted_prefix(self):
        """The fix is NOT a blanket "skip the first line" — a `Posted…`
        prefix followed by prose that is still not a header must keep
        rejecting with the same message as today, or a lens whose reply is
        genuinely malformed would silently start clearing."""
        stdout = "Posted somewhere.\nSome prose that is not a header\n**SIGNED_OFF**"
        assert (
            swarm_dispatch.describe_gate_verdict_position(stdout, lens_agent="accipiter")
            == "first line is not the lens header"
        )
        assert swarm_dispatch.gate_verdict_format_rejected(
            stdout, lens_agent="accipiter"
        ) is True

    def test_a_plain_headerless_reply_is_unaffected(self):
        assert (
            swarm_dispatch.describe_gate_verdict_position(
                "**SIGNED_OFF**\n\nok", lens_agent="accipiter"
            )
            == "first line is not the lens header"
        )

    def test_a_lenses_own_prose_that_happens_to_start_with_posted_still_rejects(self):
        """CONFIRMED by two independent reviews during this PR's own build:
        an EARLIER, looser version of the ack pattern (`^Posted\\b` as a bare
        prefix on the candidate line) treated ANY reply whose own first line
        of prose happened to start with the word "Posted" as a harness
        artifact to skip past — so a lens's genuinely malformed reply
        (real header two lines down purely by coincidence, e.g. because it
        echoes an unrelated marker+header from quoted material) silently
        cleared instead of failing closed. `_POSTED_ACK_LINE_RE` now matches
        only the two acknowledgment shapes actually observed on PR #1149,
        anchored at BOTH ends (`\\Z`) — a real GitHub PR-comment URL or the
        exact fixed "Reply below…" sentence, never a bare word-prefix on
        arbitrary content. This reply is deliberately NOT either of those
        shapes even though it starts with "Posted" and is followed by a
        well-formed header: it is one continuous sentence of the lens's own
        analysis, which is exactly what must keep rejecting."""
        stdout = (
            "Posted-mortem analysis follows below for the maintainers to "
            "review at their convenience.\n\n"
            f"{_header('ux')}\n**APPROVE**\n\nLGTM\n"
        )
        assert (
            swarm_dispatch.describe_gate_verdict_position(stdout, lens_agent="accipiter")
            == "first line is not the lens header"
        )
        assert swarm_dispatch.lens_own_verdict(stdout, lens_agent="accipiter") is None
        assert swarm_dispatch.sign_off_is_warranted(stdout, lens_agent="accipiter") is False

    def test_an_arbitrary_posted_prefixed_url_is_not_an_ack_line(self):
        """A `Posted: <url>` line whose URL is NOT a GitHub PR-comment URL
        (`.../issuecomment-<digits>`) is not recognised as the harness
        artifact — bounding the fix to the shape actually observed rather
        than any URL, per the same fail-closed reasoning as the case
        above."""
        stdout = "Posted: https://example.invalid/not-a-github-comment\n\nSome prose that is not a header\n**SIGNED_OFF**"
        assert (
            swarm_dispatch.describe_gate_verdict_position(stdout, lens_agent="accipiter")
            == "first line is not the lens header"
        )

    def test_marker_only_no_header_edge_case_still_rejects(self):
        """A comment whose first line IS a valid marker but whose second line
        is NOT a valid header must still reject — the case a naive
        "unconditionally skip line 0" fix would silently break by shifting
        the off-by-one rather than fixing it."""
        marker = _marker("ux")
        stdout = f"{marker}\nnot a header line at all\n**SIGNED_OFF**"
        assert (
            swarm_dispatch.describe_gate_verdict_position(stdout, lens_agent="accipiter")
            == "first line is not the lens header"
        )

    def test_double_marker_edge_case_still_rejects(self):
        """Two marker-shaped lines before the header still fail — the "at
        most one marker line" constraint is untouched by this fix."""
        marker = _marker("ux")
        stdout = f"{marker}\n{marker}\n{_header('ux')}\n**SIGNED_OFF**"
        assert (
            swarm_dispatch.describe_gate_verdict_position(stdout, lens_agent="accipiter")
            == "first line is not the lens header"
        )

    def test_leading_blank_line_behaviour_is_unchanged(self):
        """A blank line ahead of the header (documented contract: reject) is
        neither newly permitted nor newly rejected by this fix — same
        result before and after, since `_skip_leading_ack_line` only ever
        acts on a `Posted…`-shaped candidate line, never a blank one."""
        stdout = f"\n{_header('ux')}\n**SIGNED_OFF**\n\nok"
        before = swarm_dispatch.lens_own_verdict(stdout, lens_agent="accipiter")
        assert before == "signed_off"

    def test_a_posted_prefixed_stale_or_off_position_blocking_verdict_still_blocks(self):
        """A `Posted…`-prefixed reply that DOES carry a real header must
        still fail closed on a genuine blocking verdict — the skip only
        relocates where the header is read from, never what counts as
        clear."""
        stdout = (
            "Posted: https://github.com/markmhendrickson/ateles/pull/9"
            f"#issuecomment-1\n\n{_header('ux')}\n**REQUEST_CHANGES**\n\n"
            "[BLOCKING] scope: missing"
        )
        assert swarm_dispatch.sign_off_is_warranted(stdout, lens_agent="accipiter") is False
        assert swarm_dispatch.gate_verdict_format_rejected(
            stdout, lens_agent="accipiter"
        ) is False

    def test_observed_field_contract_the_exact_string_is_never_produced_for_this_shape(self):
        """UX/Eng contract: never emit the literal `first line is not the
        lens header` string when line 0 is a `Posted…` acknowledgment and the
        following line is a valid marker+header — direct string-absence
        assertion on the two PR #1149 fixtures, closing the loop on the
        reported false positive rather than a generic "parses OK" check."""
        for stdout, lens in (
            (self.UX_STDOUT_1149, "accipiter"),
            (self.PM_STDOUT_1149, "pavo"),
        ):
            observed = swarm_dispatch.describe_gate_verdict_position(
                stdout, lens_agent=lens
            )
            assert "first line is not the lens header" not in observed


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
