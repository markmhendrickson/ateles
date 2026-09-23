"""Fail-closed sign-off: the round answering PR #1181's reviews at d9f4d438.

One test class per finding, each written against the defect's exact shape:

1. ``TestAmbiguousVerdictDoesNotClear`` — security B1. `sign_off_is_warranted`
   read only the FIRST verdict token, so a body that quotes an earlier
   `**SIGNED_OFF**`/`**APPROVE**` and then states its own blocking verdict
   cleared the gate. The two inputs are the ones the security run used.
2. ``TestFailedSignOffNeverLeavesTheGateCleared`` — security B2 (the
   partial-write finding first raised at round 9c5d199). Driven by
   ``_Record``, a fake issue store whose signed writes really LAND, so a
   later failure is observed against the record rather than inferred from
   the return value. The two callers are tested in test_swarm_dispatch.py
   (``test_panel_keeps_a_failed_gate_pending...`` and
   ``test_issue_pipeline_does_not_build_on_a_failed_pm_sign_off``).
3. ``TestUnreadableStateRendersItsOwnToken`` — ux BLOCKING, plus the
   attribution-failure next_action (ux non-blocking).
4. ``TestAttributionMatchesTheCheckpointPattern`` — thumbprint and trusted
   tier, not the subject name alone.
5. ``TestToolDenyFromTheLiveRecord`` — the `correct` deny keyed on a live
   `gate_status` read, fail closed, on every entry point that seats a gate
   owner.
6. ``TestOtherAuthorityIsNotReportedAsALensSignOff``.

Run: pytest execution/daemons/apis/test_gate_sign_off_fail_closed.py -v
"""

from __future__ import annotations

import asyncio

import gate_waive
import swarm_dispatch
from gate_waive import (
    GATE_STATUS_FIELD,
    SIGN_OFF_SIGNING_FAILED,
    SIGN_OFF_UNREADABLE_STATE,
    IssueGateState,
    IssueGateStore,
    parse_gate_status,
)
from skill_runner import SkillResult
from swarm_dispatch import SwarmDispatcher

import test_swarm_dispatch as tsd

HEAD = "a" * 40
# An RFC 7638 thumbprint has this shape (43 base64url characters).
LENS_TP = "L" * 43
OTHER_TP = "O" * 43
# Later than any `now` a test run can capture, so the freshness check passes
# for every observation the fake record mints.
LATER = "2099-01-01T00:00:00+00:00"


# ── A fake issue store whose signed writes LAND ─────────────────────────────


