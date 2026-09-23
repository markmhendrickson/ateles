"""Sign-off residuals: the round answering PR #1181's reviews at e874537f.

One test class per item, each written against the defect's exact shape and
run RED against e874537f before the fix:

1. ``TestVerdictPositionFailsClosed`` — second security run, BLOCKING.
   `lens_own_verdict` took the FIRST attribution header, or the first
   standalone verdict line, so an output quoting an earlier clear verdict
   before the lens's own `**COMMENT**` cleared the gate.
2. ``TestEverySeatedLensIsDeniedCorrect`` — dispatcher security run,
   BLOCKING `incomplete_class_sweep`. The `correct` deny applied only to a run
   that owned a pending gate, while every seated lens got the Neotoma
   wildcard over the shared bearer.
3. ``TestFreshnessComparesInstants`` — both security runs. The attribution
   freshness check compared ISO timestamp STRINGS.
4. ``TestHistoryDoesNotEndOnAFailedSignOff`` — `owner_history` kept a
   `signed_off` entry for a sign-off that failed.
5. ``TestConcurrentSignOffsAreSerialised`` — the whole-map race: a concurrent
   sign-off brought back a gate another had just rolled back.
6. ``TestNextRunReprovesSignedOff`` — a `signed_off` left by a failed or
   cancelled sign-off was trusted by the next run; the operator text said
   the hold lasted "until a verified sign-off lands".

Run: pytest execution/daemons/apis/test_gate_sign_off_residuals.py -v
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import gate_waive
import harness_router
import skill_runner
import swarm_dispatch
from gate_waive import GATE_STATUS_FIELD, SIGN_OFF_SIGNING_FAILED, IssueGateStore
from skill_runner import SkillResult
from swarm_dispatch import SwarmDispatcher

import test_swarm_dispatch as tsd
from test_gate_sign_off_fail_closed import HEAD, LENS_TP, _Record, _wire
from test_skill_runner import _make_def


def _run(coro):
    return asyncio.run(coro)


def _warranted(stdout: str, lens: str) -> bool:
    """`sign_off_is_warranted` for *lens*.

    Falls back to the one-argument form so the RED run at e874537f, where
    the predicate took no lens identity, reaches the assertion that names the
    defect instead of stopping at a TypeError.
    """
    try:
        return swarm_dispatch.sign_off_is_warranted(stdout, lens_agent=lens)
    except TypeError:
        return swarm_dispatch.sign_off_is_warranted(stdout)


FENCE = "```"


# ── 1. Verdict position fails closed (second security run, BLOCKING) ────────


class TestVerdictPositionFailsClosed:
    def test_fenced_earlier_approve_then_own_comment(self):
        """Security run input 1: no header; a fenced block from an earlier
        round holds a standalone `**APPROVE**`, then the lens's own
        `**COMMENT**` ("not signing the gate this round")."""
        stdout = (
            "Quoting the earlier round for context:\n\n"
            f"{FENCE}\n**APPROVE**\nall checks pass\n{FENCE}\n\n"
            "**COMMENT**\n\nnot signing the gate this round"
        )
        assert _warranted(stdout, "pavo") is False

    def test_quoted_earlier_header_and_signed_off_then_own_header_and_comment(self):
        """Security run input 2: a quoted earlier comment with its own
        attribution header and `**SIGNED_OFF**`, then the lens's own header
        and `**COMMENT**`."""
        stdout = (
            "Previous round's comment, for reference:\n\n"
            f"{FENCE}\n"
            "**🤖 Pavo — Ateles swarm, pm gate owner**\n"
            "**SIGNED_OFF**\n"
            "PM gate signed off.\n"
            f"{FENCE}\n\n"
            "**🤖 Pavo — Ateles swarm, pm gate owner**\n"
            "**COMMENT**\n\n"
            "Scope changed since then; not signing this round."
        )
        assert _warranted(stdout, "pavo") is False

    def test_blockquoted_earlier_comment_then_own_comment(self):
        stdout = (
            "> **🤖 Pavo — Ateles swarm, pm gate owner**\n"
            "> **SIGNED_OFF**\n\n"
            "**🤖 Pavo — Ateles swarm, pm gate owner**\n"
            "**COMMENT**\n\nobservations only"
        )
        assert _warranted(stdout, "pavo") is False

    def test_two_headers_do_not_clear_even_when_both_verdicts_are_clear(self):
        """More than one header means the lens's own cannot be singled out."""
        stdout = (
            f"{FENCE}\n**🤖 Pavo — Ateles swarm, pm gate owner**\n**APPROVE**\n{FENCE}\n\n"
            "**🤖 Pavo — Ateles swarm, pm gate owner**\n**SIGNED_OFF**\n"
        )
        assert _warranted(stdout, "pavo") is False

    def test_a_header_naming_another_lens_does_not_clear(self):
        stdout = "**🤖 Waxwing — Ateles swarm, arch reviewer**\n**SIGNED_OFF**\n"
        assert _warranted(stdout, "pavo") is False

    def test_single_own_header_and_clear_verdict_clears(self):
        stdout = (
            "**🤖 Pavo — Ateles swarm, pm gate owner**\n**APPROVE**\n\n"
            "- [x] Acceptance criteria met"
        )
        assert _warranted(stdout, "pavo") is True

    def test_no_header_and_a_single_clear_verdict_clears(self):
        assert _warranted("**SIGNED_OFF**\nno concerns", "pavo") is True


