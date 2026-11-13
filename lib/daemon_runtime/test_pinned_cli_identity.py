"""build_pinned_env / pinned_child_problem / isolation / dotenv, synthetic keys only.

These run in CI (no node, no deployed CLI); the real-CLI proof is in
test_pinned_cli_integration.py.
"""

import json
import os
import stat

import pytest

from lib.daemon_runtime import pinned_cli_identity as pci
from lib.daemon_runtime.pinned_testing import make_synthetic_key

SUB = "piculet@ateles-swarm"


@pytest.fixture
def built(tmp_path):
    keys = tmp_path / "keys"
    make_synthetic_key(keys, iss="https://synthetic-issuer.example")
    state = tmp_path / "state"
    base = {"PATH": "/bin", "HOME": str(tmp_path / "realhome")}
    env = pci.build_pinned_env("piculet", base, keys_dir=keys, rc_dir=tmp_path, state_dir=state)
    return env, keys, state, tmp_path


def test_env_is_pinned_and_bearer_free(tmp_path):
    keys = tmp_path / "keys"
    make_synthetic_key(keys)
    base = {"PATH": "/bin", "NEOTOMA_BEARER_TOKEN": "x", "NEOTOMA_TOKEN": "y"}
    env = pci.build_pinned_env("piculet", base, keys_dir=keys, rc_dir=keys, state_dir=tmp_path / "s")
    assert env["NEOTOMA_AAUTH_SUB"] == SUB and env[pci.PIN_ENV] == SUB
    assert env["NEOTOMA_AAUTH_PRIVATE_JWK_PATH"] == str(keys / "piculet.jwk.json")
    assert "NEOTOMA_BEARER_TOKEN" not in env and "NEOTOMA_TOKEN" not in env
    assert base["NEOTOMA_BEARER_TOKEN"] == "x"  # the caller's mapping is untouched


def test_conflicting_subject_is_refused(tmp_path):
    keys = tmp_path / "keys"
    make_synthetic_key(keys)
    with pytest.raises(pci.PinnedIdentityError, match="different principal"):
        pci.build_pinned_env(
            "piculet", {"NEOTOMA_AAUTH_SUB": "waxwing@ateles-swarm"}, keys_dir=keys, state_dir=tmp_path / "s"
        )


def test_missing_key_is_refused(tmp_path):
    with pytest.raises(pci.PinnedIdentityError, match="no AAuth key"):
        pci.build_pinned_env("piculet", {}, keys_dir=tmp_path, state_dir=tmp_path / "s")


# ── Claims are set explicitly; every override is cleared ────────────────────


def test_issuer_comes_from_the_key_file_not_the_environment(tmp_path):
    keys = tmp_path / "keys"
    make_synthetic_key(keys, iss="https://synthetic-issuer.example")
    env = pci.build_pinned_env(
        "piculet",
        {"NEOTOMA_AAUTH_ISS": "https://ambient.example", "NEOTOMA_CLI_AAUTH_ISS": "https://ambient.example"},
        keys_dir=keys, rc_dir=keys, state_dir=tmp_path / "s",
    )
    assert env["NEOTOMA_AAUTH_ISS"] == "https://synthetic-issuer.example"
    assert "NEOTOMA_CLI_AAUTH_ISS" not in env


def test_issuer_falls_back_to_the_shared_default_when_the_key_has_none(tmp_path):
    from lib.daemon_runtime.aauth_httpsig import DEFAULT_AAUTH_ISSUER

    keys = tmp_path / "keys"
    make_synthetic_key(keys)
    env = pci.build_pinned_env("piculet", {}, keys_dir=keys, rc_dir=keys, state_dir=tmp_path / "s")
    assert env["NEOTOMA_AAUTH_ISS"] == DEFAULT_AAUTH_ISSUER


@pytest.mark.parametrize(
    "name",
    [*pci.SIGNING_DISABLE_KEYS, *pci.CLAIM_OVERRIDE_KEYS, "NEOTOMA_ENCRYPTION_ENABLED",
     "NEOTOMA_MNEMONIC_PASSPHRASE"],
)
def test_every_override_is_cleared(tmp_path, name):
    keys = tmp_path / "keys"
    make_synthetic_key(keys)
    env = pci.build_pinned_env(
        "piculet", {name: "1"}, keys_dir=keys, rc_dir=keys, state_dir=tmp_path / "s"
    )
    assert name not in env


@pytest.mark.parametrize("name", [*pci.SIGNING_DISABLE_KEYS])
def test_child_refuses_a_signing_disable_flag(built, name):
    env, *_ = built
    assert pci.pinned_child_problem(env) is None
    assert name in pci.pinned_child_problem({**env, name: "1"})


@pytest.mark.parametrize("name", pci.CLAIM_OVERRIDE_KEYS)
def test_child_refuses_a_claim_override(built, name):
    env, *_ = built
    assert name in pci.pinned_child_problem({**env, name: "x"})


def test_child_refuses_encryption_derived_tokens(built):
    env, *_ = built
    assert "NEOTOMA_ENCRYPTION_ENABLED" in pci.pinned_child_problem(
        {**env, "NEOTOMA_ENCRYPTION_ENABLED": "true"}
    )