class _Record:
    """In-memory issue entity. A signed `correct` mutates it and mints an
    observation carrying the signer's provenance, the way the server does.

    ``fail_fields``: fields whose write is refused (HTTP 503, nothing lands).
    ``fail_gate_writes_after``: once this many `gate_status` writes have
    landed, later ones are refused — how a test makes the compensating
    restore itself fail.
    ``raise_after_landing``: fields whose write LANDS and then the transport
    raises, the case where the caller cannot know whether it landed.
    ``attributed_sub`` / ``tier`` / ``thumbprint``: what the minted
    observation's provenance says, so a test can make the write land under a
    different principal, an untrusted tier, or a different key.
    ``tier_by_sub``: a per-signer tier, overriding ``tier`` for that subject.
    Each minted observation also carries ``fields`` ({field: value}), the
    shape the provenance re-proof reads.
    """

    def __init__(
        self,
        gate_status: dict,
        *,
        fail_fields: frozenset[str] = frozenset(),
        fail_gate_writes_after: int | None = None,
        raise_after_landing: frozenset[str] = frozenset(),
        attributed_sub: str | None = None,
        tier: str = "software",
        thumbprint: str = LENS_TP,
        tier_by_sub: dict[str, str] | None = None,
    ) -> None:
        self.gate_status = dict(gate_status)
        self.owner_history: list[dict] = []
        self.current_owner = ""
        self.provenance: dict[str, str] = {}
        self.observations: list[dict] = []
        self.writes: list[tuple[str, object, str | None]] = []
        self.gate_writes_landed = 0
        self.fail_fields = fail_fields
        self.fail_gate_writes_after = fail_gate_writes_after
        self.raise_after_landing = raise_after_landing
        self.attributed_sub = attributed_sub
        self.tier = tier
        self.thumbprint = thumbprint
        self.tier_by_sub = dict(tier_by_sub or {})

    async def load(self, repo: str, issue_number: int) -> IssueGateState:
        return IssueGateState(
            repo=repo,
            issue_number=issue_number,
            entity_id="ent_1",
            gate_status=dict(self.gate_status),
            owner_history=list(self.owner_history),
            current_owner=self.current_owner,
            field_provenance=dict(self.provenance),
        )

    async def fetch_observations(self, entity_id: str, *, limit: int = 100) -> list[dict]:
        return list(self.observations)

    def apply(self, body: dict, sub: str | None) -> tuple[int, dict]:
        field_name = body["field"]
        value = body["value"]
        self.writes.append((field_name, value, sub))
        if field_name in self.fail_fields:
            return 503, {}
        if field_name == GATE_STATUS_FIELD:
            if (
                self.fail_gate_writes_after is not None
                and self.gate_writes_landed >= self.fail_gate_writes_after
            ):
                return 503, {}
            self.gate_status = parse_gate_status(value)
            self.gate_writes_landed += 1
        elif field_name == "owner_history":
            self.owner_history = list(value)
        else:
            self.current_owner = str(value)
        observation_id = f"obs-{len(self.writes)}"
        self.provenance[field_name] = observation_id
        signer = self.attributed_sub or sub
        self.observations.insert(
            0,
            {
                "id": observation_id,
                "created_at": LATER,
                "fields": {field_name: value},
                "provenance": {
                    "agent_sub": signer,
                    "agent_thumbprint": self.thumbprint,
                    "attribution_tier": self.tier_by_sub.get(signer, self.tier),
                },
            },
        )
        if field_name in self.raise_after_landing:
            raise RuntimeError("transport dropped after the server committed")
        return 200, {}

    async def signed_request_async(
        self, method, url, body=None, agent_name="", timeout=20, *, sub=None
    ):
        return self.apply(body, sub)

    def signed_request_sync(
        self, method, url, body=None, agent_name="", timeout=20, *, sub=None
    ):
        """The REAL `neotoma_signed.signed_request` signature: synchronous."""
        return self.apply(body, sub)

    def gate_writes(self) -> list[tuple[object, str | None]]:
        return [(v, s) for f, v, s in self.writes if f == GATE_STATUS_FIELD]


def _wire(monkeypatch, rec: _Record, *, sync: bool = False) -> IssueGateStore:
    store = IssueGateStore("http://x", "daemon-bearer-tok")
    monkeypatch.setattr(
        "gate_waive._ns.agent_identity",
        lambda agent, sub=None: {
            "key": f"/keys/{agent}.jwk.json",
            "sub": sub or f"{agent}@ateles-swarm",
            "kid": f"{agent}-kid",
        },
    )
    monkeypatch.setattr(
        "gate_waive._ns.signed_request",
        rec.signed_request_sync if sync else rec.signed_request_async,
    )
    # The lens key's own RFC 7638 thumbprint. `raising=False`: the helper does
    # not exist before this round, and the RED run must still reach the
    # assertion that names the defect rather than stop at a missing name.
    monkeypatch.setattr(
        gate_waive, "_lens_key_thumbprint", lambda identity: LENS_TP, raising=False
    )
    monkeypatch.setattr(store, "load", rec.load)
    monkeypatch.setattr(store, "_observations", rec.fetch_observations)
    return store


def _run(coro):
    return asyncio.run(coro)


# ── 1. Ambiguous verdicts never clear (security B1) ─────────────────────────


