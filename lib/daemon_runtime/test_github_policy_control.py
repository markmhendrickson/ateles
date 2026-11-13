"""Inert control-reader custody and fixed provider-contract tests."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from lib import github_account as account
from lib import github_policy_control as control

spec = importlib.util.spec_from_file_location('inert_owner_reader', Path(control.__file__).with_name('github_owner_policy.py'))
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)
SYNTHETIC = 'inert-account-value-' + 'z' * 30


@pytest.fixture(autouse=True)
def private_binding(monkeypatch, tmp_path):
    import os
    path = (tmp_path / 'control-reader-ledger.json').resolve()
    monkeypatch.setenv('APIS_FLY_LEDGER_FILE', str(path))
    assert Path(os.environ['APIS_FLY_LEDGER_FILE']).resolve() == path and not path.exists()


def rule():
    return dict(id=42, source_type='Repository', source='owner/repo', target='branch', enforcement='active',
                conditions={'ref_name': {'include': ['~DEFAULT_BRANCH'], 'exclude': []}},
                bypass_actors=[], rules=[{'type': 'non_fast_forward'}])


def answer():
    return dict(principal='existing-owner', principal_id=7, host='github.com', repository='owner/repo', default_branch='main',
                rulesets=[{k: rule()[k] for k in ('id', 'source_type', 'source', 'target', 'enforcement')}], details=[rule()])


def test_named_account_is_pinned_for_user_and_never_uses_active_account(monkeypatch):
    calls = []
    monkeypatch.setenv('GH_TOKEN', 'ambient must not be used')
    monkeypatch.setenv('GITHUB_TOKEN', 'another ambient value')
    monkeypatch.setenv('GH_DEBUG', 'api')
    monkeypatch.setenv('GH_HOST', 'foreign.invalid')
    def run(args, **kw):
        calls.append((args, kw))
        return SimpleNamespace(returncode=0, stdout=SYNTHETIC, stderr=SYNTHETIC)
    def get(path, token):
        assert path == '/user' and token == SYNTHETIC
        return {'login': 'existing-owner', 'id': 7}
    monkeypatch.setattr(account.subprocess, 'run', run)
    monkeypatch.setattr(account, 'fixed_get', get)
    registered = []
    assert account.github_token_from_gh('existing-owner', register_secret=registered.append) == SYNTHETIC
    args, kw = calls[0]
    assert args == ['gh', 'auth', 'token', '--hostname', 'github.com', '--user', 'existing-owner']
    assert len(calls) == 1 and registered == [SYNTHETIC]
    assert not {'GH_TOKEN', 'GITHUB_TOKEN', 'GH_DEBUG', 'GH_HOST'} & kw['env'].keys()
    assert SYNTHETIC not in str(args)


@pytest.mark.parametrize('login', ['', 'operator', 'unassigned', 'owner/repo', '--user', 'unknown'])
def test_missing_sentinel_or_invalid_account_does_not_lookup(monkeypatch, login):
    monkeypatch.setattr(account.subprocess, 'run', lambda *a, **kw: pytest.fail('credential lookup must not occur'))
    with pytest.raises(account.AccountReadError):
        account.github_token_from_gh(login)


@pytest.mark.parametrize('kind', ['missing', 'wrong_identity', 'timeout'])
def test_failed_account_never_returns_provider_bytes(monkeypatch, kind):
    def run(*a, **kw):
        if kind == 'timeout':
            raise account.subprocess.TimeoutExpired(a[0], 20, output=SYNTHETIC)
        return SimpleNamespace(returncode=1 if kind == 'missing' else 0, stdout=SYNTHETIC, stderr=SYNTHETIC)
    monkeypatch.setattr(account.subprocess, 'run', run)
    monkeypatch.setattr(account, 'fixed_get', lambda *a: {'login': 'different-owner', 'id': 7})
    with pytest.raises(account.AccountReadError) as exc:
        account.github_token_from_gh('existing-owner')
    assert SYNTHETIC not in str(exc.value)


def test_helper_uses_one_private_credential_and_returns_only_policy(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(reader.account, 'verified_account_from_gh', lambda login: (SYNTHETIC, 7))
    def get(path, token):
        assert token == SYNTHETIC
        calls.append(path)
        if path == '/repos/owner/repo':
            return {'full_name': 'owner/repo', 'default_branch': 'main', 'ignored': SYNTHETIC}
        return [rule()] if '?' in path else {**rule(), 'ignored': SYNTHETIC}
    monkeypatch.setattr(reader.account, 'fixed_get', get)
    assert reader.main(['owner/repo', 'existing-owner']) == 0
    output = capsys.readouterr()
    data = json.loads(output.out)
    assert data == answer() and not output.err and SYNTHETIC not in output.out
    assert calls == ['/repos/owner/repo', '/repos/owner/repo/rulesets?includes_parents=true&per_page=100', '/repos/owner/repo/rulesets/42']


@pytest.mark.parametrize('kind', ['foreign_repo', 'incomplete', 'error', 'secret_echo'])
def test_helper_failures_are_fixed_nonsecret_and_no_partial_evidence(monkeypatch, capsys, kind):
    monkeypatch.setattr(reader.account, 'verified_account_from_gh', lambda login: (SYNTHETIC, 7))
    def get(path, token):
        if kind == 'error':
            raise RuntimeError(SYNTHETIC)
        if path == '/repos/owner/repo':
            return {'full_name': 'other/repo' if kind == 'foreign_repo' else 'owner/repo', 'default_branch': 'main'}
        if '?' in path:
            return [rule()] * 100 if kind == 'incomplete' else [rule()]
        return {**rule(), 'conditions': {'leak': SYNTHETIC}} if kind == 'secret_echo' else rule()
    monkeypatch.setattr(reader.account, 'fixed_get', get)
    assert reader.main(['owner/repo', 'existing-owner']) == 1
    output = capsys.readouterr()
    assert set(json.loads(output.out)) == {'error'} and SYNTHETIC not in output.out + output.err


def test_parent_pins_trusted_helper_and_no_personal_credential_env_or_argv(monkeypatch):
    monkeypatch.setenv('GH_TOKEN', SYNTHETIC)
    monkeypatch.setenv('PYTHONPATH', '/model/checkout')
    monkeypatch.setenv('GH_CONFIG_DIR', '/model/checkout')
    calls = []
    def run(args, **kw):
        calls.append((args, kw))
        return SimpleNamespace(returncode=0, stdout=json.dumps(answer()).encode())
    monkeypatch.setattr(control.subprocess, 'run', run)
    assert control.read_owner_policy('owner/repo', 'existing-owner') == answer()
    args, kw = calls[0]
    assert args[1] == '-I' and Path(args[2]) == Path(control.__file__).with_name('github_owner_policy.py')
    assert args[3:] == ['owner/repo', 'existing-owner']
    assert not {'GH_TOKEN', 'GITHUB_TOKEN', 'PYTHONPATH', 'GH_CONFIG_DIR'} & kw['env'].keys()
    assert SYNTHETIC not in str(args) + str(kw)
    assert kw['stderr'] == control.subprocess.DEVNULL and kw['timeout'] == 45


@pytest.mark.parametrize('change', [{'host': 'foreign.invalid'}, {'repository': 'foreign/repo'}, {'principal': 'other'}, {'rulesets': [rule()] * 100}])
def test_parent_rejects_wrong_context_or_incomplete_response(monkeypatch, change):
    monkeypatch.setattr(control.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps({**answer(), **change}).encode()))
    with pytest.raises(ValueError):
        control.read_owner_policy('owner/repo', 'existing-owner')


def test_http_redirect_is_denied_before_following():
    with pytest.raises(account.AccountReadError):
        account._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://foreign.invalid/user')


def test_same_selected_auth_survives_active_account_change_for_all_reads(monkeypatch, capsys):
    calls, selected = [], []
    state = {'active': 'existing-owner'}
    def select(args, **kw):
        selected.append(args)
        state['active'] = 'other-account'
        return SimpleNamespace(returncode=0, stdout=SYNTHETIC, stderr='')
    def get(path, credential):
        assert state['active'] == 'other-account' and credential == SYNTHETIC
        calls.append(path)
        if path == '/user':
            return {'login': 'existing-owner', 'id': 7}
        if path == '/repos/owner/repo':
            return {'full_name': 'owner/repo', 'default_branch': 'main'}
        return [rule()] if '?' in path else rule()
    monkeypatch.setattr(reader.account.subprocess, 'run', select)
    monkeypatch.setattr(reader.account, 'fixed_get', get)
    assert reader.main(['owner/repo', 'existing-owner']) == 0
    assert len(selected) == 1 and calls[0] == '/user' and len(calls) == 4
    assert SYNTHETIC not in capsys.readouterr().out


@pytest.mark.parametrize('kind', ['redirect', 'oversize', 'pagination', 'wrong_url'])
def test_fixed_http_transport_rejects_redirect_incomplete_or_wrong_origin(monkeypatch, kind):
    class Response:
        status = 200
        headers = {'Link': '<next>; rel="next"'} if kind == 'pagination' else {}
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def geturl(self):
            return 'https://foreign.invalid/user' if kind == 'wrong_url' else account.API + '/user'
        def read(self, size):
            assert size == account.MAX_RESPONSE + 1
            return b'x' * size if kind == 'oversize' else b'{"login":"existing-owner","id":7}'
    class Opener:
        def open(self, request, timeout):
            assert request.get_method() == 'GET' and request.full_url == account.API + '/user'
            assert request.headers['Authorization'] == 'Bearer ' + SYNTHETIC
            if kind == 'redirect':
                raise account.AccountReadError('inert redirect ' + SYNTHETIC)
            return Response()
    monkeypatch.setattr(account.urllib.request, 'build_opener', lambda *a: Opener())
    with pytest.raises(account.AccountReadError) as exc:
        account.fixed_get('/user', SYNTHETIC)
    assert SYNTHETIC not in str(exc.value)


def test_extracted_existing_caller_forwards_redaction_without_importing_workflow(monkeypatch):
    import ast
    source = Path(control.__file__).parents[1] / 'execution/scripts/rotate_swarm_secret.py'
    lines, capture = [], False
    with source.open() as stream:
        for line in stream:
            if line.startswith('def github_token_from_gh('):
                capture = True
            elif capture and line.strip() and not line[0].isspace():
                break
            if capture:
                lines.append(line)
    function = ast.parse(''.join(lines)).body[0]
    # Compile ONLY the unchanged public caller interface, never its workflow.
    registered = []
    namespace = {'Path': Path, '__file__': str(source), '_register_secret': registered.append}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<extracted narrow caller>', 'exec'), namespace)
    monkeypatch.setattr(account.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=SYNTHETIC, stderr=''))
    def opener(*a):
        class Response:
            status = 200
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def geturl(self): return account.API + '/user'
            def read(self, size): return b'{"login":"existing-owner","id":7}'
        return SimpleNamespace(open=lambda *a, **kw: Response())
    monkeypatch.setattr(account.urllib.request, 'build_opener', opener)
    assert namespace['github_token_from_gh']('existing-owner') == SYNTHETIC
    assert registered == [SYNTHETIC]


@pytest.mark.parametrize('principal_id', [None, '7', True, 0, -1])
def test_verified_numeric_identity_cannot_be_missing_or_malformed(monkeypatch, principal_id):
    monkeypatch.setattr(account.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=SYNTHETIC, stderr=''))
    monkeypatch.setattr(account, 'fixed_get', lambda *a: {'login': 'existing-owner', 'id': principal_id})
    with pytest.raises(account.AccountReadError):
        account.verified_account_from_gh('existing-owner')
    monkeypatch.setattr(control.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps({**answer(), 'principal_id': principal_id}).encode()))
    with pytest.raises(ValueError):
        control.read_owner_policy('owner/repo', 'existing-owner')


@pytest.mark.parametrize('repository', ['../repo', 'owner/../repo', 'https://foreign.invalid/repo', 'owner/repo.git', '--option/repo'])
def test_invalid_fixed_target_is_refused_before_account_lookup(monkeypatch, repository):
    monkeypatch.setattr(reader.account, 'verified_account_from_gh', lambda *a: pytest.fail('invalid target must not resolve account'))
    with pytest.raises(reader.account.AccountReadError):
        reader.read_policy(repository, 'existing-owner')


def test_escaped_credential_bytes_cannot_cross_policy_response(monkeypatch, capsys):
    planted = SYNTHETIC + '"escaped'
    monkeypatch.setattr(reader.account, 'verified_account_from_gh', lambda *a: (planted, 7))
    def get(path, credential):
        if path == '/repos/owner/repo':
            return {'full_name': 'owner/repo', 'default_branch': 'main'}
        return [rule()] if '?' in path else {**rule(), 'conditions': {'unexpected': planted}}
    monkeypatch.setattr(reader.account, 'fixed_get', get)
    assert reader.main(['owner/repo', 'existing-owner']) == 1
    text = capsys.readouterr().out
    assert planted not in text and json.dumps(planted) not in text and set(json.loads(text)) == {'error'}
