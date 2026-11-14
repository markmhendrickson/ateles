"""Unit tests for the shared settings loader (ateles#885: one file, no silent skip)."""

import pytest

from lib.daemon_runtime import shared_env as se


def test_parse_strict_names_line_not_content(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=1\nnot a setting synthetic-secret\n")
    with pytest.raises(se.SharedEnvError) as e:
        se.parse_dotenv(f)
    assert "line 2" in str(e.value) and "synthetic-secret" not in str(e.value)


def test_environment_wins_and_empty_or_placeholder_is_unset(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=file\nB=file\nC=file\nexport D='q'\n")
    target = {"A": "given", "B": "", "C": "__C__"}
    report = se.load_shared_env(target, path=f)
    assert target == {"A": "given", "B": "file", "C": "file", "D": "q"}
    assert sorted(report.loaded) == ["B", "C", "D"]


def test_only_one_file_is_consulted(tmp_path):
    shared, other = tmp_path / "shared", tmp_path / "other"
    shared.write_text("A=shared\n")
    other.write_text("A=stale\nB=stale\n")
    target = {"ATELES_DOTENV_FILE": str(shared)}
    se.load_shared_env(target)
    assert target["A"] == "shared" and "B" not in target


def test_require_settings_names_every_missing_setting(tmp_path):
    target = {"ATELES_DOTENV_FILE": str(tmp_path / "absent")}
    with pytest.raises(se.MissingSettingError) as e:
        se.require_settings("X_ONE", "X_TWO", purpose="do a thing", environ=target)
    msg = str(e.value)
    assert "X_ONE" in msg and "X_TWO" in msg and str(tmp_path / "absent") in msg


def test_non_strict_reports_a_bad_file_on_stderr(tmp_path, capsys):
    f = tmp_path / ".env"
    f.write_text("junk line\n")
    se.load_shared_env({}, path=f, strict=False)
    assert "line 1" in capsys.readouterr().err