class TestAmbiguousVerdictDoesNotClear:
    def test_quoted_signed_off_then_own_blocked_is_not_warranted(self):
        """Security run input 1, verbatim shape: a body citing another lens's
        sign-off before stating its own BLOCKED."""
        stdout = (
            "In the previous round waxwing **SIGNED_OFF** the arch gate.\n\n"
            "**BLOCKED** — the ux spec section is missing"
        )
        assert swarm_dispatch.sign_off_is_warranted(stdout, lens_agent="pavo") is False

    def test_quoted_approve_then_prose_request_changes_is_not_warranted(self):
        """Security run input 2: a quoted APPROVE, then this lens's own
        REQUEST_CHANGES whose finding is prose, not a bracketed marker."""
        stdout = (
            "> Earlier round: **APPROVE**\n\n"
            "**REQUEST_CHANGES**\n\n"
            "The migration drops the covering index; please restore it before merge."
        )
        assert swarm_dispatch.sign_off_is_warranted(stdout, lens_agent="pavo") is False

    def test_changes_requested_anywhere_is_not_warranted(self):
        stdout = "**SIGNED_OFF**\n\nGitHub still shows CHANGES_REQUESTED from the last round."
        assert swarm_dispatch.sign_off_is_warranted(stdout, lens_agent="pavo") is False

    def test_own_verdict_is_read_from_the_line_after_the_header(self):
        """The lens protocol (skill_runner.SWARM_GITHUB_CONTRACT, "Verdict
        line") puts the lens's own verdict on the line immediately after the
        attribution header. A clear token further down does not stand in
        for a verdict line that is missing or is something else."""
        header = "**🤖 Accipiter — Ateles swarm, ux gate owner**\n"
        assert swarm_dispatch.sign_off_is_warranted(
            header + "**SIGNED_OFF**\n\n- [x] design section present",
            lens_agent="accipiter",
        ) is True
        assert swarm_dispatch.sign_off_is_warranted(
            header + "Reviewed the diff.\n\n**SIGNED_OFF**",
            lens_agent="accipiter",
        ) is False

    def test_comment_is_not_a_sign_off(self):
        """`COMMENT` means "observations only"; it names no gate decision, so
        it cannot clear one."""
        assert swarm_dispatch.sign_off_is_warranted("**COMMENT**\nobservation only", lens_agent="pavo") is False

    def test_clean_explicit_clear_tokens_still_clear(self):
        """Under the lens's own header (a headerless verdict no longer counts:
        both security runs at bf97b1a4)."""
        header = "**🤖 Pavo — Ateles swarm, pm gate owner**\n"
        assert swarm_dispatch.sign_off_is_warranted(
            header + "**SIGNED_OFF**\nno concerns", lens_agent="pavo"
        ) is True
        assert swarm_dispatch.sign_off_is_warranted(
            header + "**APPROVE**\nlgtm", lens_agent="pavo"
        ) is True
        assert swarm_dispatch.sign_off_is_warranted(
            "**SIGNED_OFF**\nno concerns", lens_agent="pavo"
        ) is False


# ── 2. A failed sign-off never leaves the gate cleared (security B2) ────────