# ── 2. Every seated lens is denied `correct` (dispatcher security run) ──────


class TestEverySeatedLensIsDeniedCorrect:
    def setup_method(self) -> None:
        skill_runner._agent_def_cache.clear()

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_advisory_seat_carries_the_cli_deny(self, MockLoader, _hev, monkeypatch):
        """Falco owns no gate. Seated, it still gets the Neotoma wildcard
        over the shared bearer, so it must carry the deny."""
        instance = MagicMock()
        instance.load.return_value = _make_def(
            prompt_markdown="Role: Falco.", tool_allowlist="*",
            aauth_sub="falco@ateles-swarm", name="falco",
        )
        MockLoader.return_value = instance
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "shared-daemon-token")
        captured: list = []

        async def fake_exec(*cmd, **kwargs):
            captured.extend(cmd)
            proc = MagicMock()
            proc.returncode = 0

            async def _communicate(input=None):
                return b"**COMMENT**", b""

            proc.communicate = _communicate
            return proc

        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill md"),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            try:
                result = _run(skill_runner.run_skill(
                    "falco", "review prompt", role="falco", provider="claude",
                    task_entity_id="ent_abc", seated_reviewer=True,
                ))
            except TypeError as exc:  # RED at e874537f: no such parameter
                pytest.fail(f"run_skill has no seated_reviewer control: {exc}")

        assert result.ok
        assert "--disallowed-tools" in captured
        deny = captured[captured.index("--disallowed-tools") + 1].split(",")
        assert "mcp__mcpsrv_neotoma__correct" in deny

    def test_advisory_seat_routes_to_claude_only(self, monkeypatch, tmp_path):
        """Same fail-closed routing as a gate owner: no other adapter here can
        deny a single MCP tool, so a preference for codex is not honoured."""
        harness_router.reset_state()
        monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "codex,cursor,claude")
        monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "none"))
        monkeypatch.delenv("APIS_HARNESS_HEADROOM", raising=False)
        monkeypatch.setattr(
            skill_runner, "_provider_binaries",
            lambda: {"codex": "d", "cursor": "c", "claude": "e"},
        )
        attempted: list[tuple[str, bool]] = []

        async def fake_once(skill, prompt, *, provider, owns_pending_gate=False, **kw):
            attempted.append((provider, owns_pending_gate))
            return SkillResult(skill, True, 0, "**COMMENT**", "", provider=provider)

        monkeypatch.setattr(skill_runner, "_run_skill_once", fake_once)
        try:
            result = _run(skill_runner.run_skill(
                "falco", "review", preferred_provider="codex", seated_reviewer=True,
            ))
        except TypeError as exc:  # RED at e874537f: no such parameter
            pytest.fail(f"run_skill has no seated_reviewer control: {exc}")

        assert result.ok
        assert attempted == [("claude", True)]

    def _panel_seen(self, monkeypatch) -> dict:
        seen: dict = {}

        class _Cleared:
            found = True
            gate_status_unreadable = False
            gate_status = {"pm": "signed_off", "ux": "signed_off", "arch": "signed_off"}

        async def load(self, repo, issue_number):
            return _Cleared()

        async def all_proven(self, state, owners):
            return set()

        monkeypatch.setattr(swarm_dispatch.IssueGateStore, "load", load)
        monkeypatch.setattr(
            swarm_dispatch.IssueGateStore, "unverified_signed_off_gates",
            all_proven, raising=False,
        )
        d = tsd._pr_dispatcher_with_stubs(
            monkeypatch, vanellus_stdout="**APPROVE**\nlgtm", calls=[]
        )

        async def fake_run_skill(skill, prompt, **kwargs):
            seen[skill] = kwargs
            if skill == "lanius":
                return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
            return SkillResult(skill, True, 0, "**COMMENT**\nlgtm", "")

        monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
        _run(d._handle_pr(tsd._trigger(body="Closes #80.")))
        return seen

    def test_every_panel_seat_is_a_seated_reviewer(self, monkeypatch):
        """Every gate reads cleared, so no lens owns a pending gate: pavo is
        a gate owner re-seated after its gate cleared, phoenicurus is
        advisory for the pre-impl gates. Both must carry the deny."""
        seen = self._panel_seen(monkeypatch)
        seated = {s: kw for s, kw in seen.items() if s not in ("lanius", "vanellus")}
        assert {"pavo", "phoenicurus"} <= set(seated)
        assert seated["pavo"].get("owns_pending_gate") is False
        for skill, kwargs in seated.items():
            assert kwargs.get("seated_reviewer") is True, skill

    def test_every_issue_spec_section_is_a_seated_reviewer(self, monkeypatch):
        seen: dict = {}

        async def fake_run_skill(skill, prompt, **kwargs):
            if skill != "lanius" and "DO NOT MERGE" not in prompt:
                seen[skill] = kwargs
            return SkillResult(skill, True, 0, "text", "")

        tsd._install_pipeline_stubs(
            monkeypatch, fake_run_skill, select_agents=lambda *a, **kw: []
        )
        cfg = tsd.DispatchConfig(neotoma_token="", github_token="", auto_build=False)
        _run(SwarmDispatcher(tsd._StubNotifier(), cfg)._handle_issue_opened(
            tsd._issue_trigger()
        ))

        assert {"pavo", "cicada", "phoenicurus"} <= set(seen)
        for skill, kwargs in seen.items():
            assert kwargs.get("seated_reviewer") is True, skill

    def test_missing_lens_rerun_of_an_advisory_lens_is_a_seated_reviewer(self, monkeypatch):
        seen: dict = {}

        async def fake_run_skill(skill, prompt, **kwargs):
            seen[skill] = kwargs
            return SkillResult(skill, True, 0, "**COMMENT**\nlgtm", "")

        async def noop(self, *a, **k):
            return None

        async def no_files(self, trigger):
            return []

        monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
        monkeypatch.setattr(SwarmDispatcher, "_changed_files", no_files)
        monkeypatch.setattr(SwarmDispatcher, "_persist_panel_reviews", noop)
        monkeypatch.setattr(SwarmDispatcher, "_post_missing_panel_comments", noop)
        pr = {
            "number": 87, "title": "PR", "body": "Closes #80.",
            "user": {"login": "someone"},
            "html_url": "https://github.com/owner/repo/pull/87",
            "head": {"ref": "feature", "sha": HEAD}, "base": {"ref": "main"},
        }
        d = SwarmDispatcher(tsd._StubNotifier(), tsd._config())
        _run(d._redispatch_missing_lens("owner/repo", pr, "security"))

        assert seen["falco"].get("seated_reviewer") is True

    def test_fix_guidance_of_an_advisory_lens_is_a_seated_reviewer(self, monkeypatch):
        seen: dict = {}

        async def fake_run_skill(skill, prompt, **kwargs):
            seen.setdefault(skill, kwargs)
            return SkillResult(skill, True, 0, "guidance", "")

        async def zero(self, trigger):
            return 0

        async def noop(self, *a, **k):
            return None

        async def head(self, trigger):
            return HEAD

        monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
        monkeypatch.setattr(SwarmDispatcher, "_fix_round_count", zero)
        monkeypatch.setattr(SwarmDispatcher, "_record_fix_round", noop)
        monkeypatch.setattr(SwarmDispatcher, "_pr_head_sha", head)
        d = SwarmDispatcher(tsd._StubNotifier(), tsd._config())
        reviews = [("security", "[BLOCKING] injection: the query is unparameterised\ndetail")]
        _run(d._route_blocking_findings(
            tsd._trigger(), parent=80, reviews=reviews, verdict="request_changes"
        ))

        assert seen["falco"].get("seated_reviewer") is True


