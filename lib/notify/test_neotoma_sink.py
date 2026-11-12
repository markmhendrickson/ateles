"""Tests for the Neotoma alert sink (lib/notify/neotoma_sink.py).

No live Neotoma and no network: the HTTP calls are replaced by a fake that,
like a real server, REPLAYS the first result for a repeated idempotency key.
The sink directory is private per test (repo-root conftest).
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.notify import Notifier, Priority  # noqa: E402
from lib.notify import neotoma_sink as sink  # noqa: E402

NO_SILENCE = {"silence_start": "", "silence_end": "", "timezone": "Europe/Madrid"}
FAKE_CRED = ("https://neotoma.invalid", "fake-credential-value")


def _read(prefix: str) -> list[dict]:
    rows: list[dict] = []
    for p in sorted(sink.sink_dir().glob(f"{prefix}-*.jsonl")):
        rows += [json.loads(line) for line in p.read_text().splitlines()]
    return rows


class FakeHttp:
    """Fake transport; replays the old result for a repeated idempotency key."""

    def __init__(self, fail=None, delay=0.0, entity_id=None):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail
        self.delay = delay
        self.fixed_id = entity_id
        self.seen: dict[str, dict] = {}
        self.n = 0

    def __call__(self, url, token, payload):
        self.calls.append((url, payload))
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise self.fail
        idem = payload.get("idempotency_key")
        if idem in self.seen:
            return self.seen[idem]  # replay: the old result, no new write
        self.n += 1
        out = {"entities": [{"entity_id": self.fixed_id or f"ent_fake{self.n}"}]}
        self.seen[idem] = out
        return out

    def stores(self):
        return [p for u, p in self.calls if u.endswith("/store")]

    def corrections(self):
        return [p for u, p in self.calls if u.endswith("/correct")]


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(sink.MODE_ENV, "on")
    monkeypatch.setattr(sink, "_credentials", lambda: FAKE_CRED)
    fake = FakeHttp()
    monkeypatch.setattr(sink, "_http_post", fake)
    monkeypatch.setattr(sink, "_http_get", lambda url, token: {"snapshot": {"status": "open"}})
    return fake


# ── mapping ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "level,severity",
    [("critical", "critical"), ("blocker", "error"), ("operator_decision", "warning"), ("warn", "warning"), ("info", "info")],
)
def test_level_maps_to_severity_and_tags(on, level, severity):
    sink.record(message="disk low", level=level, handler="Apis", dedupe_key="k")
    sink.flush()
    ent = on.calls[0][1]["entities"][0]
    assert on.calls[0][0].endswith("/store")
    assert ent["entity_type"] == "escalation"
    assert ent["severity"] == severity
    assert ent["status"] == "open"
    assert ent["source_agent"] == "apis@ateles-swarm"
    assert ent["title"] == f"[apis/{level}] disk low"
    assert ent["body"] == f"{sink.UNTRUSTED_MARKER}\ndisk low"
    assert ent["tags"][:3] == ["notify", f"level:{level}", "daemon:apis"]
    assert ent["tags"][3].startswith("shape:")
    assert set(ent) == {"entity_type", "title", "body", "severity", "source_agent", "status", "tags"}


def test_every_notifier_priority_reaches_the_sink_in_shadow(monkeypatch):
    n = Notifier(rubric=NO_SILENCE)
    for prio in Priority:
        n.send(f"smoke {prio.value}", priority=prio, handler="test")
    assert sorted(r["level"] for r in _read("shadow")) == sorted(p.value for p in Priority)


# ── dedup key, title ─────────────────────────────────────────────────────────


def test_derived_key_is_stable_across_digits_and_hex():
    a = sink.derive_key("apis", "PR #1234 failed at 3f9a2b7c1d after 17s")
    b = sink.derive_key("apis", "PR #99 failed at 00ff11aa22 after 5s")
    assert a == b
    assert a != sink.derive_key("apis", "PR #1234 passed")
    assert a != sink.derive_key("formica", "PR #1234 failed at 3f9a2b7c1d after 17s")


def test_two_entity_ids_collapse_to_one_key():
    a = sink.derive_key("apis", "Task ent_99ace4dd6673aa36ed08b1fe stalled 42m")
    b = sink.derive_key("apis", "Task ent_abcdef0123456789abcdef01 stalled 17m")
    c = sink.derive_key("apis", "Task ent_abcdefabcdefabcdefabcdef stalled 3m")  # letters only
    assert a == b == c and "ent_#" in a and "99ace" not in a
    u1 = sink.derive_key("apis", "run 123e4567-e89b-12d3-a456-426614174000 failed")
    u2 = sink.derive_key("apis", "run 00000000-e89b-12d3-a456-426614174999 failed")
    assert u1 == u2


def test_title_is_the_readable_first_occurrence_and_stays_stable(on):
    sink.record(message="PR #1723 blocked: CI red\nsecond line", level="blocker", handler="apis")
    sink.record(message="PR #1724 blocked: CI red\nsecond line", level="blocker", handler="apis")
    sink.flush()
    assert len(on.stores()) == 1  # same shape, collapsed
    assert on.stores()[0]["entities"][0]["title"] == "[apis/blocker] PR #1723 blocked: CI red"


def test_caller_dedupe_key_groups_alerts_and_title_stays_readable(on):
    sink.record(message="Consent channel down since 10:01", level="warn", handler="monedula", dedupe_key="monedula:consent")
    sink.record(message="Consent channel down since 10:42", level="warn", handler="monedula", dedupe_key="monedula:consent")
    sink.flush()
    assert [p["entities"][0]["title"] for p in on.stores()] == ["[monedula/warn] Consent channel down since 10:01"]


def test_at_most_one_write_per_key_per_window(on):
    for i in range(5):
        sink.record(message=f"disk {i} GB free", level="warn", handler="apis")
    sink.flush()
    assert len(on.calls) == 1


def test_window_expiry_allows_another_write(on, monkeypatch):
    monkeypatch.setenv(sink.WINDOW_ENV, "0.05")
    sink.record(message="m", level="warn", handler="apis", dedupe_key="k")
    sink.flush()
    time.sleep(0.1)
    sink.record(message="m", level="warn", handler="apis", dedupe_key="k")
    sink.flush()
    assert len(on.stores()) == 2


def test_window_survives_a_restart(on):
    sink.record(message="m", level="warn", handler="a", dedupe_key="k")
    sink.flush()
    sink._reset_for_tests()
    sink.record(message="m", level="warn", handler="a", dedupe_key="k")
    sink.flush()
    assert len(on.stores()) == 1


def test_repeats_are_counted_in_the_next_write(on, monkeypatch):
    monkeypatch.setenv(sink.WINDOW_ENV, "0.05")
    sink.record(message="m", level="warn", handler="a", dedupe_key="k")
    sink.record(message="m", level="warn", handler="a", dedupe_key="k")
    sink.record(message="m", level="warn", handler="a", dedupe_key="k")
    sink.flush()
    time.sleep(0.1)
    sink.record(message="m", level="warn", handler="a", dedupe_key="k")
    sink.flush()
    assert "2 similar alert(s) suppressed" in on.stores()[1]["entities"][0]["body"]


# ── clear_dedupe, generations, expiry ────────────────────────────────────────


def test_clear_dedupe_resolves_the_stored_entity(on):
    n = Notifier(rubric=NO_SILENCE)
    n.send("channel down", priority=Priority.BLOCKER, handler="monedula", dedupe_key="monedula:chan")
    sink.flush()
    n.clear_dedupe("monedula:chan")
    sink.flush()
    corrections = on.corrections()
    assert [(c["field"], c["value"]) for c in corrections] == [
        ("status", "resolved"),
        ("resolved_at", corrections[1]["value"]),
    ]
    assert all(c["entity_id"] == "ent_fake1" for c in corrections)


def test_clear_dedupe_after_a_restart_still_resolves(on):
    sink.record(message="gate failing", level="blocker", handler="apis", dedupe_key="apis:gate")
    sink.flush()
    sink._reset_for_tests()  # a restart: only the state files survive
    Notifier(rubric=NO_SILENCE).clear_dedupe("apis:gate")
    sink.flush()
    assert [c["field"] for c in on.corrections()] == ["status", "resolved_at"]


def test_clear_dedupe_for_an_unknown_key_writes_nothing(on):
    Notifier(rubric=NO_SILENCE).clear_dedupe("never-sent")
    sink.flush()
    assert on.calls == []


def test_recurrence_after_resolve_in_the_same_window_reaches_a_replaying_server(on):
    sink.record(message="m", level="warn", handler="a", dedupe_key="k")
    sink.flush()
    sink.resolve("k")
    sink.flush()
    sink.record(message="m", level="warn", handler="a", dedupe_key="k")
    sink.flush()
    stores = on.stores()
    assert len(stores) == 2
    assert stores[0]["idempotency_key"] != stores[1]["idempotency_key"]
    assert stores[1]["entities"][0]["title"] == "[a/warn] m (#2)"
    assert len({r["entity_id"] for r in _read("stored")}) == 2  # the replaying fake made a NEW entity


def test_a_hand_resolved_entity_is_not_reopened(on, monkeypatch):
    monkeypatch.setenv(sink.WINDOW_ENV, "0.05")
    sink.record(message="still bad", level="warn", handler="a", dedupe_key="k")
    sink.flush()
    time.sleep(0.1)
    monkeypatch.setattr(sink, "_http_get", lambda url, token: {"snapshot": {"status": "resolved"}})
    sink.record(message="still bad", level="warn", handler="a", dedupe_key="k")
    sink.flush()
    assert [p["entities"][0]["title"] for p in on.stores()] == ["[a/warn] still bad", "[a/warn] still bad (#2)"]


def test_a_still_open_entity_is_updated_in_place_after_the_window(on, monkeypatch):
    monkeypatch.setenv(sink.WINDOW_ENV, "0.05")
    sink.record(message="still bad", level="warn", handler="a", dedupe_key="k")
    sink.flush()
    time.sleep(0.1)
    sink.record(message="still bad", level="warn", handler="a", dedupe_key="k")
    sink.flush()
    stores = on.stores()
    assert len({p["entities"][0]["title"] for p in stores}) == 1
    assert len({p["idempotency_key"] for p in stores}) == 2  # a replay must not swallow the update
    assert len({r["entity_id"] for r in _read("stored")}) == 2  # the replaying fake saw two real writes


def test_keyless_alerts_auto_resolve_after_expiry_but_keyed_ones_do_not(on, monkeypatch):
    monkeypatch.setenv(sink.EXPIRE_ENV, "0.05")
    sink.record(message="keyless thing", level="warn", handler="a")
    sink.record(message="keyed thing", level="warn", handler="a", dedupe_key="keyed")
    sink.flush()
    time.sleep(0.1)
    sink._last_maintenance_expire = 0.0
    sink.record(message="another", level="warn", handler="a")
    sink.flush()
    resolved = [c for c in on.corrections() if c["field"] == "status"]
    assert len(resolved) == 1 and resolved[0]["entity_id"] == "ent_fake1"


# ── modes ────────────────────────────────────────────────────────────────────


def test_shadow_writes_the_file_and_makes_no_network_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network call in shadow mode")

    monkeypatch.setattr(sink, "_http_post", boom)
    monkeypatch.setattr(sink, "_http_get", boom)
    monkeypatch.setattr(sink, "_credentials", lambda: FAKE_CRED)
    monkeypatch.delenv(sink.MODE_ENV, raising=False)  # default is shadow
    sink.record(message="hello", level="warn", handler="apis")
    sink.flush()
    rows = _read("shadow")
    assert len(rows) == 1 and rows[0]["entity"]["body"].endswith("hello")
    path = next(sink.sink_dir().glob("shadow-*.jsonl"))
    assert path.stat().st_mode & 0o777 == 0o600
    assert _read("spool") == []


def test_off_writes_nothing(monkeypatch):
    monkeypatch.setenv(sink.MODE_ENV, "off")
    monkeypatch.setattr(sink, "_http_post", lambda *a: pytest.fail("network call while off"))
    sink.record(message="hello", level="critical", handler="apis")
    sink.flush()
    assert list(sink.sink_dir().glob("*")) == []


def test_unknown_mode_falls_back_to_shadow(monkeypatch):
    monkeypatch.setenv(sink.MODE_ENV, "bogus")
    assert sink.mode() == "shadow"


# ── failure spools, never raises ─────────────────────────────────────────────


def test_on_without_credential_spools(monkeypatch):
    monkeypatch.setenv(sink.MODE_ENV, "on")
    monkeypatch.setattr(sink, "_credentials", lambda: ("https://x.invalid", ""))
    monkeypatch.setattr(sink, "_http_post", lambda *a: pytest.fail("no credential, no call"))
    sink.record(message="m", level="blocker", handler="cotinga")
    sink.flush()
    rows = _read("spool")
    assert len(rows) == 1 and rows[0]["spool_reason"] == "no-credential"
    path = next(sink.sink_dir().glob("spool-*.jsonl"))
    assert path.stat().st_mode & 0o777 == 0o600


def test_http_failure_spools(on, monkeypatch):
    monkeypatch.setattr(sink, "_http_post", FakeHttp(fail=RuntimeError("500")))
    sink.record(message="m", level="blocker", handler="apis")
    sink.flush()
    assert [r["spool_reason"] for r in _read("spool")] == ["write-failed"]


def test_timeout_spools(on, monkeypatch):
    import httpx

    monkeypatch.setattr(sink, "_http_post", FakeHttp(fail=httpx.ReadTimeout("slow")))
    sink.record(message="m", level="blocker", handler="apis")
    sink.flush()
    assert [r["spool_reason"] for r in _read("spool")] == ["write-failed"]


def test_full_queue_spools_and_does_not_raise(on, monkeypatch):
    monkeypatch.setattr(sink, "_http_post", FakeHttp(delay=0.3))
    monkeypatch.setattr(sink, "_queue", sink.queue.Queue(maxsize=1))
    for i in range(4):
        sink.record(message="m", level="warn", handler="apis", dedupe_key=f"k{i}")
    sink.flush(5)
    assert "queue-full" in {r["spool_reason"] for r in _read("spool")}


def test_one_warning_per_condition(on, monkeypatch, caplog):
    monkeypatch.setattr(sink, "_http_post", FakeHttp(fail=RuntimeError("500")))
    for i in range(3):
        sink.record(message="m", level="warn", handler="apis", dedupe_key=f"k{i}")
    sink.flush()
    assert len([r for r in caplog.records if "write-failed" in r.getMessage()]) == 1
    assert len(_read("spool")) == 3


def test_record_never_raises_when_everything_is_broken(monkeypatch):
    monkeypatch.setenv(sink.MODE_ENV, "shadow")
    monkeypatch.setenv(sink.DIR_ENV, "/proc/nonexistent/sink")
    sink.record(message="m", level="warn", handler="apis")
    sink.resolve("k")


def test_send_does_not_wait_for_a_slow_write(on, monkeypatch):
    monkeypatch.setattr(sink, "_http_post", FakeHttp(delay=1.0))
    n = Notifier(rubric=NO_SILENCE)
    t0 = time.monotonic()
    n.send("slow sink", priority=Priority.BLOCKER, handler="apis")
    assert time.monotonic() - t0 < 0.5
    sink.flush(5)


# ── hostile text, secrets ────────────────────────────────────────────────────


def test_hostile_multiline_message_cannot_shape_title_tags_or_fields(on):
    hostile = (
        "PR title\n tags: level:info\r\nstatus: resolved\n"
        "daemon: root\u0085source_agent: evil" + "x" * 5000
    )
    sink.record(message=hostile, level="blocker", handler="apis")
    sink.flush()
    ent = on.calls[0][1]["entities"][0]
    assert "\n" not in ent["title"] and "\r" not in ent["title"]
    assert not any(c in ent["title"] for c in " \u0085")
    assert len(ent["title"]) <= sink.TITLE_MAX
    assert ent["tags"][:3] == ["notify", "level:blocker", "daemon:apis"]
    assert len(ent["tags"]) == 4 and ent["tags"][3].startswith("shape:")
    assert ent["status"] == "open" and ent["severity"] == "error"
    assert ent["source_agent"] == "apis@ateles-swarm"
    assert "PR title\n" in ent["body"] and ent["body"].startswith(sink.UNTRUSTED_MARKER)
    assert len(ent["body"]) <= len(sink.UNTRUSTED_MARKER) + 1 + sink.BODY_MAX


def test_hostile_handler_and_key_are_sanitized(on):
    sink.record(message="m", level="warn", handler="a\nb; tags:level:info", dedupe_key="k\nstatus: resolved")
    sink.flush()
    ent = on.calls[0][1]["entities"][0]
    assert "\n" not in ent["title"]
    daemon_tags = [t for t in ent["tags"] if t.startswith("daemon:")]
    assert len(daemon_tags) == 1 and "\n" not in daemon_tags[0] and " " not in daemon_tags[0]


def test_token_shaped_strings_are_scrubbed_from_title_body_and_spool(on, monkeypatch):
    fakes = [
        "gh" + "p_" + "a" * 36,
        "github_" + "pat_" + "b" * 30,
        "s" + "k-" + "c" * 24,
        "xo" + "xb-" + "1234567890-abc",
        "Bear" + "er " + "d" * 20,
    ]
    msg = " ".join(fakes)
    sink.record(message=msg, level="warn", handler="apis")
    sink.flush()
    ent = on.stores()[0]["entities"][0]
    for f in fakes:
        assert f not in ent["title"] and f not in ent["body"]
    assert "[redacted]" in ent["body"]
    monkeypatch.setattr(sink, "_http_post", FakeHttp(fail=RuntimeError("x")))
    sink.record(message=msg + " again", level="warn", handler="apis")
    sink.flush()
    assert not any(f in json.dumps(_read("spool")) for f in fakes)


# ── growth bounds ────────────────────────────────────────────────────────────


def test_flood_of_distinct_keys_keeps_state_and_files_bounded(monkeypatch):
    monkeypatch.setattr(sink, "MAX_NEW_KEYS_PER_HOUR", 10**6)
    monkeypatch.setattr(sink, "STATE_MAX_KEYS", 50)
    monkeypatch.setattr(sink, "FILE_MAX_BYTES", 20000)
    for i in range(400):
        sink.record(message=f"alert {i} " + "p" * 500, level="warn", handler="apis", dedupe_key=f"flood-{i}")
    state = json.loads((sink.sink_dir() / "state-shadow-apis.json").read_text())
    assert len(state["entries"]) <= 50
    shadow = next(sink.sink_dir().glob("shadow-*.jsonl"))
    assert shadow.stat().st_size <= 20000


def test_new_key_rate_is_capped_per_daemon_per_hour(on, monkeypatch):
    monkeypatch.setattr(sink, "MAX_NEW_KEYS_PER_HOUR", 5)
    for i in range(30):
        sink.record(message=f"m{i}", level="warn", handler="apis", dedupe_key=f"k{i}")
    sink.flush()
    assert len(on.stores()) == 5


def test_old_sink_files_are_pruned():
    d = sink.sink_dir()
    d.mkdir(parents=True, exist_ok=True)
    old = d / "shadow-20200101.jsonl"
    old.write_text("{}\n")
    os.utime(old, (1, 1))
    sink._reset_for_tests()
    sink.record(message="m", level="warn", handler="apis")
    assert not old.exists()


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_summary_cli_counts_by_day_level_daemon_and_top_titles(monkeypatch, capsys):
    sink.record(message="a", level="warn", handler="apis")
    sink.record(message="b", level="info", handler="apis")
    sink.record(message="c", level="warn", handler="formica")
    assert sink.main(["--summary"]) == 0
    out = capsys.readouterr().out
    assert "3 rows" in out and "top 10 titles" in out and "would store" in out
    assert "[apis/warn] a" in out and "formica" in out and "info" in out
    assert sink.main([]) == 2
