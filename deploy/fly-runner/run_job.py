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
5. runs the CLI under a hard timeout, inside a hard deadline for the whole
   entrypoint (``ATELES_RUN_DEADLINE``): at that deadline everything the job
   started is killed and this process exits, whatever it is waiting on, so
   the machine stops;
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
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
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
# The longest timeout a job may carry (fly_runner.MAX_JOB_TIMEOUT_SECONDS).
MAX_JOB_TIMEOUT_SECONDS = 6 * 3600
EXIT_TIMEOUT = 124
EXIT_DEADLINE = 98
EXIT_PROBE_FAILED = 97
EXIT_JOB_INVALID = 96
EXIT_RECORD_FAILED = 95

# The whole entrypoint's deadline, set by Apis in the machine environment.
DEADLINE_ENV = "ATELES_RUN_DEADLINE"
# How far ahead a deadline may be: the longest job plus Apis's grace.
MAX_DEADLINE_AHEAD_SECONDS = MAX_JOB_TIMEOUT_SECONDS + 600
# Time kept back from the CLI, before the deadline, to write the record (at
# most this, and at most a quarter of what is left).
RECORD_RESERVE_SECONDS = 45.0
# The longest wait for the CLI's output once it has been killed.
COLLECT_GRACE_SECONDS = 5.0
# Without a usable deadline nothing runs; the refusal itself is bounded too.
REFUSAL_BOUND_SECONDS = 120.0
_DEADLINE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+(Z|[+-][0-9]{2}:[0-9]{2})",
                          re.ASCII)  # fmt: skip

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
    if "timeout_seconds" not in spec:
        raise JobError("timeout_seconds is missing")
    timeout_seconds = spec["timeout_seconds"]
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
        raise JobError("timeout_seconds is not an integer")
    if not 1 <= timeout_seconds <= MAX_JOB_TIMEOUT_SECONDS:
        raise JobError(f"timeout_seconds is outside 1 to {MAX_JOB_TIMEOUT_SECONDS}")
    base_url = str(spec.get("neotoma_base_url") or "").rstrip("/")
    if not base_url.startswith("https://"):
        raise JobError("neotoma_base_url must be an https URL")
    run_id = str(spec.get("run_id") or "")
    if not run_id:
        raise JobError("run_id missing")
    for required in ("role", "provider"):
        if not isinstance(spec.get(required), str) or not spec[required]:
            raise JobError(f"{required} missing")
    for listed in ("probe_env_names", "probe_paths", "credential_names",
                   "child_credential_names"):  # fmt: skip
        if listed in spec and not isinstance(spec[listed], list):
            raise JobError(f"{listed} is not a list")
    return Job(
        run_id=run_id,
        role=spec["role"],
        provider=spec["provider"],
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
    if os.geteuid() != 0:
        return None
    try:
        import pwd

        entry = pwd.getpwnam(name) if name else None
    except (ImportError, KeyError):
        entry = None
    if entry is None:
        # Running the CLI as root would hand it the runner's own secrets:
        # without the unprivileged user, the job does not run at all.
        raise RuntimeError("the unprivileged child user is unavailable")
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


def prepare_child(job: Job, home: Path, base_env,
                  ident: tuple[int, int] | None = None) -> tuple[list[str], dict[str, str]]:
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
    _chown_tree(home, ident)
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
        return True  # cannot tell: treated as reachable, so the probe fails closed


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
        expanded = Path(raw.replace("~", child_env["HOME"], 1)) if raw.startswith("~") else Path(raw)
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


def parse_deadline(raw: str | None, now: float) -> float:
    """``ATELES_RUN_DEADLINE`` (ISO 8601 with a UTC offset) as epoch seconds.
    Missing, malformed, without an offset, already past or further ahead
    than the longest job allows: ``JobError``, and nothing runs."""
    if raw is None:
        raise JobError(f"{DEADLINE_ENV} is missing")
    if not _DEADLINE_RE.fullmatch(raw):
        raise JobError(f"{DEADLINE_ENV} is malformed")
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise JobError(f"{DEADLINE_ENV} is malformed") from None
    if when.tzinfo is None:
        raise JobError(f"{DEADLINE_ENV} has no UTC offset")
    deadline = when.timestamp()
    if not now < deadline <= now + MAX_DEADLINE_AHEAD_SECONDS:
        raise JobError(f"{DEADLINE_ENV} is outside the allowed window")
    return deadline


# What the job started, for the cleanup at a timeout or at the deadline.
_JOB_PROCESSES: dict[str, int | None] = {"pgid": None, "uid": None}


def become_subreaper() -> bool:
    """On Linux, make this process the reaper of every orphaned descendant,
    so a process the job detaches still counts as one of ours."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    except (OSError, AttributeError):
        return False


def _proc_table() -> dict[int, tuple[int, int]]:
    """``pid -> (ppid, real uid)`` from /proc; empty where there is none."""
    table: dict[int, tuple[int, int]] = {}
    proc = Path("/proc")
    if not proc.is_dir():
        return table
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text()
        except OSError:
            continue
        fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
        try:
            ppid = int(fields["PPid"].split()[0])
            uid = int(fields["Uid"].split()[0])
        except (KeyError, ValueError, IndexError):
            continue
        table[int(entry.name)] = (ppid, uid)
    return table


def job_processes(me: int, child_uid: int | None) -> set[int]:
    """Every process the job started: all descendants of this process
    (orphans are re-parented here, see ``become_subreaper``) and, when the
    CLI runs as its own unprivileged user, every process of that user."""
    table = _proc_table()
    children: dict[int, list[int]] = {}
    for pid, (ppid, _uid) in table.items():
        if ppid not in children:
            children[ppid] = []
        children[ppid].append(pid)
    found: set[int] = set()
    stack = list(children[me]) if me in children else []
    while stack:
        pid = stack.pop()
        if pid not in found:
            found.add(pid)
            stack.extend(children[pid] if pid in children else [])
    if child_uid is not None and child_uid != os.getuid():
        found |= {pid for pid, (_ppid, uid) in table.items() if uid == child_uid}
    found.discard(me)
    return found


def kill_job_processes(rounds: int = 5) -> None:
    """SIGKILL everything the job started: its process group, then every
    descendant and every process of the child user, repeated so that a
    process forked meanwhile is caught too."""
    pgid, uid = _JOB_PROCESSES["pgid"], _JOB_PROCESSES["uid"]
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    for _ in range(rounds):
        pids = job_processes(os.getpid(), uid)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.05)


def arm_hard_deadline(deadline: float, *, clock=time.time, hard_exit=os._exit,
                      kill=kill_job_processes) -> threading.Timer:  # fmt: skip
    """At *deadline*, kill everything the job started and exit this process
    at once, whatever it is waiting on, so the machine stops."""
    def fire() -> None:
        try:
            print("fly-runner: hard deadline reached; stopping the job", flush=True)
            kill()
        finally:
            hard_exit(EXIT_DEADLINE)

    timer = threading.Timer(max(0.0, deadline - clock()), fire)
    timer.daemon = True
    timer.start()
    return timer


def run_cli(
    argv: list[str],
    env: dict[str, str],
    stdin: bytes | None,
    timeout: float,
    cwd: str,
    ident: tuple[int, int] | None = None,
    collect_by: float | None = None,
) -> tuple[int, bytes, bytes, bool]:
    """Run the CLI in its own session under a hard timeout, as the
    unprivileged user when one is given.

    Termination and output collection are both bounded: at the timeout
    everything the job started is killed, the remaining output is collected
    for at most ``COLLECT_GRACE_SECONDS`` (and never past the wall-clock
    ``collect_by``), and then the pipes are abandoned."""
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
    _JOB_PROCESSES["pgid"] = proc.pid
    _JOB_PROCESSES["uid"] = ident[0] if ident is not None else None
    try:
        out, err = proc.communicate(input=stdin or b"", timeout=timeout)
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        pass
    exited_first = proc.poll() is not None
    kill_job_processes()
    grace = COLLECT_GRACE_SECONDS
    if collect_by is not None:
        grace = max(0.1, min(grace, (collect_by - time.time()) / 2))
    try:
        out, err = proc.communicate(timeout=grace)
    except subprocess.TimeoutExpired as exc:
        # Collection did not finish within its bound: keep what arrived
        # and stop reading.
        out, err = exc.stdout or b"", exc.stderr or b""
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    why = ("output collection did not finish within its bound" if exited_first
           else f"timed out after {timeout:g}s")  # fmt: skip
    err += f"\nrunner: {why}".encode()
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


def _variants(value: str) -> set[str]:
    """The secret as written, plus the common encodings a tool might print
    it in (base64, URL-safe base64, percent-encoding, JSON escaping)."""
    import base64
    import urllib.parse

    raw = value.encode("utf-8")
    out = {
        value,
        base64.b64encode(raw).decode(),
        base64.b64encode(raw).decode().rstrip("="),
        base64.urlsafe_b64encode(raw).decode(),
        base64.urlsafe_b64encode(raw).decode().rstrip("="),
        urllib.parse.quote(value, safe=""),
        json.dumps(value)[1:-1],
    }
    return {v for v in out if len(v) >= MIN_SCRUB_LENGTH}


def scrub(text: str, secrets: dict[str, str]) -> str:
    """Replace every copy of a job secret, and of each listed single-layer
    encoding of it, with a named marker.

    Best effort, not a guarantee: a finite list of exact variants cannot
    catch every transformation (a nested or custom encoding survives). It
    keeps routine echoes of a job secret out of the record; it does not stop
    a CLI that is trying to exfiltrate one. The real boundary is that the job
    holds only short-lived, single-job credentials."""
    pairs = [
        (name, variant)
        for name, value in _scrub_values(secrets).items()
        if value and len(value) >= MIN_SCRUB_LENGTH
        for variant in _variants(value)
    ]
    for name, variant in sorted(pairs, key=lambda kv: -len(kv[1])):
        text = text.replace(variant, f"[redacted:{name}]")
    return text


def sanitize_and_cap(data: bytes, cap: int, secrets: dict[str, str]) -> tuple[str, bool]:
    """Sanitize the WHOLE output first, then keep its last *cap* bytes.

    The order matters: capping first could cut a secret in two at the
    boundary and leave an unrecognisable fragment of it. The tail is kept
    because a dispatch's verdict line is its final line; the cut is moved to
    a character boundary, and at worst splits a redaction marker.
    """
    text = scrub(data.decode("utf-8", errors="replace"), secrets)
    encoded = text.encode("utf-8")
    if len(encoded) <= cap:
        return text, False
    return encoded[-cap:].decode("utf-8", errors="ignore"), True


def cap_stdout(data: bytes, secrets: dict[str, str] | None = None) -> tuple[str, bool]:
    """``sanitize_and_cap`` at the stdout cap."""
    return sanitize_and_cap(data, STDOUT_CAP_BYTES, secrets or {})


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
    # Sanitize job output before recording it, and only then cap it.
    out_text, truncated = sanitize_and_cap(stdout, STDOUT_CAP_BYTES, job.secrets)
    err_text, _ = sanitize_and_cap(stderr, STDERR_TAIL_BYTES, job.secrets)
    return {
        "entity_type": RESULT_ENTITY_TYPE,
        "run_id": job.run_id,
        "role": job.role,
        "provider": job.provider,
        "repository": job.repository,
        "machine_id": job.machine_id,
        "exit_code": exit_code,
        "stdout": out_text,
        "stdout_truncated": truncated,
        "stderr_tail": err_text,
        "wall_ms": wall_ms,
        "timed_out": timed_out,
        "refused": scrub(refused, job.secrets),
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


def main(job_dir: Path = DEFAULT_JOB_DIR, *, writer=write_record, runner=run_cli,
         environ=None, hard_exit=os._exit) -> int:  # fmt: skip
    started = time.monotonic()
    environ = os.environ if environ is None else environ
    now = time.time()
    try:
        deadline = parse_deadline(environ[DEADLINE_ENV] if DEADLINE_ENV in environ else None, now)
        deadline_problem = ""
    except JobError as exc:
        # Nothing runs without a deadline; the refusal itself is bounded.
        deadline, deadline_problem = now + REFUSAL_BOUND_SECONDS, str(exc)
    timer = arm_hard_deadline(deadline, hard_exit=hard_exit)
    try:
        return _main(job_dir, writer, runner, started, deadline, deadline_problem)
    finally:
        timer.cancel()


def _main(job_dir: Path, writer, runner, started: float, deadline: float,
          deadline_problem: str) -> int:  # fmt: skip
    try:
        job = load_job(job_dir)
    except JobError as exc:
        delete_job_dir(job_dir)
        print(f"fly-runner: job refused: {exc}", flush=True)
        return EXIT_JOB_INVALID

    home = ram_home(job.run_id)
    try:
        try:
            ident = child_identity(job.child_user)
            identity_problem = ""
        except RuntimeError as exc:
            ident, identity_problem = None, str(exc)
        argv, child_env = prepare_child(job, home, os.environ, ident)
        remaining = deadline - time.time()
        cli_budget = min(float(job.timeout_seconds),
                         remaining - min(RECORD_RESERVE_SECONDS, remaining / 4))  # fmt: skip
        if deadline_problem:
            delete_job_dir(job_dir)
            refused = deadline_problem
            probe = {"ok": False, "present": ["<deadline>"]}
            rc, out, err, timed_out = EXIT_JOB_INVALID, b"", refused.encode(), False
        elif identity_problem:
            delete_job_dir(job_dir)
            refused = identity_problem
            probe = {"ok": False, "present": ["<child user>"]}
            rc, out, err, timed_out = EXIT_PROBE_FAILED, b"", refused.encode(), False
        elif not delete_job_dir(job_dir):
            refused = "job directory could not be deleted"
            probe = {"ok": False, "present": ["<job dir>"]}
            rc, out, err, timed_out = EXIT_PROBE_FAILED, b"", refused.encode(), False
        else:
            probe = credential_probe(job, child_env, job_dir=job_dir)
            refused = ""
            if not probe["ok"]:
                refused = "credential probe found: " + ", ".join(probe["present"])
                rc, out, err, timed_out = EXIT_PROBE_FAILED, b"", refused.encode(), False
            elif not cli_budget >= 1:
                refused = "no time left before the run's deadline"
                rc, out, err, timed_out = EXIT_TIMEOUT, b"", refused.encode(), True
            else:
                rc, out, err, timed_out = runner(
                    argv,
                    child_env,
                    job.stdin,
                    cli_budget,
                    str(home),
                    ident,
                    deadline,
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


def entrypoint(job_dir: Path, **kwargs) -> int:
    """The machine's entrypoint process: it becomes the reaper of everything
    the job starts (so all of it can be found and killed), then runs the job.
    Only the real entrypoint process does this, never a caller of ``main``."""
    become_subreaper()
    return main(job_dir, **kwargs)


if __name__ == "__main__":
    raise SystemExit(entrypoint(Path(os.environ.get("ATELES_RUNNER_JOB_DIR") or DEFAULT_JOB_DIR)))
