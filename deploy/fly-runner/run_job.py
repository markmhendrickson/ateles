#!/usr/bin/env python3
"""Run exactly one swarm dispatch on a Fly Machine (ateles#1456).

Apis (on the operator's Mac) writes this job's files into the machine config
under ``/run/job/`` and starts the machine. This entrypoint then:

1. reads the job: ``job.json`` (non-secret settings), ``argv.json``, an
   optional ``stdin`` payload and one file per credential under ``secrets/``;
2. builds the CLI child's environment FROM SCRATCH with
   ``lib.environment_allowlist.runner_child_environment``: only the runner
   subset of the shared allowlist, plus the job's declared child credentials.
   Nothing reaches the child by inheritance;
3. deletes ``/run/job`` before the CLI starts, and refuses to run if it cannot;
4. runs the credential-absence probe (names only, never values) and refuses to
   run the CLI when any forbidden credential is reachable;
5. runs the CLI under a hard timeout;
6. removes exact copies of this job's own secrets from the output;
7. writes ONE result record to Neotoma under the role's own identity, keyed by
   the run id. Apis reads that record back; the machine's logs are only a
   secondary diagnostic and carry a one-line summary, never output.

Stdlib only. The two ``lib`` modules it imports are copied into the image next
to it; nothing else from the repository is needed.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE, _HERE.parent, _HERE.parent.parent):
    if (_candidate / "lib" / "environment_allowlist.py").is_file():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from lib.environment_allowlist import (  # noqa: E402
    RUNNER_CHILD_CREDENTIAL_NAMES,
    runner_child_environment,
)

# The job file name of the role's own AAuth private key, when the job ships
# one. It is credential-named, so it can never pass the child allowlist; only
# this process reads it, to sign the result record.
AAUTH_JWK_JOB_NAME = "NEOTOMA_AAUTH_PRIVATE_JWK"
SWARM_SUB_SUFFIX = "@ateles-swarm"

DEFAULT_JOB_DIR = Path("/run/job")
RESULT_ENTITY_TYPE = "fly_runner_result"
STDOUT_CAP_BYTES = 256 * 1024
STDERR_TAIL_BYTES = 4 * 1024
# A secret shorter than this is not scrubbed by exact match: replacing a
# 3-character string would shred ordinary output. Every real credential the
# runner carries is far longer.
MIN_SCRUB_LENGTH = 8
# The placeholder Apis puts in argv where the Claude ``--mcp-config`` path
# goes. The real file is written here, on RAM-backed storage, after the job
# directory is read and before it is deleted.
MCP_CONFIG_PLACEHOLDER = "@@ATELES_JOB_MCP_CONFIG@@"

# Exit codes the runner itself produces (never the CLI's own).
EXIT_TIMEOUT = 124
EXIT_PROBE_FAILED = 97
EXIT_JOB_INVALID = 96
EXIT_RECORD_FAILED = 95

# Names that must never be readable on the runner. Another role's PAT and the
# operator's canary name arrive in job.json (names only).
DEFAULT_PROBE_ENV_NAMES: tuple[str, ...] = (
    "NEOTOMA_BEARER_TOKEN",
    "ATELES_AGENT_PAT",
    "NEOTOMA_AGENT_PAT",
    "ATELES_REVIEWER_APP_PRIVATE_KEY",
    "NEOTOMA_AAUTH_PRIVATE_JWK",
    "NEOTOMA_AAUTH_PRIVATE_JWK_PATH",
    "TELEGRAM_BOT_TOKEN",
    "APIS_GITHUB_WEBHOOK_SECRET",
    "APIS_FLY_API_TOKEN",
    "SOPS_AGE_KEY_FILE",
    "SOPS_AGE_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CURSOR_API_KEY",
)
DEFAULT_PROBE_PATHS: tuple[str, ...] = (
    "~/.config/neotoma/.env",
    "/root/.config/neotoma/.env",
    "~/.config/sops/age/keys.txt",
    "/root/.config/sops/age/keys.txt",
    "/secrets/age/keys.txt",
    "/secrets/ateles-private",
    "/app/deploy/cloud/entrypoint.sh",
)


def probe_paths_default() -> tuple[str, ...]:
    return DEFAULT_PROBE_PATHS


class JobError(Exception):
    """The job files are missing or malformed; nothing ran."""


@dataclass
class Job:
    run_id: str
    role: str
    provider: str
    repository: str
    timeout_seconds: int
    argv: list[str]
    stdin: bytes | None
    secrets: dict[str, str]
    child_credential_names: tuple[str, ...]
    neotoma_token_name: str
    neotoma_jwk_name: str
    neotoma_base_url: str
    probe_env_names: tuple[str, ...] = ()
    probe_paths: tuple[str, ...] = ()
    login_home_links: dict[str, str] = field(default_factory=dict)
    codex_home: str = ""
    machine_id: str = ""
    child_user: str = "agent"


def load_job(job_dir: Path) -> Job:
    """Read the job files. Raises ``JobError`` naming what is wrong, never a
    secret value."""
    try:
        spec = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        argv = json.loads((job_dir / "argv.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise JobError(f"job files unreadable: {type(exc).__name__}") from None
    if not isinstance(spec, dict) or not isinstance(argv, list) or not argv:
        raise JobError("job.json must be an object and argv.json a non-empty list")
    if not all(isinstance(a, str) for a in argv):
        raise JobError("argv.json must hold strings only")
    stdin_path = job_dir / "stdin"
    stdin = stdin_path.read_bytes() if stdin_path.is_file() else None
    secrets: dict[str, str] = {}
    secrets_dir = job_dir / "secrets"
    for name in spec.get("credential_names") or []:
        if not isinstance(name, str) or not name.isidentifier():
            raise JobError("credential_names must be identifiers")
        path = secrets_dir / name
        try:
            secrets[name] = path.read_text(encoding="utf-8").strip()
        except OSError:
            raise JobError(f"declared credential {name} has no file") from None
        if not secrets[name]:
            raise JobError(f"declared credential {name} is empty")
    child_names = tuple(spec.get("child_credential_names") or ())
    for name in child_names:
        if name not in RUNNER_CHILD_CREDENTIAL_NAMES:
            raise JobError(f"credential {name} may not reach a runner child")
        if name not in secrets:
            raise JobError(f"child credential {name} was not declared")
    token_name = str(spec.get("neotoma_token_name") or "")
    jwk_name = str(spec.get("neotoma_jwk_name") or "")
    if token_name and token_name not in secrets:
        raise JobError("the role's Neotoma token was named but not shipped")
    if jwk_name and jwk_name not in secrets:
        raise JobError("the role's AAuth key was named but not shipped")
    if not token_name and not jwk_name:
        raise JobError("the role's own Neotoma identity (token or AAuth key) is missing")
    try:
        timeout_seconds = int(spec["timeout_seconds"])
    except (KeyError, TypeError, ValueError):
        raise JobError("timeout_seconds missing or not an integer") from None
    if timeout_seconds <= 0:
        raise JobError("timeout_seconds must be positive")
    base_url = str(spec.get("neotoma_base_url") or "").rstrip("/")
    if not base_url.startswith("https://"):
        raise JobError("neotoma_base_url must be an https URL")
    run_id = str(spec.get("run_id") or "")
    if not run_id:
        raise JobError("run_id missing")
    return Job(
        run_id=run_id,
        role=str(spec.get("role") or ""),
        provider=str(spec.get("provider") or ""),
        repository=str(spec.get("repository") or ""),
        timeout_seconds=timeout_seconds,
        argv=list(argv),
        stdin=stdin,
        secrets=secrets,
        child_credential_names=child_names,
        neotoma_token_name=token_name,
        neotoma_jwk_name=jwk_name,
        neotoma_base_url=base_url,
        probe_env_names=tuple(spec.get("probe_env_names") or ()),
        probe_paths=tuple(spec.get("probe_paths") or ()),
        login_home_links=dict(spec.get("login_home_links") or {}),
        codex_home=str(spec.get("codex_home") or ""),
        machine_id=str(spec.get("machine_id") or os.environ.get("FLY_MACHINE_ID", "")),
        child_user=str(spec.get("child_user") or "agent"),
    )


def child_identity(name: str) -> tuple[int, int] | None:
    """(uid, gid) to run the CLI as, when this process is root and the image's
    unprivileged user exists. Running the CLI as a separate user is what keeps
    it out of this process's memory and files (the role's signing key and
    Neotoma credential stay with the runner, never with the agent)."""
    if os.geteuid() != 0 or not name:
        return None
    try:
        import pwd

        entry = pwd.getpwnam(name)
    except (ImportError, KeyError):
        return None
    return entry.pw_uid, entry.pw_gid


def _chown_tree(root: Path, ident: tuple[int, int] | None) -> None:
    if ident is None:
        return
    uid, gid = ident
    for dirpath, dirnames, filenames in os.walk(root):
        os.chown(dirpath, uid, gid)
        for name in filenames:
            os.chown(os.path.join(dirpath, name), uid, gid, follow_symlinks=False)


def ram_home(run_id: str) -> Path:
    """A fresh, private HOME on RAM-backed storage (``/dev/shm`` on Linux).
    Stopping the machine clears it, so nothing here outlives the job."""
    base = Path("/dev/shm") if Path("/dev/shm").is_dir() else Path(tempfile.gettempdir())
    safe = "".join(c for c in run_id if c.isalnum() or c in "-_")[:40] or "run"
    home = Path(tempfile.mkdtemp(prefix=f"ateles-home-{safe}-", dir=base))
    os.chmod(home, 0o700)
    return home


def write_mcp_config(home: Path, base_url: str, token: str) -> Path:
    """The Claude child's Neotoma MCP config, as a mode-0600 file (never argv)."""
    cfg_dir = home / ".ateles"
    cfg_dir.mkdir(mode=0o700, exist_ok=True)
    path = cfg_dir / "mcp.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "mcpServers": {
                    "mcpsrv_neotoma": {
                        "type": "http",
                        "url": f"{base_url}/mcp",
                        "headers": {"Authorization": f"Bearer {token}"},
                    }
                }
            },
            fh,
        )
    return path


