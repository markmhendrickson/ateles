"""The Fly runner image context, its entrypoint and its child environment
(ateles#1456).

Lives beside the Apis suite so the required ``Run swarm daemon (apis) tests``
CI lane collects it: a test under ``deploy/`` would run in no lane.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
_RUNNER_DIR = _REPO_ROOT / "deploy" / "fly-runner"
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib import environment_allowlist as allow  # noqa: E402


def _load_run_job():
    spec = importlib.util.spec_from_file_location("fly_run_job", _RUNNER_DIR / "run_job.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["fly_run_job"] = module
    spec.loader.exec_module(module)
    return module


run_job = _load_run_job()

# Things that must never be part of the runner image or its build context.
FORBIDDEN_CONTEXT_PATTERNS = (
    re.compile(r"\bsops\b", re.IGNORECASE),
    re.compile(r"\bage\b.*\bkey", re.IGNORECASE),
    re.compile(r"keys\.txt"),
    re.compile(r"SOPS_AGE", re.IGNORECASE),
    re.compile(r"\.config/sops"),
    re.compile(r"deploy/cloud/entrypoint\.sh"),
    re.compile(r"ateles-private"),
    re.compile(r"\.sops\.enc"),
)


def _instruction_lines(path: Path) -> list[str]:
    """Dockerfile/TOML lines that are not comments (comments may name the
    forbidden things in order to forbid them)."""
    return [
        line for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]  # fmt: skip


def _dockerignore_context(tracked: list[str]) -> list[str]:
    """Which tracked files the runner's build context would contain, applying
    ``Dockerfile.dockerignore`` with Docker's last-match-wins semantics."""
    rules = []
    for raw in (_RUNNER_DIR / "Dockerfile.dockerignore").read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        rules.append((negate, line[1:] if negate else line))
    kept = []
    for path in tracked:
        included = True
        for negate, pattern in rules:
            if pattern in ("*", "**") or fnmatch.fnmatch(path, pattern):
                included = negate
        if included:
            kept.append(path)
    return kept


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files"], capture_output=True, text=True, check=True
    ).stdout.split()
    # Include the runner's own new files even before they are committed.
    extra = [str(p.relative_to(_REPO_ROOT)) for p in _RUNNER_DIR.iterdir() if p.is_file()]
    return sorted(set(out) | set(extra))


class TestImageContext:
    def test_dockerfile_and_fly_toml_carry_no_secret_tooling(self):
        for name in ("Dockerfile", "fly.toml"):
            for line in _instruction_lines(_RUNNER_DIR / name):
                for pattern in FORBIDDEN_CONTEXT_PATTERNS:
                    assert not pattern.search(line), f"{name}: {line!r} matches {pattern.pattern}"

    def test_build_context_is_an_allowlist_without_secret_paths(self):
        context = _dockerignore_context(_tracked_files())
        assert context, "the runner context lets nothing in"
        assert "deploy/cloud/entrypoint.sh" not in context
        for path in context:
            for pattern in FORBIDDEN_CONTEXT_PATTERNS:
                assert not pattern.search(path), f"{path} is in the runner context"
        assert set(context) == {
            "lib/environment_allowlist.py",
            "lib/credential_env_names.py",
            "lib/daemon_runtime/aauth_httpsig.py",
            "deploy/fly-runner/run_job.py",
        }

    def test_every_copy_source_is_in_the_context(self):
        context = set(_dockerignore_context(_tracked_files()))
        for line in _instruction_lines(_RUNNER_DIR / "Dockerfile"):
            if line.startswith("COPY"):
                sources = line.split()[1:-1]
                for src in sources:
                    assert src in context, f"COPY source {src} is not in the context"
                    assert not src.startswith("deploy/cloud"), src

    def test_the_context_files_themselves_never_shell_out_to_sops(self):
        for rel in _dockerignore_context(_tracked_files()):
            text = (_REPO_ROOT / rel).read_text()
            assert "sops --decrypt" not in text
            assert "deploy/cloud/entrypoint.sh" not in text or rel.endswith("run_job.py")

    def test_fly_toml_has_no_services_and_no_secrets(self):
        lines = _instruction_lines(_RUNNER_DIR / "fly.toml")
        for line in lines:
            assert not re.match(r"\[\[?(services|http_service|env|mounts)", line.strip()), line

    def test_the_check_would_catch_a_regression(self):
        bad = "RUN curl -o /usr/local/bin/sops https://example.invalid/sops"
        assert any(p.search(bad) for p in FORBIDDEN_CONTEXT_PATTERNS)
        assert any(p.search("COPY deploy/cloud/entrypoint.sh /x") for p in FORBIDDEN_CONTEXT_PATTERNS)