# ── 3. Freshness compares instants, not strings ─────────────────────────────


def _obs(created_at: str) -> dict:
    return {
        "id": "obs-1",
        "created_at": created_at,
        "provenance": {
            "agent_sub": "waxwing@ateles-swarm",
            "agent_thumbprint": LENS_TP,
            "attribution_tier": "software",
        },
    }


def _fresh(created_at: str, not_before: str) -> bool:
    return gate_waive._observation_is_attributed(
        _obs(created_at),
        lens_sub="waxwing@ateles-swarm",
        lens_thumbprint=LENS_TP,
        not_before=not_before,
    )


class TestFreshnessComparesInstants:
    def test_z_second_stamp_is_older_than_a_fractional_floor_in_the_same_second(self):
        """`...:05Z` sorts after `...:05.5+00:00` as a string, although it is
        half a second EARLIER."""
        assert _fresh("2026-09-23T12:00:05Z", "2026-09-23T12:00:05.500000+00:00") is False

    def test_offset_stamp_is_compared_as_the_same_instant(self):
        """14:00:05+02:00 is 12:00:05Z: earlier than the floor."""
        assert _fresh("2026-09-23T14:00:05+02:00", "2026-09-23T12:00:05.500000+00:00") is False

    def test_a_later_instant_is_fresh_across_shapes(self):
        assert _fresh("2026-09-23T12:00:06Z", "2026-09-23T12:00:05.900000+00:00") is True
        assert _fresh("2026-09-23T14:00:06+02:00", "2026-09-23T12:00:05.900000+00:00") is True

    def test_an_unparseable_stamp_is_not_attributed(self):
        assert _fresh("yesterday", "2026-09-23T12:00:05+00:00") is False


