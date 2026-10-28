#!/usr/bin/env python3
"""Staged rotation for a credential the swarm both issues and consumes.

Operator ruling (conformance.md row 118 / authority_model.md#grants,
2026-09-26, decision `credential_rotation_split_by_issuer`): a credential the
swarm both issues and consumes — an agent's signing key, a shared secret
internal to the record — is rotated by the swarm **unattended**, through the
dual-admit staging `authority_model.md#grants` already requires, plus a live
verification before the old value retires. A check that fails, or that
cannot be run, is not a hold: the rotation rolls back, restoring the old
value's standing, and either outcome is written to the record for the
operator to read.

This script covers the two shared secrets named in Neotoma task
ent_52d71d10709080d847d07ab1 as first users of that ruling:

    github_webhook_secret   APIS_GITHUB_WEBHOOK_SECRET
                             issuer: GitHub webhook config (one hook per repo,
                             passed as --github-hook owner/repo:id);
                             consumer: apis (github_gateway.py)
    approve_email_secret    APIS_APPROVE_EMAIL_SECRET
                             issuer: none (internal-only); consumers: apis
                             (github_gateway.py /approve-email + /approve-release)
                             and turdus (presents the header)

WHERE THE VALUES LIVE. Both secrets are ordinary members of the Design B
secrets pipeline (docs/secrets_management.md): canonical in 1Password, mapped
by the private manifest (`secrets/manifest.env-map.json`) block named in
`RotationTarget.manifest_file`, snapshotted to `secrets/<block>.sops.enc`, and
materialized into the block's `target` dotenv — the file apis and turdus load
at startup. The block holds only these swarm-issued secrets, so a rotation
never decrypts or re-encrypts a snapshot that carries a provider key or a
root secret. The canonical 1Password reference is read from the manifest at
run time, never hardcoded here.

SEQUENCE (a real run; `--dry-run` prints the same plan with no side effects):
  0. preflight — refuse before touching anything unless: daemon restarts are
     allowed (`ATELES_ALLOW_DAEMON_RESTART=1`), the manifest maps the target,
     the snapshot, the dotenv, and 1Password all agree on the current value,
     no ``_NEXT`` slot is left over from an earlier run, and (webhook only)
     the GitHub tripwire, a token, and at least one hook are supplied.
  1. generate — a new random value, held only in memory.
  2. stage (dual-admit) — write the new value to the ``_NEXT`` slot in the
     snapshot AND materialize it into the dotenv the daemon reads.
  3. restart apis, reading back a NEW pid that is still alive after a settle
     delay — a restart is not reported until the new process is seen.
  4. live check under dual-admit — a probe the script signs itself (a signed
     /github/webhook ping, or an /approve-email call) must be accepted under
     the NEW value. GitHub has not been told anything yet. Failure: roll back.
  5. write the canonical store — `op item edit` via a mode-600 template file
     (the same helper rotate_provider_key.py uses), then read it back and
     compare in memory. Failure: restore the old value there and roll back.
     This is what stops the next `secrets_publish.py` from resurrecting the
     retired value.
  6. update the issuer (webhook only) — PATCH every named GitHub hook. A
     failure here is a CHECKPOINT, not a rollback: dual-admit stays on, so no
     delivery is rejected whichever value GitHub actually holds.
  7. promote — the new value becomes primary and ``_NEXT`` is removed, in the
     snapshot and in the dotenv. Restart every daemon that PRESENTS the
     secret first (turdus, for approve_email_secret), then apis, each with a
     pid read-back. Presenters go first so there is no window in which apis
     admits only the new value while a presenter still sends the old one.
  8. post-promotion checks — apis accepts the new value, REJECTS the old one
     (401: the old value is genuinely retired), every presenter's own loaded
     configuration is accepted (turdus: its module, loading its dotenv the way
     the daemon does, from the daemon's checkout), and (webhook only) a ping
     GitHub itself signs is delivered with HTTP 200. Any failure re-opens
     dual-admit with the old value in ``_NEXT`` and raises a CHECKPOINT, so a
     consumer still on the old value keeps working while a human looks.

Outcomes, printed and returned as the exit code: 0 rolled forward (or
dry-run), 1 rolled back, 2 checkpoint, 3 refused at preflight (nothing
changed), 4 internal error (state unknown), 64 bad arguments. Every failure
inside steps 2-8 — a check that fails, one that cannot run, or an unexpected
exception — takes the rollback (before promotion) or re-open (after) path;
none escapes as a traceback.

HARD RULES enforced by this script itself, not just by policy:
  - Never prints a secret value. Status lines name variables, files, pids and
    HTTP statuses only.
  - Never calls the real GitHub webhook-config write API unless
    `--i-know-this-calls-real-github` is passed.
  - Never restarts a real daemon unless `ATELES_ALLOW_DAEMON_RESTART=1` is in
    this process's own environment (never read from argv).
  - A secret value never appears on a subprocess argv: `sops` gets a mode-600
    temp file, `op` gets a mode-600 template file, the probes send it in an
    HTTP header or use it only as an HMAC key, and GitHub gets it in a JSON
    request body.

Usage:
    python execution/scripts/rotate_swarm_secret.py approve_email_secret --dry-run
    ATELES_ALLOW_DAEMON_RESTART=1 \\
        python execution/scripts/rotate_swarm_secret.py approve_email_secret
    ATELES_ALLOW_DAEMON_RESTART=1 \\
        python execution/scripts/rotate_swarm_secret.py github_webhook_secret \\
        --github-hook <owner>/<repo>:<hook_id> [--github-hook ...] \\
        --github-token-gh-user <login> --i-know-this-calls-real-github
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rotate_provider_key as rpk  # noqa: E402
import secrets_lib as sl  # noqa: E402

REDACTED = "<redacted>"
DEFAULT_BASE_URL = "http://127.0.0.1:8742"
# The checkout the daemons run from (CLAUDE.md, "Which checkout a daemon runs
# from"). The presenter check imports the daemon's code from HERE, not from
# the worktree this script happens to live in.
DEFAULT_DAEMON_CHECKOUT = Path.home() / "ateles-rc-src"

# Reused, not re-implemented: the 1Password write helper (template file, never
# argv) and its redaction registry belong to rotate_provider_key.py.
op_write_password_field = rpk.op_write_password_field


def _register_secret(value: str | None) -> None:
    """Teach the shared redactor a value so no op/HTTP error text can echo it."""
    rpk._known_secrets.register(value)


def _safe(text: str) -> str:
    return rpk._safe_excerpt(str(text))


# ---------------------------------------------------------------------------
# Rotation targets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RotationTarget:
    """Everything rotate_swarm_secret needs to know about one shared secret.

    Kept as data rather than branching `if name == "..."` throughout the
    module, so adding a third shared secret later is one new entry here.
    """

    name: str
    env_var: str
    next_env_var: str
    manifest_file: str  # manifest block (and secrets/<block>.sops.enc) carrying it
    daemon_plist: str  # launchctl label of the daemon that VERIFIES the secret
    has_external_issuer: bool  # True only for github_webhook_secret
    consumers: tuple[str, ...]
    verify_path: str  # HTTP path on the daemon to probe for live verification
    # launchctl labels of daemons that PRESENT the secret to `daemon_plist`.
    # Restarted after promotion (before the verifier) and checked from their
    # own side; each needs an entry in PRESENTER_VERIFIERS.
    presenter_plists: tuple[str, ...] = ()


# Both live in their own manifest block. Keeping them out of the `neotoma`
# block means a rotation never decrypts or rewrites the snapshot that also
# carries provider keys and the Neotoma mnemonic (a root secret).
SWARM_SHARED_SECRETS_BLOCK = "apis"

TARGETS: dict[str, RotationTarget] = {
    "github_webhook_secret": RotationTarget(
        name="github_webhook_secret",
        env_var="APIS_GITHUB_WEBHOOK_SECRET",
        next_env_var="APIS_GITHUB_WEBHOOK_SECRET_NEXT",
        manifest_file=SWARM_SHARED_SECRETS_BLOCK,
        daemon_plist="com.ateles.apis",
        has_external_issuer=True,
        consumers=("apis (github_gateway.py — verify_github_signature_any)",),
        verify_path="/github/webhook",
    ),
    "approve_email_secret": RotationTarget(
        name="approve_email_secret",
        env_var="APIS_APPROVE_EMAIL_SECRET",
        next_env_var="APIS_APPROVE_EMAIL_SECRET_NEXT",
        manifest_file=SWARM_SHARED_SECRETS_BLOCK,
        daemon_plist="com.ateles.apis",
        has_external_issuer=False,
        consumers=(
            "apis (github_gateway.py — /approve-email, /approve-release)",
            "turdus (turdus.py — presents the header)",
        ),
        verify_path="/approve-email",
        presenter_plists=("com.ateles.turdus",),
    ),
}


# ---------------------------------------------------------------------------
# Step 1: generate
# ---------------------------------------------------------------------------


def generate_new_value() -> str:
    """A fresh random secret. 43 URL-safe base64 chars ≈ 256 bits, matching
    the entropy GitHub's own webhook-secret guidance recommends."""
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# Snapshot + dotenv writes (steps 2, 7, rollback, re-open)
# ---------------------------------------------------------------------------


