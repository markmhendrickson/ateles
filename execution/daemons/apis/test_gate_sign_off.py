"""IssueGateStore.sign_off — the lens-signed gate write (ateles#795 amended ADR).

## The bug

The dispatcher recorded a gate-owning lens's verdict by writing to Neotoma
with the Apis daemon's OWN bearer token, attributing the write to Apis rather
than to the reviewing lens (pavo/waxwing/accipiter/...). The operator's
amended decision on ateles#795: the write must be signed with the REVIEWING
LENS's own AAuth keypair via `lib/daemon_runtime/neotoma_signed.py`, never the
daemon bearer — and never silently fall back to the bearer if signing is
unavailable, which would reproduce the exact bug.

## What each test class proves

- ``TestFailsClosedOnMissingKey`` — constraint 1: no AAuth key for the lens ->
  refuse, never fall back to the bearer.
- ``TestExplicitSubjectNeverAmbient`` — constraint 2: the write is signed as
  the LENS's subject even when this process's own ``NEOTOMA_AAUTH_SUB`` names
  a different principal (the daemon).
- ``TestOnlyDeclaredFieldsWritten`` — constraint 4: only ``gate_status`` and
  ``owner_history`` are ever sent; ``gate_writeback_outcome`` (confirmed
  undeclared on prod's `issue` schema) is never attempted.
- ``TestReadBackAssertion`` — the write is verified by re-reading the entity,
  not trusted from a 2xx response.
- ``TestSafetyPreconditions`` — the (repo, issue) exactness and pending-gate
  checks copied from `waive()`'s shape, narrowed to (repo, issue, gate).

Run: pytest execution/daemons/apis/test_gate_sign_off.py -v
"""

from __future__ import annotations

from unittest import mock

import pytest

from gate_waive import (
    SIGN_OFF_ENTITY_NOT_FOUND,
    SIGN_OFF_GATE_NOT_PENDING,
    SIGN_OFF_NO_HEAD,
    SIGN_OFF_NO_SIGNING_KEY,
    SIGN_OFF_SIGNING_FAILED,
    SIGN_OFF_UNREADABLE_STATE,
    SIGN_OFF_VERIFY_FAILED,
    IssueGateState,
    IssueGateStore,
    gate_status_is_unreadable,
    owner_history_is_unreadable,
    parse_gate_status,
    parse_owner_history,
)

HEAD = "a" * 40

# The attribution read-back (Falco's CONFIRMED BLOCKING finding, ateles#795 /
# PR #1181) needs a `gate_status` -> observation-id provenance entry AND a
# matching observation carrying that observation's OWN provenance
# (`agent_sub`, freshness). `PAST` / `NOW` bound the freshness check the same
# way `sign_off` itself does (`observed_at >= now`, where `now` is captured
# BEFORE the write) without depending on wall-clock time in the test.
PAST = "2020-01-01T00:00:00+00:00"
NOW = "2030-01-01T00:00:00+00:00"


def _state(
    gate_status: dict | None = None,
    found: bool = True,
    entity_id: str = "ent_1",
    current_owner: str = "",
    field_provenance: dict | None = None,
) -> IssueGateState:
    return IssueGateState(
        repo="o/r",
        issue_number=795,
        entity_id=entity_id if found else "",
        gate_status=gate_status or {},
        owner_history=[],
        current_owner=current_owner,
        field_provenance=(
            field_provenance
            if field_provenance is not None
            else {"gate_status": "obs-1"}  # vocab-ok: live Neotoma wire field name
        ),
    )


def _attributed_observation(
    lens_sub: str, *, observation_id: str = "obs-1", observed_at: str = NOW
) -> dict:
    """An observation whose OWN provenance names *lens_sub* as the signer."""
    return {
        "id": observation_id,
        "created_at": observed_at,
        "provenance": {"agent_sub": lens_sub},
    }


