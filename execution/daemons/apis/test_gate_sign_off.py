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
    SIGN_OFF_HEAD_MISMATCH,
    SIGN_OFF_NO_SIGNING_KEY,
    SIGN_OFF_SIGNING_FAILED,
    SIGN_OFF_VERIFY_FAILED,
    IssueGateState,
    IssueGateStore,
)

HEAD = "a" * 40


def _state(gate_status: dict | None = None, found: bool = True, entity_id: str = "ent_1") -> IssueGateState:
    return IssueGateState(
        repo="o/r",
        issue_number=795,
        entity_id=entity_id if found else "",
        gate_status=gate_status or {},
        owner_history=[],
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

        outcome = await store.sign_off("o/r", 795, "arch", "waxwing", HEAD)

        assert outcome.ok
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

        outcome = await store.sign_off("o/r", 795, "pm", "pavo", HEAD)

        assert outcome.ok
        assert set(sent_fields) == {"gate_status", "owner_history"}  # vocab-ok: live Neotoma wire field name
        assert "gate_writeback_outcome" not in sent_fields


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

        outcome = await store.sign_off("o/r", 795, "ux", "accipiter", HEAD)

        assert outcome.ok
        assert outcome.verified
        assert outcome.error == ""


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
    async def test_already_signed_off_is_an_idempotent_no_op(self, monkeypatch):
        store = IssueGateStore("http://x", "daemon-bearer-tok")
        monkeypatch.setattr(
            "gate_waive._ns.agent_identity",
            lambda agent, sub=None: _identity(agent, sub or f"{agent}@ateles-swarm"),
        )
        write = mock.AsyncMock()
        monkeypatch.setattr("gate_waive._ns.signed_request", write)
        monkeypatch.setattr(
            store, "load", _LoadSequence([_state({"arch": "signed_off"})])
        )

        outcome = await store.sign_off("o/r", 795, "arch", "waxwing", HEAD)

        assert outcome.ok
        assert outcome.verified
        write.assert_not_called()

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
        assert outcome.error == SIGN_OFF_HEAD_MISMATCH
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