def prepare_child(job: Job, home: Path, base_env) -> tuple[list[str], dict[str, str]]:
    """The argv and environment the CLI runs with."""
    argv = list(job.argv)
    if MCP_CONFIG_PLACEHOLDER in argv:
        if job.neotoma_token_name:
            cfg = write_mcp_config(
                home, job.neotoma_base_url, job.secrets[job.neotoma_token_name]
            )
            argv = [str(cfg) if a == MCP_CONFIG_PLACEHOLDER else a for a in argv]
        else:
            # An MCP session authenticates with one static bearer and cannot
            # carry a per-request AAuth signature, so a role whose only
            # identity is its signing key gets no Neotoma MCP server here.
            # The shared daemon bearer is never a substitute.
            argv = _drop_flag_with_value(argv, "--mcp-config", MCP_CONFIG_PLACEHOLDER)
    for rel, target in job.login_home_links.items():
        # Long-lived login machines: only the CLI's own login directory lives
        # on the volume; HOME itself stays on RAM-backed storage.
        link = home / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            link.symlink_to(target)
    overrides = {
        "HOME": str(home),
        "TMPDIR": str(home / "tmp"),
        "NEOTOMA_BASE_URL": job.neotoma_base_url,
    }
    (home / "tmp").mkdir(mode=0o700, exist_ok=True)
    _chown_tree(home, child_identity(job.child_user))
    if job.codex_home:
        overrides["CODEX_HOME"] = job.codex_home
    credentials = {name: job.secrets[name] for name in job.child_credential_names}
    env = runner_child_environment(base_env, credentials, overrides=overrides)
    return argv, env


