"""Unit tests for the shared settings loader (ateles#885: one file, no silent skip)."""

import pytest

from lib.daemon_runtime import shared_env as se


def test_parse_strict_names_line_not_content(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=1\nnot a setting synthetic-secret\n")
    with pytest.raises(se.SharedEnvError) as e:
        se.parse_dotenv(f)
    assert ": 2" in str(e.value) and "synthetic-secret" not in str(e.value)


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
    assert ": 1" in capsys.readouterr().err


# --- round 2: a bad line must not strip the other settings -------------------

BAD_LINES = {
    "dashed name": "MY-KEY=SYNTHVAL",
    "dotted name": "MY.KEY=SYNTHVAL",
    "words only": "a stray note with synthetic-secret-words",
    "colon form": "NAME: SYNTHVAL",
}


@pytest.mark.parametrize("bad", list(BAD_LINES.values()), ids=list(BAD_LINES))
def test_one_bad_line_among_good_ones_loads_the_good_ones(tmp_path, capsys, bad):
    f = tmp_path / ".env"
    f.write_text(f"GOOD_ONE=aaa\n{bad}\nGOOD_TWO=ccc\n")
    target = {}
    report = se.load_shared_env(target, path=f, strict=False)
    assert target == {"GOOD_ONE": "aaa", "GOOD_TWO": "ccc"}
    assert report.loaded == ["GOOD_ONE", "GOOD_TWO"]
    err = capsys.readouterr().err
    assert str(f) in err and ": 2 " in err
    assert err.count("WARNING") == 1
    assert "SYNTHVAL" not in err and "synthetic-secret" not in err


def test_multiline_quoted_value_loads_the_rest(tmp_path, capsys):
    f = tmp_path / ".env"
    f.write_text('GOOD_ONE=aaa\nKEY="first\nsynthetic-continuation"\nGOOD_TWO=ccc\n')
    target = {}
    se.load_shared_env(target, path=f, strict=False)
    assert target["GOOD_ONE"] == "aaa" and target["GOOD_TWO"] == "ccc"
    err = capsys.readouterr().err
    assert "synthetic-continuation" not in err and "3" in err


def test_leading_byte_order_mark_still_loads_the_first_line(tmp_path, capsys):
    f = tmp_path / ".env"
    f.write_bytes(b"\xef\xbb\xbfFIRST=one\nSECOND=two\n")
    target = {}
    se.load_shared_env(target, path=f, strict=False)
    assert target == {"FIRST": "one", "SECOND": "two"}
    assert capsys.readouterr().err == ""


def test_several_bad_lines_are_reported_once_by_number(tmp_path, capsys):
    f = tmp_path / ".env"
    f.write_text("A=1\nbad one\nB=2\nbad-two=x\nC=3\nbad three\n")
    target = {}
    se.load_shared_env(target, path=f, strict=False)
    assert target == {"A": "1", "B": "2", "C": "3"}
    err = capsys.readouterr().err
    assert err.count("WARNING") == 1 and "2, 4, 6" in err
    se.load_shared_env({}, path=f, strict=False)  # same file again: not repeated
    assert capsys.readouterr().err == ""


def test_strict_still_raises_for_bad_lines(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=1\nbad line\n")
    with pytest.raises(se.SharedEnvError):
        se.load_shared_env({}, path=f, strict=True)


def test_placeholder_is_exact_shape_not_any_double_underscore_value():
    assert se._unset("__NEOTOMA_TOKEN__") and se._unset("  ") and se._unset("")
    assert not se._unset("__real") and not se._unset("__a b__")


def test_fallback_fills_only_unset_names_and_warns_by_name(tmp_path, capsys):
    legacy = tmp_path / "legacy.env"
    legacy.write_text("ONLY_LEGACY=synthetic-legacy-value\nBOTH=legacy\n")
    target = {"BOTH": "shared", "ATELES_DOTENV_FILE": str(tmp_path / "shared")}
    used = se.load_fallback_file(legacy, target)
    assert used == ["ONLY_LEGACY"]
    assert target["BOTH"] == "shared" and target["ONLY_LEGACY"] == "synthetic-legacy-value"
    err = capsys.readouterr().err
    assert "ONLY_LEGACY" in err and str(legacy) in err and "BOTH" not in err
    assert "synthetic-legacy-value" not in err


# --- should_load is the pytest / skip guard (ateles#1285) ---------------------


def test_should_load_false_under_pytest_module():
    assert se.should_load(environ={}, modules={"pytest": object()}) is False


def test_should_load_false_with_pytest_current_test():
    assert se.should_load(environ={"PYTEST_CURRENT_TEST": "x::y"}, modules={}) is False


@pytest.mark.parametrize("flag", ["1", "true", "yes", "TRUE"])
def test_should_load_false_with_skip_flag(flag):
    assert se.should_load(environ={"ATELES_SKIP_DOTENV": flag}, modules={}) is False


def test_should_load_true_otherwise_and_package_agrees():
    from lib.daemon_runtime import _dotenv_should_load

    assert se.should_load(environ={}, modules={}) is True
    assert se.should_load(environ={"ATELES_SKIP_DOTENV": "0"}, modules={}) is True
    assert _dotenv_should_load(environ={}, modules={}) is True
    assert _dotenv_should_load(environ={"ATELES_SKIP_DOTENV": "1"}, modules={}) is False
