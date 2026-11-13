"""Cross-process cache and paging for the session-side agent_policy read.

`policy_skill_renderer.fetch_active_policy_rows` is the one helper every
rule-delivery hook goes through. These tests exercise it with a fake
transport (`_request`) or a local fake HTTP server; nothing reaches a real
Neotoma. The repo conftest turns the cache off for every test, so each test
here turns it on against its own temporary directory.
"""

from __future__ import annotations

import http.server
import json
import os
import stat
import threading
import time

import pytest

import policy_skill_renderer as renderer

cache_mod = renderer.policy_fetch_cache

_TOKEN_CANARY = "tok-canary-6f1d0c2a9b"


def _entity(i: int, status: str = "active") -> dict:
    eid = f"ent_policy_{i:05d}"
    return {
        "entity_id": eid,
        "snapshot": {
            "snapshot": {
                "rule": f"Rule {i}.",
                "applies_when": "always",
                "scope": "global",
                "status": status,
                "rule_kind": "mandatory",
            }
        },
    }


class _CountingTransport:
    """Fake `_request`: serves `entities` with offset/limit paging when asked."""

    def __init__(self, entities: list[dict], *, honour_offset: bool = True,
                 report_total: bool = False, total_override: int | None = None):
        self.entities = entities
        self.honour_offset = honour_offset
        self.report_total = report_total
        self.total_override = total_override
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, body, timeout=10.0):
        self.calls.append((url, dict(body)))
        limit = int(body.get("limit") or 200)
        offset = int(body.get("offset") or 0) if self.honour_offset else 0
        page = self.entities[offset:offset + limit]
        out = {"entities": page}
        if self.report_total:
            out["total"] = (
                self.total_override if self.total_override is not None else len(self.entities)
            )
        return out


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "cache" / "policy"
    monkeypatch.setenv(cache_mod.TTL_ENV, "120")
    monkeypatch.setenv(cache_mod.DIR_ENV, str(d))
    return d


@pytest.fixture
def transport(monkeypatch):
    t = _CountingTransport([_entity(i) for i in range(5)])
    monkeypatch.setattr(renderer, "_request", t)
    return t


BASE = "https://neotoma.example.test"


# ---------------------------------------------------------------------------
# Cache hits, expiry and keying
# ---------------------------------------------------------------------------
class TestCacheReuse:
    def test_two_calls_within_ttl_make_one_fetch(self, cache_dir, transport):
        first = renderer.fetch_active_policy_rows(base_url=BASE)
        second = renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 1
        assert first == second
        assert {r["_entity_id"] for r in second} == {f"ent_policy_{i:05d}" for i in range(5)}

    def test_expired_ttl_refetches(self, cache_dir, transport, monkeypatch):
        now = [1_000_000.0]
        monkeypatch.setattr(cache_mod.time, "time", lambda: now[0])
        renderer.fetch_active_policy_rows(base_url=BASE)
        now[0] += 119.0
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 1
        now[0] += 2.0  # 121 s after the fetch
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 2

    def test_entry_stamped_in_the_future_is_a_miss(self, cache_dir, transport, monkeypatch):
        now = [1_000_000.0]
        monkeypatch.setattr(cache_mod.time, "time", lambda: now[0])
        renderer.fetch_active_policy_rows(base_url=BASE)
        now[0] -= 30.0  # clock stepped backwards
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 2

    def test_different_base_url_gets_its_own_entry(self, cache_dir, transport):
        renderer.fetch_active_policy_rows(base_url=BASE)
        renderer.fetch_active_policy_rows(base_url="https://other.example.test")
        assert len(transport.calls) == 2
        renderer.fetch_active_policy_rows(base_url=BASE)
        renderer.fetch_active_policy_rows(base_url="https://other.example.test")
        assert len(transport.calls) == 2
        assert len(list(cache_dir.glob("*.json"))) == 2

    def test_different_body_gets_its_own_entry(self, cache_dir):
        calls = []

        def fetch_a():
            calls.append("a")
            return [{"_entity_id": "a"}]

        def fetch_b():
            calls.append("b")
            return [{"_entity_id": "b"}]

        body_a = {"entity_type": "agent_policy", "limit": 200}
        body_b = {"entity_type": "agent_policy", "limit": 100}
        assert cache_mod.cached_fetch(BASE, body_a, fetch_a) == [{"_entity_id": "a"}]
        assert cache_mod.cached_fetch(BASE, body_b, fetch_b) == [{"_entity_id": "b"}]
        assert cache_mod.cached_fetch(BASE, body_a, fetch_a) == [{"_entity_id": "a"}]
        assert cache_mod.cached_fetch(BASE, body_b, fetch_b) == [{"_entity_id": "b"}]
        assert calls == ["a", "b"]
        assert cache_mod.cache_key(BASE, body_a) != cache_mod.cache_key(BASE, body_b)

    def test_ttl_zero_disables_the_cache_and_creates_nothing(self, cache_dir, transport, monkeypatch):
        monkeypatch.setenv(cache_mod.TTL_ENV, "0")
        renderer.fetch_active_policy_rows(base_url=BASE)
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 2
        assert not cache_dir.exists()

    def test_live_fetch_error_propagates_and_caches_nothing(self, cache_dir, monkeypatch):
        def boom(url, body, timeout=10.0):
            raise OSError("connection refused")

        monkeypatch.setattr(renderer, "_request", boom)
        with pytest.raises(OSError):
            renderer.fetch_active_policy_rows(base_url=BASE)
        assert list(cache_dir.glob("*.json")) == []


