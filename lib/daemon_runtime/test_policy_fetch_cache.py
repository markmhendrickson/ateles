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

_TOKEN_CANARY = "tok-canary-6f1d0c2a9b"  # gitleaks:allow — synthetic test value
_OTHER_TOKEN = "tok-canary-other-48e1"  # gitleaks:allow — synthetic test value


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
        b'{"v": 2, "key": "wrong", "fetched_at": 0, "rows": []}',
        b'{"v": 2}',
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
        with pytest.raises(renderer.PolicyPagingError, match="repeats"):
            renderer.fetch_active_policy_rows(base_url=BASE)
        # Caught on the second page, not after the 25-page cap.
        assert len(t.calls) == 2

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


# ---------------------------------------------------------------------------
# Correction round: token-scoped key, outage marker, pruning, backup
# exclusion, the read's age, and the checks that previously had no test.
# ---------------------------------------------------------------------------
import subprocess  # noqa: E402
import sys  # noqa: E402


def _fake_uid_fstat(monkeypatch, *, dirs: bool = False, files: bool = False):
    """Make `os.fstat` report another owner for directories and/or regular
    files only, so each owner check can be exercised on its own."""
    real_fstat = os.fstat

    def fake(fd):
        st = real_fstat(fd)
        hit = (dirs and stat.S_ISDIR(st.st_mode)) or (files and stat.S_ISREG(st.st_mode))
        if not hit:
            return st
        seq = list(tuple(st))
        seq[4] = st.st_uid + 1
        return os.stat_result(seq)

    monkeypatch.setattr(cache_mod.os, "fstat", fake)


class TestTokenScopedKey:
    def test_two_tokens_on_one_url_do_not_share_an_entry(self, cache_dir, transport, monkeypatch):
        monkeypatch.setattr(renderer, "NEOTOMA_BEARER_TOKEN", _TOKEN_CANARY)
        renderer.fetch_active_policy_rows(base_url=BASE)
        monkeypatch.setattr(renderer, "NEOTOMA_BEARER_TOKEN", _OTHER_TOKEN)
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 2
        assert len(list(cache_dir.glob("*.json"))) == 2
        # Each token still hits its own entry.
        renderer.fetch_active_policy_rows(base_url=BASE)
        monkeypatch.setattr(renderer, "NEOTOMA_BEARER_TOKEN", _TOKEN_CANARY)
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 2

    def test_neither_token_nor_its_digest_is_written(self, cache_dir, transport, monkeypatch):
        monkeypatch.setattr(renderer, "NEOTOMA_BEARER_TOKEN", _TOKEN_CANARY)
        renderer.fetch_active_policy_rows(base_url=BASE)
        digest = cache_mod._principal_digest(_TOKEN_CANARY)
        assert digest and _TOKEN_CANARY not in digest
        for path in cache_dir.iterdir():
            for needle in (_TOKEN_CANARY, digest):
                assert needle not in path.name
                assert needle.encode() not in path.read_bytes()

    def test_key_changes_with_the_token_only_through_its_digest(self):
        body = {"entity_type": "agent_policy"}
        assert cache_mod.cache_key(BASE, body, _TOKEN_CANARY) != cache_mod.cache_key(BASE, body, _OTHER_TOKEN)
        assert cache_mod.cache_key(BASE, body, _TOKEN_CANARY) != cache_mod.cache_key(BASE, body, "")
        assert cache_mod.cache_key(BASE, body, _TOKEN_CANARY) == cache_mod.cache_key(BASE, body, _TOKEN_CANARY)


class TestOutageMarker:
    def test_concurrent_failing_fetches_take_one_timeout_not_two(self, cache_dir):
        body = {"entity_type": "agent_policy", "limit": 200}
        calls = []
        errors = []
        delay = 1.0

        def failing_fetch():
            calls.append(threading.current_thread().name)
            time.sleep(delay)
            raise OSError("timed out")

        def worker():
            try:
                cache_mod.cached_fetch(BASE, body, failing_fetch)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, name=f"hook-{i}") for i in range(2)]
        start = time.monotonic()
        threads[0].start()
        time.sleep(0.1)  # the second hook arrives while the first is fetching
        threads[1].start()
        for t in threads:
            t.join(timeout=10)
        elapsed = time.monotonic() - start
        assert len(calls) == 1, calls
        assert elapsed < 1.6 * delay, elapsed
        kinds = sorted(type(e).__name__ for e in errors)
        assert kinds == ["OSError", "PolicyFetchRecentlyFailed"]
        assert list(cache_dir.glob("*.json")) == []

    def test_a_later_caller_still_tries_live_after_an_old_failure(self, cache_dir):
        body = {"entity_type": "agent_policy", "limit": 200}
        with pytest.raises(OSError):
            cache_mod.cached_fetch(BASE, body, lambda: (_ for _ in ()).throw(OSError("down")))
        assert cache_mod.cached_fetch(BASE, body, lambda: [{"_entity_id": "back"}]) == [
            {"_entity_id": "back"}
        ]
        # Success clears the marker.
        assert list(cache_dir.glob("*.failed")) == []