def _mock_attributed_observations(monkeypatch, store, lens_sub: str, **kwargs) -> None:
    """Wire `store._observations` to return one observation attributed to
    *lens_sub*, so tests that exist to prove something OTHER than the
    attribution read-back itself do not need to re-derive this shape."""
    monkeypatch.setattr(
        store,
        "_observations",
        mock.AsyncMock(return_value=[_attributed_observation(lens_sub, **kwargs)]),
    )


class _LoadSequence:
    """Replays canned `load()` results in order; the last one repeats."""

    def __init__(self, states: list[IssueGateState]) -> None:
        self.states = states
        self.calls = 0

    async def __call__(self, repo: str, issue_number: int) -> IssueGateState:
        i = min(self.calls, len(self.states) - 1)
        self.calls += 1
        return self.states[i]


def _identity(agent: str, sub: str) -> dict:
    return {"key": f"/keys/{agent}.jwk.json", "sub": sub, "kid": f"{agent}-kid"}


# ── Constraint 1: fail closed on missing signing capability ─────────────────


class TestFailsClosedOnMissingKey:
    """No AAuth key for the lens -> refuse. NEVER fall back to the bearer."""

    @pytest.mark.asyncio
    async def test_no_key_refuses_before_any_write(self, monkeypatch):
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr("gate_waive._ns.agent_identity", lambda agent, sub=None: None)
        write = mock.AsyncMock()
        monkeypatch.setattr("gate_waive._ns.signed_request", write)
        load_spy = _LoadSequence([_state({"arch": "pending"})])
        monkeypatch.setattr(store, "load", load_spy)

        outcome = await store.sign_off("o/r", 795, "arch", "waxwing", HEAD)

        assert not outcome.ok
        assert outcome.error == SIGN_OFF_NO_SIGNING_KEY
        write.assert_not_called()
        # The precondition is checked BEFORE the state re-read: no key means
        # no possible write, so there is nothing to re-read for.
        assert load_spy.calls == 0

    @pytest.mark.asyncio
    async def test_signing_failure_never_falls_back_to_bearer(self, monkeypatch):
        """The signed subprocess raising must classify as a failure, not
        trigger any bearer-token write. There is no bearer write path in this
        method at all — this test proves the failure return, not a fallback,
        because the only way to prove the ABSENCE of a fallback is to show
        that the sole write attempt was the signed one and it is the one that
        failed."""
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )

        async def _raise(*a, **kw):
            raise RuntimeError("signed_fetch returned non-JSON: <stderr redacted>")

        monkeypatch.setattr("gate_waive._ns.signed_request", _raise)
        monkeypatch.setattr(store, "load", _LoadSequence([_state({"arch": "pending"})]))

        outcome = await store.sign_off("o/r", 795, "arch", "waxwing", HEAD)

        assert not outcome.ok
        assert outcome.error == SIGN_OFF_SIGNING_FAILED


# ── Constraint 2: explicit subject, never ambient ────────────────────────────


class TestExplicitSubjectNeverAmbient:
    """The write signs as the LENS, even when this process's own
    NEOTOMA_AAUTH_SUB names a different principal (the daemon)."""

    @pytest.mark.asyncio
    async def test_signs_as_lens_not_as_ambient_daemon_sub(self, monkeypatch):
        monkeypatch.setenv("NEOTOMA_AAUTH_SUB", "apis@ateles-swarm")
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub) if sub else None,
        )
        seen_subs: list[str | None] = []

        async def _capture(method, url, body=None, agent_name="", timeout=20, *, sub=None):
            seen_subs.append(sub)
            return 200, {}

        monkeypatch.setattr("gate_waive._ns.signed_request", _capture)
        monkeypatch.setattr(
            store,
            "load",
            _LoadSequence(
                [_state({"arch": "pending"}), _state({"arch": "signed_off"})]
            ),
        )
        _mock_attributed_observations(monkeypatch, store, "waxwing@ateles-swarm")

        outcome = await store.sign_off("o/r", 795, "arch", "waxwing", HEAD)

        assert outcome.ok
        assert outcome.verified
        assert outcome.lens_sub == "waxwing@ateles-swarm"
        assert seen_subs, "signed_request was never called"
        for sub in seen_subs:
            assert sub == "waxwing@ateles-swarm"
            assert sub != "apis@ateles-swarm"