class TestFailedSignOffNeverLeavesTheGateCleared:
    def test_owner_history_failure_leaves_gate_pending(self, monkeypatch):
        """Security run probe 1: owner_history 503. The gate must not read
        `signed_off` afterwards, and the outcome reports what the record
        actually holds."""
        rec = _Record({"ux": "pending", "arch": "signed_off"}, fail_fields=frozenset({"owner_history"}))
        store = _wire(monkeypatch, rec)

        outcome = _run(store.sign_off("o/r", 795, "ux", "accipiter", HEAD))

        assert not outcome.ok
        assert outcome.error == SIGN_OFF_SIGNING_FAILED
        assert rec.gate_status["ux"] == "pending"
        assert rec.gate_status["arch"] == "signed_off"
        assert getattr(outcome, "observed_state", None) == "pending"

    def test_attribution_failure_restores_the_gate(self, monkeypatch):
        """Security run probe 2: the write lands but the observation is
        attributed to the daemon. The gate is restored by a lens-signed
        compensating correction and read back."""
        rec = _Record({"ux": "pending"}, attributed_sub="apis@ateles-swarm")
        store = _wire(monkeypatch, rec)

        outcome = _run(store.sign_off("o/r", 795, "ux", "accipiter", HEAD))

        assert not outcome.ok
        assert not outcome.verified
        assert rec.gate_status["ux"] == "pending"
        assert outcome.error == getattr(gate_waive, "SIGN_OFF_ATTRIBUTION_FAILED", "<missing>")
        assert getattr(outcome, "observed_state", None) == "pending"
        restore_value, restore_sub = rec.gate_writes()[-1]
        assert parse_gate_status(restore_value)["ux"] == "pending"
        assert restore_sub == "accipiter@ateles-swarm"

    def test_unconfirmed_restore_is_its_own_failure_class(self, monkeypatch):
        """The restore write itself is refused, so the gate still reads
        `signed_off`. That must surface as "may read cleared without a
        verified sign-off", never as a plain write failure or as pending."""
        rec = _Record(
            {"ux": "pending"},
            attributed_sub="apis@ateles-swarm",
            fail_gate_writes_after=1,
        )
        store = _wire(monkeypatch, rec)

        outcome = _run(store.sign_off("o/r", 795, "ux", "accipiter", HEAD))

        assert not outcome.ok
        assert rec.gate_status["ux"] == "signed_off"
        assert outcome.error == getattr(gate_waive, "SIGN_OFF_CLEARED_UNVERIFIED", "<missing>")
        assert getattr(outcome, "observed_state", None) == "signed_off"

    def test_gate_write_that_lands_then_raises_is_restored(self, monkeypatch):
        rec = _Record({"ux": "pending"}, raise_after_landing=frozenset({GATE_STATUS_FIELD}))
        store = _wire(monkeypatch, rec)

        outcome = _run(store.sign_off("o/r", 795, "ux", "accipiter", HEAD))

        assert not outcome.ok
        assert outcome.error == SIGN_OFF_SIGNING_FAILED
        assert rec.gate_status["ux"] == "pending"
        assert getattr(outcome, "observed_state", None) == "pending"

    def test_failed_re_sign_of_an_already_signed_gate_is_reported_as_cleared_unverified(
        self, monkeypatch
    ):
        """A re-sign whose attribution fails leaves a `signed_off` this call
        could not attribute. Its prior value was already `signed_off`, so
        there is nothing to restore to, and the honest report is that the
        gate reads cleared without a verified sign-off."""
        rec = _Record({"ux": "signed_off"}, attributed_sub="apis@ateles-swarm")
        store = _wire(monkeypatch, rec)

        outcome = _run(store.sign_off("o/r", 795, "ux", "accipiter", HEAD))

        assert not outcome.ok
        assert outcome.error == getattr(gate_waive, "SIGN_OFF_CLEARED_UNVERIFIED", "<missing>")
        assert getattr(outcome, "observed_state", None) == "signed_off"

    def test_gate_status_is_written_last(self, monkeypatch):
        rec = _Record({"pm": "pending"})
        store = _wire(monkeypatch, rec)

        outcome = _run(store.sign_off("o/r", 795, "pm", "pavo", HEAD, next_owner="accipiter"))

        assert outcome.ok and outcome.verified
        assert [f for f, _, _ in rec.writes][-1] == GATE_STATUS_FIELD
        assert rec.gate_status["pm"] == "signed_off"

    def test_real_synchronous_signed_request_is_supported(self, monkeypatch):
        """`neotoma_signed.signed_request` is a plain function returning a
        tuple. Awaiting its return value raises TypeError AFTER the node
        helper has already sent the write, so every live sign-off landed
        `signed_off` and then reported a signing failure."""
        rec = _Record({"arch": "pending"})
        store = _wire(monkeypatch, rec, sync=True)

        outcome = _run(store.sign_off("o/r", 795, "arch", "waxwing", HEAD))

        assert outcome.ok, outcome.error
        assert outcome.verified
        assert rec.gate_status["arch"] == "signed_off"


# ── 3. Unreadable state and attribution render their own tokens (ux) ────────


class _RecordingClient:
    def __init__(self):
        self.posted: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None):
        class _Resp:
            def raise_for_status(self_inner):
                pass

            def json(self_inner):
                return []

        return _Resp()

    async def post(self, url, json=None, headers=None):
        self.posted.append(json)

        class _Resp:
            def raise_for_status(self_inner):
                pass

        return _Resp()


