"""Render controlled cleanup outcomes through the actual job/eval assertion."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


def _run_cleanup_fixture(tmp_path, case, prior_cleanup_failure, scenario, *, inject="none"):
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
    from execution.daemons.apis.fly_cleanup_test_support import cleanup_error
    filename = "synthetic_private_cleanup_filename"
    original_write = Path.write_text
    if INJECT == "original_refused":
        def original(root):
            raise target.runner.rd.RepoWorkError(target.runner.rd.WORK_FAILED,
                                                "synthetic_private_fixture_error")
    fixture = {"prior_cleanup_failure": False, "faults": [],
               "original_cleanup": "not_attempted", "planting": "not_started",
               "errors": []}
    if INJECT == "clear_refused":
        original_clear = target.runner.rd._clear
        def clear_refused(root):
            if fixture["original_cleanup"] == "started":
                raise PermissionError(errno.EACCES, "synthetic_private_fixture_error", str(root))
            return original_clear(root)
        monkeypatch.setattr(target.runner.rd, "_clear", clear_refused)
    original_clear_step = target.runner.rd._clear
    def capture_clear(root):
        try:
            return original_clear_step(root)
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            if fixture["original_cleanup"] == "started" and not fixture["errors"]:
                fixture["errors"].append(cleanup_error(exc))
            raise
    monkeypatch.setattr(target.runner.rd, "_clear", capture_clear)
    def controlled(root):
        if INJECT == "plant_refused":
            def failed_write(path, *args, **kwargs):
                if path == root or path.parent == root:
                    raise OSError(errno.EACCES, "synthetic_private_fixture_error", str(path))
                return original_write(path, *args, **kwargs)
            monkeypatch.setattr(Path, "write_text", failed_write)
        try:
            if PRIOR_CLEANUP_FAILURE:
                fixture["prior_cleanup_failure"] = True
                raise target.runner.rd.RepoWorkError(target.runner.rd.WORK_FAILED,
                                                    "synthetic prior cleanup refusal")
            fixture["original_cleanup"] = "started"
            original(root)
            fixture["original_cleanup"] = "returned"
            fixture["planting"] = "started"
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
            fixture["planting"] = "completed"
            if INJECT == "replace_completed":
                root.unlink()
                root.mkdir()
                (root / filename).write_text("synthetic_private_cleanup_content")
        except (target.runner.rd.RepoWorkError, OSError) as exc:
            if fixture["original_cleanup"] == "started":
                fixture["original_cleanup"] = "refused"
            elif fixture["planting"] == "started":
                fixture["planting"] = "refused"
            if not fixture["errors"]:
                fixture["errors"] = [cleanup_error(
                    exc, boundary_error=target.runner.rd.RepoWorkError)]
            if isinstance(exc, target.runner.rd.RepoWorkError):
                raise
            # Fixture-only normalization; explicit stage assertions still fail.
            raise target.runner.rd.RepoWorkError(
                target.runner.rd.WORK_FAILED, "diagnostic fixture planting failed") from None
        finally:
            # A real cleanup refusal must not skip the intended observation fault.
            if CASE in {"root_lstat", "entry_lstat"}:
                def failed_lstat(path, *args, **kwargs):
                    if path == root and CASE == "root_lstat" or path.parent == root and CASE == "entry_lstat":
                        fixture["faults"].append(CASE)
                        raise OSError(errno.EACCES, filename, str(path))
                    return original_lstat(path, *args, **kwargs)
                monkeypatch.setattr(Path, "lstat", failed_lstat)
            if CASE == "listdir":
                def failed_listdir(path):
                    if Path(path) == root:
                        fixture["faults"].append(CASE)
                        raise OSError(errno.EACCES, filename, str(path))
                    return original_listdir(path)
                monkeypatch.setattr(os, "listdir", failed_listdir)
            if CASE == "walk":
                def failed_walk(path, *args, **kwargs):
                    if Path(path) == root:
                        fixture["faults"].append(CASE)
                        if kwargs.get("onerror"):
                            kwargs["onerror"](OSError(errno.EACCES, filename, str(path)))
                        return iter(())
                    return original_walk(path, *args, **kwargs)
                monkeypatch.setattr(os, "walk", failed_walk)
            Path(EVIDENCE).with_name("fixture-proof.json").write_text(json.dumps(fixture))
    monkeypatch.setattr(target.runner.rd, "remove_work", controlled)
    run_scenario = target.runner.run_scenario
    def capture(*args, **kwargs):
        trace = run_scenario(*args, **kwargs)
        Path(EVIDENCE).write_text(json.dumps(trace.cleanup))
        Path(EVIDENCE).with_name("fixture-proof.json").write_text(json.dumps(fixture))
        branch = "refs/heads/" + target.runner.BRANCH
        assert trace.rc == 0
        if SCENARIO == "delivery_pushes_and_opens_a_pull_request":
            assert trace.record["delivery_status"] == "opened"
            assert trace.record["pr_number"] == 7
            assert trace.record["pr_url"] == target.runner._pr(7)["html_url"]
            assert trace.remote_after[branch] == trace.record["delivery_commit"]
            assert trace.remote_after[branch] != trace.remote_before.get(branch)
            assert [call["method"] for call in trace.api_calls] == ["GET", "POST"]
        else:
            assert trace.record["delivery_status"] == "not_delivered"
            assert trace.remote_after.get(branch) == trace.remote_before.get(branch)
            assert trace.api_calls == []
        if not PRIOR_CLEANUP_FAILURE and (
                fixture["original_cleanup"] != "returned" or fixture["planting"] != "completed"):
            pytest.fail("diagnostic fixture stage failure: " + json.dumps({
                "original_cleanup": fixture["original_cleanup"],
                "planting": fixture["planting"], "errors": fixture["errors"],
                "cleanup": trace.cleanup}), pytrace=False)
        return trace
    monkeypatch.setattr(target.runner, "run_scenario", capture)
    target.test_scenario(target._by_id(SCENARIO), tmp_path, monkeypatch)
'''.replace("HERE", repr(str(here))).replace("CASE", repr(case))
       .replace("EVIDENCE", repr(str(evidence))).replace("SCENARIO", repr(scenario))
       .replace("PRIOR_CLEANUP_FAILURE", repr(prior_cleanup_failure))
       .replace("INJECT", repr(inject)))
    runtime = tmp_path / "runtime"
    done = subprocess.run([sys.executable, "-m", "pytest", str(script), "-vv", "--tb=short",
                           "--no-header", "--basetemp", str(runtime), "-p", "no:cacheprovider"],
                          capture_output=True, text=True, timeout=60, check=False)
    output = done.stdout + done.stderr
    details = json.loads(evidence.read_text()) if evidence.exists() else None
    proof_path = evidence.with_name("fixture-proof.json")
    proof = json.loads(proof_path.read_text()) if proof_path.exists() else None
    return done, output, details, proof, runtime


def _assert_cleanup_fixture(result, case, prior_cleanup_failure):
    done, output, details, proof, runtime = result
    stage_evidence = "diagnostic fixture stage evidence: " + json.dumps({
        "original_cleanup": proof["original_cleanup"],
        "planting": proof["planting"], "errors": proof["errors"],
        "cleanup": details})
    if not prior_cleanup_failure:
        assert proof["original_cleanup"] == "returned" and proof["planting"] == "completed", stage_evidence
    assert done.returncode == 1 and "1 failed" in output
    assert "the work directory was left behind" in output
    assert '"leftovers":' in output and '"root":' in output
    assert all(value not in output for value in (
        "synthetic_private_cleanup_filename", "synthetic_private_cleanup_content",
        "synthetic_private_outside", str(runtime)))
    assert proof["prior_cleanup_failure"] is prior_cleanup_failure
    assert len(details["leftovers"]) <= 64
    if case == "overflow":
        assert '"truncated": true' in output
        assert details["truncated"] is True and len(details["leftovers"]) == 64
    elif case in {"root_lstat", "listdir", "entry_lstat", "walk"}:
        assert proof["faults"] and set(proof["faults"]) == {case}
        assert details["incomplete"] is True
        assert any(row.get("errno") == 13 for row in details["leftovers"])
        assert '"incomplete": true' in output and '"errno": 13' in output
    elif case in {"root_link", "dangling_link"}:
        assert '"kind": "link"' in output
    elif case == "root_file":
        assert details["root"]["kind"] == "file", stage_evidence
    else:
        assert '"relative": "<entry>"' in output and '"kind": "file"' in output


@pytest.mark.parametrize("scenario", ["delivery_pushes_and_opens_a_pull_request",
                                      "refused_import_pushes_nothing"])
@pytest.mark.parametrize("case, prior_cleanup_failure", [
    (case, False) for case in (
        "residual", "overflow", "root_link", "dangling_link", "root_file",
        "root_lstat", "listdir", "entry_lstat", "walk")
] + [(case, True) for case in ("root_lstat", "listdir", "entry_lstat", "walk")])
def test_actual_eval_cleanup_failure_keeps_only_bounded_evidence(
        tmp_path, case, prior_cleanup_failure, scenario):
    result = _run_cleanup_fixture(tmp_path, case, prior_cleanup_failure, scenario)
    _assert_cleanup_fixture(result, case, prior_cleanup_failure)


@pytest.mark.parametrize("scenario", ["delivery_pushes_and_opens_a_pull_request",
                                      "refused_import_pushes_nothing"])
@pytest.mark.parametrize("case", ["root_file", "root_lstat"])
@pytest.mark.parametrize("inject", ["original_refused", "plant_refused"])
def test_fixture_stage_refusal_is_bounded_and_cannot_pass(tmp_path, scenario, case, inject):
    result = _run_cleanup_fixture(tmp_path, case, False, scenario, inject=inject)
    done, output, details, proof, runtime = result
    assert done.returncode == 1 and "1 failed" in output
    assert all(value not in output for value in (
        "synthetic_private_fixture_error", "synthetic_private_cleanup_filename",
        "synthetic_private_cleanup_content", str(runtime)))
    assert "diagnostic fixture stage failure" in output
    assert details is not None and proof is not None
    assert proof["original_cleanup"] == ("refused" if inject == "original_refused" else "returned")
    assert proof["planting"] == ("not_started" if inject == "original_refused" else "refused")
    assert len(proof["errors"]) == 1
    assert set(proof["errors"][0]) == {"type", "errno"}
    assert proof["errors"] == ([{"type": "RepoWorkError", "errno": None}]
                               if inject == "original_refused" else
                               [{"type": "PermissionError", "errno": 13}])
    if case == "root_lstat":
        assert proof["faults"] and details["incomplete"]
    elif inject == "plant_refused":
        assert details["root"] is None  # An absent root cannot hide fixture failure.
    with pytest.raises(AssertionError, match="diagnostic fixture stage evidence"):
        _assert_cleanup_fixture(result, case, False)


@pytest.mark.parametrize("scenario", ["delivery_pushes_and_opens_a_pull_request",
                                      "refused_import_pushes_nothing"])
@pytest.mark.parametrize("case", ["root_file", "root_lstat"])
def test_fixture_success_has_positive_stage_witness(tmp_path, scenario, case):
    result = _run_cleanup_fixture(tmp_path, case, False, scenario)
    _assert_cleanup_fixture(result, case, False)
    proof = result[3]
    assert proof["original_cleanup"] == "returned"
    assert proof["planting"] == "completed"
    assert proof["errors"] == []


@pytest.mark.parametrize("scenario", ["delivery_pushes_and_opens_a_pull_request",
                                      "refused_import_pushes_nothing"])
def test_completed_planting_with_different_root_still_fails(tmp_path, scenario):
    result = _run_cleanup_fixture(tmp_path, "root_file", False, scenario,
                                  inject="replace_completed")
    proof, details = result[3], result[2]
    assert proof["original_cleanup"] == "returned" and proof["planting"] == "completed"
    assert details["root"]["kind"] == "directory"
    with pytest.raises(AssertionError, match="diagnostic fixture stage evidence"):
        _assert_cleanup_fixture(result, "root_file", False)


@pytest.mark.parametrize("scenario", ["delivery_pushes_and_opens_a_pull_request",
                                      "refused_import_pushes_nothing"])
@pytest.mark.parametrize("case", ["root_file", "root_lstat"])
def test_fixture_keeps_cleanup_error_before_boundary_normalization(tmp_path, scenario, case):
    result = _run_cleanup_fixture(tmp_path, case, False, scenario, inject="clear_refused")
    done, output, details, proof, runtime = result
    assert done.returncode == 1 and "1 failed" in output
    assert proof["original_cleanup"] == "refused" and proof["planting"] == "not_started"
    assert proof["errors"] == [{"type": "PermissionError", "errno": 13}]
    assert "diagnostic fixture stage failure" in output
    assert all(value not in output for value in (
        "synthetic_private_fixture_error", "synthetic_private_cleanup_filename",
        "synthetic_private_cleanup_content", str(runtime)))
    if case == "root_lstat":
        assert proof["faults"] and details["incomplete"]
    with pytest.raises(AssertionError, match="diagnostic fixture stage evidence"):
        _assert_cleanup_fixture(result, case, False)