# ── Constraint 4: only declared schema fields ────────────────────────────────


class TestOnlyDeclaredFieldsWritten:
    """gate_writeback_outcome (confirmed undeclared) must never be sent."""

    @pytest.mark.asyncio
    async def test_only_gate_status_and_owner_history_are_sent(self, monkeypatch):
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        sent_fields: list[str] = []

        async def _capture(method, url, body=None, agent_name="", timeout=20, *, sub=None):
            sent_fields.append(body["field"])
            return 200, {}

        monkeypatch.setattr("gate_waive._ns.signed_request", _capture)
        monkeypatch.setattr(
            store,
            "load",
            _LoadSequence(
                [_state({"pm": "pending"}), _state({"pm": "signed_off"})]
            ),
        )

        _mock_attributed_observations(monkeypatch, store, "pavo@ateles-swarm")
        outcome = await store.sign_off("o/r", 795, "pm", "pavo", HEAD)

        assert outcome.ok
        assert set(sent_fields) == {"gate_status", "owner_history"}  # vocab-ok: live Neotoma wire field name
        assert "gate_writeback_outcome" not in sent_fields


# ── current_owner advance (PR #1181, provider-table round) ──────────────────
#
# Moves the phase-handoff write the lens's own prompt used to make via
# in-session `correct()` onto the dispatcher's signed `sign_off` path, same as
# `gate_status` itself. Opt-in via `next_owner`: omitted, behaviour is
# unchanged from before this parameter existed.


class TestCurrentOwnerAdvance:
    @pytest.mark.asyncio
    async def test_next_owner_writes_current_owner_in_the_same_signed_call(
        self, monkeypatch
    ):
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        sent: list[tuple[str, object]] = []

        async def _capture(method, url, body=None, agent_name="", timeout=20, *, sub=None):
            sent.append((body["field"], body["value"]))
            return 200, {}

        monkeypatch.setattr("gate_waive._ns.signed_request", _capture)
        monkeypatch.setattr(
            store,
            "load",
            _LoadSequence(
                [
                    _state({"pm": "pending"}, current_owner="pavo"),
                    _state({"pm": "signed_off"}, current_owner="accipiter"),
                ]
            ),
        )

        _mock_attributed_observations(monkeypatch, store, "pavo@ateles-swarm")
        outcome = await store.sign_off(
            "o/r", 795, "pm", "pavo", HEAD, next_owner="accipiter"
        )

        assert outcome.ok
        fields_sent = {f for f, _ in sent}
        assert "current_owner" in fields_sent, (
            "next_owner was supplied — current_owner must be written in the "
            "same signed call as gate_status/owner_history"
        )
        assert dict(sent)["current_owner"] == "accipiter"

    @pytest.mark.asyncio
    async def test_omitted_next_owner_never_writes_current_owner(self, monkeypatch):
        """Default behaviour (no next_owner) must be byte-for-byte the same
        as before this parameter existed — no current_owner write attempted."""
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        sent_fields: list[str] = []

        async def _capture(method, url, body=None, agent_name="", timeout=20, *, sub=None):
            sent_fields.append(body["field"])
            return 200, {}

        monkeypatch.setattr("gate_waive._ns.signed_request", _capture)
        monkeypatch.setattr(
            store,
            "load",
            _LoadSequence(
                [_state({"pm": "pending"}), _state({"pm": "signed_off"})]
            ),
        )

        _mock_attributed_observations(monkeypatch, store, "pavo@ateles-swarm")
        outcome = await store.sign_off("o/r", 795, "pm", "pavo", HEAD)

        assert outcome.ok
        assert "current_owner" not in sent_fields
        assert set(sent_fields) == {"gate_status", "owner_history"}  # vocab-ok: live Neotoma wire field name

    @pytest.mark.asyncio
    async def test_current_owner_readback_mismatch_is_verify_failed(self, monkeypatch):
        """A landed gate_status write is not evidence current_owner ALSO
        survived — same read-it-back discipline, checked as its own field."""
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        monkeypatch.setattr(
            "gate_waive._ns.signed_request",
            mock.AsyncMock(return_value=(200, {})),
        )
        monkeypatch.setattr(
            store,
            "load",
            _LoadSequence(
                [
                    _state({"pm": "pending"}, current_owner="pavo"),
                    # gate_status landed but current_owner silently dropped —
                    # the undeclared/dropped-field shape this repo has
                    # documented before.
                    _state({"pm": "signed_off"}, current_owner="pavo"),
                ]
            ),
        )

        outcome = await store.sign_off(
            "o/r", 795, "pm", "pavo", HEAD, next_owner="accipiter"
        )

        assert not outcome.ok
        assert outcome.error == SIGN_OFF_VERIFY_FAILED

    @pytest.mark.asyncio
    async def test_current_owner_is_a_declared_field(self):
        from gate_waive import _SIGN_OFF_DECLARED_FIELDS

        assert "current_owner" in _SIGN_OFF_DECLARED_FIELDS


