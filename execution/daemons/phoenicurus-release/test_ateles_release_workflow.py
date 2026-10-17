"""Bind changelog preparation to the actual workflow and its existing CI lane."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "execution/scripts"))

import pytest  # noqa: E402

from test_public_rule_sources import _load, _triggers  # noqa: E402

WORKFLOW = ROOT / ".github/workflows/ateles-release.yml"
ARTIFACT_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
SHA = "a" * 40


def preparation_contract(document):
    assert _triggers(document) == {"push", "workflow_dispatch"}
    assert document.get("on", document.get(True))["push"]["branches"] == ["main"]
    assert document["permissions"] == {"contents": "read"}
    assert set(document["jobs"]) == {"prepare"}
    job = document["jobs"]["prepare"]
    assert set(job) == {"runs-on", "steps"}
    assert job["runs-on"] == "ubuntu-latest"
    steps = job["steps"]
    assert [step.get("id") for step in steps] == [None, "tag", "check", "notes", None]
    assert [step["uses"] for step in steps if "uses" in step] == [
        "actions/checkout@v4",
        ARTIFACT_ACTION,
    ]
    assert steps[0]["with"] == {
        "fetch-depth": 0,
        "persist-credentials": False,
        "ref": "${{ github.sha }}",
    }
    assert not re.search(
        r"secrets\.|(?:GH|GITHUB)_TOKEN|contents:\s*write", json.dumps(document)
    )
    assert not re.search(
        r"\bgh\s+release\b|\bgit\s+(?:push|tag\s+[^\n$\"()])", json.dumps(steps)
    )
    for step in steps[1:4]:
        assert set(step) <= {"name", "id", "if", "env", "run"}
    assert steps[3]["if"] == steps[4]["if"] == "steps.check.outputs.count != '0'"
    assert steps[4]["with"] == {
        "name": "ateles-notes-${{ github.sha }}",
        "path": "${{ runner.temp }}/ateles-release-notes/notes.md",
        "if-no-files-found": "error",
    }
    return steps


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_actual_workflow_prepares_without_publication(event):
    document = _load(WORKFLOW.read_text())
    assert event in _triggers(document)
    preparation_contract(document)


def test_existing_ci_collects_this_regression():
    ci = _load((ROOT / ".github/workflows/ateles-tests.yml").read_text())
    triggers = ci.get("on", ci.get(True))
    assert ".github/workflows/**" in triggers["pull_request"]["paths"]
    assert (
        "execution/daemons/phoenicurus-release/**" in triggers["pull_request"]["paths"]
    )
    commands = [
        step.get("run", "") for job in ci["jobs"].values() for step in job["steps"]
    ]
    assert any(
        "pytest execution/daemons/phoenicurus-release/ -q" in c for c in commands
    )


def _executable(path, body):
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o700)


def run_preparation(
    tmp_path, *, prev="v2030.01.01", count=2, actual_sha=SHA, document=None
):
    """Execute real run blocks with finite fake command boundaries, no real git/gh."""
    steps = preparation_contract(document or _load(WORKFLOW.read_text()))
    binary = tmp_path / "bin"
    binary.mkdir()
    trace = tmp_path / "commands.jsonl"
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"prev": prev, "count": count, "sha": actual_sha}))
    _executable(
        binary / "git",
        """
import json, os, sys
from pathlib import Path
a = sys.argv[1:]
with open(os.environ['COMMAND_TRACE'], 'a') as stream:
    stream.write(json.dumps(a) + '\\n')
f = json.loads(Path(os.environ['COMMAND_FIXTURE']).read_text())
if a == ['rev-parse', 'HEAD']: print(f['sha'])
elif a == ['tag']: print('v2030.01.02')
elif a == ['describe', '--tags', '--abbrev=0', os.environ['SOURCE_SHA']]:
    if not f['prev']: sys.exit(1)
    print(f['prev'])
elif a == ['rev-list', '--no-merges', '--count', f['prev']+'..'+os.environ['SOURCE_SHA']]:
    print(f['count'])
elif a == ['log', '--no-merges', '--pretty=format:- %s (%h)',
           (f['prev']+'..' if f['prev'] else '')+os.environ['SOURCE_SHA']]:
    print('- synthetic change (aaaaaaa)')
else: sys.exit('Refused command outside preparation read boundary')
""",
    )
    _executable(binary / "gh", "raise SystemExit('Publication boundary invoked')\n")
    _executable(binary / "date", "print('2030.01.02')\n")
    _executable(
        binary / "python3",
        f"""