# ---------------------------------------------------------------------------
# The token never reaches the cache
# ---------------------------------------------------------------------------
class _Handler(http.server.BaseHTTPRequestHandler):
    entities: list[dict] = []
    auth_seen: list[str] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length) or b"{}")
        self.auth_seen.append(self.headers.get("Authorization", ""))
        offset = int(req.get("offset") or 0)
        limit = int(req.get("limit") or 200)
        body = json.dumps({"entities": self.entities[offset:offset + limit]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: A003
        pass


class TestTokenNeverCached:
    def test_token_is_not_in_the_cache_file_or_its_name(self, cache_dir, monkeypatch):
        handler = type("H", (_Handler,), {"entities": [_entity(i) for i in range(3)],
                                          "auth_seen": []})
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            monkeypatch.setattr(renderer, "NEOTOMA_BEARER_TOKEN", _TOKEN_CANARY)
            base = f"http://127.0.0.1:{server.server_address[1]}"
            renderer.fetch_active_policy_rows(base_url=base, timeout=5)
            renderer.fetch_active_policy_rows(base_url=base, timeout=5)
        finally:
            server.shutdown()
            thread.join(timeout=5)
        # The token really was in flight, once: the second call was a hit.
        assert handler.auth_seen == [f"Bearer {_TOKEN_CANARY}"]
        files = [p for p in cache_dir.rglob("*") if p.is_file()]
        assert files, "expected a cache entry to have been written"
        for path in files:
            assert _TOKEN_CANARY not in path.name
            assert _TOKEN_CANARY.encode() not in path.read_bytes()
        assert _TOKEN_CANARY not in "".join(str(p) for p in cache_dir.rglob("*"))


# ---------------------------------------------------------------------------
# Permissions and refusal
# ---------------------------------------------------------------------------
class TestPermissions:
    def test_entry_is_0600_and_dir_is_0700_whatever_the_umask(self, cache_dir, transport):
        old = os.umask(0)
        try:
            renderer.fetch_active_policy_rows(base_url=BASE)
        finally:
            os.umask(old)
        assert stat.S_IMODE(os.lstat(cache_dir).st_mode) == 0o700
        entries = list(cache_dir.glob("*.json"))
        assert len(entries) == 1
        assert stat.S_IMODE(os.lstat(entries[0]).st_mode) == 0o600
        for lock in cache_dir.glob("*.lock"):
            assert stat.S_IMODE(os.lstat(lock).st_mode) == 0o600
        assert list(cache_dir.glob(".*.tmp")) == []

    def test_default_location_walks_into_a_0700_leaf(self, tmp_path, transport, monkeypatch):
        monkeypatch.setenv(cache_mod.TTL_ENV, "120")
        monkeypatch.delenv(cache_mod.DIR_ENV, raising=False)
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        renderer.fetch_active_policy_rows(base_url=BASE)
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 1
        leaf = tmp_path / "xdg" / "ateles" / "policy"
        assert stat.S_IMODE(os.lstat(leaf).st_mode) == 0o700
        assert len(list(leaf.glob("*.json"))) == 1


class TestRefusal:
    def test_symlinked_cache_dir_is_refused_and_fetches_live(self, tmp_path, transport, monkeypatch):
        real = tmp_path / "elsewhere"
        real.mkdir(mode=0o700)
        link = tmp_path / "link"
        link.symlink_to(real)
        monkeypatch.setenv(cache_mod.TTL_ENV, "120")
        monkeypatch.setenv(cache_mod.DIR_ENV, str(link))
        rows1 = renderer.fetch_active_policy_rows(base_url=BASE)
        rows2 = renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 2  # no caching through the link
        assert len(rows1) == len(rows2) == 5
        assert list(real.iterdir()) == []  # nothing written through it

    def test_symlinked_intermediate_dir_is_refused(self, tmp_path, transport, monkeypatch):
        xdg = tmp_path / "xdg"
        xdg.mkdir()
        real = tmp_path / "elsewhere"
        real.mkdir(mode=0o700)
        (xdg / "ateles").symlink_to(real)
        monkeypatch.setenv(cache_mod.TTL_ENV, "120")
        monkeypatch.delenv(cache_mod.DIR_ENV, raising=False)
        monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))
        renderer.fetch_active_policy_rows(base_url=BASE)
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 2
        assert list(real.iterdir()) == []

    def test_symlinked_entry_is_refused_and_fetches_live(self, cache_dir, transport, monkeypatch):
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 1
        entry = next(cache_dir.glob("*.json"))
        # Move the valid entry aside and point the entry name at it.
        target = cache_dir.parent / "planted.json"
        os.replace(entry, target)
        entry.symlink_to(target)
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 2

    def test_other_owned_cache_dir_is_refused(self, cache_dir, transport, monkeypatch):
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 1
        real_uid = os.getuid()
        monkeypatch.setattr(cache_mod.os, "getuid", lambda: real_uid + 1)
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 2

    def test_group_readable_entry_is_refused(self, cache_dir, transport):
        renderer.fetch_active_policy_rows(base_url=BASE)
        entry = next(cache_dir.glob("*.json"))
        os.chmod(entry, 0o640)
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 2

    def test_open_cache_dir_is_refused(self, cache_dir, transport):
        renderer.fetch_active_policy_rows(base_url=BASE)
        os.chmod(cache_dir, 0o755)
        renderer.fetch_active_policy_rows(base_url=BASE)
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 3

    @pytest.mark.parametrize("content", [
        b"",
        b"{not json",
        b"[]",
        b'{"v": 1, "key": "wrong", "fetched_at": 0, "rows": []}',
        b'{"v": 1}',
    ])
    def test_corrupt_entry_falls_through_and_is_rewritten(self, cache_dir, transport, content):
        renderer.fetch_active_policy_rows(base_url=BASE)
        entry = next(cache_dir.glob("*.json"))
        entry.write_bytes(content)
        rows = renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 2
        assert len(rows) == 5
        # The live result replaced the corrupt entry, so the next call hits.
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 2

    def test_wrong_row_shape_in_a_fresh_entry_falls_through(self, cache_dir, transport):
        renderer.fetch_active_policy_rows(base_url=BASE)
        entry = next(cache_dir.glob("*.json"))
        data = json.loads(entry.read_bytes())
        data["rows"] = ["not a dict"]
        entry.write_text(json.dumps(data))
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 2