# ── Read-back assertion ──────────────────────────────────────────────────────


class TestReadBackAssertion:
    """A 2xx from signed_request is not evidence the write landed."""

    @pytest.mark.asyncio
    async def test_write_succeeds_but_readback_shows_unchanged_is_a_failure(
        self, monkeypatch
    ):
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        monkeypatch.setattr(
            "gate_waive._ns.signed_request",
            mock.AsyncMock(return_value=(200, {})),
        )
        # The write reports 200 both times, but the RE-READ still shows
        # `pending` — the silent-drop failure mode this repo has documented
        # (undeclared fields, fire-and-forget writes).
        monkeypatch.setattr(
            store,
            "load",
            _LoadSequence(
                [_state({"ux": "pending"}), _state({"ux": "pending"})]
            ),
        )

        outcome = await store.sign_off("o/r", 795, "ux", "accipiter", HEAD)

        assert not outcome.ok
        assert outcome.error == SIGN_OFF_VERIFY_FAILED
        assert not outcome.verified

    @pytest.mark.asyncio
    async def test_write_and_readback_agree_is_success(self, monkeypatch):
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        monkeypatch.setattr(
            "gate_waive._ns.signed_request",
            mock.AsyncMock(return_value=(200, {})),
        )
        monkeypatch.setattr(
            store,
            "load",
            _LoadSequence(
                [_state({"ux": "pending"}), _state({"ux": "signed_off"})]
            ),
        )
        _mock_attributed_observations(monkeypatch, store, "accipiter@ateles-swarm")

        outcome = await store.sign_off("o/r", 795, "ux", "accipiter", HEAD)

        assert outcome.ok
        assert outcome.verified
        assert outcome.error == ""

    @pytest.mark.asyncio
    async def test_non_2xx_status_is_an_explicit_failure_not_just_a_readback_gap(
        self, monkeypatch
    ):
        """Falco's security review, PR #1181 (NON-BLOCKING, defense in depth):
        `signed_request` returns its HTTP status rather than raising on a
        non-2xx. A caller that discards the status relies entirely on the
        read-back to catch a rejected write — this asserts the status itself
        is checked, classifying the failure as a write failure even in the
        (contrived) case where a stale read-back would otherwise appear to
        agree with what was attempted."""
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        monkeypatch.setattr(
            "gate_waive._ns.signed_request",
            mock.AsyncMock(return_value=(403, {"error": "forbidden"})),
        )
        monkeypatch.setattr(
            store,
            "load",
            _LoadSequence([_state({"ux": "pending"})]),
        )

        outcome = await store.sign_off("o/r", 795, "ux", "accipiter", HEAD)

        assert not outcome.ok
        assert outcome.error == SIGN_OFF_SIGNING_FAILED


