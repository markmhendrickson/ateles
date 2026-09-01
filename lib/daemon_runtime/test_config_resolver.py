"""
Tests for lib/daemon_runtime/config_resolver.py and the SSE subscription guard.

Two things are under test, and the second matters as much as the first:

  1. Resolution order and degradation — env wins, Neotoma next, cache when
     Neotoma is down, hard failure only when nothing resolves.
  2. That the loud failure is actually LOUD through the real entrypoint. A
     guard that raises into a broad `except Exception` retry loop is not a
     guard; the test drives SSEClient.stream(), not just the private method.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

import lib.daemon_runtime.config_resolver as cr
import lib.daemon_runtime.sse_client as sc
from lib.daemon_runtime.config_resolver import (
    ConfigResolutionError,
    ConfigSpec,
    resolve,
)
from lib.daemon_runtime.sse_client import MissingSubscriptionError, SSEClient


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point the cache at a temp dir and clear inherited env for every test."""
    monkeypatch.setattr(cr, "CONFIG_CACHE_DIR", tmp_path / "cache")
    for var in (
        "NEOTOMA_SSE_SUBSCRIPTION_ID",
        "NEOTOMA_SSE_SUBSCRIPTION_ID_APIS",
        "NEOTOMA_SSE_SUBSCRIPTION_ID_TESTD",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _no_neotoma(monkeypatch):
    monkeypatch.setattr(cr, "_fetch_from_neotoma", lambda daemon: ({}, None))


def _neotoma_returns(monkeypatch, values, entity_id="ent_test"):
    monkeypatch.setattr(
        cr, "_fetch_from_neotoma", lambda daemon: (values, entity_id)
    )


# ── resolution order ────────────────────────────────────────────────────────


def test_env_var_wins_over_neotoma(monkeypatch):
    """Env is the operator override and the escape hatch — it must always win,
    so adopting the resolver cannot break a daemon that works today."""
    _neotoma_returns(monkeypatch, {"sse_subscription_id": "from-neotoma"})
    monkeypatch.setenv("SUB_ENV", "from-env")

    got = resolve("apis", [ConfigSpec(key="sse_subscription_id", env_var="SUB_ENV")])

    assert got.get("sse_subscription_id") == "from-env"
    assert got.source_of("sse_subscription_id") == "env"


def test_neotoma_used_when_env_absent(monkeypatch):
    _neotoma_returns(monkeypatch, {"sse_subscription_id": "sub-123"})

    got = resolve("apis", [ConfigSpec(key="sse_subscription_id", env_var="SUB_ENV")])

    assert got.get("sse_subscription_id") == "sub-123"
    assert got.source_of("sse_subscription_id") == "neotoma"
    assert got.entity_id == "ent_test"
    assert got.degraded is False


def test_plist_placeholder_is_not_a_value(monkeypatch):
    """launchd plists carry __PLACEHOLDER__ markers. Treating one as a real
    value is how a daemon ends up subscribing to a literal placeholder."""
    _neotoma_returns(monkeypatch, {"sse_subscription_id": "real-id"})
    monkeypatch.setenv("SUB_ENV", "__NEOTOMA_SSE_SUBSCRIPTION_ID__")

    got = resolve("apis", [ConfigSpec(key="sse_subscription_id", env_var="SUB_ENV")])

    assert got.get("sse_subscription_id") == "real-id"
    assert got.source_of("sse_subscription_id") == "neotoma"


# ── degradation: Neotoma down ───────────────────────────────────────────────


def test_cache_serves_config_when_neotoma_is_down(monkeypatch):
    """The central degradation requirement: a daemon must still start on
    last-known-good config when Neotoma is unreachable."""
    _neotoma_returns(monkeypatch, {"sse_subscription_id": "sub-cached"})
    resolve("apis", [ConfigSpec(key="sse_subscription_id")])  # populates cache

    _no_neotoma(monkeypatch)  # Neotoma now down
    got = resolve("apis", [ConfigSpec(key="sse_subscription_id")])

    assert got.get("sse_subscription_id") == "sub-cached"
    assert got.source_of("sse_subscription_id") == "cache"
    assert got.degraded is True


def test_degraded_run_is_reported_not_silent(monkeypatch, caplog):
    """Running on stale cache is allowed, but must be visible in the log."""
    _neotoma_returns(monkeypatch, {"sse_subscription_id": "sub-old"})
    resolve("apis", [ConfigSpec(key="sse_subscription_id")])

    # Backdate the cache well past the staleness threshold.
    path = cr._cache_path("apis")
    payload = json.loads(path.read_text())
    payload["_cached_at"] = time.time() - (30 * 24 * 3600)
    path.write_text(json.dumps(payload))

    _no_neotoma(monkeypatch)
    with caplog.at_level("WARNING"):
        got = resolve("apis", [ConfigSpec(key="sse_subscription_id")])

    assert got.get("sse_subscription_id") == "sub-old"
    assert any("may be stale" in r.message for r in caplog.records)


def test_slow_neotoma_does_not_block_startup(monkeypatch):
    """A 60s Neotoma read must not become a 60s daemon startup. The fetch is
    time-boxed; an exception from the client falls through to cache."""

    def _slow(*args, **kwargs):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(cr.httpx, "get", _slow)
    _neotoma_returns(monkeypatch, {"k": "v"})
    resolve("apis", [ConfigSpec(key="k")])  # seed cache

    monkeypatch.undo()
    monkeypatch.setattr(cr, "CONFIG_CACHE_DIR", cr.CONFIG_CACHE_DIR)
    monkeypatch.setattr(cr.httpx, "get", _slow)

    started = time.monotonic()
    got = resolve("apis", [ConfigSpec(key="k", required=False)])
    assert time.monotonic() - started < 5  # no hang


# ── loud failure ────────────────────────────────────────────────────────────


def test_unresolvable_required_config_raises_with_remedy(monkeypatch):
    """The whole point: unresolvable config fails loudly and names the fix,
    rather than resolving to an empty string."""
    _no_neotoma(monkeypatch)

    with pytest.raises(ConfigResolutionError) as exc:
        resolve(
            "apis",
            [
                ConfigSpec(
                    key="sse_subscription_id",
                    env_var="SUB_ENV",
                    remedy="create the subscription and record its id",
                )
            ],
        )

    msg = str(exc.value)
    assert "sse_subscription_id" in msg
    assert "SUB_ENV" in msg
    assert "create the subscription and record its id" in msg


def test_optional_config_falls_back_to_default(monkeypatch):
    _no_neotoma(monkeypatch)
    got = resolve(
        "apis", [ConfigSpec(key="poll", required=False, default=30)]
    )
    assert got.get("poll") == 30
    assert got.source_of("poll") == "default"


def test_named_but_absent_secret_is_reported(monkeypatch, caplog):
    """Config names a secret; the VALUE stays in SOPS. A missing secret becomes
    a named, logged condition instead of a silent empty credential."""
    _neotoma_returns(monkeypatch, {"sse_subscription_id": "sub-1"})
    monkeypatch.delenv("SOME_TOKEN", raising=False)

    with caplog.at_level("ERROR"):
        got = resolve(
            "apis",
            [ConfigSpec(key="sse_subscription_id", secret_name="SOME_TOKEN")],
        )

    assert "SOME_TOKEN" in got.missing_secrets
    assert any("NOT set in the environment" in r.message for r in caplog.records)


def test_secret_values_are_never_written_to_cache(monkeypatch):
    """Guards the hard constraint: both repos are public and the cache is a
    plain file — only config may ever land in it."""
    _neotoma_returns(monkeypatch, {"sse_subscription_id": "sub-1"})
    resolve("apis", [ConfigSpec(key="sse_subscription_id", secret_name="TOK")])

    monkeypatch.setenv("TOK", "super-secret-value")
    body = cr._cache_path("apis").read_text()
    assert "super-secret-value" not in body


def test_exact_daemon_name_match_required(monkeypatch):
    """A fuzzy `search` hit must never hand one daemon another's config —
    the class of mistake that pointed a deploy at a client's app."""

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "entities": [
                    {
                        "entity_id": "ent_other",
                        "snapshot": {
                            "daemon_name": "apis-dns-watchdog",
                            "config": {"sse_subscription_id": "WRONG"},
                        },
                    }
                ]
            }

    monkeypatch.setattr(cr.httpx, "get", lambda *a, **k: _Resp())
    values, entity_id = cr._fetch_from_neotoma("apis")

    assert values == {}
    assert entity_id is None