def _drop_flag_with_value(argv: list[str], flag: str, value: str) -> list[str]:
    out: list[str] = []
    skip = False
    for i, arg in enumerate(argv):
        if skip:
            skip = False
            continue
        if arg == flag and i + 1 < len(argv) and argv[i + 1] == value:
            skip = True
            continue
        out.append(arg)
    return out


def delete_job_dir(job_dir: Path) -> bool:
    """Remove the job files. True only when the directory is really gone."""
    shutil.rmtree(job_dir, ignore_errors=True)
    return not job_dir.exists()


def _readable(path: Path) -> bool:
    """Whether this process could read *path*. Checked by permission, never by
    reading content: the probe reports names, and has no use for a byte."""
    try:
        return path.exists() and os.access(path, os.R_OK)
    except OSError:
        return False


def credential_probe(
    job: Job, child_env: dict[str, str], *, job_dir: Path, own_env=None
) -> dict:
    """Try to reach each forbidden credential; report present/absent by NAME.

    Checks the child's environment, this process's own environment, the job
    directory (which must already be gone) and the fixed path list. Also
    lists the child environment by name and reports any name that is neither
    on the runner subset nor a declared child credential.
    """
    from lib.environment_allowlist import is_runner_environment_name

    own_env = os.environ if own_env is None else own_env
    names = list(dict.fromkeys((*DEFAULT_PROBE_ENV_NAMES, *job.probe_env_names)))
    env_report = {
        name: ("present" if (name in child_env or name in own_env) else "absent")
        for name in names
        if name not in job.child_credential_names
    }
    path_report = {}
    for raw in dict.fromkeys((*probe_paths_default(), *job.probe_paths)):
        expanded = Path(raw.replace("~", child_env.get("HOME", "~"), 1)) if raw.startswith("~") else Path(raw)
        root_expanded = Path(os.path.expanduser(raw))
        present = _readable(expanded) or _readable(root_expanded)
        path_report[raw] = "present" if present else "absent"
    path_report["<job dir>"] = "present" if job_dir.exists() else "absent"
    declared = set(job.child_credential_names)
    undeclared = sorted(
        n for n in child_env if not is_runner_environment_name(n) and n not in declared
    )
    present = sorted(
        [n for n, v in env_report.items() if v == "present"]
        + [p for p, v in path_report.items() if v == "present"]
        + undeclared
    )
    return {
        "env": env_report,
        "paths": path_report,
        "child_env_names": sorted(child_env),
        "undeclared_child_env_names": undeclared,
        "present": present,
        "ok": not present,
    }