# ── Safety preconditions (copied shape from waive()/_matches) ───────────────


class TestSafetyPreconditions:
    @pytest.mark.asyncio
    async def test_no_entity_refuses(self, monkeypatch):
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        write = mock.AsyncMock()
        monkeypatch.setattr("gate_waive._ns.signed_request", write)
        monkeypatch.setattr(store, "load", _LoadSequence([_state(found=False)]))

        outcome = await store.sign_off("o/r", 795, "arch", "waxwing", HEAD)

        assert not outcome.ok
        assert outcome.error == SIGN_OFF_ENTITY_NOT_FOUND
        write.assert_not_called()

    @pytest.mark.asyncio
    async def test_already_signed_off_still_performs_the_lens_signed_write(
        self, monkeypatch
    ):
        """An already-`signed_off` gate is idempotent at the VALUE level (the
        write doesn't change what's stored) but NOT skipped: a caller-observed
        `signed_off` snapshot proves nothing about who signed it, so `sign_off`
        always re-performs the lens-signed write and read-back rather than
        trusting a snapshot it did not itself just verify (Falco's security
        review, PR #1181 — the CLEARED-state no-op was flagged as a sink that
        would let an unattributed shared-bearer write pass through as a
        verified lens sign-off merely because the value happened to match)."""
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        write = mock.AsyncMock(return_value=(200, {}))
        monkeypatch.setattr("gate_waive._ns.signed_request", write)
        monkeypatch.setattr(
            store, "load", _LoadSequence([_state({"arch": "signed_off"})])
        )
        _mock_attributed_observations(monkeypatch, store, "waxwing@ateles-swarm")

        outcome = await store.sign_off("o/r", 795, "arch", "waxwing", HEAD)

        assert outcome.ok
        assert outcome.verified
        assert write.call_count == 2  # gate_status + owner_history
        for call in write.call_args_list:
            assert call.kwargs["sub"] == "waxwing@ateles-swarm", (
                "the re-sign must still be attributed to THIS lens, never a "
                "prior (possibly unattributed) clearer"
            )

    @pytest.mark.asyncio
    async def test_waived_gate_is_also_a_no_op_not_an_overwrite(self, monkeypatch):
        """A gate an operator already waived must not be overwritten to
        signed_off by a late-arriving lens verdict."""
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        write = mock.AsyncMock()
        monkeypatch.setattr("gate_waive._ns.signed_request", write)
        monkeypatch.setattr(
            store, "load", _LoadSequence([_state({"arch": "waived"})])
        )

        outcome = await store.sign_off("o/r", 795, "arch", "waxwing", HEAD)

        assert outcome.ok
        write.assert_not_called()

    @pytest.mark.asyncio
    async def test_unexpected_gate_value_refuses_rather_than_overwrite(
        self, monkeypatch
    ):
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        write = mock.AsyncMock()
        monkeypatch.setattr("gate_waive._ns.signed_request", write)
        monkeypatch.setattr(
            store, "load", _LoadSequence([_state({"arch": "blocked"})])
        )

        outcome = await store.sign_off("o/r", 795, "arch", "waxwing", HEAD)

        assert not outcome.ok
        assert outcome.error == SIGN_OFF_GATE_NOT_PENDING
        write.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_head_sha_refuses(self, monkeypatch):
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        write = mock.AsyncMock()
        monkeypatch.setattr("gate_waive._ns.signed_request", write)
        monkeypatch.setattr(store, "load", _LoadSequence([_state({"arch": "pending"})]))

        outcome = await store.sign_off("o/r", 795, "arch", "waxwing", "")

        assert not outcome.ok
        assert outcome.error == SIGN_OFF_NO_HEAD
        write.assert_not_called()


# ── No secret material ever surfaces in the outcome or its logs ─────────────