def _materialize(target: RotationTarget, *, remove: tuple[str, ...] = ()) -> str:
    """Push the target's snapshot into the dotenv its daemons read.

    Without this a snapshot write never reaches a running daemon: apis and
    turdus load the materialized dotenv, not the snapshot. Names only."""
    dest, n_values, changed = sl.materialize_block(target.manifest_file, remove=remove)
    detail = ", ".join(changed) if changed else "none"
    return (
        f"materialized secrets/{target.manifest_file} -> {dest} "
        f"({n_values} var(s); changed: {detail})"
    )


def _base_url_is_safe(base_url: str) -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")


def _other_blocks_mapping(target: RotationTarget, manifest: dict) -> list[str]:
    """Manifest blocks OTHER than the target's that map its variable or its
    _NEXT slot (same ENVIRONMENT overlay as secrets_publish.py)."""
    names = (target.env_var, target.next_env_var)
    found = []
    for block in manifest.get("files", {}):
        if block == target.manifest_file:
            continue
        try:
            refs = sl.resolve_refs(manifest, block, os.environ.get("ENVIRONMENT"))
        except (KeyError, AttributeError, TypeError):
            continue
        if any(n in refs for n in names):
            found.append(block)
    return found


def _read_snapshot(target: RotationTarget) -> dict[str, str]:
    enc = sl.enc_file(target.manifest_file)
    return sl.sops_decrypt_dotenv(enc) if enc.exists() else {}


def _write_snapshot(target: RotationTarget, values: dict[str, str]) -> None:
    sl.sops_encrypt_dotenv(sl.to_dotenv(values), sl.enc_file(target.manifest_file))


def stage_next_value(target: RotationTarget, new_value: str, *, dry_run: bool) -> str:
    """Write `new_value` into the target's `_NEXT` slot — snapshot first, then
    the materialized dotenv — so the next daemon restart admits both values.
    Returns a status line (never the value)."""
    if dry_run:
        return (
            f"[dry-run] would set {target.next_env_var}={REDACTED} in "
            f"secrets/{target.manifest_file}.sops.enc, then materialize it into "
            "the block's dotenv"
        )
    current = _read_snapshot(target)
    current[target.next_env_var] = new_value
    _write_snapshot(target, current)
    return (
        f"staged {target.next_env_var} in secrets/{target.manifest_file}.sops.enc; "
        + _materialize(target)
    )


def promote_and_retire(target: RotationTarget, new_value: str, *, dry_run: bool) -> str:
    """NEW becomes primary, the `_NEXT` slot is removed — in the snapshot and
    in the dotenv. Returns a status line (never the value)."""
    if dry_run:
        return (
            f"[dry-run] would set {target.env_var}={REDACTED} and remove "
            f"{target.next_env_var} in secrets/{target.manifest_file}.sops.enc "
            "and in the materialized dotenv"
        )
    current = _read_snapshot(target)
    current[target.env_var] = new_value
    current.pop(target.next_env_var, None)
    _write_snapshot(target, current)
    return (
        f"promoted {target.env_var}, removed {target.next_env_var}; "
        + _materialize(target, remove=(target.next_env_var,))
    )


def roll_back(target: RotationTarget, *, dry_run: bool) -> str:
    """Clear the staged `_NEXT` value, leave the primary untouched, and remove
    `_NEXT` from the dotenv too. Returns a status line (never a value).

    The rollback the operator ruling requires: "a check that fails, or that
    cannot be run, is not a hold... the rotation rolls back, restoring the old
    credential's standing". Removing `_NEXT` from the dotenv — not only from
    the snapshot — is what makes the daemon's post-restart state identical to
    its pre-rotation state: materializing alone only adds and updates, so a
    `_NEXT` line left in the dotenv would keep the overlap window open.

    Only ever called before the GitHub issuer is touched (see `run_rotation`),
    so there is nothing on the GitHub side to revert.
    """
    if dry_run:
        return (
            f"[dry-run] would remove {target.next_env_var} from the snapshot and "
            "the dotenv (rollback)"
        )
    enc = sl.enc_file(target.manifest_file)
    if not enc.exists():
        return f"nothing to roll back — no snapshot for {target.manifest_file}"
    current = sl.sops_decrypt_dotenv(enc)
    if target.next_env_var in current:
        current.pop(target.next_env_var, None)
        _write_snapshot(target, current)
        head = f"rolled back — cleared {target.next_env_var} in the snapshot; "
    else:
        head = f"{target.next_env_var} already clear in the snapshot; "
    return head + _materialize(target, remove=(target.next_env_var,))