# ---------------------------------------------------------------------------
# Single flight: a process waiting on the lock reads what the holder wrote
# ---------------------------------------------------------------------------
class TestSingleFlight:
    def test_waiter_uses_the_entry_written_by_the_lock_holder(self, cache_dir):
        body = {"entity_type": "agent_policy", "limit": 200}
        key = cache_mod.cache_key(BASE, body)
        dir_fd, dir_path = cache_mod._open_cache_dir()
        try:
            lock = cache_mod._Lock(dir_fd, key, 1.0)
            assert lock.acquired
            calls = []
            result = {}

            def waiter():
                result["rows"] = cache_mod.cached_fetch(
                    BASE, body, lambda: calls.append(1) or [{"_entity_id": "live"}]
                )

            t = threading.Thread(target=waiter)
            t.start()
            time.sleep(0.2)
            assert t.is_alive()  # blocked on the lock, not fetching
            cache_mod._write_entry(dir_fd, dir_path, key, [{"_entity_id": "holder"}], time.time())
            lock.release()
            t.join(timeout=10)
        finally:
            os.close(dir_fd)
        assert calls == []
        assert result["rows"] == [{"_entity_id": "holder"}]


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------
class TestPaging:
    def test_paging_past_200_returns_every_row(self, monkeypatch):
        t = _CountingTransport([_entity(i) for i in range(450)])
        monkeypatch.setattr(renderer, "_request", t)
        rows = renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(rows) == 450
        assert {r["_entity_id"] for r in rows} == {f"ent_policy_{i:05d}" for i in range(450)}
        assert [body.get("offset") for _url, body in t.calls] == [0, 200, 400]

    def test_exactly_one_full_page_reads_the_empty_next_page(self, monkeypatch):
        t = _CountingTransport([_entity(i) for i in range(200)])
        monkeypatch.setattr(renderer, "_request", t)
        assert len(renderer.fetch_active_policy_rows(base_url=BASE)) == 200
        assert [body.get("offset") for _url, body in t.calls] == [0, 200]

    def test_reported_total_stops_paging_without_an_extra_request(self, monkeypatch):
        t = _CountingTransport([_entity(i) for i in range(400)], report_total=True)
        monkeypatch.setattr(renderer, "_request", t)
        assert len(renderer.fetch_active_policy_rows(base_url=BASE)) == 400
        assert len(t.calls) == 2

    def test_inactive_rows_on_later_pages_are_still_filtered(self, monkeypatch):
        ents = [_entity(i) for i in range(250)] + [_entity(999, status="retired")]
        t = _CountingTransport(ents)
        monkeypatch.setattr(renderer, "_request", t)
        rows = renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(rows) == 250
        assert "ent_policy_00999" not in {r["_entity_id"] for r in rows}

    def test_server_ignoring_offset_fails_loudly(self, monkeypatch):
        t = _CountingTransport([_entity(i) for i in range(300)], honour_offset=False)
        monkeypatch.setattr(renderer, "_request", t)
        with pytest.raises(renderer.PolicyPagingError):
            renderer.fetch_active_policy_rows(base_url=BASE)

    def test_short_read_against_reported_total_fails_loudly(self, monkeypatch):
        t = _CountingTransport([_entity(i) for i in range(50)], report_total=True,
                               total_override=80)
        monkeypatch.setattr(renderer, "_request", t)
        with pytest.raises(renderer.PolicyPagingError):
            renderer.fetch_active_policy_rows(base_url=BASE)

    def test_page_cap_fails_loudly(self, monkeypatch):
        monkeypatch.setattr(renderer, "POLICY_MAX_PAGES", 2)
        t = _CountingTransport([_entity(i) for i in range(450)])
        monkeypatch.setattr(renderer, "_request", t)
        with pytest.raises(renderer.PolicyPagingError):
            renderer.fetch_active_policy_rows(base_url=BASE)

    def test_paging_failure_is_not_cached(self, cache_dir, monkeypatch):
        t = _CountingTransport([_entity(i) for i in range(300)], honour_offset=False)
        monkeypatch.setattr(renderer, "_request", t)
        with pytest.raises(renderer.PolicyPagingError):
            renderer.fetch_active_policy_rows(base_url=BASE)
        assert list(cache_dir.glob("*.json")) == []