# ── 4. owner_history does not end on a failed sign-off ──────────────────────


class TestHistoryDoesNotEndOnAFailedSignOff:
    def test_plain_gate_write_failure_is_recorded_after_the_signed_off_entry(
        self, monkeypatch
    ):
        """The security run's case: the gate write 503s without landing, the
        gate reads `pending`, no restore runs — and the history used to end on
        `{action: signed_off, actor: pavo}`."""
        rec = _Record({"pm": "pending"}, fail_fields=frozenset({GATE_STATUS_FIELD}))
        store = _wire(monkeypatch, rec)

        outcome = _run(store.sign_off("o/r", 795, "pm", "pavo", HEAD))

        assert not outcome.ok
        assert rec.gate_status["pm"] == "pending"
        actions = [e.get("action") for e in rec.owner_history]
        assert actions[-1] == "sign_off_failed", actions
        assert rec.owner_history[-1]["reason"] == SIGN_OFF_SIGNING_FAILED
        assert rec.owner_history[-1]["gate_after"] == "pending"
        assert "signed_off" in actions  # the entry written before the gate stays

    def test_rolled_back_sign_off_is_recorded_as_a_failure(self, monkeypatch):
        rec = _Record({"pm": "pending"}, attributed_sub="apis@ateles-swarm")
        store = _wire(monkeypatch, rec)

        outcome = _run(store.sign_off("o/r", 795, "pm", "pavo", HEAD))

        assert not outcome.ok
        assert rec.gate_status["pm"] == "pending"
        assert rec.owner_history[-1]["action"] == "sign_off_failed"
        assert rec.owner_history[-1]["reason"] == gate_waive.SIGN_OFF_ATTRIBUTION_FAILED

    def test_the_safety_field_is_still_written_after_the_history_entry(self, monkeypatch):
        """The e874537f ordering is kept: `signed_off` history first, the gate
        after it, the failure note last."""
        rec = _Record({"pm": "pending"}, fail_fields=frozenset({GATE_STATUS_FIELD}))
        store = _wire(monkeypatch, rec)

        _run(store.sign_off("o/r", 795, "pm", "pavo", HEAD))

        order = [f for f, _, _ in rec.writes]
        assert order == ["owner_history", GATE_STATUS_FIELD, "owner_history"], order

    def test_a_successful_sign_off_writes_no_failure_note(self, monkeypatch):
        rec = _Record({"pm": "pending"})
        store = _wire(monkeypatch, rec)

        outcome = _run(store.sign_off("o/r", 795, "pm", "pavo", HEAD))

        assert outcome.ok and outcome.verified
        assert [e.get("action") for e in rec.owner_history] == ["signed_off"]