# ── The child environment allowlist ──────────────────────────────────────────

FORBIDDEN_NAMES = (
    "NEOTOMA_BEARER_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "APIS_GITHUB_WEBHOOK_SECRET",
    "PAVO_AGENT_PAT",
    "ATELES_AGENT_PAT",
    "ATELES_FLY_CANARY_SENTINEL",
    "APIS_FLY_API_TOKEN",
    "LANIUS_NEOTOMA_TOKEN",
    "NEOTOMA_AAUTH_PRIVATE_JWK",
    "SSH_AUTH_SOCK",
    "GIT_CONFIG_GLOBAL",
    "XDG_CONFIG_HOME",
    "APIS_DISPATCH_TIMEOUT",  # a daemon setting: allowed on the Mac, not on the runner
)


class TestRunnerChildEnvironment:
    def _ambient(self):
        env = {name: f"value-of-{name.lower()}" for name in FORBIDDEN_NAMES}
        env.update({"PATH": "/usr/bin", "LANG": "C.UTF-8", "HOME": "/root"})
        return env

    def test_forbidden_names_never_reach_the_child(self):
        child = allow.runner_child_environment(
            self._ambient(), {"GITHUB_TOKEN": "ghs_job", "GH_TOKEN": "ghs_job"},
            overrides={"HOME": "/dev/shm/h"},
        )  # fmt: skip
        for name in FORBIDDEN_NAMES:
            assert name not in child, name
        for value in self._ambient().values():
            if value.startswith("value-of-"):
                assert value not in child.values()
        assert child["GITHUB_TOKEN"] == "ghs_job"
        assert child["HOME"] == "/dev/shm/h"
        assert child["PATH"] == "/usr/bin"

    def test_a_forbidden_credential_cannot_be_declared_for_the_child(self):
        for name in ("NEOTOMA_BEARER_TOKEN", "ATELES_AGENT_PAT", "LANIUS_NEOTOMA_TOKEN",
                     "NEOTOMA_AAUTH_PRIVATE_JWK"):  # fmt: skip
            with pytest.raises(ValueError, match=name):
                allow.runner_child_environment({}, {name: "x"})

    def test_overrides_must_be_on_the_subset(self):
        with pytest.raises(ValueError):
            allow.runner_child_environment({}, {}, overrides={"NEOTOMA_BEARER_TOKEN": "x"})

    def test_the_subset_never_widens_the_shared_allowlist(self):
        for name in allow.RUNNER_ENVIRONMENT_NAMES:
            assert allow.is_allowed_environment_name(name), name
        assert allow.RUNNER_CHILD_CREDENTIAL_NAMES == {
            "GITHUB_TOKEN", "GH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
        }  # fmt: skip


# ── run_job.py ───────────────────────────────────────────────────────────────


def _write_job(job_dir: Path, *, secrets: dict[str, str], spec: dict | None = None,
               argv: list[str] | None = None) -> None:  # fmt: skip
    (job_dir / "secrets").mkdir(parents=True)
    base = {
        "run_id": "fly-test-1",
        "role": "lanius",
        "provider": "claude",
        "repository": "owner/repo",
        "timeout_seconds": 30,
        "credential_names": sorted(secrets),
        "child_credential_names": [n for n in ("GITHUB_TOKEN", "GH_TOKEN") if n in secrets],
        "neotoma_token_name": "LANIUS_NEOTOMA_TOKEN" if "LANIUS_NEOTOMA_TOKEN" in secrets else "",
        "neotoma_jwk_name": (
            "NEOTOMA_AAUTH_PRIVATE_JWK" if "NEOTOMA_AAUTH_PRIVATE_JWK" in secrets else ""
        ),
        "neotoma_base_url": "https://neotoma.example.test",
        "probe_env_names": ["PAVO_AGENT_PAT", "ATELES_FLY_CANARY_SENTINEL"],
        "probe_paths": [],
        "child_user": "",
    }
    base.update(spec or {})
    (job_dir / "job.json").write_text(json.dumps(base))
    (job_dir / "argv.json").write_text(json.dumps(argv or ["claude", "--print"]))
    (job_dir / "stdin").write_bytes(b"the task")
    for name, value in secrets.items():
        (job_dir / "secrets" / name).write_text(value)


