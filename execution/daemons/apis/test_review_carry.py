"""Narrow re-review rounds and the combined pm/qa/ux pass.

Operator rulings 2026-09-29 (Phase A3 plan ent_c2fa995ec1058a3e7b8b5b20):
`rereview_only_blockers_and_touched_areas` and `combined_pm_qa_ux_pass`.

Every test that decides a lens set drives the REAL readers (`lens_records`,
`lens_own_verdict`, `sign_off_is_warranted`) on comment bodies shaped like a
lens's own reply, so a change to what the gate reads breaks these.

Run: pytest execution/daemons/apis/test_review_carry.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parents[1]), str(_HERE.parents[2])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import review_carry  # noqa: E402
import review_delta  # noqa: E402
import swarm_dispatch as sd  # noqa: E402
from review_panel import lens_by_name  # noqa: E402
from skill_runner import SkillResult  # noqa: E402

OLD = "b" * 40
NEW = "a" * 40
FIVE = ["pm", "arch", "ux", "qa", "security"]
HEADER_NAME = {
    "pm": "Pavo", "arch": "Waxwing", "ux": "Accipiter", "qa": "Phoenicurus",
    "security": "Falco",
}
SECURITY_FILE = ".claude/hooks/some_guard.py"  # a security path: every lens
TEST_FILE = "execution/scripts/test_guard.py"  # allowlisted: qa only
DOC_FILE = "docs/guide/how_to.md"  # allowlisted prose docs: ux only
UNMAPPED_FILE = "lib/some_util.py"  # code: every lens


def _body(lens: str, head: str, verdict: str = "SIGNED_OFF", finding: str = "") -> str:
    text = (
        f"{sd.compose_lens_review_marker(lens, head)}\n"
        f"**\U0001f916 {HEADER_NAME[lens]} — Ateles swarm, {lens} lens panelist**\n"
        f"**{verdict}**\n"
    )
    return text + (f"\n{finding}\n" if finding else "")


def _comment(i: int, lens: str, head: str, verdict: str = "SIGNED_OFF", finding: str = "") -> dict:
    return {
        "id": i,
        "created_at": f"2026-09-29T10:{i:02d}:00Z",
        "html_url": f"https://github.com/o/r/pull/1#issuecomment-{i}",
        "body": _body(lens, head, verdict, finding),
    }


def _round(head: str, *, blocked: tuple[str, ...] = (), start: int = 0) -> list[dict]:
    out = []
    for n, lens in enumerate(FIVE):
        if lens in blocked:
            out.append(_comment(start + n, lens, head, "BLOCKED", "[BLOCKING] correctness: wrong"))
        else:
            out.append(_comment(start + n, lens, head))
    return out


def _delta(*files: str) -> review_delta.Delta:
    return review_delta.Delta(lines=1, files=tuple(files))


def _select(comments, delta, *, forced=()):
    records = sd.lens_records(comments)
    return review_carry.select_rerun(FIVE, records, NEW, {OLD: delta}, forced=forced)


# ── selection ───────────────────────────────────────────────────────────────


class TestSelection:
    def test_a_security_blocker_fixed_in_a_test_reruns_security_and_qa_only(self):
        sel = _select(_round(OLD, blocked=("security",)), _delta(TEST_FILE))
        assert sel.rerun == {"security", "qa"}
        assert set(sel.carried) == {"pm", "arch", "ux"}
        assert all(c.head == OLD for c in sel.carried.values())

    def test_a_docs_change_also_reruns_ux(self):
        sel = _select(_round(OLD, blocked=("security",)), _delta(TEST_FILE, DOC_FILE))
        assert sel.rerun == {"security", "qa", "ux"}

    def test_a_code_fix_to_a_security_blocker_reruns_every_lens(self):
        sel = _select(_round(OLD, blocked=("security",)), _delta(SECURITY_FILE))
        assert sel.rerun == set(FIVE)
        assert sel.carried == {}

    @pytest.mark.parametrize(
        "path",
        [
            "src/cli/index.ts",  # a ux pattern
            "src/cli/permissions.ts",
            "src/services/docs/render.ts",  # a ux pattern
            ".claude/skills/x/SKILL.md",  # a ux pattern, and an agent prompt
            "execution/scripts/release_notes.py",  # a pm pattern, and code
            "src/server/routes.ts",  # an arch pattern
        ],
    )
    def test_a_code_fix_under_a_pm_or_ux_pattern_still_reruns_security_and_arch(self, path):
        sel = _select(_round(OLD, blocked=("pm",)), _delta(path))
        assert {"security", "arch"} <= sel.rerun, path
        assert "security" not in sel.carried and "arch" not in sel.carried

    def test_an_unmapped_file_reruns_every_lens(self):
        sel = _select(_round(OLD, blocked=("security",)), _delta(UNMAPPED_FILE))
        assert sel.rerun == set(FIVE)
        assert sel.carried == {}

    def test_a_lens_that_blocked_is_never_carried(self):
        # arch blocked; the fix touched only docs, which is not arch's area.
        sel = _select(_round(OLD, blocked=("arch",)), _delta(DOC_FILE))
        assert "arch" in sel.rerun and "arch" not in sel.carried
        assert sel.rerun == {"arch", "ux"}

    def test_an_empty_delta_with_a_moved_head_carries_nothing(self):
        """An empty delta means "nothing measured", not "nothing changed" (security
        review of ateles#1368): the head moved, so it is unknown."""
        records = sd.lens_records(_round(OLD))
        sel = review_carry.select_rerun(FIVE, records, NEW, {OLD: review_delta.Delta(0, ())})
        assert sel.rerun == set(FIVE) and sel.carried == {}

    def test_a_lens_whose_latest_verdict_blocked_is_not_carried_from_its_earlier_clear(self):
        mid = "c" * 40
        comments = _round(mid) + [
            _comment(20, "qa", OLD, "BLOCKED", "[BLOCKING] tests: none")
        ]
        records = sd.lens_records(comments)
        # qa cleared at `mid`, then blocked at OLD (its latest verdict).
        assert review_carry.carry_candidate(records["qa"], NEW) is None

    def test_a_block_later_overwritten_by_a_clear_at_the_same_head_is_not_carried(self):
        comments = [
            _comment(1, "qa", OLD, "BLOCKED", "[BLOCKING] tests: none"),
            _comment(2, "qa", OLD),
        ]
        assert review_carry.carry_candidate(sd.lens_records(comments)["qa"], NEW) is None

    def test_an_unreadable_delta_carries_nothing(self):
        records = sd.lens_records(_round(OLD, blocked=("security",)))
        sel = review_carry.select_rerun(FIVE, records, NEW, {OLD: None})
        assert sel.rerun == set(FIVE)
        assert sel.carried == {}

    def test_a_lens_with_no_earlier_verdict_runs(self):
        comments = [c for c in _round(OLD) if "review:qa" not in c["body"]]
        sel = _select(comments, _delta(DOC_FILE))
        assert "qa" in sel.rerun

    def test_a_pending_gate_owner_is_forced_to_rerun(self):
        sel = _select(_round(OLD), _delta(DOC_FILE), forced={"arch"})
        assert "arch" in sel.rerun

    def test_a_superseded_earlier_verdict_still_carries(self):
        retired = [
            {**c, "body": sd.compose_superseded_verdict(c["body"], NEW)}
            for c in _round(OLD)
        ]
        sel = _select(retired, _delta(DOC_FILE))
        assert set(sel.carried) == {"pm", "arch", "qa", "security"}
        assert sel.rerun == {"ux"}

    def test_a_lens_already_reviewed_at_the_current_head_is_not_a_carry(self):
        comments = _round(OLD) + [_comment(30, "pm", NEW)]
        records = sd.lens_records(comments)
        assert review_carry.carry_candidate(records["pm"], NEW) is None

    @pytest.mark.parametrize(
        "path",
        [
            # the paths the security and arch reviews measured as narrowed
            "apps/task-dashboard/server/auth.ts",
            "apps/task-dashboard/server/neotomaProxy.ts",
            "src/cli/permissions.ts",
            "inspector/src/api/store.ts",
            "src/services/docs/render.ts",
            ".github/workflows/ci.yml",
            "tests/conftest.py",
            "execution/scripts/release_notes.py",
            "CLAUDE.md",
            "docs/agents/pavo.md",
            "src/mcp/server.ts",
            "src/cli/index.ts",
            "src/server/routes.ts",
            "execution/daemons/apis/grant_checker.py",
            "lib/daemon_runtime/aauth_signer.py",
            "execution/hooks/x.py",
            "services/webhook_handler.py",
            # dependency manifests and code
            "package.json",
            "requirements-dev.txt",
            "pyproject.toml",
            "lib/some_util.py",
            "README.md",
            # a test file inside a security path stays a security path
            ".claude/hooks/test_guard.py",
        ],
    )
    def test_code_and_sensitive_paths_belong_to_every_lens(self, path):
        assert review_carry.lenses_for_path(path) == review_carry.ALL_LENSES

    @pytest.mark.parametrize(
        "path,lenses",
        [
            ("docs/guide/how_to.md", {"ux"}),
            ("docs/foundation/principles.md", {"arch", "ux"}),
            ("execution/scripts/test_thing.py", {"qa"}),
            ("CHANGELOG.md", {"pm"}),
        ],
    )
    def test_only_the_explicit_allowlist_narrows(self, path, lenses):
        assert review_carry.lenses_for_path(path) == lenses


# ── the combined pm / qa / ux pass ──────────────────────────────────────────


def _combined_reply(verdicts: dict[str, str]) -> str:
    out = []
    for lens, verdict in verdicts.items():
        out.append(
            f"{review_carry.combined_delimiter(lens)}\n"
            f"**\U0001f916 {HEADER_NAME[lens]} — Ateles swarm, {lens} lens panelist**\n"
            f"**{verdict}**\n"
            + ("[BLOCKING] scope: unrequested change\n" if verdict == "BLOCKED" else "Looks right.\n")
        )
    return "\n".join(out)


class TestCombinedPass:
    def test_three_verdicts_the_gate_reads(self):
        reply = _combined_reply({"pm": "SIGNED_OFF", "qa": "SIGNED_OFF", "ux": "SIGNED_OFF"})
        blocks = review_carry.split_combined_reply(reply, ["pm", "qa", "ux"])
        assert set(blocks) == {"pm", "qa", "ux"}
        comments = []
        for i, lens in enumerate(["pm", "qa", "ux"]):
            body = review_carry.compose_combined_comment(
                blocks[lens], sd.compose_lens_review_marker(lens, NEW)
            )
            agent = lens_by_name(lens).agent
            # the same reader the approval gate uses
            assert sd.lens_own_verdict(body, lens_agent=agent) == "signed_off"
            assert sd.sign_off_is_warranted(body, lens_agent=agent)
            comments.append({"id": i, "created_at": f"2026-09-29T10:0{i}:00Z", "body": body})
        records = sd.lens_records(comments)
        assert {lens: records[lens][-1].cleared for lens in ("pm", "qa", "ux")} == {
            "pm": True, "qa": True, "ux": True,
        }

    def test_each_lens_keeps_its_own_verdict(self):
        reply = _combined_reply({"pm": "SIGNED_OFF", "qa": "BLOCKED", "ux": "SIGNED_OFF"})
        blocks = review_carry.split_combined_reply(reply, ["pm", "qa", "ux"])
        assert sd.sign_off_is_warranted(blocks["pm"], lens_agent="pavo")
        assert not sd.sign_off_is_warranted(blocks["qa"], lens_agent="phoenicurus")
        assert sd.lens_own_verdict(blocks["qa"], lens_agent="phoenicurus") == "blocked"
        assert sd.sign_off_is_warranted(blocks["ux"], lens_agent="accipiter")

    def test_a_missing_or_repeated_block_is_dropped_not_repaired(self):
        reply = _combined_reply({"pm": "SIGNED_OFF", "qa": "SIGNED_OFF"})
        assert set(review_carry.split_combined_reply(reply, ["pm", "qa", "ux"])) == {"pm", "qa"}
        twice = reply + "\n" + reply
        assert review_carry.split_combined_reply(twice, ["pm", "qa", "ux"]) == {}

    def test_a_block_carrying_another_lens_header_is_not_a_clear(self):
        reply = _combined_reply({"pm": "SIGNED_OFF", "qa": "SIGNED_OFF", "ux": "SIGNED_OFF"})
        reply = reply.replace(
            "Looks right.\n\n" + review_carry.combined_delimiter("qa"),
            "Looks right.\n**\U0001f916 Waxwing — Ateles swarm, arch lens panelist**\n\n"
            + review_carry.combined_delimiter("qa"),
            1,
        )
        blocks = review_carry.split_combined_reply(reply, ["pm", "qa", "ux"])
        assert sd.lens_own_verdict(blocks["pm"], lens_agent="pavo") is None
        assert not sd.sign_off_is_warranted(blocks["pm"], lens_agent="pavo")


@pytest.mark.asyncio
class TestDispatcherCombinedPass:
    async def test_one_dispatch_posts_three_own_comments_and_accepts_them(self, monkeypatch):
        calls: list[dict] = []
        posted: list[dict] = []

        async def fake_run_skill(agent, prompt, **kwargs):
            calls.append({"agent": agent, **kwargs})
            reply = _combined_reply(
                {"pm": "SIGNED_OFF", "qa": "SIGNED_OFF", "ux": "SIGNED_OFF"}
            )
            return SkillResult(
                skill=agent, ok=True, returncode=0, stdout=reply, stderr=""
            )

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                posted.append(json)

                class _R:
                    def raise_for_status(self):
                        pass

                return _R()

        monkeypatch.setattr(sd, "run_skill", fake_run_skill)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client())
        monkeypatch.setattr(sd, "usable_providers", lambda: set())
        from test_gate_sign_off_dispatch import _StubNotifier, _config, _trigger

        d = sd.SwarmDispatcher(_StubNotifier(), _config())
        panel = [lens_by_name(x) for x in ("pm", "qa", "ux", "security")]
        results = await d._run_combined_pass(
            _trigger(head_sha=NEW),
            panel,
            {},
            80,
            NEW,
            ["execution/daemons/apis/swarm_dispatch.py"],
            pending_gates=set(),
            live_gates={},
            signals=None,
        )
        assert len(calls) == 1, "one dispatch for the three lenses"
        assert calls[0]["action_class"] == "lens_review:pm"
        assert set(results) == {"pm", "qa", "ux"}
        assert len(posted) == 3
        for lens, body in zip(("pm", "qa", "ux"), (p["body"] for p in posted)):
            assert body.startswith(sd.compose_lens_review_marker(lens, NEW))
            assert sd.sign_off_is_warranted(body, lens_agent=lens_by_name(lens).agent)

    async def test_fewer_than_two_combinable_lenses_dispatches_nothing_combined(
        self, monkeypatch
    ):
        async def boom(*a, **k):
            raise AssertionError("no combined dispatch expected")

        monkeypatch.setattr(sd, "run_skill", boom)
        from test_gate_sign_off_dispatch import _StubNotifier, _config, _trigger

        d = sd.SwarmDispatcher(_StubNotifier(), _config())
        panel = [lens_by_name(x) for x in ("pm", "security", "arch")]
        assert await d._run_combined_pass(
            _trigger(), panel, {}, 80, NEW, pending_gates=set(), live_gates={}, signals=None
        ) == {}


