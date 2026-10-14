"""Absence must never become a value in the Fly runner (ateles#1456).

Round after round of review found another place where a missing, null or
malformed field was quietly read as a default: an empty config, a zero spend,
a new accounting period, "another holder", a successful outcome. This test
makes that class mechanically visible. It parses ``fly_runner.py`` and
``deploy/fly-runner/run_job.py`` and fails on:

* ``.get(key, default)``: a lookup that substitutes a default;
* ``x or <literal>``, ``x or {}``, ``x or []``, ``x or ()`` and similar:
  an ``or`` whose fallback is a constant or an empty container;
* ``.setdefault(...)``;
* an ``except`` clause that returns ``True``, ``None``, ``""``, ``0`` or an
  empty container (a failure reported as success or as "nothing there").

Every finding must be on ``ALLOWLIST`` with a one-line reason, keyed
``file:function:expression``; an allowlist entry that no longer matches
anything fails the test too, so the list cannot rot.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
FILES = (
    "execution/daemons/apis/fly_runner.py",
    "deploy/fly-runner/run_job.py",
)

_EMPTY_CALLS = {"dict", "list", "tuple", "set", "str", "frozenset"}

ALLOWLIST: dict[str, str] = {
    "deploy/fly-runner/run_job.py:<module>:os.environ.get('ATELES_RUNNER_JOB_DIR') or DEFAULT_JOB_DIR":
        'unset means the fixed job directory',
    'deploy/fly-runner/run_job.py:_readable:return True':
        'a path it cannot check counts as reachable, so the probe fails closed',
    "deploy/fly-runner/run_job.py:auth_headers_for:jwk.get('iss') or httpsig.DEFAULT_AAUTH_ISSUER":
        "the signer's documented default issuer",
    "deploy/fly-runner/run_job.py:auth_headers_for:jwk.get('kid') or ''":
        'a key id is optional in the signature',
    "deploy/fly-runner/run_job.py:auth_headers_for:jwk.get('sub') or ''":
        "a missing subject cannot equal the role's and is refused",
    "deploy/fly-runner/run_job.py:auth_headers_for:str(jwk.get('kid') or '').strip() or None":
        'a key id is optional in the signature',
    'deploy/fly-runner/run_job.py:cap_stdout:secrets or {}':
        'a caller with no secrets has nothing to redact',
    "deploy/fly-runner/run_job.py:load_job:os.environ.get('FLY_MACHINE_ID', '')":
        'the machine id is a diagnostic label in the record',
    "deploy/fly-runner/run_job.py:load_job:spec.get('child_credential_names') or ()":
        'no child credentials is the narrowest child environment',
    "deploy/fly-runner/run_job.py:load_job:spec.get('child_user') or 'agent'":
        "the image's unprivileged user; child_identity refuses if it is missing",
    "deploy/fly-runner/run_job.py:load_job:spec.get('codex_home') or ''":
        'no CODEX_HOME override on a per-job machine',
    "deploy/fly-runner/run_job.py:load_job:spec.get('credential_names') or []":
        'no declared credentials; the identity check then refuses the job',
    "deploy/fly-runner/run_job.py:load_job:spec.get('login_home_links') or {}":
        'no links: a per-job machine has no login directory',
    "deploy/fly-runner/run_job.py:load_job:spec.get('neotoma_base_url') or ''":
        'absent is refused straight after (must be https)',
    "deploy/fly-runner/run_job.py:load_job:spec.get('neotoma_jwk_name') or ''":
        'absent means no key; refused unless a bearer is named',
    "deploy/fly-runner/run_job.py:load_job:spec.get('neotoma_token_name') or ''":
        'absent means no bearer; refused unless a key is named',
    "deploy/fly-runner/run_job.py:load_job:spec.get('probe_env_names') or ()":
        'no extra probe names; the fixed probe list always applies',
    "deploy/fly-runner/run_job.py:load_job:spec.get('probe_paths') or ()":
        'no extra probe paths; the fixed probe list always applies',
    "deploy/fly-runner/run_job.py:load_job:spec.get('repository') or ''":
        'the repository is a label in the record only',
    "deploy/fly-runner/run_job.py:load_job:spec.get('run_id') or ''":
        'absent is refused straight after',
    "deploy/fly-runner/run_job.py:ram_home:''.join((c for c in run_id if c.isalnum() or c in '-_'))[:40] or 'run'":
        'a cosmetic temp-directory name prefix',
    "deploy/fly-runner/run_job.py:run_cli:stdin or b''":
        'a job with no stdin file sends nothing on stdin',
    "execution/daemons/apis/fly_runner.py:FlyConfig.from_env:env.get('APIS_FLY_API_BASE', '')":
        'unset means the documented Machines API base URL',
    "execution/daemons/apis/fly_runner.py:FlyConfig.from_env:env.get('APIS_FLY_API_BASE', '').strip() or FLY_API_BASE_DEFAULT":
        'unset means the documented Machines API base URL',
    "execution/daemons/apis/fly_runner.py:FlyConfig.from_env:env.get('APIS_FLY_API_TOKEN', '')":
        'unset is reported by FlyConfig.missing() and refuses Fly',
    "execution/daemons/apis/fly_runner.py:FlyConfig.from_env:env.get('APIS_FLY_APP', '')":
        'unset is reported by FlyConfig.missing() and refuses Fly',
    "execution/daemons/apis/fly_runner.py:FlyConfig.from_env:env.get('APIS_FLY_CODEX_MACHINE_ID', '')":
        'unset: Codex is not eligible for Fly (build_job refuses)',
    "execution/daemons/apis/fly_runner.py:FlyConfig.from_env:env.get('APIS_FLY_CURSOR_LOGIN_DIRS', '')":
        'unset means the documented default Cursor login directories',
    "execution/daemons/apis/fly_runner.py:FlyConfig.from_env:env.get('APIS_FLY_CURSOR_MACHINE_ID', '')":
        'unset: Cursor is not eligible for Fly (build_job refuses)',
    "execution/daemons/apis/fly_runner.py:FlyConfig.from_env:env.get('APIS_FLY_GITHUB_APP_PREFIX', '')":
        'unset means the documented default GitHub App',
    "execution/daemons/apis/fly_runner.py:FlyConfig.from_env:env.get('APIS_FLY_GITHUB_APP_PREFIX', '').strip() or 'ATELES_REVIEWER_APP'":
        'unset means the documented default GitHub App',
    "execution/daemons/apis/fly_runner.py:FlyConfig.from_env:env.get('APIS_FLY_IMAGE', '')":
        'unset is reported by FlyConfig.missing() and refuses Fly',
    "execution/daemons/apis/fly_runner.py:FlyConfig.from_env:env.get('APIS_FLY_LEDGER_FILE', '')":
        'unset means the documented default ledger path',
    "execution/daemons/apis/fly_runner.py:FlyConfig.from_env:env.get('APIS_FLY_LOCAL_FALLBACK', '1')":
        'the documented pilot default: provably-unstarted launches may run locally',
    "execution/daemons/apis/fly_runner.py:FlyConfig.from_env:env.get('APIS_FLY_PROBE_ENV_NAMES', '')":
        'unset means no extra probe names; the fixed probe list always applies',
    "execution/daemons/apis/fly_runner.py:FlyConfig.from_env:env.get('APIS_FLY_REGION', '')":
        'unset is reported by FlyConfig.missing() and refuses Fly',
    "execution/daemons/apis/fly_runner.py:FlyExecutor._launch:created.get('instance_id') or ''":
        "the create answer's version only scopes the wait; absent means wait without it",
    "execution/daemons/apis/fly_runner.py:FlyExecutor._settle_locked:machine_id or '(unknown)'":
        'log text only',
    'execution/daemons/apis/fly_runner.py:FlyExecutor.maybe_reap:return []':
        'a failed reaper settles nothing: every reservation stays held',
    'execution/daemons/apis/fly_runner.py:FlyLedger.heartbeat:return':
        'no heartbeat on an unreadable ledger: liveness only gets staler, never fresher',
    "execution/daemons/apis/fly_runner.py:FlyMachinesClient._call:resp.json().get('error') or resp.text":
        'error message text only; the status code decides',
    'execution/daemons/apis/fly_runner.py:FlyMachinesClient.get:return None':
        'only an explicit 404: the documented absence of the machine',
    'execution/daemons/apis/fly_runner.py:FlyMachinesClient.get_lease:return None':
        'only an explicit 404: the documented absence of a lease',
    "execution/daemons/apis/fly_runner.py:_csv:raw or ''":
        'an unset list setting is an empty list',
    "execution/daemons/apis/fly_runner.py:_github_request:raw or b'{}'":
        "an empty GitHub body (revoke's 204); a mint without a token is refused",
    'execution/daemons/apis/fly_runner.py:_owner_heartbeat:return None':
        "None is 'unknown', which _reapable never treats as stale",
    'execution/daemons/apis/fly_runner.py:_parse_ts:return None':
        'an unreadable time: the reaper then charges the whole window',
    "execution/daemons/apis/fly_runner.py:build_job:jwk.get('sub') or ''":
        "a missing subject cannot equal the role's and is refused",
    "execution/daemons/apis/fly_runner.py:build_job:os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', '')":
        'unset is refused straight after: Claude is not eligible',
    "execution/daemons/apis/fly_runner.py:build_job:os.environ.get('NEOTOMA_BASE_URL', '')":
        'unset is refused straight after (must be https)',
    "execution/daemons/apis/fly_runner.py:build_job:os.environ.get('NEOTOMA_BEARER_TOKEN', '')":
        'only compared with the role token, to refuse the shared bearer',
    "execution/daemons/apis/fly_runner.py:build_job:os.environ.get(token_name, '')":
        'unset role token: the role then needs its own key, or is refused',
    "execution/daemons/apis/fly_runner.py:job_files:job.stdin or b''":
        'a job with no stdin sends an empty stdin file',
    "execution/daemons/apis/fly_runner.py:mint_job_github_token:payload.get('token') or ''":
        'an empty token is refused on the next line',
    "execution/daemons/apis/fly_runner.py:process_start_token:return ''":
        "'' is 'alive, start unknown', the direction that never proves death",
    'execution/daemons/apis/fly_runner.py:process_start_token:return None':
        "only ProcessLookupError: the kernel's positive 'no such process'",
    "execution/daemons/apis/fly_runner.py:read_result_record:os.environ.get('NEOTOMA_BASE_URL', '')":
        'unset makes the result read fail: an unknown outcome',
    "execution/daemons/apis/fly_runner.py:read_result_record:os.environ.get('NEOTOMA_BEARER_TOKEN', '')":
        'unset makes the result read fail: an unknown outcome',
    "execution/daemons/apis/fly_runner.py:role_jwk_path:os.environ.get('ATELES_PRIVATE_KEYS_DIR', '')":
        'unset means the role has no key file',
    "execution/daemons/apis/fly_runner.py:runner_classes:os.environ.get('APIS_FLY_RUNNER_CLASSES', '')":
        'unset means no class is opted in: the default-off guarantee',
}


def _is_default_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
        return not (getattr(node, "keys", None) or getattr(node, "elts", None))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id in _EMPTY_CALLS and not node.args and not node.keywords
    return False


def _is_permissive_return(node: ast.Return) -> bool:
    value = node.value
    if value is None:
        return True
    if isinstance(value, ast.Constant):
        v = value.value
        if isinstance(v, bool):
            return v is True  # ``return False`` reports failure: never flagged
        return v is None or v == "" or (isinstance(v, (int, float)) and v == 0)
    return _is_default_literal(value) and not isinstance(value, ast.Constant)


class _Finder(ast.NodeVisitor):
    def __init__(self, rel: str):
        self.rel = rel
        self.stack: list[str] = []
        self.findings: set[str] = set()

    def _key(self, node: ast.AST) -> str:
        where = ".".join(self.stack) if self.stack else "<module>"
        return f"{self.rel}:{where}:{ast.unparse(node)}"

    def _scoped(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Attribute):
            if node.func.attr == "get" and (len(node.args) >= 2 or node.keywords):
                self.findings.add(self._key(node))
            if node.func.attr == "setdefault":
                self.findings.add(self._key(node))
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp):
        if isinstance(node.op, ast.Or) and _is_default_literal(node.values[-1]):
            self.findings.add(self._key(node))
        elif isinstance(node.op, ast.Or) and len(node.values) == 2 and any(
            isinstance(v, ast.Attribute) or isinstance(v, ast.Name) for v in node.values[-1:]
        ) and isinstance(node.values[0], ast.Call) and isinstance(
            node.values[0].func, ast.Attribute
        ) and node.values[0].func.attr in ("get", "strip"):
            self.findings.add(self._key(node))  # e.g. env.get(...) or FALLBACK
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        for inner in ast.walk(node):
            if isinstance(inner, ast.Return) and _is_permissive_return(inner):
                self.findings.add(self._key(inner))
        self.generic_visit(node)


def _findings() -> set[str]:
    found: set[str] = set()
    for rel in FILES:
        tree = ast.parse((_REPO / rel).read_text(encoding="utf-8"))
        finder = _Finder(rel)
        finder.visit(tree)
        found |= finder.findings
    return found


def test_no_unlisted_absence_as_value():
    unlisted = sorted(_findings() - set(ALLOWLIST))
    assert unlisted == [], "absence turned into a value, not on the allowlist:\n" + "\n".join(
        unlisted
    )


def test_no_stale_allowlist_entries():
    stale = sorted(set(ALLOWLIST) - _findings())
    assert stale == [], "allowlist entries that no longer match anything:\n" + "\n".join(stale)


def test_every_allowlist_entry_has_a_reason():
    assert all(isinstance(r, str) and len(r) > 10 for r in ALLOWLIST.values())


def test_the_lint_catches_each_pattern():
    sample = '''
def f(d, e):
    a = d.get("x", {})
    b = d.get("y") or ""
    c = e or []
    d.setdefault("z", 1)
    try:
        pass
    except Exception:
        return True
'''
    finder = _Finder("sample.py")
    finder.visit(ast.parse(sample))
    kinds = finder.findings
    assert any(".get('x', {})" in k for k in kinds)
    assert any("or ''" in k for k in kinds)
    assert any("or []" in k for k in kinds)
    assert any("setdefault" in k for k in kinds)
    assert any(k.endswith(":return True") for k in kinds)