def _test_jwk() -> dict:
    from cryptography.hazmat.primitives.asymmetric import ec

    from lib.daemon_runtime.aauth_httpsig import _b64url_nopad

    key = ec.generate_private_key(ec.SECP256R1())
    nums = key.private_numbers()
    return {
        "kty": "EC", "crv": "P-256",
        "x": _b64url_nopad(nums.public_numbers.x.to_bytes(32, "big")),
        "y": _b64url_nopad(nums.public_numbers.y.to_bytes(32, "big")),
        "d": _b64url_nopad(nums.private_value.to_bytes(32, "big")),
        "sub": "lanius@ateles-swarm",
    }  # fmt: skip


@pytest.fixture(autouse=True)
def _probe_paths_in_tmp(monkeypatch, tmp_path):
    """Probe synthetic paths, never the test host's real credential files."""
    monkeypatch.setattr(
        run_job, "probe_paths_default",
        lambda: (str(tmp_path / "operator.env"), str(tmp_path / "age-keys.txt")),
    )  # fmt: skip


class TestRunJob:
    def test_a_readable_secret_path_fails_the_probe(self, tmp_path, monkeypatch):
        for name in run_job.DEFAULT_PROBE_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        (tmp_path / "operator.env").write_text("SYNTHETIC=1\n")
        job_dir = tmp_path / "job"
        _write_job(job_dir, secrets={"LANIUS_NEOTOMA_TOKEN": "own-token-123"})
        stored = {}
        rc = run_job.main(
            job_dir, writer=lambda b, a, r: stored.setdefault("r", r) is not None,
            runner=lambda *a, **k: pytest.fail("the CLI must not run"),
        )  # fmt: skip
        assert rc == run_job.EXIT_PROBE_FAILED
        assert str(tmp_path / "operator.env") in stored["r"]["refused"]

    def test_end_to_end_with_bearer_identity(self, tmp_path, monkeypatch):
        job_dir = tmp_path / "job"
        _write_job(job_dir, secrets={
            "GITHUB_TOKEN": "ghs_jobtoken_value_123", "GH_TOKEN": "ghs_jobtoken_value_123",
            "LANIUS_NEOTOMA_TOKEN": "role-own-neotoma-token-xyz",
        }, argv=["claude", "--print", "--mcp-config", run_job.MCP_CONFIG_PLACEHOLDER])  # fmt: skip
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "shared-bearer-canary")
        monkeypatch.setenv("PAVO_AGENT_PAT", "pavo-pat-canary")
        seen = {}

        def fake_runner(argv, env, stdin, timeout, cwd, ident=None):
            seen["argv"], seen["env"], seen["stdin"] = argv, env, stdin
            seen["job_dir_exists"] = job_dir.exists()
            cfg = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())
            seen["mcp_auth"] = cfg["mcpServers"]["mcpsrv_neotoma"]["headers"]["Authorization"]
            out = b"leaked ghs_jobtoken_value_123 here\nGATE_INHERITANCE: clear\n"
            return 0, out, b"", False

        stored = {}

        def fake_writer(base_url, auth, record):
            stored["record"] = record
            stored["headers"] = auth(f"{base_url}/store", b"{}")
            return True

        # Running as a non-root test user, the environment probe still fails
        # because NEOTOMA_BEARER_TOKEN is in THIS process's environment.
        rc = run_job.main(job_dir, writer=fake_writer, runner=fake_runner)
        assert rc == run_job.EXIT_PROBE_FAILED
        assert "NEOTOMA_BEARER_TOKEN" in stored["record"]["refused"]
        assert "PAVO_AGENT_PAT" in stored["record"]["refused"]
        assert "argv" not in seen  # the CLI never ran
        assert not job_dir.exists()

        # Clean runner process environment: the CLI runs.
        monkeypatch.delenv("NEOTOMA_BEARER_TOKEN")
        monkeypatch.delenv("PAVO_AGENT_PAT")
        _write_job(job_dir, secrets={
            "GITHUB_TOKEN": "ghs_jobtoken_value_123", "GH_TOKEN": "ghs_jobtoken_value_123",
            "LANIUS_NEOTOMA_TOKEN": "role-own-neotoma-token-xyz",
        }, argv=["claude", "--print", "--mcp-config", run_job.MCP_CONFIG_PLACEHOLDER])  # fmt: skip
        rc = run_job.main(job_dir, writer=fake_writer, runner=fake_runner)
        record = stored["record"]
        assert rc == 0, record["refused"]
        assert seen["job_dir_exists"] is False
        assert seen["stdin"] == b"the task"
        assert seen["env"]["GITHUB_TOKEN"] == "ghs_jobtoken_value_123"
        assert "LANIUS_NEOTOMA_TOKEN" not in seen["env"]
        assert seen["mcp_auth"] == "Bearer role-own-neotoma-token-xyz"
        assert "ghs_jobtoken_value_123" not in record["stdout"]
        assert "[redacted:" in record["stdout"]
        assert record["stdout"].rstrip().endswith("GATE_INHERITANCE: clear")
        assert record["probe_ok"] is True and record["exit_code"] == 0
        probe = json.loads(record["probe_report"])
        assert probe["env"]["NEOTOMA_BEARER_TOKEN"] == "absent"
        assert probe["env"]["ATELES_AGENT_PAT"] == "absent"
        assert probe["env"]["ATELES_FLY_CANARY_SENTINEL"] == "absent"
        assert probe["undeclared_child_env_names"] == []
        assert "role-own-neotoma-token-xyz" not in record["probe_report"]
        assert stored["headers"] == {"Authorization": "Bearer role-own-neotoma-token-xyz"}

    def test_aauth_key_signs_the_record_and_never_reaches_the_child(self, tmp_path, monkeypatch):
        pytest.importorskip("cryptography")
        pytest.importorskip("jwt")
        for name in run_job.DEFAULT_PROBE_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        jwk = _test_jwk()
        job_dir = tmp_path / "job"
        _write_job(job_dir, secrets={"NEOTOMA_AAUTH_PRIVATE_JWK": json.dumps(jwk)},
                   argv=["claude", "--print", "--mcp-config", run_job.MCP_CONFIG_PLACEHOLDER,
                         "--other"])  # fmt: skip
        seen, stored = {}, {}

        def fake_runner(argv, env, stdin, timeout, cwd, ident=None):
            seen["argv"], seen["env"] = argv, env
            return 0, f"echo {jwk['d']}".encode(), b"", False

        def fake_writer(base_url, auth, record):
            stored["record"] = record
            stored["headers"] = auth(f"{base_url}/store", b'{"a":1}')
            return True

        assert run_job.main(job_dir, writer=fake_writer, runner=fake_runner) == 0
        # No own bearer: no MCP server, and no shared bearer in its place.
        assert "--mcp-config" not in seen["argv"] and "--other" in seen["argv"]
        assert "NEOTOMA_AAUTH_PRIVATE_JWK" not in seen["env"]
        assert not any(jwk["d"] in v for v in seen["env"].values())
        assert jwk["d"] not in stored["record"]["stdout"]
        headers = {k.lower(): v for k, v in stored["headers"].items()}
        assert "authorization" not in headers
        assert headers["signature"].startswith("aasig=:")
        assert "signature-key" in headers

    def test_a_key_for_another_role_is_refused(self, tmp_path, monkeypatch):
        pytest.importorskip("cryptography")
        for name in run_job.DEFAULT_PROBE_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        jwk = dict(_test_jwk(), sub="pavo@ateles-swarm")
        job_dir = tmp_path / "job"
        _write_job(job_dir, secrets={"NEOTOMA_AAUTH_PRIVATE_JWK": json.dumps(jwk)})
        rc = run_job.main(
            job_dir, writer=lambda *a: True,
            runner=lambda *a, **k: (0, b"", b"", False),
        )  # fmt: skip
        assert rc == run_job.EXIT_RECORD_FAILED

    def test_a_job_without_its_own_identity_is_refused_and_deleted(self, tmp_path):
        job_dir = tmp_path / "job"
        _write_job(job_dir, secrets={"GITHUB_TOKEN": "fixture-gh-value"})
        rc = run_job.main(job_dir, writer=lambda *a: True, runner=None)
        assert rc == run_job.EXIT_JOB_INVALID
        assert not job_dir.exists()

    def test_a_job_cannot_hand_the_shared_bearer_to_the_child(self, tmp_path):
        job_dir = tmp_path / "job"
        _write_job(job_dir, secrets={
            "NEOTOMA_BEARER_TOKEN": "shared", "LANIUS_NEOTOMA_TOKEN": "own-token-123",
        }, spec={"child_credential_names": ["NEOTOMA_BEARER_TOKEN"]})  # fmt: skip
        with pytest.raises(run_job.JobError, match="NEOTOMA_BEARER_TOKEN"):
            run_job.load_job(job_dir)

    def test_hard_timeout_kills_the_cli(self, tmp_path):
        rc, out, err, timed_out = run_job.run_cli(
            [sys.executable, "-c", "import time; print('x', flush=True); time.sleep(30)"],
            {"PATH": os.environ.get("PATH", "")}, None, 1, str(tmp_path),
        )  # fmt: skip
        assert timed_out and rc == run_job.EXIT_TIMEOUT
        assert b"timed out" in err

    def test_stdout_cap_keeps_the_tail(self):
        data = b"a" * (run_job.STDOUT_CAP_BYTES + 10) + b"\nVERDICT"
        text, truncated = run_job.cap_stdout(data)
        assert truncated and text.endswith("VERDICT")
        assert len(text.encode()) == run_job.STDOUT_CAP_BYTES


