"""run_pytest_hermetic builds the child environment from a fixed allowlist."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_pytest_hermetic as runner  # noqa: E402


def test_build_env_drops_every_non_allowlisted_host_variable():
    host = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "DUMMY_SECRET_TOKEN": "DUMMYSECRETVALUE_abcdef123456",
        "NEOTOMA_AAUTH_PRIVATE_JWK_PATH": "/dummy/host/DUMMYHOST.jwk.json",
    }
    env = runner.build_env(host)
    assert sorted(env) == ["HOME", "LANG", "PATH", "TMPDIR"]


def test_build_env_supplies_defaults_when_the_host_has_none():
    env = runner.build_env({})
    assert sorted(env) == ["HOME", "LANG", "PATH", "TMPDIR"]
