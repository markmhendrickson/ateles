"""Tests for trigger_swarm_pr._ensure_secrets_env (ateles#524, PR #527 round 2).

trigger_swarm_pr.py is an operator/CI tool: DispatcherConfig (imported via
swarm_dispatch) reads its token env vars at class-definition time, so
_ensure_secrets_env() must materialize them from the SOPS snapshot *before*
swarm_dispatch is imported, while leaving the daemon's already-set-env-vars
path untouched. secrets_lib, github_gateway, swarm_dispatch, and lib.notify
are all stubbed so import succeeds without a real age key, a real daemon
dependency graph, or a live GitHub token.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_DAEMON_DIR = _SCRIPTS.parents[0] / "daemons" / "apis"
for _p in (str(_SCRIPTS), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _fresh_module(monkeypatch, sops_pairs, decrypt_raises=False):
    """Import trigger_swarm_pr with secrets_lib + its downstream imports stubbed.

    Mirrors _fresh_module in test_triage_backfill_token.py. trigger_swarm_pr
    additionally imports github_gateway, swarm_dispatch, and lib.notify at
    module level (after _ensure_secrets_env() runs) — those are stubbed too
    so the real daemon dependency graph (aiohttp, gate_waive, issue_spec, …)
    is never touched.
    """
    stub_secrets = types.ModuleType("secrets_lib")

    class _P:
        def __init__(self, path):
            self._path = path

        def __str__(self):
            return self._path

    stub_secrets.enc_file = lambda name: _P("/fake/neotoma.sops.enc")  # type: ignore[attr-defined]
    stub_secrets.DEFAULT_AGE_KEY_FILE = "/fake/age/keys.txt"  # type: ignore[attr-defined]

    def _decrypt(src):
        if decrypt_raises:
            raise RuntimeError("no age key")
        return dict(sops_pairs)

    stub_secrets.sops_decrypt_dotenv = _decrypt  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "secrets_lib", stub_secrets)

    stub_gateway = types.ModuleType("github_gateway")
    stub_gateway.parse_github_event = lambda *a, **k: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "github_gateway", stub_gateway)

    stub_dispatch = types.ModuleType("swarm_dispatch")

    class _SwarmDispatcher:
        def __init__(self, notifier):
            self.notifier = notifier

        async def handle_trigger(self, trigger):
            return None

    stub_dispatch.SwarmDispatcher = _SwarmDispatcher  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "swarm_dispatch", stub_dispatch)

    stub_lib = sys.modules.get("lib") or types.ModuleType("lib")
    stub_notify = types.ModuleType("lib.notify")

    class _Notifier:
        pass

    stub_notify.Notifier = _Notifier  # type: ignore[attr-defined]
    stub_notify.Priority = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "lib", stub_lib)
    monkeypatch.setitem(sys.modules, "lib.notify", stub_notify)

    sys.modules.pop("trigger_swarm_pr", None)
    return importlib.import_module("trigger_swarm_pr")


def _clear_swarm_env(monkeypatch):
    for var in (
        "NEOTOMA_BEARER_TOKEN",
        "NEOTOMA_BEARER_TOKEN_PROD",
        "GITHUB_TOKEN",
        "ATELES_AGENT_PAT",
        "NEOTOMA_AGENT_PAT",
    ):
        monkeypatch.delenv(var, raising=False)


def test_early_return_when_both_primary_vars_set(monkeypatch):
    """Daemon path: NEOTOMA_BEARER_TOKEN + GITHUB_TOKEN already set → env
    unchanged, SOPS never consulted."""
    _clear_swarm_env(monkeypatch)
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "daemon-neotoma")
    monkeypatch.setenv("GITHUB_TOKEN", "daemon-github")
    mod = _fresh_module(monkeypatch, {"NEOTOMA_BEARER_TOKEN": "sops-neotoma"})

    mod._ensure_secrets_env()

    assert mod.os.environ["NEOTOMA_BEARER_TOKEN"] == "daemon-neotoma"
    assert mod.os.environ["GITHUB_TOKEN"] == "daemon-github"


def test_materializes_missing_keys_from_sops(monkeypatch):
    """Bare operator/CI shell: both primary vars absent → SOPS-sourced values
    are written into the environment."""
    _clear_swarm_env(monkeypatch)
    mod = _fresh_module(
        monkeypatch,
        {
            "NEOTOMA_BEARER_TOKEN": "sops-neotoma",
            "NEOTOMA_BEARER_TOKEN_PROD": "sops-neotoma-prod",
            "GITHUB_TOKEN": "sops-github",
        },
    )

    mod._ensure_secrets_env()

    assert mod.os.environ["NEOTOMA_BEARER_TOKEN"] == "sops-neotoma"
    assert mod.os.environ["NEOTOMA_BEARER_TOKEN_PROD"] == "sops-neotoma-prod"
    assert mod.os.environ["GITHUB_TOKEN"] == "sops-github"


def test_does_not_overwrite_preset_value(monkeypatch):
    """A pre-set value for one of the wanted vars survives even when the
    primary pair is absent and SOPS has a different value for it."""
    _clear_swarm_env(monkeypatch)
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN_PROD", "preset-value")
    mod = _fresh_module(
        monkeypatch,
        {
            "NEOTOMA_BEARER_TOKEN": "sops-neotoma",
            "GITHUB_TOKEN": "sops-github",
            "NEOTOMA_BEARER_TOKEN_PROD": "sops-would-overwrite",
        },
    )

    mod._ensure_secrets_env()

    assert mod.os.environ["NEOTOMA_BEARER_TOKEN_PROD"] == "preset-value"


def test_ateles_agent_pat_maps_to_github_token(monkeypatch):
    """When GITHUB_TOKEN isn't materialized directly, ATELES_AGENT_PAT (once
    pulled from SOPS) is mapped into GITHUB_TOKEN."""
    _clear_swarm_env(monkeypatch)
    mod = _fresh_module(
        monkeypatch,
        {
            "NEOTOMA_BEARER_TOKEN": "sops-neotoma",
            "ATELES_AGENT_PAT": "sops-ateles-pat",
        },
    )

    mod._ensure_secrets_env()

    assert mod.os.environ["ATELES_AGENT_PAT"] == "sops-ateles-pat"
    assert mod.os.environ["GITHUB_TOKEN"] == "sops-ateles-pat"


def test_neotoma_agent_pat_maps_to_github_token_when_ateles_absent(monkeypatch):
    """NEOTOMA_AGENT_PAT is the fallback mapping when ATELES_AGENT_PAT isn't
    present in the SOPS snapshot either."""
    _clear_swarm_env(monkeypatch)
    mod = _fresh_module(
        monkeypatch,
        {
            "NEOTOMA_BEARER_TOKEN": "sops-neotoma",
            "NEOTOMA_AGENT_PAT": "sops-neotoma-pat",
        },
    )

    mod._ensure_secrets_env()

    assert mod.os.environ["NEOTOMA_AGENT_PAT"] == "sops-neotoma-pat"
    assert mod.os.environ["GITHUB_TOKEN"] == "sops-neotoma-pat"


def test_decrypt_failure_is_silent_no_op(monkeypatch):
    """No snapshot / no age key: sops_decrypt_dotenv raises, and
    _ensure_secrets_env swallows it and leaves env as-is (caller — the
    GitHub/Neotoma clients further down main() — errors clearly on the
    missing token instead)."""
    _clear_swarm_env(monkeypatch)
    mod = _fresh_module(monkeypatch, {}, decrypt_raises=True)

    mod._ensure_secrets_env()  # must not raise

    assert mod.os.environ.get("NEOTOMA_BEARER_TOKEN") is None
    assert mod.os.environ.get("GITHUB_TOKEN") is None