# ── Sanitize job output before recording it (round 5) ────────────────────────


def _job_with_secret(secret: str):
    return run_job.Job(
        run_id="r", role="lanius", provider="claude", repository="o/r", timeout_seconds=5,
        argv=["claude"], stdin=None, secrets={"GITHUB_TOKEN": secret},
        child_credential_names=("GITHUB_TOKEN",), neotoma_token_name="",
        neotoma_jwk_name="", neotoma_base_url="https://neotoma.example.test",
    )  # fmt: skip


def _fragments(secret: str, length: int = 8) -> list[str]:
    return [secret[i:i + length] for i in range(len(secret) - length + 1)]


@pytest.mark.parametrize("stream,cap", [("stdout", run_job.STDOUT_CAP_BYTES),
                                        ("stderr", run_job.STDERR_TAIL_BYTES)])  # fmt: skip
def test_a_secret_straddling_the_cap_leaves_no_fragment(stream, cap):
    planted = "fixture-planted-value-0123456789abcdef"
    secret = planted
    job = _job_with_secret(secret)
    # The cap falls inside the secret: its tail half lands in the kept part.
    data = b"x" * 100 + secret.encode() + b"y" * (cap - len(secret) // 2)
    record = run_job.build_record(
        job, exit_code=0, stdout=data if stream == "stdout" else b"",
        stderr=data if stream == "stderr" else b"", wall_ms=1, probe={"ok": True},
        timed_out=False,
    )  # fmt: skip
    kept = record["stdout" if stream == "stdout" else "stderr_tail"]
    assert not any(f in kept for f in _fragments(secret)), "a fragment of the secret survived"


def test_encoded_copies_of_a_secret_are_redacted():
    import base64
    import urllib.parse

    planted = "fixture/planted+value=0123456789"
    secret = planted
    job = _job_with_secret(secret)
    encodings = [
        base64.b64encode(secret.encode()).decode(),
        base64.urlsafe_b64encode(secret.encode()).decode().rstrip("="),
        urllib.parse.quote(secret, safe=""),
        json.dumps(secret)[1:-1],
    ]
    out = ("\n".join(encodings) + "\n" + secret).encode()
    record = run_job.build_record(
        job, exit_code=0, stdout=out, stderr=out, wall_ms=1, probe={"ok": True},
        timed_out=False, refused=f"probe saw {secret}",
    )  # fmt: skip
    for field in ("stdout", "stderr_tail", "refused"):
        assert secret not in record[field]
        for enc in encodings:
            assert enc not in record[field], (field, enc)


@pytest.mark.parametrize("value", [0, -30, 30.0, True, "30", None, 10**9, "NaN"], ids=repr)
def test_a_job_timeout_that_is_not_a_bounded_integer_is_refused(tmp_path, value):
    job_dir = tmp_path / "job"
    _write_job(job_dir, secrets={"LANIUS_NEOTOMA_TOKEN": "own-token-123"},
               spec={"timeout_seconds": value})  # fmt: skip
    with pytest.raises(run_job.JobError, match="timeout_seconds"):
        run_job.load_job(job_dir)


def test_the_runner_and_executor_agree_on_the_longest_job_timeout():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import fly_runner

    assert run_job.MAX_JOB_TIMEOUT_SECONDS == fly_runner.MAX_JOB_TIMEOUT_SECONDS