def reopen_dual_admit(target: RotationTarget, old_value: str, new_value: str) -> str:
    """After a failed post-promotion step: leave apis admitting BOTH values
    while a human looks, reconciled against what the snapshot actually holds.

    - Promotion landed (primary = new): the retired value goes back in `_NEXT`.
    - Promotion did not land (primary = old, e.g. its snapshot write failed):
      the new value is re-staged in `_NEXT`. Writing the old value there would
      overwrite the staged new value, and apis would then reject the new value
      that 1Password and, for the webhook, GitHub already hold.
    - Primary is neither: raise. This function never guesses.
    """
    current = _read_snapshot(target)
    primary = current.get(target.env_var, "")
    if hmac.compare_digest(primary.encode(), new_value.encode()):
        current[target.next_env_var] = old_value
        head = (f"re-opened dual-admit: new value primary, retired value restored to "
                f"{target.next_env_var}")
    elif hmac.compare_digest(primary.encode(), old_value.encode()):
        current[target.next_env_var] = new_value
        head = (f"re-opened dual-admit: promotion had NOT landed in the snapshot — old "
                f"value still primary, new value re-staged in {target.next_env_var}")
    else:
        raise RuntimeError(
            f"the snapshot's {target.env_var} matches neither the old nor the new value"
        )
    _write_snapshot(target, current)
    return f"{head}; " + _materialize(target)


def raise_issuer_checkpoint(target: RotationTarget, *, reason: str) -> str:
    """The issuer update failed after the dual-admit probe already passed.

    Neither a promotion (GitHub might not hold the new value) nor a rollback
    (GitHub might already hold it, e.g. a timeout after the write applied).
    Dual-admit stays on — the daemon admits both values, so no delivery is
    rejected either way — and a human resolves it. This function only
    reports; `run_rotation` is what stops.
    """
    return (
        f"CHECKPOINT: {target.name} issuer update failed ({reason}) after "
        "dual-admit verification already passed. Dual-admit is left ON "
        f"({target.next_env_var} still staged) — the daemon admits both "
        "values, so no delivery is rejected either way. Manual resolution "
        "required: confirm which secret each GitHub hook actually holds, then "
        "finish by hand (a re-run is refused while the staged value is present). "
        "Do not run secrets_publish.py for this block until resolved (it would "
        "drop the staged value). This script will not guess. See "
        "docs/secrets_management.md, 'Resolving a checkpoint'."
    )


# ---------------------------------------------------------------------------
# Step 5: the canonical store (1Password)
# ---------------------------------------------------------------------------


class CanonicalStoreError(RuntimeError):
    """The canonical store could not be written or read back. Never a value."""


def canonical_ref(target: RotationTarget, manifest: dict | None = None) -> str | None:
    """The op:// reference the manifest maps the target's env var to, with the
    same ENVIRONMENT overlay secrets_publish.py applies."""
    manifest = manifest if manifest is not None else sl.load_manifest()
    try:
        refs = sl.resolve_refs(manifest, target.manifest_file, os.environ.get("ENVIRONMENT"))
    except KeyError:
        return None
    return refs.get(target.env_var)


def read_canonical(ref: str) -> str:
    return sl.op_read(ref)


def write_canonical_store(ref: str, value: str, *, what: str) -> str:
    """Write `value` to the 1Password field `ref` names, then read it back and
    compare in memory. Raises CanonicalStoreError on any failure."""
    try:
        op_write_password_field(ref, rpk._field_label_from_ref(ref), value)
    except Exception as exc:  # noqa: BLE001 — surfaced, never a value
        raise CanonicalStoreError(f"write failed: {_safe(exc)}") from None
    try:
        readback = read_canonical(ref)
    except Exception as exc:  # noqa: BLE001
        raise CanonicalStoreError(f"read-back failed: {_safe(exc)}") from None
    if not hmac.compare_digest(readback.encode(), value.encode()):
        raise CanonicalStoreError("read-back does not match the value written")
    return f"wrote the {what} value to 1Password ({ref}) and read it back: match"


def _restore_canonical(ref: str, old_value: str) -> tuple[bool, str]:
    """Put the old value back in 1Password. Returns (restored, status line);
    a failed restore is the caller's to escalate, never a footnote."""
    try:
        return True, write_canonical_store(ref, old_value, what="retired (restored)")
    except Exception as exc:  # noqa: BLE001 — CanonicalStoreError or anything else
        return False, f"1Password restore FAILED ({_safe(exc)})"


def _canonical_mismatch_checkpoint(target: RotationTarget, ref: str) -> str:
    return (
        f"CHECKPOINT: 1Password ({ref}) may still hold the NEW value while the snapshot "
        f"and the dotenv hold the old one. Do NOT run secrets_publish.py for the "
        f"'{target.manifest_file}' block: it would make the new value primary with no "
        "dual-admit. Set the 1Password field back to the snapshot's value by hand; the "
        "next run's preflight refuses until all three agree. See "
        "docs/secrets_management.md, 'Resolving a checkpoint'."
    )


# ---------------------------------------------------------------------------
# Step 6: the issuer (GitHub webhook config) — webhook secret only
# ---------------------------------------------------------------------------


class GitHubAPIError(RuntimeError):
    """Raised on a non-2xx GitHub response. Message never includes the secret."""


def _github_http(
    method: str, url: str, headers: dict, body: bytes | None
) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        # Network failure, reset or timeout: status 0 is never a valid HTTP
        # status, so callers treat it as "could not be confirmed".
        return 0, f"{type(exc).__name__}: {exc}"


def _github_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ateles-rotate-swarm-secret",
    }


def update_github_webhook_secret(
    *,
    github_token: str,
    repo: str,
    hook_id: str,
    new_secret: str,
    http_request: Callable[..., tuple[int, str]] | None = None,
) -> None:
    """PATCH one GitHub webhook's config to `new_secret`.

    The secret travels only in the JSON body (GitHub's PATCH
    /repos/{owner}/{repo}/hooks/{id}/config contract), never in a header,
    URL, or log. `http_request(method, url, headers, body)` is injectable so
    tests never make a real network call.
    """
    url = f"https://api.github.com/repos/{repo}/hooks/{hook_id}/config"
    body = json.dumps({"secret": new_secret}).encode("utf-8")
    request = http_request or _github_http
    status, text = request("PATCH", url, _github_headers(github_token), body)
    if not (200 <= status < 300):
        safe_text = text.replace(new_secret, REDACTED) if new_secret in text else text
        raise GitHubAPIError(
            f"PATCH {repo} hooks/{hook_id}/config -> HTTP {status}: {_safe(safe_text)[:300]}"
        )