def _render(monkeypatch, failed) -> str:
    import httpx

    client = _RecordingClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")
    d = SwarmDispatcher(tsd._StubNotifier(), tsd._config())
    _run(d._surface_failed_sign_offs(tsd._trigger(), 80, failed))
    assert len(client.posted) == 1
    return client.posted[0]["body"]


class TestUnreadableStateRendersItsOwnToken:
    def test_mapping_functions_name_the_unreadable_state(self):
        reason = swarm_dispatch.sign_off_design_reason(SIGN_OFF_UNREADABLE_STATE)
        assert reason == "gate_state_unreadable"
        assert swarm_dispatch.sign_off_failure_class(SIGN_OFF_UNREADABLE_STATE) == (
            "gate state unreadable"
        )
        next_action = swarm_dispatch.sign_off_next_action(reason)
        assert "AAuth" not in next_action
        assert "ATELES_AAUTH_KEYS_DIR" not in next_action
        assert "corrupted" in next_action

    def test_rendered_comment_for_unreadable_state(self, monkeypatch):
        body = _render(monkeypatch, [("ux", "accipiter", SIGN_OFF_UNREADABLE_STATE, "unreadable")])
        assert "`gate_state_unreadable`" in body
        assert "gate_writeback_denied" not in body
        assert "ATELES_AAUTH_KEYS_DIR" not in body
        assert "- observed: `gate_status.ux` re-read as `unreadable`" in body

    def test_rendered_comment_for_attribution_failure_names_signer_and_staleness(
        self, monkeypatch
    ):
        error = getattr(gate_waive, "SIGN_OFF_ATTRIBUTION_FAILED", "<missing>")
        body = _render(monkeypatch, [("arch", "waxwing", error, "pending")])
        assert "`gate_writeback_unconfirmed`" in body
        assert "wrong signer" in body
        assert "stale observation" in body

    def test_rendered_comment_states_the_actual_reread_value(self, monkeypatch):
        error = getattr(gate_waive, "SIGN_OFF_CLEARED_UNVERIFIED", "<missing>")
        body = _render(monkeypatch, [("arch", "waxwing", error, "signed_off")])
        assert "`gate_cleared_unverified`" in body
        assert "- observed: `gate_status.arch` re-read as `signed_off`" in body
        assert "still read `pending`" not in body


# ── 4. Attribution matches the checkpoint pattern ───────────────────────────


class TestAttributionMatchesTheCheckpointPattern:
    def test_guest_tier_without_thumbprint_is_not_verified(self, monkeypatch):
        """The security run's probe: `attribution_tier: guest`, no
        thumbprint, right subject — used to verify."""
        rec = _Record({"arch": "pending"}, tier="guest", thumbprint="")
        store = _wire(monkeypatch, rec)

        outcome = _run(store.sign_off("o/r", 795, "arch", "waxwing", HEAD))

        assert not outcome.verified
        assert not outcome.ok
        assert rec.gate_status["arch"] == "pending"

    def test_wrong_key_thumbprint_is_not_verified(self, monkeypatch):
        rec = _Record({"arch": "pending"}, thumbprint=OTHER_TP)
        store = _wire(monkeypatch, rec)

        outcome = _run(store.sign_off("o/r", 795, "arch", "waxwing", HEAD))

        assert not outcome.verified
        assert not outcome.ok

    def test_trusted_tier_and_matching_thumbprint_verify(self, monkeypatch):
        rec = _Record({"arch": "pending"}, tier="operator_attested")
        store = _wire(monkeypatch, rec)

        outcome = _run(store.sign_off("o/r", 795, "arch", "waxwing", HEAD))

        assert outcome.ok and outcome.verified

    def test_unreadable_lens_key_refuses_before_any_write(self, monkeypatch):
        """No thumbprint for the lens key means attribution could never be
        proven, so nothing is written."""
        rec = _Record({"arch": "pending"})
        store = _wire(monkeypatch, rec)
        monkeypatch.setattr(gate_waive, "_lens_key_thumbprint", lambda identity: None, raising=False)

        outcome = _run(store.sign_off("o/r", 795, "arch", "waxwing", HEAD))

        assert not outcome.ok
        assert outcome.error == gate_waive.SIGN_OFF_NO_SIGNING_KEY
        assert rec.writes == []