class TestPruning:
    def test_expired_files_are_deleted_on_write(self, cache_dir, transport):
        renderer.fetch_active_policy_rows(base_url=BASE)
        old = time.time() - 1000
        stale_entry = cache_dir / ("a" * 64 + ".json")
        stale_marker = cache_dir / ("b" * 64 + ".failed")
        stale_tmp = cache_dir / (".{}.json.123.{}.tmp".format("c" * 64, "d" * 16))
        fresh_other = cache_dir / ("e" * 64 + ".json")
        lock = cache_dir / ("f" * 64 + ".lock")
        unrelated = cache_dir / "notes.txt"
        for p in (stale_entry, stale_marker, stale_tmp, fresh_other, lock, unrelated):
            p.write_text("{}")
            os.chmod(p, 0o600)
        for p in (stale_entry, stale_marker, stale_tmp, lock, unrelated):
            os.utime(p, (old, old))
        renderer.fetch_active_policy_rows(base_url="https://other.example.test")
        assert not stale_entry.exists()
        assert not stale_marker.exists()
        assert not stale_tmp.exists()
        assert fresh_other.exists()  # not expired yet
        assert lock.exists()  # lock files are never removed
        assert unrelated.exists()  # only names this module writes
        assert len(list(cache_dir.glob("*.json"))) == 3

    def test_young_temp_file_survives_even_past_a_short_ttl(self, cache_dir, transport, monkeypatch):
        monkeypatch.setenv(cache_mod.TTL_ENV, "1")
        renderer.fetch_active_policy_rows(base_url=BASE)
        tmp = cache_dir / (".{}.json.123.{}.tmp".format("c" * 64, "d" * 16))
        tmp.write_text("{}")
        past = time.time() - 30  # older than the TTL, younger than the grace
        os.utime(tmp, (past, past))
        renderer.fetch_active_policy_rows(base_url="https://other.example.test")
        assert tmp.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Time Machine exclusion is macOS-only")
class TestBackupExclusionOnMac:
    def test_cache_dir_carries_the_backup_exclusion_attribute(self, cache_dir, transport):
        renderer.fetch_active_policy_rows(base_url=BASE)
        out = subprocess.run(
            ["/usr/bin/xattr", "-px", cache_mod.BACKUP_EXCLUDE_XATTR, str(cache_dir)],
            capture_output=True, text=True,
        )
        assert out.returncode == 0, out.stderr
        assert "com.apple.backupd".encode().hex() in out.stdout.replace(" ", "").replace("\n", "").lower()


class TestBackupExclusionFallback:
    def test_tmutil_is_used_when_the_attribute_cannot_be_set(self, cache_dir, transport, monkeypatch):
        monkeypatch.setattr(cache_mod, "_IS_DARWIN", True)

        def no_xattr(fd):
            raise OSError("no attribute support")

        ran = []
        monkeypatch.setattr(cache_mod, "_set_backup_exclusion_xattr", no_xattr)
        monkeypatch.setattr(cache_mod.subprocess, "run", lambda argv, **kw: ran.append(argv))
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert ran == [["tmutil", "addexclusion", str(cache_dir)]]
        # Not re-run once the directory exists.
        renderer.fetch_active_policy_rows(base_url="https://other.example.test")
        assert len(ran) == 1

    def test_cache_carries_on_when_neither_works(self, cache_dir, transport, monkeypatch):
        monkeypatch.setattr(cache_mod, "_IS_DARWIN", True)

        def boom(*a, **kw):
            raise OSError("unavailable")

        monkeypatch.setattr(cache_mod, "_set_backup_exclusion_xattr", boom)
        monkeypatch.setattr(cache_mod.subprocess, "run", boom)
        renderer.fetch_active_policy_rows(base_url=BASE)
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 1


class TestReadAge:
    def test_age_is_zero_live_and_the_entry_age_when_cached(self, cache_dir, transport, monkeypatch):
        now = [2_000_000.0]
        monkeypatch.setattr(cache_mod.time, "time", lambda: now[0])
        _rows, age = renderer.fetch_active_policy_rows_with_age(base_url=BASE)
        assert age == 0.0
        now[0] += 37.0
        _rows, age = renderer.fetch_active_policy_rows_with_age(base_url=BASE)
        assert age == pytest.approx(37.0)
        assert len(transport.calls) == 1


class TestFailureLabels:
    def test_paging_error_is_not_labelled_as_unreachable(self):
        assert "paging failed" in renderer.describe_fetch_failure(renderer.PolicyPagingError("x"))
        assert "reach" not in renderer.describe_fetch_failure(renderer.PolicyPagingError("x"))
        assert renderer.describe_fetch_failure(OSError("x")) == "could not reach Neotoma"
        assert "concurrent" in renderer.describe_fetch_failure(
            cache_mod.PolicyFetchRecentlyFailed("x")
        )