# ── The CLI cannot reach a stored token or a project .env ───────────────────


def test_isolation_is_private_and_holds_no_token(built):
    env, _, state, tmp_path = built
    home = state / "home"
    assert env[pci.CLI_HOME_ENV] == str(home) and env["HOME"] != str(home)
    for d in (state, home, home / ".config" / "neotoma", state / "project"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
    config = json.loads((home / ".config" / "neotoma" / "config.json").read_text())
    assert config == {}
    assert env["NEOTOMA_PROJECT_ROOT"] == str(state / "project")
    assert json.loads((state / "project" / "package.json").read_text())["name"] == "neotoma"
    assert not pci.stored_token_reachable(home)


def test_the_real_home_is_not_changed_for_the_rest_of_the_chain(built):
    env, *_ = built
    assert env["HOME"].endswith("realhome")  # only the CLI argv prefix swaps it


def test_isolation_prefix_wraps_only_the_cli(built):
    env, _, state, _ = built
    assert pci.cli_isolation_prefix(env) == [
        "/usr/bin/env", f"HOME={state / 'home'}", f"NEOTOMA_PROJECT_ROOT={state / 'project'}",
    ]
    assert pci.cli_isolation_prefix({"HOME": "/h"}) == []  # unpinned: untouched


def test_child_refuses_a_reachable_stored_token_in_the_real_home(built, tmp_path):
    env, *_ = built
    (tmp_path / "realhome" / ".config" / "neotoma").mkdir(parents=True)
    (tmp_path / "realhome" / ".config" / "neotoma" / "config.json").write_text(
        json.dumps({"access_token": "synthetic"})
    )
    bare = {k: v for k, v in env.items() if k not in (pci.CLI_HOME_ENV, pci.CLI_PROJECT_ROOT_ENV)}
    problem = pci.pinned_child_problem(bare)
    assert "stored CLI token" in problem and "synthetic" not in problem


def test_child_refuses_a_token_in_the_private_home(built):
    env, _, state, _ = built
    (state / "home" / ".config" / "neotoma" / "config.json").write_text('{"access_token": "synthetic"}')
    assert "private CLI home" in pci.pinned_child_problem(env)


def test_an_unreadable_config_counts_as_reachable(tmp_path):
    (tmp_path / ".config" / "neotoma").mkdir(parents=True)
    (tmp_path / ".config" / "neotoma" / "config.json").write_text("{not json")
    assert pci.stored_token_reachable(tmp_path)


def test_child_refuses_a_project_env_file_and_a_foreign_root(built):
    env, _, state, tmp_path = built
    (state / "project" / ".env").write_text("x=1")
    assert ".env" in pci.pinned_child_problem(env)
    (state / "project" / ".env").unlink()
    assert "project root" in pci.pinned_child_problem({**env, "NEOTOMA_PROJECT_ROOT": str(tmp_path)})


def test_a_symlinked_state_dir_is_refused(tmp_path):
    keys = tmp_path / "keys"
    make_synthetic_key(keys)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / "link").symlink_to(target)
    with pytest.raises(pci.PinnedIdentityError, match="not trustworthy"):
        pci.build_pinned_env("piculet", {}, keys_dir=keys, rc_dir=keys, state_dir=tmp_path / "link")


# ── Older checks, kept ──────────────────────────────────────────────────────


def test_child_problem_none_when_not_pinned():
    assert pci.pinned_child_problem({"NEOTOMA_BEARER_TOKEN": "x"}) is None


def test_child_problem_names_the_variable_not_its_value(built):
    env, *_ = built
    sentinel = "synthetic-" + "sentinel-not-real"  # built at runtime: not a secret-shaped literal
    env = dict(env, NEOTOMA_BEARER_TOKEN=sentinel)
    problem = pci.pinned_child_problem(env)
    assert "NEOTOMA_BEARER_TOKEN" in problem and sentinel not in problem


def test_dotenv_cannot_reintroduce_a_bearer_in_a_pinned_run(tmp_path, monkeypatch):
    for name in pci.BEARER_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(pci.PIN_ENV, SUB)

    def load(path):
        os.environ.setdefault("NEOTOMA_BEARER_TOKEN", "from-file")
        os.environ.setdefault("SYNTHETIC_OTHER_SETTING", "kept")
        return True

    try:
        pci.load_dotenv_unless_pinned_bearer(load, tmp_path / ".env")
        assert "NEOTOMA_BEARER_TOKEN" not in os.environ
        assert os.environ["SYNTHETIC_OTHER_SETTING"] == "kept"
    finally:
        os.environ.pop("SYNTHETIC_OTHER_SETTING", None)


def test_dotenv_is_untouched_when_not_pinned(tmp_path, monkeypatch):
    monkeypatch.delenv(pci.PIN_ENV, raising=False)
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)

    def load(path):
        os.environ.setdefault("NEOTOMA_BEARER_TOKEN", "from-file")

    try:
        pci.load_dotenv_unless_pinned_bearer(load, tmp_path / ".env")
        assert os.environ["NEOTOMA_BEARER_TOKEN"] == "from-file"
    finally:
        os.environ.pop("NEOTOMA_BEARER_TOKEN", None)