# ── 5. The tool deny comes from the live record ─────────────────────────────


class _LiveGates:
    found = True
    gate_status_unreadable = False

    def __init__(self, gate_status):
        self.gate_status = gate_status


class TestToolDenyFromTheLiveRecord:
    def test_pure_predicate(self):
        deny = getattr(swarm_dispatch, "gate_owner_tool_deny", None)
        assert deny is not None, "gate_owner_tool_deny is missing"
        assert deny("arch", None) is True  # unknown record: fail closed
        assert deny("arch", {"arch": "pending"}) is True
        assert deny("arch", {}) is True  # absent gate reads as pending
        assert deny("arch", {"arch": "signed_off"}) is False
        assert deny("", None) is False  # advisory lens owns no gate
        assert deny("security", None) is False
        assert deny("qa", {"qa": "signed_off"}, reported_pending=True) is True

    def _panel_kwargs(self, monkeypatch, load_impl) -> dict:
        seen: dict = {}
        calls: list = []
        monkeypatch.setattr(swarm_dispatch.IssueGateStore, "load", load_impl)

        # A `signed_off` in these records is backed by its owner's own signed
        # write; the provenance re-proof is tested in
        # test_gate_sign_off_residuals.py.
        async def all_proven(self, state, owners):
            return set()

        monkeypatch.setattr(
            swarm_dispatch.IssueGateStore,
            "unverified_signed_off_gates",
            all_proven,
            raising=False,
        )
        d = tsd._pr_dispatcher_with_stubs(monkeypatch, vanellus_stdout="**APPROVE**\nlgtm", calls=calls)

        async def fake_run_skill(skill, prompt, **kwargs):
            seen[skill] = kwargs
            if skill == "lanius":
                # Lanius omits GATE_PENDING entirely: the fail-open-to-clear shape.
                return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
            return SkillResult(skill, True, 0, "**COMMENT**\nlgtm", "")

        monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
        _run(d._handle_pr(tsd._trigger(body="Closes #80.")))
        seated = sorted(seen)
        assert "pavo" in seated
        return seen

    def test_panel_denies_a_gate_owner_the_record_shows_pending(self, monkeypatch):
        """pavo (pm) is always seated. Lanius reported no pending gate, but
        the record says pm is pending, so the run must carry the deny."""
        async def load(self, repo, issue_number):
            return _LiveGates({"pm": "pending", "ux": "signed_off", "arch": "signed_off"})

        seen = self._panel_kwargs(monkeypatch, load)
        assert seen["pavo"]["owns_pending_gate"] is True

    def test_panel_denies_when_the_live_read_fails(self, monkeypatch):
        async def load(self, repo, issue_number):
            raise RuntimeError("neotoma unavailable")

        seen = self._panel_kwargs(monkeypatch, load)
        assert seen["pavo"]["owns_pending_gate"] is True

    def test_panel_denies_when_the_record_is_unreadable(self, monkeypatch):
        async def load(self, repo, issue_number):
            state = _LiveGates({})
            state.gate_status_unreadable = True
            return state

        seen = self._panel_kwargs(monkeypatch, load)
        assert seen["pavo"]["owns_pending_gate"] is True

    def test_panel_does_not_deny_a_gate_the_record_shows_cleared(self, monkeypatch):
        async def load(self, repo, issue_number):
            return _LiveGates({"pm": "signed_off", "ux": "signed_off", "arch": "signed_off"})

        seen = self._panel_kwargs(monkeypatch, load)
        assert seen["pavo"]["owns_pending_gate"] is False

    def test_missing_lens_rerun_carries_the_deny(self, monkeypatch):
        seen: dict = {}

        async def load(self, repo, issue_number):
            return _LiveGates({"arch": "pending"})

        async def fake_run_skill(skill, prompt, **kwargs):
            seen[skill] = kwargs
            return SkillResult(skill, True, 0, "**COMMENT**\nlgtm", "")

        async def noop(self, *a, **k):
            return None

        async def no_files(self, trigger):
            return []

        monkeypatch.setattr(swarm_dispatch.IssueGateStore, "load", load)
        monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
        monkeypatch.setattr(SwarmDispatcher, "_changed_files", no_files)
        monkeypatch.setattr(SwarmDispatcher, "_persist_panel_reviews", noop)
        monkeypatch.setattr(SwarmDispatcher, "_post_missing_panel_comments", noop)
        pr = {
            "number": 87,
            "title": "PR",
            "body": "Closes #80.",
            "user": {"login": "someone"},
            "html_url": "https://github.com/owner/repo/pull/87",
            "head": {"ref": "feature", "sha": HEAD},
            "base": {"ref": "main"},
        }
        d = SwarmDispatcher(tsd._StubNotifier(), tsd._config())
        _run(d._redispatch_missing_lens("owner/repo", pr, "arch"))

        assert seen["waxwing"].get("owns_pending_gate") is True

    def test_fix_guidance_run_of_a_gate_owner_carries_the_deny(self, monkeypatch):
        seen: dict = {}

        async def load(self, repo, issue_number):
            raise RuntimeError("neotoma unavailable")

        async def fake_run_skill(skill, prompt, **kwargs):
            seen.setdefault(skill, kwargs)
            return SkillResult(skill, True, 0, "guidance", "")

        async def zero(self, trigger):
            return 0

        async def noop(self, *a, **k):
            return None

        async def head(self, trigger):
            return HEAD

        monkeypatch.setattr(swarm_dispatch.IssueGateStore, "load", load)
        monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
        monkeypatch.setattr(SwarmDispatcher, "_fix_round_count", zero)
        monkeypatch.setattr(SwarmDispatcher, "_record_fix_round", noop)
        monkeypatch.setattr(SwarmDispatcher, "_pr_head_sha", head)
        d = SwarmDispatcher(tsd._StubNotifier(), tsd._config())
        reviews = [("arch", "[BLOCKING] contract: the gate write is unordered\ndetail")]
        _run(d._route_blocking_findings(tsd._trigger(), parent=80, reviews=reviews, verdict="request_changes"))

        assert seen["waxwing"].get("owns_pending_gate") is True