class TestAggregationNamesCarriedLenses:
    def test_vanellus_is_told_carried_lenses_are_not_missing(self):
        from test_gate_sign_off_dispatch import _trigger

        carried = {"pm": review_carry.Carried("pm", OLD, "signed_off", "u")}
        prompt = sd.SwarmDispatcher._vanellus_prompt(
            _trigger(head_sha=NEW), 80, ["security"], [("security", "**APPROVE**")],
            reviewed_head=NEW, carried=carried,
        )
        assert "CARRIED FORWARD" in prompt and OLD in prompt
        assert "NOT RECEIVED" in prompt  # the instruction not to list them


@pytest.mark.asyncio
class TestDispatcherSelectionReadsTheThread:
    async def test_a_test_only_security_fix_selects_security_and_qa_from_real_comments(self, monkeypatch):
        comments = _round(OLD, blocked=("security",))

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None, params=None):
                class _R:
                    def __init__(s, payload):
                        s._p = payload

                    def raise_for_status(s):
                        pass

                    def json(s):
                        return s._p

                if "/compare/" in url:
                    sha = url.rsplit("...", 1)[1]
                    files = [{"filename": "a.txt", "patch": "+x\n", "changes": 1}]
                    if sha == NEW:
                        files.append(
                            {"filename": TEST_FILE, "patch": "+y\n", "changes": 1}
                        )
                    return _R({"files": files})
                page = (params or {}).get("page", 1)
                return _R(comments if page == 1 else [])

        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client())
        from test_gate_sign_off_dispatch import _StubNotifier, _config, _trigger

        d = sd.SwarmDispatcher(_StubNotifier(), _config())
        panel = [lens_by_name(x) for x in FIVE]
        sel = await d._rereview_selection(
            _trigger(head_sha=NEW, base_ref="main"), panel, NEW, forced=set()
        )
        assert sel is not None
        assert sel.rerun == {"security", "qa"}
        assert set(sel.carried) == {"pm", "arch", "ux"}

    async def test_a_failed_read_keeps_the_full_panel(self, monkeypatch):
        class _Boom:
            async def __aenter__(self):
                raise httpx.ConnectError("down")

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Boom())
        from test_gate_sign_off_dispatch import _StubNotifier, _config, _trigger

        d = sd.SwarmDispatcher(_StubNotifier(), _config())
        panel = [lens_by_name(x) for x in FIVE]
        assert await d._rereview_selection(
            _trigger(head_sha=NEW), panel, NEW, forced=set()
        ) is None