def github_ping_check(
    *,
    github_token: str,
    repo: str,
    hook_id: str,
    http_request: Callable[..., tuple[int, str]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = 12,
    interval: float = 5.0,
) -> "VerificationResult":
    """Ask GitHub to send its own signed `ping` to the hook, then read the
    delivery's HTTP status back from the deliveries API.

    Run after promotion (apis admits only the new value), a 200 proves GitHub
    signs with the new value — the one check no self-signed probe can make.
    """
    request = http_request or _github_http
    headers = _github_headers(github_token)
    base = f"https://api.github.com/repos/{repo}/hooks/{hook_id}"

    def _deliveries() -> list[dict] | None:
        status, text = request("GET", f"{base}/deliveries?per_page=30", headers, None)
        if not (200 <= status < 300):
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        return data if isinstance(data, list) else None

    before = _deliveries()
    if before is None:
        return VerificationResult(False, f"{repo} hook {hook_id}: deliveries unreadable")
    high_water = max((int(d.get("id", 0)) for d in before), default=0)
    status, _text = request("POST", f"{base}/pings", headers, b"")
    if status != 204:
        return VerificationResult(False, f"{repo} hook {hook_id}: ping request -> HTTP {status}")
    for _ in range(attempts):
        sleep(interval)
        after = _deliveries() or []
        pings = [
            d for d in after
            if d.get("event") == "ping" and int(d.get("id", 0)) > high_water
        ]
        if pings:
            code = pings[0].get("status_code")
            if code == 200:
                return VerificationResult(
                    True, f"{repo} hook {hook_id}: GitHub-signed ping delivered (200)", 200
                )
            return VerificationResult(
                False, f"{repo} hook {hook_id}: GitHub-signed ping -> {code}", code
            )
    return VerificationResult(False, f"{repo} hook {hook_id}: ping delivery never appeared")


def github_token_from_gh(login: str) -> str:
    """The keyring token `gh` holds for `login`, verified to resolve to that
    login before use (an empty or wrong-account token is refused). Ambient
    GH_TOKEN/GITHUB_TOKEN are ignored so the keyring entry is what is read."""
    env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
    result = subprocess.run(
        ["gh", "auth", "token", "--user", login],
        capture_output=True, text=True, timeout=20, env=env,
    )
    token = result.stdout.strip()
    if result.returncode != 0 or len(token) < 20:
        raise RuntimeError(f"no usable gh keyring token for {login!r}")
    _register_secret(token)
    status, text = _github_http("GET", "https://api.github.com/user", _github_headers(token), None)
    found = json.loads(text).get("login") if status == 200 else None
    if found != login:
        raise RuntimeError(f"gh token for {login!r} resolves to {found!r} (HTTP {status})")
    return token


# ---------------------------------------------------------------------------
# Daemon restart with pid read-back (steps 3 and 7)
# ---------------------------------------------------------------------------

RESTART_TIMEOUT_S = 30.0
RESTART_SETTLE_S = 3.0


@dataclass
class RestartResult:
    ok: bool
    line: str
    old_pid: int | None = None
    new_pid: int | None = None

    def __str__(self) -> str:
        return self.line


def read_launchd_pid(
    label: str, *, runner: Callable[..., subprocess.CompletedProcess] | None = None
) -> int | None:
    """The running pid launchd reports for `label`, or None when it has no
    process. Parses `launchctl list`'s PID/Status/Label table — never `print`,
    which would dump the job's environment."""
    run = runner or subprocess.run
    result = run(["launchctl", "list"], capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == label:
            return int(parts[0]) if parts[0].isdigit() else None
    return None


def restart_daemon(
    plist_label: str,
    *,
    dry_run: bool,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    pid_reader: Callable[[str], int | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float | None = None,
    settle: float | None = None,
) -> RestartResult:
    """Restart one daemon via `launchctl kickstart -k` and read back its pid.

    Reports success only when launchd shows a NEW pid that is still the same
    process after a settle delay (a crash-looping daemon changes pid again or
    shows none). Runs only with `ATELES_ALLOW_DAEMON_RESTART=1` in this
    process's own environment; otherwise prints the command and returns.
    """
    uid = os.environ.get("UID") or str(os.getuid())
    cmd = ["launchctl", "kickstart", "-k", f"gui/{uid}/{plist_label}"]
    if dry_run or os.environ.get("ATELES_ALLOW_DAEMON_RESTART") != "1":
        return RestartResult(dry_run, f"[not run] {' '.join(cmd)} (then read back a new pid)")
    timeout = RESTART_TIMEOUT_S if timeout is None else timeout
    settle = RESTART_SETTLE_S if settle is None else settle
    read_pid = pid_reader or (lambda label: read_launchd_pid(label))
    run = runner or subprocess.run
    try:
        return _kickstart_and_read_back(plist_label, cmd, run, read_pid, sleep, timeout, settle)
    except Exception as exc:  # noqa: BLE001 — a restart that cannot be confirmed is a failure
        return RestartResult(
            False, f"restart FAILED: {plist_label} ({type(exc).__name__}: {_safe(exc)})"
        )


def _kickstart_and_read_back(plist_label, cmd, run, read_pid, sleep, timeout, settle):
    old_pid = read_pid(plist_label)
    result = run(cmd, capture_output=True, text=True, timeout=30)  # type: ignore[call-arg]
    if result.returncode != 0:
        return RestartResult(
            False, f"restart FAILED: {' '.join(cmd)} -> {_safe(result.stderr)}", old_pid
        )
    new_pid: int | None = None
    waited = 0.0
    step = 0.5
    while waited <= timeout:
        pid = read_pid(plist_label)
        if pid is not None and pid != old_pid:
            new_pid = pid
            break
        sleep(step)
        waited += step
    if new_pid is None:
        return RestartResult(
            False,
            f"restart NOT CONFIRMED: {plist_label} pid still {old_pid} after {timeout:.0f}s",
            old_pid,
        )
    sleep(settle)
    settled = read_pid(plist_label)
    if settled != new_pid:
        return RestartResult(
            False,
            f"restart UNSTABLE: {plist_label} pid {old_pid} -> {new_pid} -> {settled} "
            f"within {settle:.0f}s",
            old_pid,
            new_pid,
        )
    return RestartResult(
        True,
        f"restarted {plist_label}: pid {old_pid} -> {new_pid} (read back after {settle:.0f}s)",
        old_pid,
        new_pid,
    )


# ---------------------------------------------------------------------------
# Live verification — real probes against the daemon
# ---------------------------------------------------------------------------


@dataclass
class VerificationResult:
    ok: bool
    detail: str  # human-readable, never the secret value
    status: int | None = None  # HTTP status, when there was one


def _default_post(url: str, headers: dict, body: bytes) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — URLError, reset, read timeout: no answer
        return 0, f"{type(exc).__name__}: {exc}"


def verify_github_webhook_secret(
    new_secret: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    http_post: Callable[..., tuple[int, str]] | None = None,
) -> VerificationResult:
    """Send a signed `ping` delivery, HMAC-signed with `new_secret` — what
    GitHub's own redelivery of a ping looks like to the daemon."""
    body = b'{"zen":"rotation-verify"}'
    sig = "sha256=" + hmac.new(new_secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "X-GitHub-Event": "ping",
        "X-GitHub-Delivery": f"rotation-verify-{int(time.time())}",
        "X-Hub-Signature-256": sig,
        "Content-Type": "application/json",
    }
    post = http_post or _default_post
    status, _text = post(f"{base_url}/github/webhook", headers, body)
    if status == 200:
        return VerificationResult(True, "signed ping accepted (200)", status)
    return VerificationResult(False, f"signed ping rejected (status={status})", status)


def verify_approve_email_secret(
    new_secret: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    http_post: Callable[..., tuple[int, str]] | None = None,
) -> VerificationResult:
    """POST /approve-email carrying `new_secret` with an out-of-range
    pr_number (0): the route checks the secret first and then rejects the
    body with 400, so 400 means "secret accepted" and no merge can follow."""
    body = json.dumps({"repository": "rotation-verify/none", "pr_number": 0}).encode()
    headers = {"X-Approve-Secret": new_secret, "Content-Type": "application/json"}
    post = http_post or _default_post
    status, _text = post(f"{base_url}/approve-email", headers, body)
    if status == 400:
        return VerificationResult(True, "secret accepted (400 on deliberately invalid body)", 400)
    if status == 401:
        return VerificationResult(False, "secret rejected (401)", 401)
    if status == 503:
        return VerificationResult(False, "route not configured (503) — secret never reached", 503)
    return VerificationResult(False, f"unexpected status={status}", status)


VERIFIERS: dict[str, Callable[..., VerificationResult]] = {
    "github_webhook_secret": verify_github_webhook_secret,
    "approve_email_secret": verify_approve_email_secret,
}


# The presenter check runs in a child process so the daemon's module loads
# its configuration exactly as the daemon does (lib.daemon_runtime reading
# the dotenv at import). It prints only the HTTP status apis returned.
_TURDUS_PROBE = r"""
import json, sys, urllib.error, urllib.request
root = sys.argv[1]
sys.path.insert(0, root)
sys.path.insert(0, root + "/execution/daemons/turdus")
import turdus
if not turdus.APIS_APPROVE_EMAIL_SECRET:
    print("status=unset")
    raise SystemExit(0)
req = urllib.request.Request(
    turdus.APIS_APPROVE_URL,
    data=json.dumps({"repository": "rotation-verify/none", "pr_number": 0,
                     "sender": "rotation-verify"}).encode(),
    headers={"X-Approve-Secret": turdus.APIS_APPROVE_EMAIL_SECRET,
             "Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(f"status={resp.status}")
except urllib.error.HTTPError as exc:
    print(f"status={exc.code}")
except Exception:
    print("status=0")
"""

# Variables that would make the child load something other than the daemon's
# own configuration (or skip loading it).
_PRESENTER_ENV_STRIP = (
    "APIS_APPROVE_EMAIL_SECRET",
    "APIS_APPROVE_EMAIL_SECRET_NEXT",
    "APIS_APPROVE_EMAIL_URL",
    "ATELES_SKIP_DOTENV",
    "ATELES_DOTENV_FILE",
    "PYTEST_CURRENT_TEST",
    "TURDUS_DRY_RUN",
)


def verify_turdus_presents_accepted_secret(
    *, daemon_checkout: Path, runner: Callable[..., subprocess.CompletedProcess] | None = None
) -> VerificationResult:
    """From turdus's side: import turdus from the daemon's checkout, let it
    load its configuration the way the daemon does, and POST the secret IT
    holds to apis. 400 = apis accepted turdus's secret. Run after apis has
    retired the old value, so a pass proves turdus holds the new one."""
    env = {k: v for k, v in os.environ.items() if k not in _PRESENTER_ENV_STRIP}
    run = runner or subprocess.run
    try:
        result = run(
            [sys.executable, "-c", _TURDUS_PROBE, str(daemon_checkout)],
            capture_output=True, text=True, timeout=60, env=env, cwd=str(daemon_checkout),
        )
    except Exception as exc:  # noqa: BLE001
        return VerificationResult(False, f"turdus-side probe did not run ({type(exc).__name__})")
    match = re.search(r"status=(\w+)", result.stdout or "")
    if result.returncode != 0 or not match:
        return VerificationResult(
            False, f"turdus-side probe did not run (exit {result.returncode})"
        )
    code = match.group(1)
    if code == "400":
        return VerificationResult(True, "turdus's loaded secret accepted by apis (400)", 400)
    if code == "unset":
        return VerificationResult(False, "turdus loads no approve secret")
    return VerificationResult(False, f"turdus's loaded secret -> status={code}")


PRESENTER_VERIFIERS: dict[str, Callable[..., VerificationResult]] = {
    "com.ateles.turdus": verify_turdus_presents_accepted_secret,
}

PROBE_READY_TIMEOUT_S = 30.0


def _probe(
    verifier: Callable[..., VerificationResult],
    value: str,
    base_url: str,
    sleep: Callable[[float], None] = time.sleep,
) -> VerificationResult:
    """Run a probe, retrying only while the daemon is not yet listening
    (status 0) after a restart. Any HTTP answer is final."""
    waited = 0.0
    while True:
        result = verifier(value, base_url=base_url)
        if result.status != 0 or waited >= PROBE_READY_TIMEOUT_S:
            return result
        sleep(1.0)
        waited += 1.0


# ---------------------------------------------------------------------------
# Preflight (step 0)
# ---------------------------------------------------------------------------


@dataclass
class Preflight:
    ok: bool
    reason: str = ""
    ref: str = ""
    old_value: str = field(default="", repr=False)


def preflight(
    target: RotationTarget,
    *,
    hooks: list[tuple[str, str]],
    github_token: str | None,
    allow_real_github_write: bool,
    base_url: str = DEFAULT_BASE_URL,
) -> Preflight:
    """Everything a real run needs, checked before anything is written."""
    if not _base_url_is_safe(base_url):
        return Preflight(False, f"--base-url {base_url!r} would send the secret off this "
                         "machine in clear text — use a loopback http URL or https")
    if os.environ.get("ATELES_ALLOW_DAEMON_RESTART") != "1":
        return Preflight(False, "ATELES_ALLOW_DAEMON_RESTART=1 is not set — a real "
                         "rotation cannot take effect without restarting the daemons")
    if target.has_external_issuer:
        if not allow_real_github_write:
            return Preflight(False, "--i-know-this-calls-real-github not passed — a real "
                             "rotation of an externally-issued secret cannot complete "
                             "without updating the issuer")
        if not hooks:
            return Preflight(False, "no --github-hook given — the issuer cannot be updated")
        if not github_token:
            return Preflight(False, "no GitHub token — pass --github-token-gh-user LOGIN "
                             "or --github-token-ref op://...; the issuer cannot be updated")
    for label in target.presenter_plists:
        if label not in PRESENTER_VERIFIERS:
            return Preflight(False, f"no presenter check for {label}")
    try:
        manifest = sl.load_manifest()
    except (OSError, ValueError) as exc:
        return Preflight(False, f"secrets manifest unreadable ({type(exc).__name__})")
    ref = canonical_ref(target, manifest)
    if not ref:
        return Preflight(False, f"manifest block {target.manifest_file!r} does not map "
                         f"{target.env_var} to a 1Password reference — onboard the block "
                         "first (see docs/secrets_management.md, 'Where these two secrets live')")
    others = _other_blocks_mapping(target, manifest)
    if others:
        return Preflight(False, f"{target.env_var} is also mapped by manifest block(s) "
                         f"{', '.join(others)}; a later publish or materialize of those "
                         "would bring the retired value back — remove the duplicate first")
    if not sl.enc_file(target.manifest_file).exists():
        return Preflight(False, f"no snapshot secrets/{target.manifest_file}.sops.enc — "
                         "onboard the block first (see docs/secrets_management.md, 'Where these two secrets live')")
    snapshot = _read_snapshot(target)
    old_value = snapshot.get(target.env_var, "")
    if not old_value:
        return Preflight(False, f"snapshot carries no {target.env_var}")
    _register_secret(old_value)
    if target.next_env_var in snapshot:
        return Preflight(False, f"{target.next_env_var} is already staged in the snapshot — "
                         "an earlier rotation left dual-admit open; resolve it first")
    env_file = sl.block_env_file(manifest, target.manifest_file)
    dotenv = sl.parse_dotenv(env_file.read_text()) if env_file.exists() else {}
    if target.next_env_var in dotenv:
        return Preflight(False, f"{target.next_env_var} is present in {env_file} — an earlier "
                         "rotation left dual-admit open; resolve it first")
    if not hmac.compare_digest(dotenv.get(target.env_var, "").encode(), old_value.encode()):
        return Preflight(False, f"{env_file} and the snapshot disagree on {target.env_var} — "
                         "run secrets_materialize.py for the block first")
    try:
        canonical = read_canonical(ref)
    except Exception as exc:  # noqa: BLE001
        return Preflight(False, f"1Password unreadable ({_safe(exc)})")
    if not hmac.compare_digest(canonical.encode(), old_value.encode()):
        return Preflight(False, f"1Password ({ref}) and the snapshot disagree on "
                         f"{target.env_var} — republish the block first")
    return Preflight(True, ref=ref, old_value=old_value)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class RotationReport:
    target_name: str
    dry_run: bool
    steps: list[str] = field(default_factory=list)
    rolled_forward: bool = False
    rolled_back: bool = False
    checkpoint_raised: bool = False
    refused: bool = False
    verification: VerificationResult | None = None
    snapshot_changed: bool = False
    canonical_touched: bool = False

    def add(self, line) -> None:
        self.steps.append(str(line))

    def outcome(self) -> str:
        if self.refused:
            return "refused at preflight (nothing staged, nothing restarted)"
        if self.checkpoint_raised:
            return "CHECKPOINT — dual-admit left ON, manual resolution required"
        if self.rolled_forward:
            return "rolled forward (new value promoted, old retired)"
        if self.rolled_back:
            return "rolled back (old value retained, new value discarded)"
        return "dry-run (no state changed)"

    def as_neotoma_notes(self) -> str:
        """A record-safe summary for the operator: outcome, live check, steps.
        Never a secret value — every step line is value-free by construction."""
        lines = [f"rotate_swarm_secret {self.target_name}: {self.outcome()}"]
        if self.verification is not None:
            lines.append(f"live check: {self.verification.detail}")
        lines.extend(self.steps)
        return "\n".join(lines)


def _dry_run_plan(
    report: RotationReport, target: RotationTarget, base_url: str, hooks: list
) -> RotationReport:
    report.add("[dry-run] preflight: restart flag, manifest mapping, snapshot = dotenv = "
               "1Password on the current value, no leftover _NEXT")
    report.add("[dry-run] would generate a new value in memory (never logged)")
    report.add(stage_next_value(target, "", dry_run=True))
    report.add(restart_daemon(target.daemon_plist, dry_run=True))
    report.add(
        f"[dry-run] would POST a signed probe to {base_url}{target.verify_path} "
        "under the NEW value (dual-admit; GitHub not yet touched)"
    )
    report.add("[dry-run] would write the new value to the manifest's 1Password "
               "reference and read it back")
    if target.has_external_issuer:
        named = ", ".join(f"{r}:{h}" for r, h in hooks) or "<--github-hook OWNER/REPO:ID>"
        report.add(
            f"[dry-run] would PATCH GitHub webhook config ({named}) to the new value "
            "ONLY IF that probe passes (requires --i-know-this-calls-real-github)"
        )
    report.add(promote_and_retire(target, "", dry_run=True))
    for label in target.presenter_plists:
        report.add(restart_daemon(label, dry_run=True))
    report.add(restart_daemon(target.daemon_plist, dry_run=True))
    checks = ["apis accepts the new value", "apis rejects the old value (401)"]
    checks += [f"{label} presents an accepted value" for label in target.presenter_plists]
    if target.has_external_issuer:
        checks.append("a GitHub-signed ping is delivered with 200")
    report.add("[dry-run] post-promotion checks: " + "; ".join(checks))
    return report


def _rollback(report: RotationReport, target: RotationTarget) -> RotationReport:
    try:
        report.add(roll_back(target, dry_run=False))
        report.snapshot_changed = True
    except Exception as exc:  # noqa: BLE001
        report.add(f"CHECKPOINT: rollback itself failed ({_safe(exc)}) — "
                   f"{target.next_env_var} may still be staged")
        report.checkpoint_raised = True
        return report
    restart = restart_daemon(target.daemon_plist, dry_run=False)
    report.add(restart)
    if not restart.ok:
        report.add(f"CHECKPOINT: {target.daemon_plist} did not confirm a restart after "
                   "rollback — it may still be running with the staged value admitted")
        report.checkpoint_raised = True
        return report
    report.rolled_back = True
    return report


def _post_promotion_checks(
    target: RotationTarget,
    *,
    new_value: str,
    old_value: str,
    base_url: str,
    daemon_checkout: Path,
    hooks: list[tuple[str, str]],
    github_token: str | None,
) -> list[VerificationResult]:
    results: list[VerificationResult] = []
    verifier = VERIFIERS[target.name]
    new_check = _probe(verifier, new_value, base_url)
    results.append(VerificationResult(new_check.ok, f"new value: {new_check.detail}",
                                      new_check.status))
    old_check = _probe(verifier, old_value, base_url)
    results.append(VerificationResult(
        old_check.status == 401,
        f"old value retired: {old_check.detail}",
        old_check.status,
    ))
    for label in target.presenter_plists:
        results.append(PRESENTER_VERIFIERS[label](daemon_checkout=daemon_checkout))
    if target.has_external_issuer:
        for repo, hook_id in hooks:
            results.append(github_ping_check(
                github_token=github_token or "", repo=repo, hook_id=hook_id
            ))
    return results


def run_rotation(
    target: RotationTarget,
    *,
    dry_run: bool,
    github_token: str | None = None,
    github_repo: str | None = None,
    github_hook_id: str | None = None,
    github_hooks: list[tuple[str, str]] | tuple = (),
    allow_real_github_write: bool = False,
    base_url: str = DEFAULT_BASE_URL,
    daemon_checkout: Path = DEFAULT_DAEMON_CHECKOUT,
) -> RotationReport:
    """Run the full staged sequence for one target and return a report.
    The module docstring's SEQUENCE section is the specification."""
    report = RotationReport(target_name=target.name, dry_run=dry_run)
    hooks = list(github_hooks)
    if github_repo and github_hook_id:
        hooks.append((github_repo, str(github_hook_id)))

    if dry_run:
        return _dry_run_plan(report, target, base_url, hooks)

    # 0. preflight. An unexpected error here changed nothing, so it is a refusal.
    try:
        pre = preflight(target, hooks=hooks, github_token=github_token,
                        allow_real_github_write=allow_real_github_write, base_url=base_url)
    except Exception as exc:  # noqa: BLE001
        pre = Preflight(False, f"preflight could not run ({type(exc).__name__}: {_safe(exc)})")
    if not pre.ok:
        report.add(f"REFUSED: {pre.reason}")
        report.refused = True
        return report
    report.add(f"preflight passed: snapshot, dotenv and 1Password ({pre.ref}) agree "
               f"on {target.env_var}; no leftover {target.next_env_var}")
    old_value, ref = pre.old_value, pre.ref

    # 1. generate
    new_value = generate_new_value()
    _register_secret(new_value)
    report.add("generated new value in memory (never logged)")

    # Steps 2-5 run before anything outside this machine knows the new value.
    # ANY failure among them — a check that fails or one that cannot run,
    # including an unexpected exception — rolls back.
    try:
        _stage_check_and_store(report, target, new_value, ref, base_url)
    except Exception as exc:  # noqa: BLE001 — _RollBack or anything unexpected
        if isinstance(exc, _RollBack):
            report.add(str(exc))
            restore = exc.restore_canonical
        else:
            report.add(f"unexpected error before promotion ({type(exc).__name__}: "
                       f"{_safe(exc)}) — rolling back")
            restore = report.canonical_touched
        restored = True
        if restore:
            restored, line = _restore_canonical(ref, old_value)
            report.add(line)
        _rollback(report, target)
        if not restored:
            report.add(_canonical_mismatch_checkpoint(target, ref))
            report.checkpoint_raised = True
        return report

    # 6. issuer. A failure here is a checkpoint: GitHub's state is unconfirmed.
    if target.has_external_issuer:
        for repo, hook_id in hooks:
            try:
                update_github_webhook_secret(
                    github_token=github_token or "", repo=repo, hook_id=hook_id,
                    new_secret=new_value,
                )
                report.add(f"updated GitHub webhook config for {repo}:{hook_id}")
            except Exception as exc:  # noqa: BLE001 — GitHubAPIError or anything else
                reason = str(exc) if isinstance(exc, GitHubAPIError) else (
                    f"{type(exc).__name__}: {_safe(exc)}")
                report.add(raise_issuer_checkpoint(target, reason=reason))
                restored, line = _restore_canonical(ref, old_value)
                report.add(line)
                if not restored:
                    report.add(f"1Password ({ref}) may still hold the NEW value — set it to "
                               "match whichever value you resolve GitHub to.")
                report.checkpoint_raised = True
                return report

    # 7-8. promote, restart presenters then the verifier, check. Any failure,
    # including an unexpected exception, re-opens dual-admit with the old value.
    failures: list[str] = []
    try:
        report.add(promote_and_retire(target, new_value, dry_run=False))
        for label in (*target.presenter_plists, target.daemon_plist):
            restart = restart_daemon(label, dry_run=False)
            report.add(restart)
            if not restart.ok:
                failures.append(f"{label} restart not confirmed")
        if not failures:
            for check in _post_promotion_checks(
                target, new_value=new_value, old_value=old_value, base_url=base_url,
                daemon_checkout=daemon_checkout, hooks=hooks, github_token=github_token,
            ):
                report.add(f"post-promotion check: {check.detail} -> "
                           f"{'ok' if check.ok else 'FAILED'}")
                if not check.ok:
                    failures.append(check.detail)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"unexpected error after promotion began "
                        f"({type(exc).__name__}: {_safe(exc)})")
    if failures:
        report.checkpoint_raised = True
        report.add(f"CHECKPOINT: post-promotion step failed ({'; '.join(failures)}).")
        report.add(_reopen_after_failure(target, old_value, new_value))
        return report

    report.rolled_forward = True
    return report


class _RollBack(Exception):
    """A pre-promotion step failed; carries a value-free reason."""

    def __init__(self, reason: str, *, restore_canonical: bool = False) -> None:
        super().__init__(reason)
        self.restore_canonical = restore_canonical


def _stage_check_and_store(
    report: RotationReport, target: RotationTarget, new_value: str, ref: str, base_url: str
) -> None:
    """Steps 2-5. Raises _RollBack (or anything unexpected) to roll back."""
    report.snapshot_changed = True  # set before the write: a partial write still needs committing
    report.add(stage_next_value(target, new_value, dry_run=False))
    restart = restart_daemon(target.daemon_plist, dry_run=False)
    report.add(restart)
    if not restart.ok:
        raise _RollBack(f"{target.daemon_plist} restart not confirmed — rolling back")
    result = _probe(VERIFIERS[target.name], new_value, base_url)
    report.verification = result
    report.add(f"dual-admit check: {result.detail}")
    if not result.ok:
        raise _RollBack("dual-admit check failed — rolling back")
    report.canonical_touched = True
    try:
        report.add(write_canonical_store(ref, new_value, what="new"))
    except CanonicalStoreError as exc:
        raise _RollBack(f"1Password write FAILED ({exc}) — rolling back",
                        restore_canonical=True) from None


def _reopen_after_failure(target: RotationTarget, old_value: str, new_value: str) -> str:
    """Re-open dual-admit and say truthfully what state that left, whether or
    not it worked."""
    try:
        line = reopen_dual_admit(target, old_value, new_value)
    except Exception as exc:  # noqa: BLE001
        return (f"re-opening dual-admit FAILED ({type(exc).__name__}: {_safe(exc)}). "
                f"The new value may be primary with NO {target.next_env_var}: a consumer "
                "still holding the old value is being rejected. Resolve now — see "
                "docs/secrets_management.md, 'Resolving a checkpoint'.")
    restart = restart_daemon(target.daemon_plist, dry_run=False)
    if not restart.ok:
        return (f"{line}; but {restart.line} — the running {target.daemon_plist} may not "
                "admit the old value yet. Resolve now — see docs/secrets_management.md, "
                "'Resolving a checkpoint'.")
    return (f"{line}; {restart.line}. apis now admits both the old and the new value, so "
            "a consumer holding either keeps working. 1Password holds the new value. See "
            "docs/secrets_management.md, 'Resolving a checkpoint'.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_hook(spec: str) -> tuple[str, str]:
    repo, sep, hook_id = spec.rpartition(":")
    if not sep or "/" not in repo or not hook_id.isdigit():
        raise argparse.ArgumentTypeError(f"expected owner/repo:hook_id, got {spec!r}")
    return repo, hook_id


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        description=(
            "Staged rotation for a swarm-issued-and-consumed shared secret "
            "(authority_model.md#grants; conformance.md row 118). Without "
            "--dry-run this is LIVE: it writes the snapshot, the dotenv and "
            "1Password and restarts daemons, and refuses unless "
            "ATELES_ALLOW_DAEMON_RESTART=1 is set."
        ),
        epilog=(
            "exit codes: 0 rolled forward (or dry-run); 1 rolled back, nothing "
            "rotated; 2 checkpoint, dual-admit left on, resolve by hand; 3 refused "
            "at preflight, nothing changed; 4 internal error, state unknown; "
            "64 usage error. See docs/secrets_management.md, 'Swarm-run rotation'."
        ),
    )
    parser.add_argument("target", choices=sorted(TARGETS), help="Which secret to rotate.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan with no values and no side effects.",
    )
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL,
        help="Apis gateway base URL for the live-verification probe.",
    )
    parser.add_argument(
        "--daemon-checkout", type=Path, default=DEFAULT_DAEMON_CHECKOUT,
        help="Checkout the daemons run from (the presenter check imports from it).",
    )
    parser.add_argument(
        "--github-hook", action="append", default=[], type=_parse_hook,
        metavar="OWNER/REPO:HOOK_ID",
        help="A webhook whose secret to update (repeatable; github_webhook_secret only).",
    )
    parser.add_argument("--github-repo", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--github-hook-id", default=None, help=argparse.SUPPRESS)
    token = parser.add_mutually_exclusive_group()
    token.add_argument(
        "--github-token-ref", default=None,
        help="op:// reference to a GitHub token that can edit repository webhooks.",
    )
    token.add_argument(
        "--github-token-gh-user", default=None, metavar="LOGIN",
        help="Use the gh keyring token for LOGIN (verified to resolve to LOGIN).",
    )
    parser.add_argument(
        "--notes-file", type=Path, default=None,
        help="Write the value-free outcome notes here, for the credential registry.",
    )
    parser.add_argument(
        "--i-know-this-calls-real-github", action="store_true",
        help="Tripwire: without it the GitHub webhook-config write never runs.",
    )
    return parser


EXIT_ROLLED_FORWARD = 0  # also dry-run
EXIT_ROLLED_BACK = 1
EXIT_CHECKPOINT = 2
EXIT_REFUSED = 3
EXIT_INTERNAL_ERROR = 4  # the script itself crashed; state unknown — inspect
EXIT_USAGE = 64  # bad arguments (argparse's own 2 would read as a checkpoint)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # noqa: D102
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _follow_up(report: RotationReport, target: RotationTarget) -> str | None:
    """What to do with the private-repo snapshot, by outcome. None if nothing."""
    if not report.snapshot_changed:
        return None
    snap = f"secrets/{target.manifest_file}.sops.enc"
    if report.rolled_forward:
        return (f"REQUIRED: commit the snapshot so other machines materialize the new "
                f"value:\n    cd {sl.SECRETS_BASE} && git add {snap} && git commit -m "
                f"\"chore(secrets): rotate {target.env_var}\" && git push")
    if report.checkpoint_raised:
        return (f"Do NOT commit {snap} until the checkpoint is resolved (see "
                "docs/secrets_management.md, 'Resolving a checkpoint').")
    return (f"No rotation happened. {snap} was re-encrypted with the same contents; "
            f"discard that change:\n    cd {sl.SECRETS_BASE} && git checkout -- {snap}")


def _notes_path_problem(path: Path) -> str | None:
    """Why the notes file could not be written, checked BEFORE the rotation runs
    so a bad path is a usage error, never a crash after a finished rotation."""
    if path.is_dir():
        return f"--notes-file {path} is a directory"
    parent = path.parent if str(path.parent) else Path(".")
    if not parent.is_dir():
        return f"--notes-file directory {parent} does not exist"
    if not os.access(parent, os.W_OK) or (path.exists() and not os.access(path, os.W_OK)):
        return f"--notes-file {path} is not writable"
    return None


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    target = TARGETS[args.target]
    if args.notes_file and not args.dry_run:
        problem = _notes_path_problem(args.notes_file)
        if problem:
            parser.error(problem)  # exits EXIT_USAGE before anything changes

    try:
        report = _run_from_args(args, target)
    except Exception as exc:  # noqa: BLE001 — run_rotation should not raise; say so if it does
        print(f"\n=== rotate_swarm_secret: {target.name} ===")
        print(f"  INTERNAL ERROR ({type(exc).__name__}: {_safe(exc)}). State unknown: check "
              f"the snapshot, the dotenv and 1Password for {target.next_env_var} before "
              "running again.")
        print("No secret value is printed above or anywhere in this run's output.")
        return EXIT_INTERNAL_ERROR

    print(f"\n=== rotate_swarm_secret: {target.name} ===")
    for line in report.steps:
        print(f"  {line}")
    print(f"  outcome: {report.outcome()}")
    follow_up = _follow_up(report, target)
    if follow_up:
        print(f"  {follow_up}")
    if args.notes_file and not report.dry_run:
        # The rotation is over: a failed write must not change its exit code.
        try:
            args.notes_file.write_text(report.as_neotoma_notes() + "\n")
            print(f"  record: value-free notes written to {args.notes_file} — store them on "
                  "the credential registry row (last_rotated_at on a rolled-forward run)")
        except OSError as exc:
            print(f"  WARNING: could not write --notes-file ({type(exc).__name__}: "
                  f"{_safe(exc)}); the lines above are the record. The exit code below "
                  "still reports the rotation's outcome.")
    elif not report.dry_run and not report.refused:
        print("  record: store this outcome on the credential registry row "
              "(last_rotated_at on a rolled-forward run); --notes-file writes it to a file")
    print("No secret value is printed above or anywhere in this run's output.")

    if report.refused:
        return EXIT_REFUSED
    if report.checkpoint_raised:
        return EXIT_CHECKPOINT
    if report.rolled_back:
        return EXIT_ROLLED_BACK
    return EXIT_ROLLED_FORWARD


def _run_from_args(args: argparse.Namespace, target: RotationTarget) -> RotationReport:
    github_token = None
    if not args.dry_run and target.has_external_issuer:
        try:
            if args.github_token_ref:
                github_token = sl.op_read(args.github_token_ref)
            elif args.github_token_gh_user:
                github_token = github_token_from_gh(args.github_token_gh_user)
        except Exception as exc:  # noqa: BLE001 — a token problem changes nothing: refuse
            report = RotationReport(target_name=target.name, dry_run=False, refused=True)
            report.add(f"REFUSED: GitHub token unavailable ({_safe(exc)})")
            return report
        _register_secret(github_token)
    return run_rotation(
        target,
        dry_run=args.dry_run,
        github_token=github_token,
        github_repo=args.github_repo,
        github_hook_id=args.github_hook_id,
        github_hooks=args.github_hook,
        allow_real_github_write=args.i_know_this_calls_real_github,
        base_url=args.base_url,
        daemon_checkout=args.daemon_checkout,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
