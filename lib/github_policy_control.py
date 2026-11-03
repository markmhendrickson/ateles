"""Trusted parent side of the directly awaited, fixed owner-policy reader."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from lib.github_account import control_environment, validate_login


def read_owner_policy(repository: str, login: str) -> dict:
    """No credential crosses this interface, only actual bounded provider data.

    The executable/source are fixed in the trusted control checkout, never task
    fields, cwd, a model checkout, a cached file or a task-authored assertion.
    """
    validate_login(login)
    helper = Path(__file__).resolve().with_name("github_owner_policy.py")
    result = subprocess.run(
        [sys.executable, "-I", str(helper), repository, login],
        cwd=helper.parent, env=control_environment(), stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=45, check=False)
    if result.returncode or len(result.stdout) > 524288:
        raise ValueError("the explicit owner policy reader did not complete")
    answer = json.loads(result.stdout)
    if (not isinstance(answer, dict) or set(answer) != {
            "principal", "principal_id", "host", "repository", "default_branch", "rulesets", "details"}
            or answer["principal"] != login
            or isinstance(answer["principal_id"], bool) or not isinstance(answer["principal_id"], int)
            or answer["principal_id"] <= 0 or answer["host"] != "github.com"
            or str(answer["repository"]).lower() != repository.lower()
            or not isinstance(answer["default_branch"], str) or not answer["default_branch"]
            or not isinstance(answer["rulesets"], list) or len(answer["rulesets"]) >= 100
            or not isinstance(answer["details"], list) or len(answer["details"]) >= 100):
        raise ValueError("the explicit owner policy response did not match its invocation")
    return answer
