"""
Regression tests for ateles#682: every participation_record was stranded at
`dispatched`. No gate ever recorded a terminal state.

Two defects compound, and fixing either alone is not enough:

1. `record_satisfied` / `record_skipped` never passed `agent`, which the
   participation_record schema marks REQUIRED. It was not even a parameter of
   either function. They also wrote field names the schema does not declare —
   `artifact_refs` (schema: `artifact_ref`) and `error` (schema: `skip_reason`).

2. `_upsert` swallowed every failure. Crucially, Neotoma does NOT reject an
   incomplete write: it answers **HTTP 200** with `required_fields_missing`
   and a `MISSING_REQUIRED_FIELD` entry in `store_warnings`, then stores a row
   without the missing field. A caller checking only the status code sees
   success. This is why the breakage was invisible for 165 rows.

The critical property under test is READ-BACK: a satisfied gate must produce a
durable row that reads back as `satisfied` **with its agent**. A test asserting
only that the call did not raise passes against the unfixed code, because the
swallow guarantees nothing raises — that is precisely the trap this file exists
to avoid.

Run with: pytest execution/daemons/anthus/test_participation_terminal_write.py -v
"""

from __future__ import annotations

import participation
import pytest

# Mirrors the live schema (participation_record v1.2.0) as verified against
# Neotoma prod on 2026-09-02 via describe_entity_type.
REQUIRED_FIELDS = {"work_entity_id", "gate_name", "agent", "status"}
DECLARED_FIELDS = REQUIRED_FIELDS | {
    "workflow_definition_id",
    "dispatched_at",
    "satisfied_at",
    "skipped_at",
    "artifact_ref",
    "skip_reason",
    "aauth_token_jti",
    "version",
    "agent_definition_ref",
    "agent_definition_observation_id",
    "agent_strategy_ref",
}