def run_cli(
    argv: list[str],
    env: dict[str, str],
    stdin: bytes | None,
    timeout: int,
    cwd: str,
    ident: tuple[int, int] | None = None,
) -> tuple[int, bytes, bytes, bool]:
    """Run the CLI in its own process group under a hard timeout, as the
    unprivileged user when one is given."""
    exe = shutil.which(argv[0], path=env.get("PATH")) or argv[0]
    drop = {} if ident is None else {"user": ident[0], "group": ident[1], "extra_groups": []}
    try:
        proc = subprocess.Popen(
            [exe, *argv[1:]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            start_new_session=True,
            **drop,
        )
    except OSError as exc:
        return 127, b"", f"runner: could not start {argv[0]}: {exc}".encode(), False
    try:
        out, err = proc.communicate(input=stdin or b"", timeout=timeout)
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        out, err = proc.communicate()
        err += f"\nrunner: timed out after {timeout}s".encode()
        return EXIT_TIMEOUT, out, err, True


def _scrub_values(secrets: dict[str, str]) -> dict[str, str]:
    """Every exact string worth scrubbing: each secret, plus the private
    scalar of a shipped JWK (which could be echoed on its own)."""
    values = dict(secrets)
    jwk_raw = secrets.get(AAUTH_JWK_JOB_NAME)
    if jwk_raw:
        try:
            d = json.loads(jwk_raw).get("d")
        except (ValueError, AttributeError):
            d = None
        if isinstance(d, str) and d:
            values[f"{AAUTH_JWK_JOB_NAME}.d"] = d
    return values


def scrub(text: str, secrets: dict[str, str]) -> str:
    """Replace every exact copy of a job secret with a named marker."""
    for name, value in sorted(_scrub_values(secrets).items(), key=lambda kv: -len(kv[1])):
        if value and len(value) >= MIN_SCRUB_LENGTH:
            text = text.replace(value, f"[redacted:{name}]")
    return text


def cap_stdout(data: bytes) -> tuple[str, bool]:
    """Keep the LAST ``STDOUT_CAP_BYTES``: a dispatch's verdict line is its
    final line, so the tail is the part a caller parses."""
    if len(data) <= STDOUT_CAP_BYTES:
        return data.decode("utf-8", errors="replace"), False
    return data[-STDOUT_CAP_BYTES:].decode("utf-8", errors="replace"), True


def build_record(
    job: Job,
    *,
    exit_code: int,
    stdout: bytes,
    stderr: bytes,
    wall_ms: int,
    probe: dict,
    timed_out: bool,
    refused: str = "",
) -> dict:
    out_text, truncated = cap_stdout(stdout)
    err_text = stderr[-STDERR_TAIL_BYTES:].decode("utf-8", errors="replace")
    return {
        "entity_type": RESULT_ENTITY_TYPE,
        "run_id": job.run_id,
        "role": job.role,
        "provider": job.provider,
        "repository": job.repository,
        "machine_id": job.machine_id,
        "exit_code": exit_code,
        "stdout": scrub(out_text, job.secrets),
        "stdout_truncated": truncated,
        "stderr_tail": scrub(err_text, job.secrets),
        "wall_ms": wall_ms,
        "timed_out": timed_out,
        "refused": refused,
        "probe_ok": bool(probe.get("ok")),
        "probe_report": json.dumps(probe, sort_keys=True),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _load_httpsig_module():
    """``aauth_httpsig`` loaded by file path, so the package ``__init__`` (and
    everything it imports) is never needed on the runner."""
    import importlib.util

    for root in sys.path:
        path = Path(root) / "lib" / "daemon_runtime" / "aauth_httpsig.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("_runner_aauth_httpsig", path)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            sys.modules["_runner_aauth_httpsig"] = module
            spec.loader.exec_module(module)
            return module
    raise RuntimeError("aauth_httpsig.py is not on the runner")


def auth_headers_for(job: Job):
    """A function ``(url, body) -> headers`` that authenticates as the role.

    Signs with the role's own AAuth key when the job shipped one (preferred:
    a signature is bound to this one request); otherwise presents the role's
    own Neotoma bearer. The shared daemon bearer never reaches a runner.
    """
    if job.neotoma_jwk_name:
        httpsig = _load_httpsig_module()
        jwk = json.loads(job.secrets[job.neotoma_jwk_name])
        expected_sub = f"{job.role}{SWARM_SUB_SUFFIX}"
        sub = str(jwk.get("sub") or "").strip()
        if sub != expected_sub:
            raise RuntimeError("the shipped AAuth key belongs to a different role")
        signer = httpsig.HttpSigSigner(
            private_jwk=jwk,
            sub=sub,
            iss=str(jwk.get("iss") or httpsig.DEFAULT_AAUTH_ISSUER),
            kid=str(jwk.get("kid") or "").strip() or None,
        )

        def _signed(url: str, body: bytes) -> dict[str, str]:
            return signer.sign_headers(method="POST", url=url, body=body)

        return _signed
    token = job.secrets[job.neotoma_token_name]

    def _bearer(url: str, body: bytes) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    return _bearer


def write_record(base_url: str, auth, record: dict, *, attempts: int = 3) -> bool:
    """POST the record to Neotoma ``/store`` as the role's own principal."""
    payload = json.dumps(
        {
            "idempotency_key": f"fly-runner-result-{record['run_id']}",
            "observation_source": "workflow_state",
            "entities": [record],
        }
    ).encode()
    url = f"{base_url}/store"
    for attempt in range(attempts):
        try:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ateles-fly-runner/1.0",
                **auth(url, payload),
            }
        except Exception as exc:  # noqa: BLE001 — never echo key material
            print(f"fly-runner: could not authenticate the record write: {type(exc).__name__}", flush=True)
            return False
        req = urllib.request.Request(url, data=payload, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if 200 <= resp.status < 300:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2 * (attempt + 1))
    return False


def main(job_dir: Path = DEFAULT_JOB_DIR, *, writer=write_record, runner=run_cli) -> int:
    started = time.monotonic()
    try:
        job = load_job(job_dir)
    except JobError as exc:
        delete_job_dir(job_dir)
        print(f"fly-runner: job refused: {exc}", flush=True)
        return EXIT_JOB_INVALID

    home = ram_home(job.run_id)
    try:
        argv, child_env = prepare_child(job, home, os.environ)
        if not delete_job_dir(job_dir):
            refused = "job directory could not be deleted"
            probe = {"ok": False, "present": ["<job dir>"]}
            rc, out, err, timed_out = EXIT_PROBE_FAILED, b"", refused.encode(), False
        else:
            probe = credential_probe(job, child_env, job_dir=job_dir)
            refused = ""
            if not probe["ok"]:
                refused = "credential probe found: " + ", ".join(probe["present"])
                rc, out, err, timed_out = EXIT_PROBE_FAILED, b"", refused.encode(), False
            else:
                rc, out, err, timed_out = runner(
                    argv,
                    child_env,
                    job.stdin,
                    job.timeout_seconds,
                    str(home),
                    child_identity(job.child_user),
                )
        record = build_record(
            job,
            exit_code=rc,
            stdout=out,
            stderr=err,
            wall_ms=int((time.monotonic() - started) * 1000),
            probe=probe,
            timed_out=timed_out,
            refused=refused,
        )
        try:
            auth = auth_headers_for(job)
        except Exception as exc:  # noqa: BLE001 — never echo key material
            print(f"fly-runner: no usable role identity: {type(exc).__name__}", flush=True)
            return EXIT_RECORD_FAILED
        stored = writer(job.neotoma_base_url, auth, record)
        print(
            f"fly-runner: run={job.run_id} role={job.role} provider={job.provider} "
            f"rc={rc} stdout={len(out)}B stderr={len(err)}B probe_ok={probe.get('ok')} "
            f"recorded={stored}",
            flush=True,
        )
        return rc if stored else EXIT_RECORD_FAILED
    finally:
        shutil.rmtree(home, ignore_errors=True)
        job.secrets.clear()


if __name__ == "__main__":
    raise SystemExit(main(Path(os.environ.get("ATELES_RUNNER_JOB_DIR") or DEFAULT_JOB_DIR)))