# ── the combined prompt keeps each lens's duties (pm review of ateles#1368) ──


def _dispatcher():
    from test_gate_sign_off_dispatch import _StubNotifier, _config

    return sd.SwarmDispatcher(_StubNotifier(), _config())


class TestCombinedPromptKeepsEachLensDuties:
    FILES = ["execution/daemons/apis/swarm_dispatch.py"]

    def _prompt(self, lenses=("pm", "qa", "ux"), expectations=None):
        from test_gate_sign_off_dispatch import _trigger

        panel = [lens_by_name(x) for x in lenses]
        t = _trigger(head_sha=NEW, body="Closes #80.")
        return t, _dispatcher()._combined_prompt(
            t, panel, expectations or {}, NEW, 80, self.FILES
        )

    def test_each_lens_own_agent_prompt_is_in_it(self):
        _, prompt = self._prompt()
        d = _dispatcher()
        for agent in ("pavo", "phoenicurus", "accipiter"):
            own = d._lens_agent_prompt(agent)
            assert own.strip(), agent
            assert own in prompt, agent

    def test_pm_gets_the_design_basis_check_and_every_lens_the_reading_list(self):
        t, prompt = self._prompt()
        assert sd.design_basis_block(t.body, where="the PR body") in prompt
        readings = sd.reading_block(self.FILES)
        assert readings and readings in prompt
        assert prompt.count(readings) == 1  # stated once, not three times

    def test_the_expectation_checkoff_uses_the_parent_issue(self):
        _, prompt = self._prompt(expectations={"pavo": "- [ ] scope matches"})
        assert "scope matches" in prompt
        assert "issues/80/comments" in prompt
        assert f"{sd.EXPECTATION_MARKER} (pm)" in prompt

    def test_every_lens_gets_the_gate_verdict_format_and_the_evidence_bar(self):
        _, prompt = self._prompt()
        for lens in ("pm", "qa", "ux"):
            agent = lens_by_name(lens).agent
            assert sd.gate_verdict_instruction(agent, f"{lens} lens panelist") in prompt
        assert prompt.count("EVIDENCE BAR FOR BLOCKING") == 3

    def test_the_duties_are_the_same_helper_the_solo_prompt_uses(self):
        from test_gate_sign_off_dispatch import _trigger

        t = _trigger(head_sha=NEW, body="Closes #80.")
        lens = lens_by_name("pm")
        duties = sd.SwarmDispatcher._lens_duty_blocks(t, lens, "- [ ] x", 80, False, self.FILES)
        solo = sd.SwarmDispatcher._panelist_prompt(
            t, lens, "- [ ] x", 80, has_worktree=False, changed_files=self.FILES,
            reviewed_head=NEW,
        )
        for block in duties:
            assert block.strip() in solo

    def test_the_template_has_no_parenthetical_verdict_and_no_artifact_line(self):
        _, prompt = self._prompt()
        assert "(or `**BLOCKED**`" not in prompt
        assert "**<VERDICT>**" in prompt
        assert "artifact or attribution line of your own" in prompt
        assert "diff-only in the combined pass" in prompt  # qa says so

    @pytest.mark.asyncio
    async def test_a_lens_whose_own_prompt_cannot_load_is_not_reviewed_in_the_pass(self, monkeypatch):
        async def boom(*a, **k):
            raise AssertionError("no dispatch expected")

        monkeypatch.setattr(sd, "run_skill", boom)
        monkeypatch.setattr(sd.SwarmDispatcher, "_lens_agent_prompt", staticmethod(lambda a: ""))
        from test_gate_sign_off_dispatch import _trigger

        panel = [lens_by_name(x) for x in ("pm", "qa", "ux")]
        assert await _dispatcher()._run_combined_pass(
            _trigger(), panel, {}, 80, NEW, self.FILES,
            pending_gates=set(), live_gates={}, signals=None,
        ) == {}