import os, sys
expected = 'execution/daemons/phoenicurus-release/tag_utils.py'
if len(sys.argv) < 3 or sys.argv[1] != expected or sys.argv[2] not in (
    'compute-tag', 'has-releasable-commits'
): raise SystemExit('Refused indirect script')
os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
""",
    )
    for command in ["mkdir", "cat"]:
        os.symlink(shutil.which(command), binary / command)
    # Only synthetic fixture inputs and the selected commands reach these shells.
    env = {
        "PATH": str(binary),
        "RUNNER_TEMP": str(tmp_path),
        "COMMAND_TRACE": str(trace),
        "COMMAND_FIXTURE": str(fixture),
    }
    outputs = {}
    replacements = {
        "github.sha": SHA,
        "github.server_url": "https://github.example",
        "github.repository": "example/project",
        "runner.temp": str(tmp_path),
    }
    for step in steps:
        if "run" not in step:
            continue
        if step.get("if") and outputs["check"]["count"] == "0":
            continue
        output = tmp_path / (step["id"] + ".output")
        replacements.update(
            {
                f"steps.{key}.outputs.{name}": value
                for key, values in outputs.items()
                for name, value in values.items()
            }
        )

        def render(value):
            return re.sub(
                r"\$\{\{\s*([^}]+?)\s*\}\}", lambda match: replacements[match[1]], value
            )

        local_env = {
            **env,
            "GITHUB_OUTPUT": str(output),
            **{key: render(value) for key, value in step.get("env", {}).items()},
        }
        subprocess.run(
            ["/bin/bash", "-euo", "pipefail", "-c", render(step["run"])],
            cwd=ROOT,
            env=local_env,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        outputs[step["id"]] = (
            dict(line.split("=", 1) for line in output.read_text().splitlines())
            if output.exists()
            else {}
        )
    return (
        outputs,
        tmp_path / "ateles-release-notes/notes.md",
        [json.loads(line) for line in trace.read_text().splitlines()],
    )


@pytest.mark.parametrize(
    "prev,count", [("v2030.01.01", 2), ("", 0), ("v2030.01.01", 0)]
)
def test_actual_run_blocks_bind_notes_and_skip_empty_history(tmp_path, prev, count):
    outputs, notes, calls = run_preparation(tmp_path, prev=prev, count=count)
    assert outputs["tag"]["tag"] == "v2030.01.02.1"
    expected = not prev or count != 0
    assert outputs["check"]["count"] == ("1" if expected else "0")
    assert notes.exists() == expected
    if expected:
        text = notes.read_text()
        assert SHA in text and "v2030.01.02.1" in text and "synthetic change" in text
        assert "not a published release" in text
        if prev:
            assert f"/compare/{prev}...{SHA}" in text
            assert f"/compare/{prev}...v2030" not in text
    assert all(a[0] not in {"push", "release"} for a in calls)
    assert ["tag"] in calls  # List only; never creates a real or fake tag.


def test_actual_run_blocks_refuse_wrong_checked_out_source(tmp_path):
    with pytest.raises(subprocess.CalledProcessError):
        run_preparation(tmp_path, actual_sha="b" * 40)


@pytest.mark.parametrize("mutation", ["write", "publisher", "secret", "reusable_job"])
def test_actual_workflow_contract_rejects_privileged_mutations(mutation):
    document = deepcopy(_load(WORKFLOW.read_text()))
    if mutation == "write":
        document["permissions"]["contents"] = "write"
    elif mutation == "publisher":
        document["jobs"]["prepare"]["steps"].append(
            {"name": "Publish", "run": "gh release create candidate"}
        )
    elif mutation == "secret":
        document["jobs"]["prepare"]["steps"][1]["env"]["GH_TOKEN"] = (
            "${{ secrets.GITHUB_TOKEN }}"
        )
    else:
        document["jobs"]["publish"] = {"uses": "./.github/workflows/publish.yml"}
    with pytest.raises(AssertionError):
        preparation_contract(document)


def test_actual_run_blocks_refuse_an_indirect_script(tmp_path):
    document = deepcopy(_load(WORKFLOW.read_text()))
    tag_step = document["jobs"]["prepare"]["steps"][1]
    tag_step["run"] = tag_step["run"].replace("tag_utils.py", "publish.py")
    # The same shell runner reaches the finite Python command boundary; it
    # cannot execute the substituted script or read any publication credential.
    with pytest.raises(subprocess.CalledProcessError) as error:
        run_preparation(tmp_path, document=document)
    assert "Refused indirect script" in error.value.stderr