# ── 5. Concurrent sign-offs on one issue are serialised ─────────────────────


class TestConcurrentSignOffsAreSerialised:
    def test_a_concurrent_sign_off_cannot_bring_back_a_rolled_back_gate(self, monkeypatch):
        """The security run's interleaving, forced:
        1. B (ux, accipiter) writes `ux: signed_off`; the observation lands at
           guest tier, so attribution fails.
        2. A (pm, pavo) loads in that window.
        3. B restores `ux: pending`.
        4. A writes `{pm: signed_off, ux: signed_off}` from its stale map.
        Without serialisation the record ends `ux: signed_off`."""
        rec = _Record(
            {"pm": "pending", "ux": "pending"},
            tier_by_sub={"accipiter@ateles-swarm": "guest"},
        )
        store = _wire(monkeypatch, rec)

        async def paced(method, url, body=None, agent_name="", timeout=20, *, sub=None):
            result = rec.apply(body, sub)
            if sub == "accipiter@ateles-swarm" and body["field"] == GATE_STATUS_FIELD:
                await asyncio.sleep(0.05)  # B's window: A loads here
            if sub == "pavo@ateles-swarm" and body["field"] == "owner_history":
                await asyncio.sleep(0.15)  # A writes its gate after B restored
            return result

        monkeypatch.setattr("gate_waive._ns.signed_request", paced)

        async def scenario():
            async def a():
                await asyncio.sleep(0.01)
                return await store.sign_off("o/r", 795, "pm", "pavo", HEAD)

            return await asyncio.gather(
                store.sign_off("o/r", 795, "ux", "accipiter", HEAD), a()
            )

        b_outcome, a_outcome = _run(scenario())

        assert not b_outcome.ok
        assert a_outcome.ok and a_outcome.verified
        assert rec.gate_status["pm"] == "signed_off"
        assert rec.gate_status["ux"] == "pending", (
            "a rolled-back gate came back through another lens's stale map"
        )


# ── 6. The next run re-proves a `signed_off` it did not verify ──────────────

OWNERS = {"pm": "pavo", "ux": "accipiter", "arch": "waxwing"}


def _unverified(store: IssueGateStore, rec: _Record, owners=OWNERS) -> set[str]:
    async def go():
        method = getattr(store, "unverified_signed_off_gates", None)
        assert method is not None, "no re-proof of a signed_off gate exists"
        return await method(await rec.load("o/r", 795), owners)

    return _run(go())


