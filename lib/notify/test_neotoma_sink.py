"""Tests for the Neotoma alert sink (lib/notify/neotoma_sink.py).

No live Neotoma and no network: the HTTP call is replaced by a fake, and the
credential pair by a monkeypatched accessor. The sink directory is private per
test (repo-root conftest).
"""

import json
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
ALWAYS_SILENT = {"silence_start": "00:00", "silence_end": "23:59", "timezone": "Europe/Madrid"}
FAKE_CRED = ("https://neotoma.invalid", "fake-credential-value")


def _read(prefix: str) -> list[dict]:
    rows: list[dict] = []
    for p in sorted(sink.sink_dir().glob(f"{prefix}-*.jsonl")):
        rows += [json.loads(line) for line in p.read_text().splitlines()]
    return rows


class FakeHttp:
    def __init__(self, fail=None, delay=0.0, entity_id="ent_fake1"):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail
        self.delay = delay
        self.entity_id = entity_id

    def __call__(self, url, token, payload):
        self.calls.append((url, payload))
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise self.fail
        return {"entities": [{"entity_id": self.entity_id}]}


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(sink.MODE_ENV, "on")
    monkeypatch.setattr(sink, "_credentials", lambda: FAKE_CRED)
    fake = FakeHttp()
    monkeypatch.setattr(sink, "_http_post", fake)
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
    assert ent["title"] == "[apis] k"
    assert ent["body"] == "disk low"
    assert {"notify", f"level:{level}", "daemon:apis"} <= set(ent["tags"])
    assert set(ent) == {"entity_type", "title", "body", "severity", "source_agent", "status", "tags"}


def test_every_notifier_priority_reaches_the_sink_in_shadow(monkeypatch):
    n = Notifier(rubric=NO_SILENCE)
    for prio in Priority:
        n.send(f"smoke {prio.value}", priority=prio, handler="test")
    assert sorted(r["level"] for r in _read("shadow")) == sorted(p.value for p in Priority)


# ── dedup key ────────────────────────────────────────────────────────────────


def test_derived_key_is_stable_across_digits_and_hex():
    a = sink.derive_key("apis", "PR #1234 failed at 3f9a2b7c1d after 17s")
    b = sink.derive_key("apis", "PR #99 failed at 00ff11aa22 after 5s")
    assert a == b
    assert a != sink.derive_key("apis", "PR #1234 passed")
    assert a != sink.derive_key("formica", "PR #1234 failed at 3f9a2b7c1d after 17s")


def test_caller_dedupe_key_is_used_in_the_title(on):
    sink.record(message="m", level="warn", handler="monedula", dedupe_key="monedula:consent")
    sink.flush()
    assert on.calls[0][1]["entities"][0]["title"] == "[monedula] monedula:consent"


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
    assert len(on.calls) == 2


# ── clear_dedupe resolves ────────────────────────────────────────────────────


def test_clear_dedupe_resolves_the_stored_entity(on):
    n = Notifier(rubric=NO_SILENCE)
    n.send("channel down", priority=Priority.BLOCKER, handler="monedula", dedupe_key="monedula:chan")
    sink.flush()
    n.clear_dedupe("monedula:chan")
    sink.flush()
    corrections = [p for u, p in on.calls if u.endswith("/correct")]
    assert [(c["field"], c["value"]) for c in corrections] == [
        ("status", "resolved"),
        ("resolved_at", corrections[1]["value"]),
    ]
    assert all(c["entity_id"] == "ent_fake1" for c in corrections)


def test_clear_dedupe_for_an_unknown_key_writes_nothing(on):
    Notifier(rubric=NO_SILENCE).clear_dedupe("never-sent")
    sink.flush()
    assert on.calls == []


def test_recurrence_after_resolve_is_stored_again(on):
    sink.record(message="m", level="warn", handler="a", dedupe_key="k")
    sink.flush()
    sink.resolve("k")
    sink.flush()
    sink.record(message="m", level="warn", handler="a", dedupe_key="k")
    sink.flush()
    assert len([1 for u, _ in on.calls if u.endswith("/store")]) == 2


# ── modes ────────────────────────────────────────────────────────────────────


def test_shadow_writes_the_file_and_makes_no_network_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network call in shadow mode")

    monkeypatch.setattr(sink, "_http_post", boom)
    monkeypatch.setattr(sink, "_credentials", lambda: FAKE_CRED)
    monkeypatch.delenv(sink.MODE_ENV, raising=False)  # default is shadow
    sink.record(message="hello", level="warn", handler="apis")
    rows = _read("shadow")
    assert len(rows) == 1 and rows[0]["entity"]["body"] == "hello"
    path = next(sink.sink_dir().glob("shadow-*.jsonl"))
    assert path.stat().st_mode & 0o777 == 0o600
    assert _read("spool") == []


def test_off_writes_nothing(monkeypatch):
    monkeypatch.setenv(sink.MODE_ENV, "off")
    sink.record(message="hello", level="critical", handler="apis")
    assert list(sink.sink_dir().glob("*.jsonl")) == []


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


# ── hostile text ─────────────────────────────────────────────────────────────


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
    assert ent["tags"] == ["notify", "level:blocker", "daemon:apis"]
    assert ent["status"] == "open" and ent["severity"] == "error"
    assert ent["source_agent"] == "apis@ateles-swarm"
    assert ent["body"].startswith("PR title\n")  # kept as data


def test_hostile_handler_and_key_are_sanitized(on):
    sink.record(message="m", level="warn", handler="a\nb; tags:level:info", dedupe_key="k\nstatus: resolved")
    sink.flush()
    ent = on.calls[0][1]["entities"][0]
    assert "\n" not in ent["title"]
    daemon_tags = [t for t in ent["tags"] if t.startswith("daemon:")]
    assert len(daemon_tags) == 1 and "\n" not in daemon_tags[0] and " " not in daemon_tags[0]


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_summary_cli_counts_by_day_level_and_daemon(monkeypatch, capsys):
    sink.record(message="a", level="warn", handler="apis")
    sink.record(message="b", level="info", handler="apis")
    sink.record(message="c", level="warn", handler="formica")
    assert sink.main(["--summary"]) == 0
    out = capsys.readouterr().out
    assert "3 rows" in out
    assert "warn" in out and "info" in out and "apis" in out and "formica" in out
    assert sink.main([]) == 2
