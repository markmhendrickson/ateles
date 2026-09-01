"""Tests for check_daemon_config_parity (ateles#642)."""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import check_daemon_config_parity as parity  # noqa: E402


def _write_plist(path: Path, *, label: str, env: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": label,
        "EnvironmentVariables": env,
    }
    path.write_bytes(plistlib.dumps(payload))


def test_passes_when_repo_and_installed_plists_match(tmp_path: Path, capsys):
    repo = tmp_path / "repo" / "apis"
    installed = tmp_path / "installed"
    sub_id = "75076982-1d71-4e9f-8859-43d633dbdccc"
    env = {"NEOTOMA_SSE_SUBSCRIPTION_ID_APIS": sub_id}
    _write_plist(repo / "com.ateles.apis.plist", label="com.ateles.apis", env=env)
    _write_plist(installed / "com.ateles.apis.plist", label="com.ateles.apis", env=env)

    code = parity.main(["check_daemon_config_parity.py", str(repo.parent), str(installed)])
    out = capsys.readouterr()

    assert code == 0
    assert "OK com.ateles.apis" in out.out
    assert out.err == ""


def test_fails_on_subscription_id_mismatch(tmp_path: Path, capsys):
    repo = tmp_path / "repo" / "apis"
    installed = tmp_path / "installed"
    _write_plist(
        repo / "com.ateles.apis.plist",
        label="com.ateles.apis",
        env={"NEOTOMA_SSE_SUBSCRIPTION_ID_APIS": "repo-id"},
    )
    _write_plist(
        installed / "com.ateles.apis.plist",
        label="com.ateles.apis",
        env={"NEOTOMA_SSE_SUBSCRIPTION_ID_APIS": "installed-id"},
    )

    code = parity.main(["check_daemon_config_parity.py", str(repo.parent), str(installed)])
    err = capsys.readouterr().err

    assert code != 0
    assert "subscription_id_mismatch" in err
    assert "com.ateles.apis" in err
    assert "repo='repo-id'" in err
    assert "installed='installed-id'" in err


def test_fails_on_missing_subscription_id_in_either_surface(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    installed = tmp_path / "installed"

    _write_plist(
        repo / "apis" / "com.ateles.apis.plist",
        label="com.ateles.apis",
        env={"NEOTOMA_SSE_SUBSCRIPTION_ID_APIS": "only-in-repo"},
    )
    _write_plist(installed / "com.ateles.apis.plist", label="com.ateles.apis", env={})

    code_repo_only = parity.main(
        ["check_daemon_config_parity.py", str(repo), str(installed)]
    )
    err_repo_only = capsys.readouterr().err
    assert code_repo_only != 0
    assert "repo_only_subscription_key" in err_repo_only

    _write_plist(
        repo / "formica" / "com.ateles.formica.plist",
        label="com.ateles.formica",
        env={},
    )
    _write_plist(
        installed / "com.ateles.formica.plist",
        label="com.ateles.formica",
        env={"NEOTOMA_SSE_SUBSCRIPTION_ID_FORMICA": "only-installed"},
    )

    code_installed_only = parity.main(
        ["check_daemon_config_parity.py", str(repo), str(installed)]
    )
    err_installed_only = capsys.readouterr().err
    assert code_installed_only != 0
    assert "installed_only_subscription_key" in err_installed_only


def test_normalizes_hyphenated_daemon_labels(tmp_path: Path, capsys):
    repo = tmp_path / "repo" / "neotoma-agent"
    installed = tmp_path / "installed"
    bad_key = "NEOTOMA_SSE_SUBSCRIPTION_ID_NEOTOMA-AGENT"
    good_key = "NEOTOMA_SSE_SUBSCRIPTION_ID_NEOTOMA_AGENT"
    sub_id = "094bd211-73af-473b-94ea-14f79eccb083"

    _write_plist(
        repo / "com.ateles.neotoma-agent.plist",
        label="com.ateles.neotoma-agent",
        env={bad_key: sub_id},
    )
    _write_plist(
        installed / "com.ateles.neotoma-agent.plist",
        label="com.ateles.neotoma-agent",
        env={bad_key: sub_id},
    )

    code = parity.main(["check_daemon_config_parity.py", str(repo.parent), str(installed)])
    err = capsys.readouterr().err

    assert code != 0
    assert "invalid_posix_env_key" in err
    assert bad_key in err
    assert good_key in err