# ── partial failures of the combined pass ────────────────────────────────────


class _PostClient:
    """Records comment POSTs; the Nth POST (1-based) fails when `fail_on` is set."""

    def __init__(self, fail_on: int | None = None):
        self.fail_on = fail_on
        self.bodies: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        self.bodies.append(json["body"])

        class _R:
            def raise_for_status(s):
                pass

        if self.fail_on == len(self.bodies):
            raise httpx.ConnectError("post failed")
        return _R()


def _reply_for(verdicts: dict[str, str]) -> SkillResult:
    return SkillResult(
        skill="pavo", ok=True, returncode=0, stdout=_combined_reply(verdicts), stderr=""
    )


@pytest.mark.asyncio
class TestCombinedPassPartialFailures:
    async def _run(self, monkeypatch, result, client):
        async def fake_run_skill(agent, prompt, **kwargs):
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(sd, "run_skill", fake_run_skill)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: client)
        monkeypatch.setattr(sd, "usable_providers", lambda: set())
        from test_gate_sign_off_dispatch import _trigger

        panel = [lens_by_name(x) for x in ("pm", "qa", "ux")]
        return await _dispatcher()._run_combined_pass(
            _trigger(head_sha=NEW), panel, {}, 80, NEW, ["execution/x.py"],
            pending_gates=set(), live_gates={}, signals=None,
        )

    async def test_one_unreadable_block_runs_that_lens_alone_and_keeps_the_others(self, monkeypatch):
        reply = _combined_reply({"pm": "SIGNED_OFF", "qa": "SIGNED_OFF", "ux": "SIGNED_OFF"})
        # qa's block loses its verdict line: unreadable.
        reply = reply.replace(
            f"**\U0001f916 Phoenicurus — Ateles swarm, qa lens panelist**\n**SIGNED_OFF**\n",
            f"**\U0001f916 Phoenicurus — Ateles swarm, qa lens panelist**\nno verdict here\n",
        )
        client = _PostClient()
        out = await self._run(
            monkeypatch, SkillResult("pavo", True, 0, reply, ""), client
        )
        assert set(out) == {"pm", "ux"}
        assert len(client.bodies) == 2  # qa was not posted; it runs alone

    async def test_a_failed_second_post_leaves_that_lens_and_later_ones_to_run_alone(self, monkeypatch):
        client = _PostClient(fail_on=2)
        out = await self._run(
            monkeypatch,
            _reply_for({"pm": "SIGNED_OFF", "qa": "SIGNED_OFF", "ux": "SIGNED_OFF"}),
            client,
        )
        assert set(out) == {"pm"}  # pm posted; qa and ux run alone

    async def test_a_failed_combined_run_falls_back_to_per_lens_runs(self, monkeypatch):
        client = _PostClient()
        failed = SkillResult("pavo", False, 1, "", "boom")
        assert await self._run(monkeypatch, failed, client) == {}
        assert client.bodies == []

    async def test_a_combined_run_that_raises_falls_back_to_per_lens_runs(self, monkeypatch):
        client = _PostClient()
        assert await self._run(monkeypatch, RuntimeError("provider down"), client) == {}
        assert client.bodies == []

    async def test_each_posted_comment_carries_provenance_and_still_reads_clear(self, monkeypatch):
        client = _PostClient()
        out = await self._run(
            monkeypatch,
            _reply_for({"pm": "SIGNED_OFF", "qa": "SIGNED_OFF", "ux": "SIGNED_OFF"}),
            client,
        )
        assert set(out) == {"pm", "qa", "ux"}
        for lens, body in zip(("pm", "qa", "ux"), client.bodies):
            assert "Combined pm/qa/ux pass" in body
            assert sd.sign_off_is_warranted(body, lens_agent=lens_by_name(lens).agent)
        assert "diff-only" in client.bodies[1]