class TestNoSecretMaterialInOutcome:
    @pytest.mark.asyncio
    async def test_signing_failure_message_carries_no_key_path_or_body(
        self, monkeypatch, caplog
    ):
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )

        async def _raise(*a, **kw):
            raise RuntimeError(
                "signed_fetch failed: Authorization: Bearer super-secret-token "
                "at /Users/markmhendrickson/repos/ateles-private/keys/waxwing.jwk.json"
            )

        monkeypatch.setattr("gate_waive._ns.signed_request", _raise)
        monkeypatch.setattr(store, "load", _LoadSequence([_state({"arch": "pending"})]))

        import logging

        with caplog.at_level(logging.ERROR, logger="apis.gate_waive"):
            outcome = await store.sign_off("o/r", 795, "arch", "waxwing", HEAD)

        assert not outcome.ok
        assert outcome.error == SIGN_OFF_SIGNING_FAILED
        assert "super-secret-token" not in outcome.error
        for record in caplog.records:
            assert "super-secret-token" not in record.getMessage()
            assert "jwk.json" not in record.getMessage()
            assert "Bearer" not in record.getMessage()


# ── Parse-failure denies: unreadable is UNKNOWN, never empty (Falco's ────────
# CONFIRMED BLOCKING finding, ateles#795 / PR #1181) ─────────────────────────
#
# `parse_gate_status`/`parse_owner_history` fold BOTH "genuinely absent" and
# "present but unparseable" to the same {}/[] — correct for `waive()` (an
# absent gate is legitimately unsigned), wrong for `sign_off`, which must
# refuse to write through an unreadable value rather than silently
# reconstructing a fresh map from the empty parse and discarding whatever
# sibling gate state it actually held.


class TestUnreadableStateIsDistinctFromAbsent:
    """The pure predicates: absent -> False, unreadable -> True."""

    def test_absent_gate_status_is_not_unreadable(self):
        assert gate_status_is_unreadable(None) is False
        assert gate_status_is_unreadable("") is False
        assert gate_status_is_unreadable({}) is False

    def test_malformed_gate_status_is_unreadable(self):
        assert gate_status_is_unreadable("not json") is True
        # Valid JSON, but not an object — a list is not a gate map.
        assert gate_status_is_unreadable("[1, 2, 3]") is True
        assert gate_status_is_unreadable(42) is True
        # Both still fold to {} via the existing parser — the SAME shape as
        # a legitimately absent field, which is exactly why the two need a
        # SEPARATE predicate rather than being inferred from the parse output.
        assert parse_gate_status("not json") == {}
        assert parse_gate_status(None) == {}

    def test_absent_owner_history_is_not_unreadable(self):
        assert owner_history_is_unreadable(None) is False
        assert owner_history_is_unreadable("") is False
        assert owner_history_is_unreadable([]) is False

    def test_malformed_owner_history_is_unreadable(self):
        assert owner_history_is_unreadable("not json") is True
        assert owner_history_is_unreadable('{"not": "a list"}') is True
        assert owner_history_is_unreadable(7) is True
        assert parse_owner_history("not json") == []


