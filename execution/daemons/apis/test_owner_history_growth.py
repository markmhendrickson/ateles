"""owner_history must not grow by the whole previous history on every sign-off.

Observed on the canary markmhendrickson/ateles#1448: the issue entity's
`owner_history` reached 1023 entries (2**10 - 1) and about 128 KB, after which the
dispatcher's signed sign-off writes started failing. The store's array reducer
UNIONS the arrays of every correction at the highest priority and does not collapse
repeats, so a writer that resends "the history it read plus one entry" doubles the
stored array each time: 1, 3, 7, ... 1023.

These tests run the REAL sign-off path against a fake store that models that
reducer (`test_gate_sign_off_fail_closed._Record`: corrections are priority 1000,
a plain store() is lower, and the field is the union of the top-priority
observations, repeats kept). Planted-red tests reinstate the old writer and show the
same assertions fail.

Run: pytest execution/daemons/apis/test_owner_history_growth.py -v
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess

import pytest

import gate_waive
from gate_waive import (
    IssueGateStore,
    dedupe_exact,
    history_entry_identity,
    http_failure_code,
    new_history_entries,
    parse_owner_history,
    signed_failure_code,
    with_entry_id,
)
from lib.daemon_runtime import neotoma_signed as ns
from test_gate_sign_off_fail_closed import HEAD, _Record, _wire


def _run(coro):
    return asyncio.run(coro)


def _heads(n: int) -> list[str]:
    return [f"{i:040x}" for i in range(1, n + 1)]


def _sign(store, head=HEAD, gate="arch", lens="waxwing"):
    return _run(store.sign_off("o/r", 795, gate, lens, head))


# ── repeated sign-offs keep the stored array bounded ─────────────────────────


def test_the_same_sign_off_repeated_is_one_entry(monkeypatch):
    rec = _Record({"arch": "pending"})
    store = _wire(monkeypatch, rec)

    for _ in range(10):
        assert _sign(store).ok

    assert len(rec.owner_history) == 1, rec.owner_history
    (entry,) = rec.owner_history
    assert entry["action"] == "signed_off" and entry["gate"] == "arch" and entry["commit"] == HEAD
    # The gate is still re-signed every time (the attribution read-back needs a
    # fresh lens-signed observation): only the history write is skipped.
    assert len(rec.gate_writes()) == 10
    assert sum(1 for f, _, _ in rec.writes if f == "owner_history") == 1


def test_n_different_sign_offs_are_n_entries_and_each_write_carries_one(monkeypatch):
    rec = _Record({"arch": "pending"})
    store = _wire(monkeypatch, rec)

    for head in _heads(12):
        assert _sign(store, head).ok

    assert len(rec.owner_history) == 12
    assert len({e["commit"] for e in rec.owner_history}) == 12
    sizes = [len(v) for f, v, _ in rec.writes if f == "owner_history"]
    assert sizes == [1] * 12, f"a write carried more than its own entry: {sizes}"


def test_the_stored_array_never_exceeds_the_number_of_distinct_events(monkeypatch):
    rec = _Record({"arch": "pending", "pm": "pending", "ux": "pending"})
    store = _wire(monkeypatch, rec)

    events = 0
    for round_ in range(5):
        for gate, lens in (("arch", "waxwing"), ("pm", "pavo"), ("ux", "accipiter")):
            _sign(store, _heads(5)[round_], gate=gate, lens=lens)
            events += 1
            assert len(rec.owner_history) <= events
    assert len(rec.owner_history) == 15


def test_history_seeded_by_a_triage_store_is_carried_up_exactly_once(monkeypatch):
    """A lower-priority history stops counting once a correction carries the field,
    so the FIRST correction carries it; later ones add only themselves."""
    rec = _Record({"arch": "pending"})
    rec.owner_history = [{"agent": "lanius", "action": "triaged", "at": "2026-10-14T10:00:00Z"}]
    store = _wire(monkeypatch, rec)

    for head in _heads(4):
        _sign(store, head)

    actions = [e["action"] for e in rec.owner_history]
    assert actions.count("triaged") == 1, actions
    assert actions.count("signed_off") == 4
    sizes = [len(v) for f, v, _ in rec.writes if f == "owner_history"]
    assert sizes == [2, 1, 1, 1], sizes


def _actions(rec) -> list[tuple[str, str]]:
    return [(e.get("action"), e.get("commit")) for e in rec.owner_history]


def test_history_never_ends_on_a_signed_off_whose_operation_failed(monkeypatch):
    """A failure on commit A, then the same failure on commit B: both stay
    attributable, and the second attempt does not end as a success."""
    rec = _Record({"pm": "pending"}, fail_fields=frozenset({gate_waive.GATE_STATUS_FIELD}))
    store = _wire(monkeypatch, rec)

    assert not _sign(store, _heads(2)[0], gate="pm", lens="pavo").ok
    assert not _sign(store, _heads(2)[1], gate="pm", lens="pavo").ok

    a, b = _heads(2)
    assert _actions(rec) == [
        ("signed_off", a), ("sign_off_failed", a), ("signed_off", b), ("sign_off_failed", b),
    ], _actions(rec)
    assert rec.owner_history[-1]["action"] == "sign_off_failed"
    assert rec.owner_history[-1]["commit"] == b


def test_success_failure_success_on_one_commit_is_told_in_order(monkeypatch):
    rec = _Record({"pm": "pending"})
    store = _wire(monkeypatch, rec)

    assert _sign(store, HEAD, gate="pm", lens="pavo").ok
    rec.fail_fields = frozenset({gate_waive.GATE_STATUS_FIELD})
    assert not _sign(store, HEAD, gate="pm", lens="pavo").ok
    rec.fail_fields = frozenset()
    assert _sign(store, HEAD, gate="pm", lens="pavo").ok

    assert _actions(rec) == [
        ("signed_off", HEAD), ("sign_off_failed", HEAD), ("signed_off", HEAD),
    ], _actions(rec)
    # And once recorded again, a repeat of the same success adds nothing.
    assert _sign(store, HEAD, gate="pm", lens="pavo").ok
    assert len(rec.owner_history) == 3


def test_a_genuine_replay_of_the_same_failed_attempt_is_one_entry(monkeypatch):
    rec = _Record({"pm": "pending"})
    store = _wire(monkeypatch, rec)
    state = _run(store.load("o/r", 795))
    kwargs = dict(
        repo="o/r", issue_number=795, gate="pm", lens_agent="pavo",
        lens_sub="pavo@ateles-swarm", error="sign_off: signed write failed",
        observed="pending", idempotency_suffix="k", head_sha=HEAD,
        attempt="2026-10-15T12:00:00.000001+00:00",
    )

    _run(store._record_failed_sign_off(state, **kwargs))
    state = _run(store.load("o/r", 795))
    _run(store._record_failed_sign_off(state, **kwargs))  # the transport replayed it
    state = _run(store.load("o/r", 795))
    _run(store._record_failed_sign_off(state, **dict(kwargs, attempt="2026-10-15T12:00:09.000001+00:00")))

    notes = [e for e in rec.owner_history if e["action"] == "sign_off_failed"]
    assert len(notes) == 2, "the replay was one entry; the NEW attempt is another"


def test_repeated_failures_grow_linearly_not_exponentially(monkeypatch):
    rec = _Record({"pm": "pending"}, fail_fields=frozenset({gate_waive.GATE_STATUS_FIELD}))
    store = _wire(monkeypatch, rec)
    for _ in range(8):
        assert not _sign(store, gate="pm", lens="pavo").ok
    assert len(rec.owner_history) == 16  # a signed_off and a failure note per attempt
    assert rec.owner_history[-1]["action"] == "sign_off_failed"


# ── a bounded page is never proof that no history correction exists ──────────


def test_an_older_history_correction_behind_many_newer_observations_is_not_missed(monkeypatch):
    """Re-signing the same commit keeps adding gate observations. After enough of
    them the history correction is older than any fixed window, and a different
    commit must STILL add one entry, not resend the snapshot plus the new one."""
    rec = _Record({"arch": "pending"})
    store = _wire(monkeypatch, rec)
    a, b = _heads(2)

    assert _sign(store, a).ok
    for _ in range(gate_waive.OBSERVATION_PAGE_SIZE * 3 + 5):  # >500 newer observations
        assert _sign(store, a).ok
    assert len(rec.observations) > 500

    assert _sign(store, b).ok

    history_writes = [v for f, v, _ in rec.writes if f == "owner_history"]
    assert [len(v) for v in history_writes] == [1, 1], history_writes
    assert len(rec.owner_history) == 2, rec.owner_history
    # The gate is still re-signed and attributed by the lens's own fresh write.
    assert rec.gate_status["arch"] == "signed_off"


def test_an_incomplete_read_is_unknown_never_absent(monkeypatch):
    rec = _Record({"arch": "pending"})
    store = _wire(monkeypatch, rec)
    for head in _heads(3):
        _sign(store, head)

    async def truncated(entity_id, *, limit, offset):
        # Newer filler observations; the history correction lies beyond the page bound.
        filler = [{"id": f"f{offset + i}", "source_priority": 1000, "fields": {"current_owner": "x"}}
                  for i in range(limit)]
        return filler, 100_000

    monkeypatch.setattr(store, "_observations_page", truncated)
    monkeypatch.setattr(gate_waive, "OBSERVATION_MAX_PAGES", 3)
    # The history correction is the OLDEST observation here, behind the page bound.
    tier = _run(store._owner_history_in_correction_tier("ent_1"))
    assert tier is None

    async def failing(entity_id, *, limit, offset):
        return None

    monkeypatch.setattr(store, "_observations_page", failing)
    assert _run(store._owner_history_in_correction_tier("ent_1")) is None

    async def exhausted(entity_id, *, limit, offset):
        return [], 0

    monkeypatch.setattr(store, "_observations_page", exhausted)
    assert _run(store._owner_history_in_correction_tier("ent_1")) is False  # proven: total 0


def test_planted_red_a_fixed_window_misses_the_older_history_correction(monkeypatch):
    """The rejected read: the newest 500 observations, an empty answer meaning 'absent'."""

    async def windowed(self, entity_id):
        rows = await self._fetch_observations(entity_id, limit=500)
        return any(
            isinstance(o.get("fields"), dict) and "owner_history" in o["fields"] for o in rows
        )

    monkeypatch.setattr(IssueGateStore, "_owner_history_in_correction_tier", windowed)
    rec = _Record({"arch": "pending"})
    store = _wire(monkeypatch, rec)
    a, b = _heads(2)
    _sign(store, a)
    for _ in range(520):
        _sign(store, a)
    _sign(store, b)
    assert len(rec.owner_history) == 3, "the window no longer hides the older correction"


# ── an already-doubled array does not mislead, and does not double again ─────


def _doubled(n: int = 1023) -> list[dict]:
    triaged = {"agent": "lanius", "action": "triaged", "at": "2026-10-14T10:00:00Z"}
    other = {"actor": "pavo", "gate": "pm", "action": "signed_off", "commit": "a" * 40}
    return [dict(triaged) if i % 2 == 0 else dict(other) for i in range(n)]


def test_the_reader_treats_exact_duplicates_as_one():
    raw = _doubled()
    assert len(raw) == 1023
    parsed = parse_owner_history(raw)
    assert len(parsed) == 2
    assert parsed[0]["action"] == "triaged"
    # Also through a JSON string, the other stored shape.
    assert len(parse_owner_history(json.dumps(raw))) == 2
    assert dedupe_exact([]) == []


def test_a_sign_off_on_a_doubled_entity_writes_one_entry_not_the_array(monkeypatch):
    rec = _Record({"arch": "pending"})
    rec._history_obs.append((1000, _doubled(1023)))
    rec.observations.append(
        {
            "id": "obs-old",
            "created_at": "2020-01-01T00:00:00+00:00",
            "source_priority": 1000,
            "fields": {"owner_history": _doubled(1023)},
        }
    )
    store = _wire(monkeypatch, rec)

    assert _sign(store).ok

    (write,) = [v for f, v, _ in rec.writes if f == "owner_history"]
    assert len(write) == 1, f"the write carried {len(write)} entries"
    assert len(rec.owner_history) == 1024, len(rec.owner_history)


# ── planted red: the old writer doubles ──────────────────────────────────────


def _install_old_writer(monkeypatch):
    """The behaviour this fixes: resend the history that was read, plus one entry."""

    async def old(self, entity_id, current, entries):
        return list(current) + list(entries)

    monkeypatch.setattr(IssueGateStore, "_history_write_value", old)


def test_planted_red_the_old_writer_doubles_on_every_sign_off(monkeypatch):
    _install_old_writer(monkeypatch)
    rec = _Record({"arch": "pending"})
    store = _wire(monkeypatch, rec)

    for head in _heads(10):
        _sign(store, head)

    assert len(rec.owner_history) == 2**10 - 1, len(rec.owner_history)


def test_planted_red_the_old_writer_duplicates_an_identical_sign_off(monkeypatch):
    _install_old_writer(monkeypatch)
    rec = _Record({"arch": "pending"})
    store = _wire(monkeypatch, rec)
    for _ in range(4):
        _sign(store)
    assert len(rec.owner_history) > 1


# ── entry identity ───────────────────────────────────────────────────────────


def test_the_timestamp_is_not_part_of_an_entrys_identity():
    a = {"gate": "arch", "action": "signed_off", "actor": "waxwing", "commit": "c1", "timestamp": "t1"}
    b = dict(a, timestamp="t2")
    c = dict(a, commit="c2")
    assert history_entry_identity(a) == history_entry_identity(b)
    assert history_entry_identity(a) != history_entry_identity(c)
    (only,) = new_history_entries([a], [b, c, c])
    assert only == {**c, "entry_id": only["entry_id"]} and len(only["entry_id"]) == 16
    # The id follows the identity, never the timestamp.
    assert with_entry_id(a)["entry_id"] == with_entry_id(b)["entry_id"] != with_entry_id(c)["entry_id"]


# ── the failure reason is a code, never the text ─────────────────────────────

HEADER_TEXT = "Bearer sk-live-should-never-appear"
KEY_PATH = "/Users/someone/keys/waxwing.jwk.json"
BODY_MARK = "BODY-MARKER-should-never-appear"


@pytest.mark.parametrize(
    "exc,code",
    [
        (ns.SignedRequestError(f"no AAuth key {KEY_PATH}", "no_key"), "no_key"),
        (ns.SignedRequestError(f"non-JSON {HEADER_TEXT}", "helper_non_json"), "helper_non_json"),
        (ns.SignedRequestError(f"fetch failed {BODY_MARK}", "transport_error"), "transport_error"),
        (ns.SignedRequestError(f"x {KEY_PATH}", "helper_error"), "helper_error"),
        (subprocess.TimeoutExpired(cmd=["node", KEY_PATH], timeout=20), "helper_timeout"),
        (FileNotFoundError(KEY_PATH), "helper_unavailable"),
        (RuntimeError(f"{HEADER_TEXT} {KEY_PATH}"), "unexpected"),
        (ValueError(BODY_MARK), "unexpected"),
        (AssertionError("undeclared"), "undeclared_field"),
    ],
)
def test_failures_are_logged_as_a_code_with_no_body_header_or_path(monkeypatch, caplog, exc, code):
    rec = _Record({"arch": "pending"})
    store = _wire(monkeypatch, rec)

    def boom(*a, **k):
        raise exc

    monkeypatch.setattr("gate_waive._ns.signed_request", boom)
    with caplog.at_level(logging.ERROR, logger="apis.gate_waive"):
        outcome = _sign(store)

    assert not outcome.ok
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert f"reason={code}" in text, text
    assert "value_bytes=" in text
    for forbidden in (HEADER_TEXT, KEY_PATH, BODY_MARK, "Bearer", ".jwk.json"):
        assert forbidden not in text, f"{forbidden!r} leaked into the log"


@pytest.mark.parametrize(
    "status,code",
    [
        (413, "payload_too_large"),
        (401, "unauthorized"),
        (403, "unauthorized"),
        (409, "conflict"),
        (404, "not_found"),
        (400, "rejected"),
        (422, "rejected"),
        (500, "server_error"),
        (503, "server_error"),
    ],
)
def test_http_failures_get_a_fixed_reason_code(status, code):
    assert http_failure_code(status) == code


def test_a_413_is_logged_as_payload_too_large_with_the_size(monkeypatch, caplog):
    rec = _Record({"arch": "pending"})
    store = _wire(monkeypatch, rec)
    monkeypatch.setattr("gate_waive._ns.signed_request", lambda *a, **k: (413, {}))
    with caplog.at_level(logging.ERROR, logger="apis.gate_waive"):
        _sign(store)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "HTTP 413" in text and "reason=payload_too_large" in text
    assert re.search(r"value_bytes=\d+", text)


def test_signed_failure_code_ignores_a_message_that_looks_like_a_code():
    assert signed_failure_code(RuntimeError("no_key")) == "unexpected"


# ── signed_request raises coded errors, with no live signing ─────────────────


def _synthetic_signed_helper(monkeypatch, directory):
    """The actual Node helper, with an inert signer and non-sensitive metadata."""
    module = directory / "dist" / "cli" / "aauth_signer.js"
    module.parent.mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text('{"type":"module"}')
    module.write_text('''import fs from "node:fs";
import path from "node:path";
export async function cliSignedFetch(url, options) {
  const marker="synthetic-body-sentinel";
  fs.writeFileSync(process.env.ATELES_TEST_HELPER_TRACE,JSON.stringify({
    pid:process.pid,helper:path.basename(process.argv[1]),received_body:options.body,
    argv_lengths:process.argv.map(value=>value.length),
    argv_contains_body:process.argv.some(value=>value.includes(marker)),
    env_max_length:Math.max(...Object.values(process.env).map(value=>value.length)),
    env_contains_body:Object.values(process.env).some(value=>value.includes(marker))
  }));
  process.stdout.write(process.env.ATELES_TEST_HELPER_OUTPUT);
  process.exit(0);
}
''')
    trace = directory / "helper-observation.json"
    monkeypatch.setattr(ns, "NEOTOMA_RC_DIR", str(directory))
    monkeypatch.setattr(ns, "AAUTH_KEYS_DIR", str(directory))
    monkeypatch.setenv("ATELES_TEST_HELPER_TRACE", str(trace))
    monkeypatch.setenv("ATELES_TEST_HELPER_OUTPUT", '{"status":200,"ok":true,"body":"{}"}')
    return trace


def _stdin_contract_problems(observed, expected_body):
    problems = []
    if observed["helper"] != "signed_fetch.mjs" or json.loads(observed["received_body"]) != expected_body:
        problems.append("body did not reach the original helper")
    if len(observed["received_body"]) <= 250_000:
        problems.append("large-body positive was not exercised")
    if any(length >= 1000 for length in observed["argv_lengths"]) or observed["argv_contains_body"]:
        problems.append("argv exposes body")
    if observed["env_max_length"] >= 100_000 or observed["env_contains_body"]:
        problems.append("environment exposes body")
    return problems


_REAL_POPEN = subprocess.Popen


def _popen_with_planted_body_exposure(command, **kwargs):
    """Real child negative control; no fake CompletedProcess or real signer."""
    import os
    target = os.environ["ATELES_TEST_EXPOSURE_TARGET"]
    if target == "argv":
        command = [*command, "synthetic-body-sentinel"]
    else:
        kwargs["env"] = dict(kwargs["env"], ATELES_TEST_PLANTED_BODY="synthetic-body-sentinel")
    return _REAL_POPEN(command, **kwargs)


def test_signed_request_codes_each_failure_without_signing_anything(monkeypatch, tmp_path):
    # Real lookup, owned process and original helper; no key material/network.
    trace = _synthetic_signed_helper(monkeypatch, tmp_path)
    with pytest.raises(ns.SignedRequestError) as raised:
        ns.signed_request("POST", "http://x/correct", {"a": 1}, agent_name="waxwing")
    assert raised.value.code == "no_key"
    assert not trace.exists(), "a missing key must not reach the helper"

    (tmp_path / "waxwing.jwk.json").write_text('{"kid":"synthetic"}')
    for stdout, code in (
        ("not json", "helper_non_json"),
        ('{"error": "fetch failed: connect ECONNRESET"}', "transport_error"),
        ('{"error": "cliSignedFetch not found under /x/dist"}', "helper_signer_missing"),
        ('{"error": "something else entirely"}', "helper_error"),
    ):
        trace.unlink(missing_ok=True)
        monkeypatch.setenv("ATELES_TEST_HELPER_OUTPUT", stdout)
        with pytest.raises(ns.SignedRequestError) as raised:
            ns.signed_request("POST", "http://x/correct", {"a": 1}, agent_name="waxwing")
        assert raised.value.code == code, (stdout, raised.value.code)
        assert isinstance(raised.value, RuntimeError), "callers that catch RuntimeError must still work"
        assert json.loads(trace.read_text())["helper"] == "signed_fetch.mjs"


def test_the_body_goes_to_the_helper_on_stdin_not_argv_or_the_environment(monkeypatch, tmp_path):
    """So its size cannot meet an argv or environment limit."""
    trace = _synthetic_signed_helper(monkeypatch, tmp_path)
    (tmp_path / "waxwing.jwk.json").write_text('{"kid":"synthetic"}')
    big = {"value": ["synthetic-body-sentinel" + "x" * 100] * 3000}
    status, _ = ns.signed_request("POST", "http://x/correct", big, agent_name="waxwing")
    assert status == 200
    assert _stdin_contract_problems(json.loads(trace.read_text()), big) == []


@pytest.mark.parametrize("target", ["argv", "environment"])
def test_stdin_contract_detects_real_partial_body_exposure(monkeypatch, tmp_path, target):
    trace = _synthetic_signed_helper(monkeypatch, tmp_path)
    (tmp_path / "waxwing.jwk.json").write_text('{"kid":"synthetic"}')
    monkeypatch.setenv("ATELES_TEST_EXPOSURE_TARGET", target)
    monkeypatch.setattr(ns.subprocess, "Popen", _popen_with_planted_body_exposure)
    big = {"value": ["synthetic-body-sentinel" + "x" * 100] * 3000}
    status, _ = ns.signed_request("POST", "http://fixture.invalid", big, agent_name="waxwing")
    assert status == 200  # the negative actually reached a real child
    problems = _stdin_contract_problems(json.loads(trace.read_text()), big)
    assert ("argv exposes body" if target == "argv" else "environment exposes body") in problems
