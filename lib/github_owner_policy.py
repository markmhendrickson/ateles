"""Short-lived trusted control reader; stdout contains only bounded policy data.

Fixed interface: python -I <this trusted file> owner/repo expected-login.
Never invoked from a model checkout. No credential is returned to the parent,
placed in argv/env/files, or given to another subprocess after account lookup.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import sys

# -I excludes cwd/PYTHONPATH. Load only the adjacent trusted shared helper;
# never import daemon bootstrap or a module from the model's repository.
_spec = importlib.util.spec_from_file_location("_control_github_account", Path(__file__).resolve().with_name("github_account.py"))
account = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(account)


def read_policy(repository: str, login: str) -> dict:
    if (not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository)
            or ".." in repository or repository.endswith(".git")):
        raise account.AccountReadError("invalid selected policy repository")
    account.validate_login(login)
    credential, principal_id = account.verified_account_from_gh(login)
    repo = account.fixed_get(f"/repos/{repository}", credential)
    if (not isinstance(repo, dict) or str(repo.get("full_name", "")).lower() != repository.lower()
            or not isinstance(repo.get("default_branch"), str) or not repo["default_branch"]):
        raise account.AccountReadError("the policy repository identity did not match")
    rows = account.fixed_get(f"/repos/{repository}/rulesets?includes_parents=true&per_page=100", credential)
    if not isinstance(rows, list) or len(rows) >= 100:
        raise account.AccountReadError("the applicable policy listing is incomplete")
    summaries, details = [], []
    for row in rows:
        if not isinstance(row, dict):
            raise account.AccountReadError("the applicable policy listing is malformed")
        number = row.get("id")
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise account.AccountReadError("the applicable policy identity is malformed")
        summaries.append({k: row.get(k) for k in ("id", "source_type", "source", "target", "enforcement")})
        if row.get("target") != "branch" or row.get("enforcement") != "active":
            continue
        detail = account.fixed_get(f"/repos/{repository}/rulesets/{number}", credential)
        if not isinstance(detail, dict):
            raise account.AccountReadError("the policy details are malformed")
        # Only provider policy fields needed by the existing qualifier cross
        # the directly awaited pipe. Omission remains omission, including bypass.
        details.append({k: detail[k] for k in ("id", "source_type", "source", "target", "enforcement", "conditions", "bypass_actors", "rules") if k in detail})
    result = {"principal": login, "principal_id": principal_id, "host": account.HOST, "repository": repo["full_name"],
              "default_branch": repo["default_branch"], "rulesets": summaries, "details": details}
    encoded = json.dumps(result)
    def contains_credential(value):
        if isinstance(value, str):
            return credential in value
        if isinstance(value, dict):
            return any(contains_credential(k) or contains_credential(v) for k, v in value.items())
        if isinstance(value, list):
            return any(contains_credential(v) for v in value)
        return False
    if contains_credential(result) or len(encoded.encode()) > 524288:
        raise account.AccountReadError("the bounded policy response cannot be returned")
    return result


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) != 2:
            raise account.AccountReadError("the selected repository and expected account are required")
        result = read_policy(*args)
    except Exception:
        # No provider stderr, credential/header, exception repr or traceback.
        print(json.dumps({"error": "the explicit owner policy read was not qualified"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
