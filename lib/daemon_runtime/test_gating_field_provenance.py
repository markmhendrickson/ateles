"""Field-provenance verification shared by checkpoint and other resolver-written fields.

``read_authenticated_checkpoint_resolution`` is a thin wrapper over a shared
verifier; ``read_authenticated_field_value`` reads any named string field
through the same checks. Synthetic data only.
"""

from __future__ import annotations

import pytest

from lib.daemon_runtime import gating

SUB = "resolver@example-swarm"
JKT = "A" * 43
OTHER = "intruder@example-swarm"
TENANT = "11111111-2222-3333-4444-555555555555"


def _obs(oid, fields, *, sub=SUB, jkt=JKT, tier="software", user=TENANT):
    return {
        "id": oid,
        "user_id": user,
        "fields": fields,
        "provenance": {
            "agent_sub": sub,
            "agent_thumbprint": jkt,
            "attribution_tier": tier,
        },
    }


def _serve(monkeypatch, observations):
    monkeypatch.setattr(
        gating, "_fetch_entity_observations", lambda eid, **k: list(observations)
    )


KW = dict(
    required_approver_sub=SUB, required_approver_jkt=JKT, expected_user_id=TENANT
)


# ── Checkpoint wrapper: behaviour unchanged ──────────────────────────────────


def _checkpoint(monkeypatch, **who):
    _serve(monkeypatch, [_obs("o1", {"status": "approved"}, **who)])
    return {"snapshot": {"status": "approved"}, "provenance": {"status": "o1"}}


def test_checkpoint_resolution_result_shape_is_unchanged(monkeypatch):
    record = _checkpoint(monkeypatch)
    got = gating.read_authenticated_checkpoint_resolution("e", record, **KW)
    assert got == {
        "principal_sub": SUB,
        "observation_id": "o1",
        "attribution_tier": "software",
        "agent_thumbprint": JKT,
    }


@pytest.mark.parametrize(
    "who",
    [
        {"sub": OTHER},
        {"jkt": "B" * 43},
        {"tier": "anonymous"},
        {"user": "99999999-2222-3333-4444-555555555555"},
    ],
)
def test_checkpoint_resolution_refuses_wrong_principal_tier_or_tenant(monkeypatch, who):
    record = _checkpoint(monkeypatch, **who)
    assert gating.read_authenticated_checkpoint_resolution("e", record, **KW) is None


def test_checkpoint_resolution_refuses_a_bad_expectation_or_mismatch(monkeypatch):
    record = _checkpoint(monkeypatch)
    for expected in ("maybe", "rejected"):
        assert (
            gating.read_authenticated_checkpoint_resolution(
                "e", record, expected_resolution=expected, **KW
            )
            is None
        )


def test_checkpoint_resolution_needs_provenance_and_configuration(monkeypatch):
    record = _checkpoint(monkeypatch)
    no_prov = {"snapshot": record["snapshot"]}
    assert gating.read_authenticated_checkpoint_resolution("e", no_prov, **KW) is None
    for bad in (
        dict(KW, required_approver_sub=""),
        dict(KW, required_approver_jkt="short"),
        dict(KW, expected_user_id=""),
    ):
        assert gating.read_authenticated_checkpoint_resolution("e", record, **bad) is None


# ── Generic field reader ─────────────────────────────────────────────────────


def _field_record(value="attended yoga", prov="o1"):
    return {"snapshot": {"operator_answer": value}, "provenance": {"operator_answer": prov}}


def test_field_value_is_returned_only_from_the_authenticated_observation(monkeypatch):
    _serve(monkeypatch, [_obs("o1", {"operator_answer": "attended yoga"})])
    assert (
        gating.read_authenticated_field_value(
            "e", _field_record(), field="operator_answer", **KW
        )
        == "attended yoga"
    )


@pytest.mark.parametrize(
    "who",
    [{"sub": OTHER}, {"jkt": "B" * 43}, {"tier": "anonymous"}, {"user": "x"}],
)
def test_field_value_refuses_wrong_principal_tier_or_tenant(monkeypatch, who):
    _serve(monkeypatch, [_obs("o1", {"operator_answer": "attended yoga"}, **who)])
    assert (
        gating.read_authenticated_field_value(
            "e", _field_record(), field="operator_answer", **KW
        )
        is None
    )


def test_a_later_write_by_another_principal_withdraws_the_value(monkeypatch):
    """The snapshot value no longer equals what the authenticating observation
    carries once someone else's write wins, so no authority is inherited."""
    _serve(
        monkeypatch,
        [
            _obs("o1", {"operator_answer": "attended yoga"}),
            _obs("o2", {"operator_answer": "attended yoga please"}, sub=OTHER),
        ],
    )
    record = _field_record(value="attended yoga please", prov="o1")
    assert (
        gating.read_authenticated_field_value("e", record, field="operator_answer", **KW)
        is None
    )
    record = _field_record(value="attended yoga please", prov="o2")
    assert (
        gating.read_authenticated_field_value("e", record, field="operator_answer", **KW)
        is None
    )


def test_field_value_refuses_missing_provenance_unknown_observation_or_blank(monkeypatch):
    _serve(monkeypatch, [_obs("o1", {"operator_answer": "attended yoga"})])
    assert gating.read_authenticated_field_value(
        "e", {"snapshot": {"operator_answer": "attended yoga"}}, field="operator_answer", **KW
    ) is None
    assert gating.read_authenticated_field_value(
        "e", _field_record(prov="missing"), field="operator_answer", **KW
    ) is None
    _serve(monkeypatch, [_obs("o1", {"operator_answer": "  "})])
    assert gating.read_authenticated_field_value(
        "e", _field_record(value="  "), field="operator_answer", **KW
    ) is None
    assert gating.read_authenticated_field_value(
        "e", _field_record(), field="", **KW
    ) is None


def test_field_value_requires_configuration(monkeypatch):
    _serve(monkeypatch, [_obs("o1", {"operator_answer": "attended yoga"})])
    for bad in (
        dict(KW, required_approver_sub=""),
        dict(KW, required_approver_jkt="short"),
        dict(KW, expected_user_id=""),
    ):
        assert gating.read_authenticated_field_value(
            "e", _field_record(), field="operator_answer", **bad
        ) is None