class TestSignOffRefusesOnUnreadableState:
    """RED on the old behaviour: an unreadable value used to fall through
    `parse_gate_status`'s {} and be treated as a legitimately-absent (hence
    pending) gate, so `sign_off` would rebuild a fresh map from the empty
    parse and WRITE through it — discarding whatever sibling gate state the
    unreadable value actually held. GREEN here: refuse before any write."""

    @pytest.mark.asyncio
    async def test_unreadable_gate_status_refuses_before_any_write(self, monkeypatch):
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        write = mock.AsyncMock()
        monkeypatch.setattr("gate_waive._ns.signed_request", write)
        # gate_status was PRESENT (Neotoma returned something) but it is not
        # valid JSON — the "unreadable" case, distinct from a missing field.
        bad_state = IssueGateState(
            repo="o/r",
            issue_number=795,
            entity_id="ent_1",
            gate_status={},  # what parse_gate_status folds the bad value to
            owner_history=[],
            gate_status_unreadable=True,
        )
        monkeypatch.setattr(store, "load", _LoadSequence([bad_state]))

        outcome = await store.sign_off("o/r", 795, "arch", "waxwing", HEAD)

        assert not outcome.ok
        assert not outcome.verified
        assert outcome.error == SIGN_OFF_UNREADABLE_STATE
        write.assert_not_called()

    @pytest.mark.asyncio
    async def test_unreadable_owner_history_refuses_before_any_write(self, monkeypatch):
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        write = mock.AsyncMock()
        monkeypatch.setattr("gate_waive._ns.signed_request", write)
        bad_state = IssueGateState(
            repo="o/r",
            issue_number=795,
            entity_id="ent_1",
            gate_status={"arch": "pending"},
            owner_history=[],
            owner_history_unreadable=True,
        )
        monkeypatch.setattr(store, "load", _LoadSequence([bad_state]))

        outcome = await store.sign_off("o/r", 795, "arch", "waxwing", HEAD)

        assert not outcome.ok
        assert not outcome.verified
        assert outcome.error == SIGN_OFF_UNREADABLE_STATE
        write.assert_not_called()

    @pytest.mark.asyncio
    async def test_readable_state_is_unaffected(self, monkeypatch):
        """Control: a genuinely readable (or genuinely absent) state is not
        refused by this check — it proceeds to the normal precondition path."""
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        monkeypatch.setattr(
            "gate_waive._ns.signed_request",
            mock.AsyncMock(return_value=(200, {})),
        )
        monkeypatch.setattr(
            store,
            "load",
            _LoadSequence(
                [_state({"arch": "pending"}), _state({"arch": "signed_off"})]
            ),
        )
        _mock_attributed_observations(monkeypatch, store, "waxwing@ateles-swarm")

        outcome = await store.sign_off("o/r", 795, "arch", "waxwing", HEAD)

        assert outcome.ok
        assert outcome.error != SIGN_OFF_UNREADABLE_STATE


# ── Attribution read-back: value landing is not attribution (Falco's ────────
# CONFIRMED BLOCKING finding, ateles#795 / PR #1181) ─────────────────────────
#
# The pre-existing read-back (`TestReadBackAssertion` above) proves the VALUE
# landed. It does NOT prove the reviewing lens was the one who wrote it — an
# already-`signed_off` gate is, by design, re-signed rather than skipped
# (`TestSafetyPreconditions::test_already_signed_off_still_performs_the_lens_signed_write`)
# precisely because a value match proves nothing about who supplied it. These
# tests exercise the OBSERVATION-level attribution check that closes that gap.


