"""GitHub App private keys never reach a dispatched agent's process.

A key lets whoever holds it mint a token for that App. With one App per swarm
identity (`lib/github_app_token.py`) the host carries nine keys (seven lens Apps, the PR-opener App and the swarm App), and an agent
process is steered by untrusted pull-request content, so a key in its
environment would let one injected agent act as any identity. The daemon mints
tokens; an agent is handed only the token it needs.

Run: pytest execution/daemons/apis/test_app_key_containment.py -v
"""

from __future__ import annotations

import pytest

import skill_runner

KEY_NAMES = [
    "ATELES_REVIEWER_APP_PRIVATE_KEY",
    "ATELES_REVIEWER_APP_PRIVATE_KEY_PATH",
    "ATELES_APP_PR_OPENER_PRIVATE_KEY",
    "ATELES_APP_PR_OPENER_PRIVATE_KEY_PATH",
    "ATELES_APP_REVIEWER_PM_PRIVATE_KEY",
    "ATELES_APP_REVIEWER_SECURITY_PRIVATE_KEY_PATH",
    "ATELES_APP_REVIEWER_LEGAL_PRIVATE_KEY",
    "ATELES_APP_SOME_FUTURE_ROLE_PRIVATE_KEY",  # an App nobody has named yet
]
SENTINEL = "SENTINEL-APP-KEY-VALUE"
NOT_KEYS = {
    "ATELES_APP_REVIEWER_PM_ID": "1",
    "ATELES_APP_PR_OPENER_INSTALLATION_ID": "2",
    "ATELES_REVIEWER_APP_ID": "3",
}


@pytest.fixture
def host_env(monkeypatch):
    for name in KEY_NAMES:
        monkeypatch.setenv(name, SENTINEL)
    for name, value in NOT_KEYS.items():
        monkeypatch.setenv(name, value)


def _no_keys(child: dict) -> None:
    assert not [name for name in KEY_NAMES if name in child]
    assert SENTINEL not in child.values()


def test_control_the_keys_are_in_the_host_environment(host_env):
    import os

    assert all(os.environ.get(name) == SENTINEL for name in KEY_NAMES)


def test_no_app_key_reaches_an_agent_child(host_env):
    child = skill_runner._subscription_only_env()
    _no_keys(child)
    # ids are not secrets, but the child environment is an allowlist and a child
    # is handed the token it needs, not the identity of the App behind it
    for name in NOT_KEYS:
        assert name not in child


def test_no_app_key_reaches_a_local_review_child(host_env, tmp_path):
    child = skill_runner._subscription_only_env(
        local_review=True, local_review_home=str(tmp_path / "home")
    )
    _no_keys(child)


def test_a_key_smuggled_in_through_env_extra_is_stripped_too(host_env):
    child = skill_runner._subscription_only_env(
        {"ATELES_APP_REVIEWER_QA_PRIVATE_KEY": SENTINEL}
    )
    assert "ATELES_APP_REVIEWER_QA_PRIVATE_KEY" not in child
    _no_keys(child)


def test_the_metered_override_does_not_release_a_key(host_env, monkeypatch):
    monkeypatch.setenv("APIS_ALLOW_METERED_HARNESS", "1")
    _no_keys(skill_runner._subscription_only_env())
