"""merge_into_env_file(remove=...), materialize_block and the atomic write.

No real sops or 1Password: the snapshot layer is a plaintext stand-in.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secrets_lib as sl  # noqa: E402


def test_merge_updates_adds_and_keeps_unmanaged(tmp_path):
    env = tmp_path / "dot"
    env.write_text('# comment\nKEEP="1"\nA="old"\n')
    changed = sl.merge_into_env_file(env, {"A": "new", "B": "added"})
    assert sorted(changed) == ["A", "B"]
    assert sl.parse_dotenv(env.read_text()) == {"KEEP": "1", "A": "new", "B": "added"}
    assert env.read_text().startswith("# comment\n")


def test_merge_remove_deletes_every_definition_of_the_key(tmp_path):
    env = tmp_path / "dot"
    env.write_text('X_NEXT="a"\nKEEP="1"\nX_NEXT="b"\n')
    assert sl.merge_into_env_file(env, {}, remove=["X_NEXT"]) == ["X_NEXT"]
    assert sl.parse_dotenv(env.read_text()) == {"KEEP": "1"}


def test_merge_remove_of_an_absent_key_changes_nothing(tmp_path):
    env = tmp_path / "dot"
    env.write_text('KEEP="1"\n')
    assert sl.merge_into_env_file(env, {}, remove=["X_NEXT"]) == []


def test_merge_refuses_update_and_remove_of_the_same_key(tmp_path):
    with pytest.raises(ValueError):
        sl.merge_into_env_file(tmp_path / "dot", {"A": "1"}, remove=["A"])


def test_merge_keeps_mode_and_leaves_no_temp_file(tmp_path):
    env = tmp_path / "dot"
    env.write_text('A="1"\n')
    os.chmod(env, 0o640)
    sl.merge_into_env_file(env, {"A": "2"})
    assert stat.S_IMODE(env.stat().st_mode) == 0o640
    assert [p.name for p in tmp_path.iterdir()] == ["dot"]


def test_merge_creates_a_new_file_0600(tmp_path):
    env = tmp_path / "sub" / "dot"
    sl.merge_into_env_file(env, {"A": "1"})
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_materialize_block_uses_the_block_target_and_removes(tmp_path, monkeypatch):
    (tmp_path / "apis.sops.enc").write_text("A=1\n")
    target = tmp_path / "dot"
    target.write_text('A="0"\nA_NEXT="x"\nOTHER="k"\n')
    monkeypatch.setattr(sl, "enc_file", lambda name: tmp_path / f"{name}.sops.enc")
    monkeypatch.setattr(
        sl, "sops_decrypt_dotenv", lambda src: sl.parse_dotenv(Path(src).read_text())
    )
    manifest = {"files": {"apis": {"target": str(target), "default": {}}}}
    dest, n, changed = sl.materialize_block("apis", manifest=manifest, remove=["A_NEXT"])
    assert dest == target and n == 1
    assert sorted(changed) == ["A", "A_NEXT"]
    assert sl.parse_dotenv(target.read_text()) == {"A": "1", "OTHER": "k"}


def test_materialize_block_without_a_snapshot_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "enc_file", lambda name: tmp_path / f"{name}.sops.enc")
    with pytest.raises(FileNotFoundError):
        sl.materialize_block("apis", manifest={"files": {"apis": {}}})


def test_block_env_file_precedence(tmp_path):
    m = {"files": {"a": {"target": "~/x.dot"}, "b": {}}}
    assert sl.block_env_file(m, "a", tmp_path / "o.dot") == tmp_path / "o.dot"
    assert sl.block_env_file(m, "a") == Path("~/x.dot").expanduser()
    assert sl.block_env_file(m, "b") == sl.DEFAULT_MATERIALIZE_TARGET


def test_materialize_cli_remove_flag(tmp_path, monkeypatch, capsys):
    import secrets_materialize

    (tmp_path / "apis.sops.enc").write_text("A=1\n")
    target = tmp_path / "dot"
    target.write_text('A="1"\nA_NEXT="x"\n')
    monkeypatch.setattr(sl, "enc_file", lambda name: tmp_path / f"{name}.sops.enc")
    monkeypatch.setattr(
        sl, "sops_decrypt_dotenv", lambda src: sl.parse_dotenv(Path(src).read_text())
    )
    monkeypatch.setattr(
        sl, "load_manifest",
        lambda: {"files": {"apis": {"target": str(target), "default": {}}}},
    )
    monkeypatch.setattr(sl, "SECRETS_BASE", tmp_path)
    assert secrets_materialize.main(["apis", "--remove", "A_NEXT"]) == 0
    assert sl.parse_dotenv(target.read_text()) == {"A": "1"}
    assert "1 changed" in capsys.readouterr().out