class TestAttributionReadBack:
    @pytest.mark.asyncio
    async def test_missing_field_provenance_is_not_verified(self, monkeypatch):
        """No `gate_status` entry in the entity's field-provenance map at all
        — cannot even identify WHICH observation to check. Must fail closed,
        never fall back to trusting the snapshot value."""
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        monkeypatch.setattr(
            "gate_waive._ns.signed_request",
            mock.AsyncMock(return_value=(200, {})),
        )
        monkeypatch.setattr(
            store,
            "load",
            _LoadSequence(
                [
                    _state({"arch": "pending"}, field_provenance={}),
                    _state({"arch": "signed_off"}, field_provenance={}),
                ]
            ),
        )
        observations_spy = mock.AsyncMock(return_value=[])
        monkeypatch.setattr(store, "_observations", observations_spy)

        outcome = await store.sign_off("o/r", 795, "arch", "waxwing", HEAD)

        assert not outcome.ok
        assert not outcome.verified
        assert outcome.error == SIGN_OFF_VERIFY_FAILED
        # No provenance entry means there is no observation id to look up —
        # the read-back must not even attempt to fetch observations blindly.
        observations_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_unattributed_observation_is_not_verified(self, monkeypatch):
        """RED on the pre-fix shape: the value read back as `signed_off`, but
        the observation that produced it is attributed to a DIFFERENT
        agent_sub (e.g. the daemon's own shared bearer, or a different lens)
        — the exact unattributed-write-passes-as-verified sink Falco's
        review named. `verified` must be False even though `ok` was headed
        toward True on the value check alone."""
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        monkeypatch.setattr(
            "gate_waive._ns.signed_request",
            mock.AsyncMock(return_value=(200, {})),
        )
        monkeypatch.setattr(
            store,
            "load",
            _LoadSequence(
                [_state({"arch": "pending"}), _state({"arch": "signed_off"})]
            ),
        )
        # The observation exists and is fresh, but names a DIFFERENT
        # agent_sub — e.g. an unattributed shared-bearer write that happened
        # to land the same value.
        monkeypatch.setattr(
            store,
            "_observations",
            mock.AsyncMock(
                return_value=[_attributed_observation("apis@ateles-swarm")]
            ),
        )

        outcome = await store.sign_off("o/r", 795, "arch", "waxwing", HEAD)

        assert not outcome.ok
        assert not outcome.verified
        assert outcome.error == SIGN_OFF_VERIFY_FAILED

    @pytest.mark.asyncio
    async def test_stale_observation_is_not_verified(self, monkeypatch):
        """The named observation exists and IS attributed to the right lens,
        but it is OLDER than this call's own pre-write read — a prior sign-off
        this call happened to observe again, not proof of a NEW write. Must
        not be verified: a re-sign is verified only by the NEW observation."""
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        monkeypatch.setattr(
            "gate_waive._ns.signed_request",
            mock.AsyncMock(return_value=(200, {})),
        )
        monkeypatch.setattr(
            store,
            "load",
            # Already signed_off going in — this is the re-sign path.
            _LoadSequence([_state({"arch": "signed_off"})]),
        )
        _mock_attributed_observations(
            monkeypatch, store, "waxwing@ateles-swarm", observed_at=PAST
        )

        outcome = await store.sign_off("o/r", 795, "arch", "waxwing", HEAD)

        assert not outcome.ok
        assert not outcome.verified
        assert outcome.error == SIGN_OFF_VERIFY_FAILED

    @pytest.mark.asyncio
    async def test_fresh_correctly_attributed_observation_is_verified(
        self, monkeypatch
    ):
        """The positive control: a re-sign whose NEW observation is fresh
        AND attributed to the reviewing lens IS verified."""
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        monkeypatch.setattr(
            "gate_waive._ns.signed_request",
            mock.AsyncMock(return_value=(200, {})),
        )
        monkeypatch.setattr(
            store,
            "load",
            _LoadSequence([_state({"arch": "signed_off"})]),
        )
        _mock_attributed_observations(
            monkeypatch, store, "waxwing@ateles-swarm", observed_at=NOW
        )

        outcome = await store.sign_off("o/r", 795, "arch", "waxwing", HEAD)

        assert outcome.ok
        assert outcome.verified
        assert outcome.error == ""

    @pytest.mark.asyncio
    async def test_observations_fetch_failure_is_not_verified(self, monkeypatch):
        """The observations read itself fails/returns nothing — UNKNOWN
        attribution, not a pass. `_observations` degrades to [] on any
        transport error (best-effort read), so this is indistinguishable
        from "no observations" and must fail closed the same way."""
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        monkeypatch.setattr(
            "gate_waive._ns.signed_request",
            mock.AsyncMock(return_value=(200, {})),
        )
        monkeypatch.setattr(
            store,
            "load",
            _LoadSequence(
                [_state({"arch": "pending"}), _state({"arch": "signed_off"})]
            ),
        )
        monkeypatch.setattr(
            store, "_observations", mock.AsyncMock(return_value=[])
        )

        outcome = await store.sign_off("o/r", 795, "arch", "waxwing", HEAD)

        assert not outcome.ok
        assert not outcome.verified
        assert outcome.error == SIGN_OFF_VERIFY_FAILED