def _land(rec: _Record, gate_status: dict, sub: str, tier: str = "software") -> None:
    saved = rec.tier
    rec.tier = tier
    rec.apply({"field": GATE_STATUS_FIELD, "value": gate_status}, sub)
    rec.tier = saved


class TestNextRunReprovesSignedOff:
    def _cancel_mid_write(self, monkeypatch, rec: _Record) -> IssueGateStore:
        """Start a sign-off, cancel it while the worker thread's gate write
        has landed and the thread is still inside `signed_request`."""
        store = _wire(monkeypatch, rec, sync=True)
        landed = threading.Event()
        release = threading.Event()

        def slow(method, url, body=None, agent_name="", timeout=20, *, sub=None):
            result = rec.apply(body, sub)
            if body["field"] == GATE_STATUS_FIELD:
                landed.set()
                release.wait(5)
            return result

        monkeypatch.setattr("gate_waive._ns.signed_request", slow)

        async def scenario():
            task = asyncio.create_task(store.sign_off("o/r", 795, "ux", "accipiter", HEAD))
            assert await asyncio.to_thread(landed.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()

        _run(scenario())
        return store

    def test_cancelled_mid_write_leaves_signed_off_that_the_next_run_does_not_trust(
        self, monkeypatch
    ):
        rec = _Record({"ux": "pending"}, tier_by_sub={"accipiter@ateles-swarm": "guest"})
        store = self._cancel_mid_write(monkeypatch, rec)

        assert rec.gate_status["ux"] == "signed_off"  # the write landed
        assert len(rec.gate_writes()) == 1  # no read-back, no restore ran
        assert _unverified(store, rec) == {"ux"}

    def test_cancelled_write_made_with_the_lens_key_at_a_trusted_tier_is_proven(
        self, monkeypatch
    ):
        """The write was the lens's own, after its clear verdict: re-proven
        from provenance, not held forever."""
        rec = _Record({"ux": "pending"})
        store = self._cancel_mid_write(monkeypatch, rec)

        assert rec.gate_status["ux"] == "signed_off"
        assert _unverified(store, rec) == set()

    def test_a_later_whole_map_write_does_not_unprove_an_earlier_gate(self, monkeypatch):
        rec = _Record({"pm": "pending", "arch": "pending"})
        store = _wire(monkeypatch, rec)
        assert _run(store.sign_off("o/r", 795, "pm", "pavo", HEAD)).verified
        assert _run(store.sign_off("o/r", 795, "arch", "waxwing", HEAD)).verified

        assert _unverified(store, rec) == set()

    def test_a_rolled_back_gate_brought_back_by_another_lens_is_unproven(self, monkeypatch):
        rec = _Record({"pm": "pending", "ux": "pending"})
        store = _wire(monkeypatch, rec)
        _land(rec, {"pm": "pending", "ux": "signed_off"}, "accipiter@ateles-swarm", "guest")
        _land(rec, {"pm": "pending", "ux": "pending"}, "accipiter@ateles-swarm")
        _land(rec, {"pm": "signed_off", "ux": "signed_off"}, "pavo@ateles-swarm")

        assert _unverified(store, rec) == {"ux"}

    def test_a_bearer_written_signed_off_is_unproven(self, monkeypatch):
        rec = _Record({"arch": "pending"})
        store = _wire(monkeypatch, rec)
        _land(rec, {"arch": "signed_off"}, "apis@ateles-swarm")

        assert _unverified(store, rec) == {"arch"}

    def test_unreadable_provenance_is_unproven(self, monkeypatch):
        rec = _Record({"arch": "pending"})
        store = _wire(monkeypatch, rec)
        _land(rec, {"arch": "signed_off"}, "waxwing@ateles-swarm")
        rec.observations = []  # the observations read came back empty

        assert _unverified(store, rec) == {"arch"}

    def _dispatcher_over(self, monkeypatch, rec: _Record) -> SwarmDispatcher:
        _wire(monkeypatch, rec)

        async def load(self, repo, issue_number):
            return await rec.load(repo, issue_number)

        async def observations(self, entity_id, *, limit=100):
            return list(rec.observations)

        monkeypatch.setattr(swarm_dispatch.IssueGateStore, "load", load)
        monkeypatch.setattr(swarm_dispatch.IssueGateStore, "_observations", observations)
        return SwarmDispatcher(tsd._StubNotifier(), tsd._config())

    def test_gates_green_does_not_trust_an_unproven_signed_off(self, monkeypatch):
        rec = _Record({"pm": "pending", "ux": "pending", "arch": "pending"})
        d = self._dispatcher_over(monkeypatch, rec)
        _land(rec, {"pm": "signed_off", "ux": "signed_off", "arch": "signed_off"},
              "apis@ateles-swarm")

        class _Lanius:
            ok = True
            stdout = "GATE_INHERITANCE: clear"

        assert _run(d._gates_green(_Lanius(), "o/r", 795)) is False

    def test_refresh_keeps_an_unproven_signed_off_gate_pending(self, monkeypatch):
        rec = _Record({"ux": "pending", "arch": "pending"})
        d = self._dispatcher_over(monkeypatch, rec)
        _land(rec, {"ux": "signed_off", "arch": "pending"}, "accipiter@ateles-swarm", "guest")

        assert _run(d._refresh_pending_gates("o/r", 795, {"ux"})) == {"ux"}

    def test_refresh_clears_a_proven_signed_off_gate(self, monkeypatch):
        rec = _Record({"ux": "pending"})
        d = self._dispatcher_over(monkeypatch, rec)
        _land(rec, {"ux": "signed_off"}, "accipiter@ateles-swarm")

        assert _run(d._refresh_pending_gates("o/r", 795, {"ux"})) == set()

    def test_panel_seats_the_owner_of_an_unproven_signed_off_gate(self, monkeypatch):
        """Lanius reads the value and reports nothing pending; the owner of an
        unproven `signed_off` must still be seated, and merge authorization
        must see the gate pending."""
        seen: dict = {}

        class _Cleared:
            found = True
            gate_status_unreadable = False
            gate_status = {"pm": "signed_off", "ux": "signed_off", "arch": "signed_off"}

        async def load(self, repo, issue_number):
            return _Cleared()

        async def ux_unproven(self, state, owners):
            return {"ux"} & set(owners)

        monkeypatch.setattr(swarm_dispatch.IssueGateStore, "load", load)
        monkeypatch.setattr(
            swarm_dispatch.IssueGateStore, "unverified_signed_off_gates",
            ux_unproven, raising=False,
        )
        d = tsd._pr_dispatcher_with_stubs(
            monkeypatch, vanellus_stdout="**APPROVE**\nlgtm", calls=[]
        )

        async def fake_run_skill(skill, prompt, **kwargs):
            seen[skill] = (prompt, kwargs)
            if skill == "lanius":
                return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
            return SkillResult(skill, True, 0, "**COMMENT**\nlgtm", "")

        monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
        _run(d._handle_pr(tsd._trigger(body="Closes #80.")))

        assert "accipiter" in seen, "the ux owner was not seated to re-sign"

    def test_a_failed_read_holds_every_pre_impl_gate(self, monkeypatch):
        async def load(self, repo, issue_number):
            raise RuntimeError("neotoma unavailable")

        monkeypatch.setattr(swarm_dispatch.IssueGateStore, "load", load)
        d = SwarmDispatcher(tsd._StubNotifier(), tsd._config())
        method = getattr(d, "_unverified_signed_off_gates", None)
        assert method is not None, "no pre-panel re-proof exists"

        assert _run(method("o/r", 795)) == set(swarm_dispatch.PRE_IMPL_GATES)
        assert _run(method("o/r", None)) == set()

    def test_operator_text_says_what_is_held_and_for_how_long(self):
        reason = swarm_dispatch.sign_off_design_reason(gate_waive.SIGN_OFF_CLEARED_UNVERIFIED)
        text = swarm_dispatch.sign_off_next_action(reason)
        assert "this run holds it as NOT cleared" in text
        assert "later runs re-check" in text
        assert "until a verified sign-off lands" not in text
