"""Render controlled cleanup outcomes through the actual job/eval assertion."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("case", ["residual", "overflow", "root_link", "dangling_link", "root_file",
                                  "root_lstat", "listdir", "entry_lstat", "walk"])
def test_actual_eval_cleanup_failure_keeps_only_bounded_evidence(tmp_path, case):
    here = Path(__file__).parent
    script = tmp_path / "test_eval_output.py"
    evidence = tmp_path / "evidence.json"
    script.write_text('''import errno
import json
import os
import sys
from pathlib import Path
import pytest
sys.path.insert(0, HERE)
pytest.register_assert_rewrite("test_fly_delivery_eval")
import test_fly_delivery_eval as target

def test_actual_eval(tmp_path, monkeypatch):
    original = target.runner.rd.remove_work
    original_lstat = Path.lstat
    original_listdir = os.listdir
    original_walk = os.walk
    filename = "synthetic_private_cleanup_filename"
    def controlled(root):
        original(root)
        if CASE == "root_file":
            root.rmdir()
            root.write_text("synthetic_private_cleanup_content")
        elif CASE in {"root_link", "dangling_link"}:
            root.rmdir()
            outside = tmp_path / "synthetic_private_outside"
            if CASE == "root_link":
                outside.mkdir()
                (outside / filename).write_text("synthetic_private_cleanup_content")
            root.symlink_to(outside, target_is_directory=True)
        else:
            for number in range(65 if CASE == "overflow" else 1):
                (root / (filename + str(number))).write_text("synthetic_private_cleanup_content")
        if CASE in {"root_lstat", "entry_lstat"}:
            def failed_lstat(path, *args, **kwargs):
                if path == root and CASE == "root_lstat" or path.parent == root and CASE == "entry_lstat":
                    raise OSError(errno.EACCES, filename, str(path))
                return original_lstat(path, *args, **kwargs)
            monkeypatch.setattr(Path, "lstat", failed_lstat)
        if CASE == "listdir":
            def failed_listdir(path):
                if Path(path) == root:
                    raise OSError(errno.EACCES, filename, str(path))
                return original_listdir(path)
            monkeypatch.setattr(os, "listdir", failed_listdir)
        if CASE == "walk":
            def failed_walk(path, *args, **kwargs):
                if Path(path) == root:
                    if kwargs.get("onerror"):
                        kwargs["onerror"](OSError(errno.EACCES, filename, str(path)))
                    return iter(())
                return original_walk(path, *args, **kwargs)
            monkeypatch.setattr(os, "walk", failed_walk)
    monkeypatch.setattr(target.runner.rd, "remove_work", controlled)
    run_scenario = target.runner.run_scenario
    def capture(*args, **kwargs):
        trace = run_scenario(*args, **kwargs)
        branch = "refs/heads/" + target.runner.BRANCH
        assert trace.rc == 0
        assert trace.record["delivery_status"] == "opened"
        assert trace.record["pr_number"] == 7
        assert trace.remote_after[branch] == trace.record["delivery_commit"]
        assert trace.remote_after[branch] != trace.remote_before.get(branch)
        assert [call["method"] for call in trace.api_calls] == ["GET", "POST"]
        Path(EVIDENCE).write_text(json.dumps(trace.cleanup))
        return trace
    monkeypatch.setattr(target.runner, "run_scenario", capture)
    target.test_scenario(target._by_id("refused_import_pushes_nothing"), tmp_path, monkeypatch)
'''.replace("HERE", repr(str(here))).replace("CASE", repr(case))
       .replace("EVIDENCE", repr(str(evidence))))
    runtime = tmp_path / "runtime"
    done = subprocess.run([sys.executable, "-m", "pytest", str(script), "-vv", "--tb=short",
                           "--no-header", "--basetemp", str(runtime), "-p", "no:cacheprovider"],
                          capture_output=True, text=True, timeout=60)
    output = done.stdout + done.stderr
    assert done.returncode == 1 and "1 failed" in output
    assert "the work directory was left behind" in output
    assert '"leftovers":' in output and '"root":' in output
    assert all(value not in output for value in (
        "synthetic_private_cleanup_filename", "synthetic_private_cleanup_content",
        "synthetic_private_outside", str(runtime)))
    details = json.loads(evidence.read_text())
    assert len(details["leftovers"]) <= 64
    if case == "overflow":
        assert '"truncated": true' in output
        assert details["truncated"] is True and len(details["leftovers"]) == 64
    elif case in {"root_lstat", "listdir", "entry_lstat", "walk"}:
        assert '"incomplete": true' in output and '"errno": 13' in output
    elif case in {"root_link", "dangling_link"}:
        assert '"kind": "link"' in output
    elif case == "root_file":
        assert details["root"]["kind"] == "file"
    else:
        assert '"relative": "<entry>"' in output and '"kind": "file"' in output
