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
SECURITY_FILE = ".claude/hooks/some_guard.py"  # security area only
DOC_FILE = "docs/guide/how_to.md"  # ux area
UNMAPPED_FILE = "lib/some_util.py"  # in no area


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
    def test_one_line_security_fix_reruns_security_only(self):
        sel = _select(_round(OLD, blocked=("security",)), _delta(SECURITY_FILE))
        assert sel.rerun == {"security"}
        assert set(sel.carried) == {"pm", "arch", "ux", "qa"}
        assert all(c.head == OLD for c in sel.carried.values())

    def test_a_docs_change_also_reruns_ux(self):
        sel = _select(
            _round(OLD, blocked=("security",)), _delta(SECURITY_FILE, DOC_FILE)
        )
        assert sel.rerun == {"security", "ux"}

    def test_an_unmapped_file_reruns_every_lens(self):
        sel = _select(_round(OLD, blocked=("security",)), _delta(UNMAPPED_FILE))
        assert sel.rerun == set(FIVE)
        assert sel.carried == {}

    def test_a_lens_that_blocked_is_never_carried(self):
        # arch blocked; the fix touched only docs, which is not arch's area.
        sel = _select(_round(OLD, blocked=("arch",)), _delta(DOC_FILE))
        assert "arch" in sel.rerun and "arch" not in sel.carried
        assert sel.rerun == {"arch", "ux"}

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
            ".claude/hooks/x.py",
            "execution/daemons/apis/auth/token.py",
            "lib/aauth_keys.py",
            ".env.example",
        ],
    )
    def test_security_paths_always_touch_security(self, path):
        assert "security" in review_carry.lenses_for_path(path)

    def test_the_map_derives_from_the_panel_registry_not_a_retyped_list(self):
        for lens in ("arch", "ux", "security"):
            patterns = lens_by_name(lens).diff_patterns
            assert patterns, lens
        assert review_carry.lenses_for_path("migrations/001.sql") >= {"arch"}
        assert review_carry.lenses_for_path("execution/x/test_thing.py") == {"qa"}


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
    async def test_a_security_only_fix_selects_security_from_real_comments(self, monkeypatch):
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
                            {"filename": SECURITY_FILE, "patch": "+y\n", "changes": 1}
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
        assert sel.rerun == {"security"}
        assert set(sel.carried) == {"pm", "arch", "ux", "qa"}

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