class _FakeNeotoma:
    """
    A stand-in for the Neotoma /store surface that reproduces the behaviour
    that made this bug invisible: an incomplete write is ACCEPTED with HTTP 200
    plus warnings, and only the DECLARED fields reach the stored snapshot.

    `rows` is the read-back surface — the assertions in this file inspect what
    actually landed, not what was sent.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}
        self.warned_writes: list[list[dict]] = []

    def store(self, body: dict) -> "_FakeResponse":
        entity = dict(body["entities"][0])
        entity.pop("entity_type", None)

        missing = [
            {"entity_type": "participation_record", "field": f}
            for f in sorted(REQUIRED_FIELDS)
            if not entity.get(f)
        ]
        unknown = sorted(k for k in entity if k not in DECLARED_FIELDS)

        # Undeclared fields do not reach the snapshot; they land in
        # raw_fragments and are invisible to every reader.
        snapshot = {k: v for k, v in entity.items() if k in DECLARED_FIELDS}

        key = (snapshot.get("work_entity_id", ""), snapshot.get("gate_name", ""))
        self.rows[key] = {**self.rows.get(key, {}), **snapshot}

        warnings = [
            {
                "code": "MISSING_REQUIRED_FIELD",
                "message": (
                    f"participation_record stored without required field "
                    f'"{m["field"]}".'
                ),
            }
            for m in missing
        ]
        if warnings:
            self.warned_writes.append(warnings)

        return _FakeResponse(
            200,
            {
                "entities": [{"entity_snapshot_after": snapshot}],
                "required_fields_missing": missing,
                "unknown_fields": unknown,
                "store_warnings": warnings,
            },
        )


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = "ok"

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, backend: _FakeNeotoma, **_kwargs):
        self._backend = backend

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url: str, json: dict):  # noqa: A002 - httpx's name
        assert url.endswith("/store"), url
        return self._backend.store(json)


@pytest.fixture
def neotoma(monkeypatch):
    backend = _FakeNeotoma()
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-token")  # gitleaks:allow
    monkeypatch.setattr(participation, "NEOTOMA_BASE_URL", "https://neotoma.test")
    monkeypatch.setattr(
        participation,
        "httpx",
        type("_M", (), {"AsyncClient": lambda **kw: _FakeClient(backend, **kw)}),
    )
    return backend


# ── The core property: a satisfied gate lands a terminal row ─────────────────


@pytest.mark.asyncio
async def test_satisfied_gate_writes_terminal_row_verified_by_readback(neotoma):
    """
    THE regression test for #682.

    Asserts on the STORED ROW, not on the absence of an exception. Against the
    unfixed code this fails on `status`/`agent`: `record_satisfied` had no
    `agent` parameter at all, so the row landed incomplete.
    """
    await participation.record_satisfied(
        work_entity_id="ent_work_1",
        gate_name="pm",
        artifact_ref="https://github.com/o/r/issues/1#issuecomment-5",
        agent="pavo",
    )

    row = neotoma.rows[("ent_work_1", "pm")]
    assert row["status"] == "satisfied", "gate did not reach a terminal state"
    assert row["agent"] == "pavo", "required `agent` never reached the stored row"
    assert row["artifact_ref"] == "https://github.com/o/r/issues/1#issuecomment-5"
    assert row.get("satisfied_at"), "terminal row carries no satisfied_at"

    # The write must be clean — no MISSING_REQUIRED_FIELD warning at all.
    assert neotoma.warned_writes == []


@pytest.mark.asyncio
async def test_skipped_gate_writes_terminal_row_verified_by_readback(neotoma):
    await participation.record_skipped(
        work_entity_id="ent_work_2",
        gate_name="ux",
        reason="fast_path",
        agent="regulus",
    )

    row = neotoma.rows[("ent_work_2", "ux")]
    assert row["status"] == "skipped"
    assert row["agent"] == "regulus"
    # `skip_reason` is the declared field; the old code wrote `error`, which
    # never reached a snapshot.
    assert row["skip_reason"] == "fast_path"
    # A skip is not a completion: it must not claim satisfied_at.
    assert row.get("skipped_at"), "skip carries no skipped_at"
    assert "satisfied_at" not in row
    assert neotoma.warned_writes == []


@pytest.mark.asyncio
async def test_dispatched_row_is_superseded_by_satisfied(neotoma):
    """
    The end-to-end shape of the bug: a gate dispatches, then satisfies. The
    stored row must end at `satisfied`. Pre-fix it stayed at `dispatched`
    forever, which is exactly the 163-row stranded population.
    """
    await participation.record_dispatched(
        work_entity_id="ent_work_3",
        workflow_definition_id="ent_wf_1",
        gate_name="pm",
        agent="pavo",
    )
    assert neotoma.rows[("ent_work_3", "pm")]["status"] == "dispatched"

    await participation.record_satisfied(
        work_entity_id="ent_work_3",
        gate_name="pm",
        artifact_ref="https://example.test/c/1",
        agent="pavo",
    )

    row = neotoma.rows[("ent_work_3", "pm")]
    assert row["status"] == "satisfied", "row remained stranded at dispatched"
    assert row["agent"] == "pavo"


# ── The swallow: a defective write must not read as success ──────────────────


@pytest.mark.asyncio
async def test_incomplete_write_raises_instead_of_being_swallowed(neotoma):
    """
    A 200 response carrying MISSING_REQUIRED_FIELD must raise. This is the
    half of the fix that makes any FUTURE instance of this defect visible
    instead of silent.
    """
    with pytest.raises(participation.ParticipationWriteFailed) as exc:
        await participation._upsert(
            {
                "work_entity_id": "ent_work_4",
                "gate_name": "qa",
                "status": "satisfied",
                # `agent` deliberately absent.
            },
            idempotency_key="satisfied-ent_work_4-qa",
        )
    assert "agent" in str(exc.value)


@pytest.mark.asyncio
async def test_undeclared_field_raises(neotoma):
    """`artifact_refs`/`error` silently vanished into raw_fragments before."""
    with pytest.raises(participation.ParticipationWriteFailed):
        await participation._upsert(
            {
                "work_entity_id": "ent_work_5",
                "gate_name": "qa",
                "agent": "sitta",
                "status": "satisfied",
                "artifact_refs": ["https://example.test/c/2"],
            },
            idempotency_key="satisfied-ent_work_5-qa",
        )


@pytest.mark.asyncio
async def test_http_error_raises(monkeypatch):
    backend = _FakeNeotoma()

    def _failing_store(_body):
        return _FakeResponse(500, {})

    backend.store = _failing_store  # type: ignore[method-assign]
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-token")  # gitleaks:allow
    monkeypatch.setattr(participation, "NEOTOMA_BASE_URL", "https://neotoma.test")
    monkeypatch.setattr(
        participation,
        "httpx",
        type("_M", (), {"AsyncClient": lambda **kw: _FakeClient(backend, **kw)}),
    )

    with pytest.raises(participation.ParticipationWriteFailed):
        await participation.record_satisfied("ent_w", "pm", "ref", "pavo")


@pytest.mark.asyncio
async def test_transport_failure_raises(monkeypatch):
    class _Boom:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_e):
            return False

        async def post(self, *_a, **_kw):
            raise ConnectionError("neotoma unreachable")

    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-token")  # gitleaks:allow
    monkeypatch.setattr(participation, "NEOTOMA_BASE_URL", "https://neotoma.test")
    monkeypatch.setattr(participation, "httpx", type("_M", (), {"AsyncClient": _Boom}))

    with pytest.raises(participation.ParticipationWriteFailed):
        await participation.record_skipped("ent_w", "pm", "fast_path", "pavo")


# ── The read path round-trips what the write path stored ────────────────────


@pytest.mark.asyncio
async def test_load_state_reads_back_the_fields_the_write_stored(neotoma, monkeypatch):
    """
    Write then read through the real read path. Pre-fix these two halves
    disagreed on field names in both directions, so even a well-formed row
    read back with no artifact.
    """
    await participation.record_satisfied(
        "ent_work_6", "pm", "https://example.test/c/9", "pavo"
    )
    stored = neotoma.rows[("ent_work_6", "pm")]

    class _QueryClient:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_e):
            return False

        async def post(self, url, json):  # noqa: A002
            assert url.endswith("/entities/query")

            class _R:
                status_code = 200

                @staticmethod
                def raise_for_status():
                    return None

                @staticmethod
                def json():
                    return {"entities": [{"snapshot": stored}]}

            return _R()

    monkeypatch.setattr(
        participation, "httpx", type("_M", (), {"AsyncClient": _QueryClient})
    )

    state = await participation.load_state_for("ent_work_6")
    assert state["pm"]["status"] == "satisfied"
    assert state["pm"]["artifact_refs"] == ["https://example.test/c/9"]