# ── the guard must survive the real entrypoint ──────────────────────────────


def test_missing_subscription_raises_through_stream(monkeypatch):
    """Drives the REAL entrypoint. stream() wraps _connect_and_stream in a broad
    `except Exception` retry loop; without an explicit re-raise this guard would
    be swallowed into a warning and an infinite quiet reconnect — reproducing
    the very failure it exists to prevent."""
    monkeypatch.setattr(sc, "resolve_subscription_id", lambda name: None)

    client = SSEClient(entity_types=["task"], handler_name="testd")

    async def _handler(event):  # pragma: no cover — never reached
        raise AssertionError("handler must not run without a subscription")

    with pytest.raises(MissingSubscriptionError) as exc:
        asyncio.run(client.stream(_handler, reconnect=True))

    msg = str(exc.value)
    assert "testd" in msg
    assert "daemon_configuration" in msg  # names the durable remedy
    assert client._running is False


def test_explicit_subscription_id_still_works(monkeypatch):
    """Constructor injection keeps working — no daemon regresses."""
    monkeypatch.setattr(sc, "resolve_subscription_id", lambda name: None)
    client = SSEClient(handler_name="testd", subscription_id="explicit-id")
    assert client._subscription_id == "explicit-id"


def test_env_var_path_needs_no_neotoma(monkeypatch):
    """Today's working daemons resolve from env without touching Neotoma."""
    monkeypatch.setenv("NEOTOMA_SSE_SUBSCRIPTION_ID_APIS", "sub-from-plist")

    def _boom(*a, **k):  # pragma: no cover
        raise AssertionError("must not call Neotoma when env is set")

    monkeypatch.setattr(cr, "_fetch_from_neotoma", _boom)
    assert sc.resolve_subscription_id("apis") == "sub-from-plist"