class TestChecksThatHadNoTest:
    def test_fresh_entry_with_the_wrong_key_falls_through(self, cache_dir, transport):
        renderer.fetch_active_policy_rows(base_url=BASE)
        entry = next(cache_dir.glob("*.json"))
        data = json.loads(entry.read_bytes())
        data["key"] = "f" * 64  # unexpired, well-formed, wrong key
        entry.write_text(json.dumps(data))
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 2

    def test_other_owned_directory_alone_is_refused(self, cache_dir, transport, monkeypatch):
        renderer.fetch_active_policy_rows(base_url=BASE)
        _fake_uid_fstat(monkeypatch, dirs=True)
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 2

    def test_other_owned_entry_alone_is_refused(self, cache_dir, transport, monkeypatch):
        renderer.fetch_active_policy_rows(base_url=BASE)
        _fake_uid_fstat(monkeypatch, files=True)
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 2

    def test_hard_linked_entry_is_refused(self, cache_dir, transport, tmp_path):
        renderer.fetch_active_policy_rows(base_url=BASE)
        entry = next(cache_dir.glob("*.json"))
        os.link(entry, tmp_path / "second-name.json")
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 2

    def test_entry_over_the_size_cap_is_refused_on_read(self, cache_dir, transport, monkeypatch):
        renderer.fetch_active_policy_rows(base_url=BASE)
        entry = next(cache_dir.glob("*.json"))
        monkeypatch.setattr(cache_mod, "MAX_ENTRY_BYTES", entry.stat().st_size - 1)
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 2

    def test_entry_over_the_size_cap_is_not_written(self, cache_dir, transport, monkeypatch):
        monkeypatch.setattr(cache_mod, "MAX_ENTRY_BYTES", 64)
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert list(cache_dir.glob("*.json")) == []

    def test_non_regular_file_at_the_entry_name_is_refused(self, cache_dir, transport):
        renderer.fetch_active_policy_rows(base_url=BASE)
        entry = next(cache_dir.glob("*.json"))
        entry.unlink()
        os.mkfifo(entry, 0o600)
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 2

    def test_a_failed_cache_write_does_not_raise(self, cache_dir, transport, monkeypatch):
        def broken_replace(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(cache_mod.os, "replace", broken_replace)
        rows = renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(rows) == 5
        assert list(cache_dir.glob(".*.tmp")) == []  # temp file cleaned up

    @pytest.mark.parametrize("raw,expected", [
        (None, cache_mod.DEFAULT_TTL_S),
        ("", cache_mod.DEFAULT_TTL_S),
        ("abc", 0.0),
        ("-5", 0.0),
        ("0", 0.0),
        ("nan", 0.0),
        ("inf", 0.0),
        ("30", 30.0),
        ("100000", cache_mod.MAX_TTL_S),
    ])
    def test_ttl_parsing(self, monkeypatch, raw, expected):
        if raw is None:
            monkeypatch.delenv(cache_mod.TTL_ENV, raising=False)
        else:
            monkeypatch.setenv(cache_mod.TTL_ENV, raw)
        assert cache_mod.ttl_seconds() == expected

    def test_relative_cache_dir_is_rejected(self, tmp_path, transport, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(cache_mod.TTL_ENV, "120")
        monkeypatch.setenv(cache_mod.DIR_ENV, "rel/cache")
        renderer.fetch_active_policy_rows(base_url=BASE)
        renderer.fetch_active_policy_rows(base_url=BASE)
        assert len(transport.calls) == 2
        assert not (tmp_path / "rel").exists()

    @pytest.mark.parametrize("page", [
        ["not", "an", "object"],
        {"entities": "not a list"},
    ])
    def test_malformed_page_fails_loudly(self, monkeypatch, page):
        monkeypatch.setattr(renderer, "_request", lambda url, body, timeout=10.0: page)
        with pytest.raises(renderer.PolicyPagingError):
            renderer.fetch_active_policy_rows(base_url=BASE)

    def test_boolean_total_is_not_a_count(self, monkeypatch):
        ents = [_entity(i) for i in range(3)]
        monkeypatch.setattr(
            renderer, "_request",
            lambda url, body, timeout=10.0: {"entities": ents, "total": True},
        )
        # True would read as total == 1 and fail the count check.
        assert len(renderer.fetch_active_policy_rows(base_url=BASE)) == 3


class TestNonRegularFileRefusedDirectly:
    @pytest.mark.parametrize("kind", ["fifo", "dir"])
    def test_non_regular_entry_raises_cache_refused(self, cache_dir, kind):
        dir_fd, _path = cache_mod._open_cache_dir()
        try:
            target = cache_dir / ("a" * 64 + ".json")
            if kind == "fifo":
                os.mkfifo(target, 0o600)
            else:
                target.mkdir(mode=0o700)
            with pytest.raises(cache_mod.CacheRefused, match="not a regular file"):
                cache_mod._read_json(dir_fd, target.name)
        finally:
            os.close(dir_fd)