# ── 6. Another authority's clear is not a lens sign-off ─────────────────────


class TestOtherAuthorityIsNotReportedAsALensSignOff:
    def test_waived_gate_is_not_verified_as_a_lens_sign_off(self, monkeypatch):
        rec = _Record({"arch": "waived"})
        store = _wire(monkeypatch, rec)

        outcome = _run(store.sign_off("o/r", 795, "arch", "waxwing", HEAD))

        assert outcome.ok  # nothing failed; nothing to surface
        assert outcome.verified is False
        assert outcome.error == getattr(gate_waive, "SIGN_OFF_OTHER_AUTHORITY", "<missing>")
        assert getattr(outcome, "observed_state", None) == "waived"
        assert rec.writes == []

    def test_log_line_does_not_credit_the_lens(self, caplog):
        describe = getattr(swarm_dispatch, "describe_sign_off_success", None)
        assert describe is not None, "describe_sign_off_success is missing"
        other = gate_waive.SignOffOutcome(
            ok=True,
            gate="arch",
            lens_agent="waxwing",
            lens_sub="waxwing@ateles-swarm",
            verified=False,
            error=getattr(gate_waive, "SIGN_OFF_OTHER_AUTHORITY", ""),
            observed_state="waived",
        )
        line = describe("o/r#87", other)
        assert "verified" not in line
        assert "signed off by" not in line
        assert "waived" in line
        signed = gate_waive.SignOffOutcome(
            ok=True, gate="arch", lens_agent="waxwing",
            lens_sub="waxwing@ateles-swarm", verified=True,
        )
        assert "signed off by waxwing" in describe("o/r#87", signed)

