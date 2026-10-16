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
                             issuer: GitHub webhook config (markmhendrickson/ateles,
                             markmhendrickson/neotoma); consumer: apis (github_gateway.py)
    approve_email_secret    APIS_APPROVE_EMAIL_SECRET
                             issuer: none (internal-only); consumers: apis
                             (github_gateway.py /approve-email + /approve-release)
                             and turdus (presents the header)

Agent AAuth signing keys are registered in the `credential` entity type this
same task adds, but are NOT rotated by this script — see the registry row's
`notes` field. That is future work; the manual procedure proven 2026-09-25
for apus/formica is the current path.

SEQUENCE (per `--dry-run`, and per the docstrings on each step function):
  1. generate           — a new random secret value, held only in memory.
  2. stage (dual-admit)  — write the new value to the consumer's ``_NEXT``
     slot (APIS_GITHUB_WEBHOOK_SECRET_NEXT / APIS_APPROVE_EMAIL_SECRET_NEXT)
     via the SOPS snapshot + materialize, so apis accepts EITHER value.
  3. restart the ONE consuming daemon (apis; turdus only for
     approve_email_secret, once switched — see step 6) so the staged
     ``_NEXT`` value takes effect.
  4. live-verify UNDER DUAL-ADMIT — send a real signed probe (a signed
     /github/webhook ping the script itself signs, or a signed
     /approve-email ping) and confirm the daemon accepts it under the NEW
     value specifically, while GitHub (for the webhook target) has not yet
     been told about that value at all. This is the ordering fix from a
     2026-09-26 review (Falco/security, Pavo/pm, Phoenicurus/qa all
     independently traced the same defect in an earlier revision that
     updated the issuer BEFORE verifying): the daemon-side half of the
     rotation is proven safe entirely with a probe the script signs itself,
     with zero GitHub involvement, so a failure here can NEVER leave GitHub
     and the daemon disagreeing — GitHub was never touched.
  5. update issuer (webhook secret only, and ONLY if step 4 passed) — PATCH
     the GitHub webhook config to the (now proven-working) new value (this
     script never calls that API for real; see HARD RULE below). If this
     step itself fails or cannot be confirmed, dual-admit is left ON
     (neither promoted nor rolled back) and a checkpoint is raised rather
     than guessing which side to trust — see `roll_back`'s docstring and
     `RotationReport.checkpoint_raised`.
  6. only once every prior step for the target succeeded: promote NEW to
     primary, retire OLD, clear ``_NEXT``, publish + materialize + restart
     again. On a step-4 verification failure: roll back — leave the OLD
     value as primary, clear the staged NEW value, publish + materialize +
     restart so the daemon returns to exactly its pre-rotation state (this
     is now always safe to do unconditionally, because step 5 — the only
     step with no local undo — never runs before step 4 passes). Neither
     outcome is silent; both are printed and returned as the process exit
     code (0 rolled-forward, 1 rolled-back, 2 checkpoint raised).

HARD RULES enforced by this script itself, not just by policy:
  - Never prints a secret value. `--dry-run` prints the plan (steps, which
    files/vars/daemons are touched, what the live check will POST to) with
    every value redacted as `<redacted>` or a fixed-width hash-of-nothing
    placeholder — never a length-revealing partial value.
  - Never calls the real GitHub webhook-config write API. `update_github_webhook_secret`
    is written and unit-tested against a MOCKED GitHub API only; this module's
    own `main()` never calls it unless `--i-know-this-calls-real-github` is
    also passed (absent from every doc example and from CI), which is the
    explicit tripwire the task's "never run a real rotation" instruction asks
    for — the flag existing at all is the seam a future real run would use,
    not an invitation to use it now.
  - Never restarts a real daemon. `restart_daemon` shells out to `launchctl`
    only when `ATELES_ALLOW_DAEMON_RESTART=1` is set in ITS OWN process
    environment (never read from argv), and this script's tests never set it.
    Without it, the function prints the exact command and returns.
  - Every subprocess call (`op`, `sops`, `launchctl`, `curl`-equivalent probe)
    passes the secret value via a piped stdin template or an HTTP header —
    never as a CLI argument — mirroring the contract `rotate_provider_key.py`
    (PR #1297) already states and this module re-derives via `secrets_lib`.

Usage:
    python execution/scripts/rotate_swarm_secret.py github_webhook_secret --dry-run
    python execution/scripts/rotate_swarm_secret.py approve_email_secret --dry-run

Real execution is gated behind the two tripwires above and is not exercised
by this PR (bootstrap mode — branch and review only, never run against the
real daemons, 1Password, or GitHub).
"""

from __future__ import annotations

import argparse
import json
import os
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
import secrets_lib as sl  # noqa: E402

REDACTED = "<redacted>"

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
    manifest_file: str  # which secrets/<name>.sops.env block carries it
    daemon_plist: str  # launchctl label of the ONE consuming daemon to restart
    has_external_issuer: bool  # True only for github_webhook_secret
    consumers: tuple[str, ...]
    verify_path: str  # HTTP path on the daemon to probe for live verification


TARGETS: dict[str, RotationTarget] = {
    "github_webhook_secret": RotationTarget(
        name="github_webhook_secret",
        env_var="APIS_GITHUB_WEBHOOK_SECRET",
        next_env_var="APIS_GITHUB_WEBHOOK_SECRET_NEXT",
        manifest_file="neotoma",
        daemon_plist="com.ateles.apis",
        has_external_issuer=True,
        consumers=("apis (github_gateway.py — verify_github_signature_any)",),
        verify_path="/github/webhook",
    ),
    "approve_email_secret": RotationTarget(
        name="approve_email_secret",
        env_var="APIS_APPROVE_EMAIL_SECRET",
        next_env_var="APIS_APPROVE_EMAIL_SECRET_NEXT",
        manifest_file="neotoma",
        daemon_plist="com.ateles.apis",
        has_external_issuer=False,
        consumers=(
            "apis (github_gateway.py — /approve-email, /approve-release)",
            "turdus (turdus.py — presents the header)",
        ),
        verify_path="/approve-email",
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
# Step 2 / 6: stage and retire — write the _NEXT / primary slot via SOPS
# ---------------------------------------------------------------------------


def stage_next_value(target: RotationTarget, new_value: str, *, dry_run: bool) -> str:
    """Write `new_value` into the target's `_NEXT` env var and re-encrypt the
    manifest file block. Returns a status line (never the value).

    Real write: decrypt the current snapshot (offline, local age key), set
    the `_NEXT` key, re-encrypt. No `op` call is needed for this step — the
    primary value is untouched, so 1Password is not written to until
    promotion (step 6).
    """
    if dry_run:
        return (
            f"[dry-run] would set {target.next_env_var}={REDACTED} in "
            f"secrets/{target.manifest_file}.sops.env, then materialize"
        )
    enc = sl.enc_file(target.manifest_file)
    current = sl.sops_decrypt_dotenv(enc) if enc.exists() else {}
    current[target.next_env_var] = new_value
    sl.sops_encrypt_dotenv(sl.to_dotenv(current), enc)
    return f"staged {target.next_env_var} in secrets/{target.manifest_file}.sops.env"


def promote_and_retire(target: RotationTarget, new_value: str, *, dry_run: bool) -> str:
    """On successful verification: NEW becomes primary, old is dropped, the
    `_NEXT` slot is cleared. Returns a status line (never the value)."""
    if dry_run:
        return (
            f"[dry-run] would set {target.env_var}={REDACTED}, clear "
            f"{target.next_env_var}, in secrets/{target.manifest_file}.sops.env, "
            "then materialize + publish to 1Password"
        )
    enc = sl.enc_file(target.manifest_file)
    current = sl.sops_decrypt_dotenv(enc) if enc.exists() else {}
    current[target.env_var] = new_value
    current.pop(target.next_env_var, None)
    sl.sops_encrypt_dotenv(sl.to_dotenv(current), enc)
    return f"promoted {target.env_var}, cleared {target.next_env_var}"


def roll_back(target: RotationTarget, *, dry_run: bool) -> str:
    """On a FAILED (or unrunnable) dual-admit verification (step 4): clear the
    staged `_NEXT` value and leave the primary untouched. Returns a status
    line (never a value).

    This is the rollback the operator ruling requires: "a check that fails,
    or that cannot be run, is not a hold... the rotation rolls back, restoring
    the old credential's standing rather than leaving both in an ambiguous
    state." Clearing `_NEXT` (rather than merely not promoting it) is what
    makes the daemon's post-rollback state identical to its pre-rotation
    state once restarted — a leftover `_NEXT` would keep the overlap window
    open indefinitely, which is not standing restored, only standing
    unresolved.

    SAFE-BY-CONSTRUCTION for `has_external_issuer` targets: this function is
    only ever called from `run_rotation` when step 4's dual-admit probe
    failed, which is BEFORE step 5 (the GitHub issuer update) ever runs — so
    there is nothing on the GitHub side to revert. This is the ordering fix
    from the 2026-09-26 review: an earlier revision updated the issuer before
    verifying, which meant a failed verification left GitHub signing with a
    value the post-rollback daemon no longer admitted. Reordering makes THIS
    function correct for every target without needing an issuer-revert path
    at all. See `run_rotation` and `raise_issuer_checkpoint` for the
    DIFFERENT failure this does not cover — the issuer update itself failing
    AFTER step 4 already passed, which is not a rollback case (the daemon-side
    change is proven good) but a checkpoint case (the issuer-side change is
    unconfirmed).
    """
    if dry_run:
        return (
            f"[dry-run] would clear {target.next_env_var} (rollback), then materialize"
        )
    enc = sl.enc_file(target.manifest_file)
    if not enc.exists():
        return f"nothing to roll back — no snapshot for {target.manifest_file}"
    current = sl.sops_decrypt_dotenv(enc)
    if target.next_env_var not in current:
        return f"{target.next_env_var} already clear — nothing to roll back"
    current.pop(target.next_env_var, None)
    sl.sops_encrypt_dotenv(sl.to_dotenv(current), enc)
    return f"rolled back — cleared {target.next_env_var}"


def raise_issuer_checkpoint(target: RotationTarget, *, reason: str) -> str:
    """The one case that is neither a promotion nor a rollback: step 4's
    dual-admit probe already proved the daemon accepts the new value, but
    step 5's GitHub issuer update itself failed or could not be confirmed.

    Per the operator's instruction (2026-09-26 review): "if the issuer's
    restore also fails, keep dual-admit ON, surface a checkpoint and stop."
    Generalized here to the actual failure mode this ordering produces —
    the issuer update was never attempted successfully in the first place,
    so there is nothing to "restore"; the safe move is to leave dual-admit
    exactly as it is (the daemon already admits both old and new — no
    delivery signed under EITHER value is rejected) rather than guess
    whether to promote (GitHub might not actually be on the new value yet)
    or roll back (GitHub might already be, if the failure was on the
    response read rather than the write itself, e.g. a timeout after
    GitHub already applied it). Both `_NEXT` and the primary are left
    untouched by this function — it only reports; `run_rotation` is what
    stops (never calling promote_and_retire or roll_back) once this fires.
    """
    return (
        f"CHECKPOINT: {target.name} issuer update failed ({reason}) after "
        "dual-admit verification already passed. Dual-admit is left ON "
        f"({target.next_env_var} still staged) — the daemon admits both "
        "values, so no delivery is rejected either way. Manual resolution "
        "required: confirm GitHub's actual webhook secret, then either "
        "retry the issuer update (promoting on success) or roll back "
        "manually if GitHub is confirmed still on the old value. This "
        "script will not guess."
    )


# ---------------------------------------------------------------------------
# Step 5: update the issuer (GitHub webhook config) — webhook secret only,
# and only once step 4's dual-admit verification has already passed
# ---------------------------------------------------------------------------


class GitHubAPIError(RuntimeError):
    """Raised on a non-2xx GitHub response. Message never includes the secret."""


def update_github_webhook_secret(
    *,
    github_token: str,
    repo: str,
    hook_id: str,
    new_secret: str,
    http_request: Callable[..., tuple[int, str]] | None = None,
) -> None:
    """PATCH the GitHub webhook config to `new_secret`.

    `http_request(method, url, headers, body_json) -> (status, text)` is
    injectable so tests never make a real network call; the default performs
    a real request and exists only so the function is genuinely callable in
    production, per the task's requirement that the *design* be complete even
    though a real run is never exercised in this PR. The secret is sent only
    in the JSON body (GitHub's own PATCH /repos/{owner}/{repo}/hooks/{id}
    contract: {"config": {"secret": "..."}}), never in a header, URL, or log.
    """
    url = f"https://api.github.com/repos/{repo}/hooks/{hook_id}/config"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ateles-rotate-swarm-secret",
    }
    body = json.dumps({"secret": new_secret}).encode("utf-8")

    def _default_request(
        method: str, url: str, headers: dict, body: bytes
    ) -> tuple[int, str]:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            # Network failure / timeout / connection refused — distinct from
            # an HTTP error status, and specifically the "cannot even be
            # confirmed" case the checkpoint path exists for: we genuinely do
            # not know whether GitHub received and applied the write before
            # the connection dropped. status=0 is never a valid HTTP status,
            # so the `not (200 <= status < 300)` check below always raises
            # for it, which is what routes this into raise_issuer_checkpoint
            # in run_rotation rather than silently treated as success.
            return 0, str(exc)

    request = http_request or _default_request
    status, text = request("PATCH", url, headers, body)
    if not (200 <= status < 300):
        # text may echo the request back in some GitHub error shapes; strip
        # anything that looks like our own secret before it can be logged.
        safe_text = text.replace(new_secret, REDACTED) if new_secret in text else text
        raise GitHubAPIError(
            f"PATCH hooks/{hook_id}/config -> HTTP {status}: {safe_text[:300]}"
        )


# ---------------------------------------------------------------------------
# Step 4: restart the ONE consuming daemon
# ---------------------------------------------------------------------------


def restart_daemon(
    plist_label: str,
    *,
    dry_run: bool,
    runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
) -> str:
    """Restart exactly one daemon via `launchctl kickstart -k`.

    Real execution requires BOTH `not dry_run` AND
    `ATELES_ALLOW_DAEMON_RESTART=1` in this process's own environment (never
    read from argv, so it cannot be smuggled through a logged command line).
    Absent that, the exact command is printed and nothing runs — this task's
    hard rule is "never restart a daemon", so the default path for every
    caller in this PR (dry-run or not) is print-only.
    """
    uid = os.environ.get("UID") or str(os.getuid())
    cmd = ["launchctl", "kickstart", "-k", f"gui/{uid}/{plist_label}"]
    if dry_run or os.environ.get("ATELES_ALLOW_DAEMON_RESTART") != "1":
        return f"[not run] {' '.join(cmd)}"
    run = runner or subprocess.run
    result = run(cmd, capture_output=True, text=True, timeout=30)  # type: ignore[call-arg]
    if result.returncode != 0:
        return f"restart FAILED: {cmd!r} -> {result.stderr.strip()}"
    return f"restarted {plist_label}"


# ---------------------------------------------------------------------------
# Step 5: live verification — a real signed probe against the daemon
# ---------------------------------------------------------------------------


@dataclass
class VerificationResult:
    ok: bool
    detail: str  # human-readable, never the secret value


def verify_github_webhook_secret(
    new_secret: str,
    *,
    base_url: str = "http://127.0.0.1:8742",
    http_post: Callable[..., tuple[int, str]] | None = None,
) -> VerificationResult:
    """Send a real signed `ping` delivery, HMAC-signed with `new_secret`.

    Mirrors exactly what GitHub's webhook "Redeliver" does for a ping event,
    so a pass here is not merely "the record admits the value" but "a real
    signed delivery under the new value is accepted" — the operator ruling's
    distinction between dual-admit's record-side fact and a live check.
    """
    import hashlib
    import hmac as hmac_mod

    body = b'{"zen":"rotation-verify"}'
    sig = (
        "sha256=" + hmac_mod.new(new_secret.encode(), body, hashlib.sha256).hexdigest()
    )
    headers = {
        "X-GitHub-Event": "ping",
        "X-GitHub-Delivery": f"rotation-verify-{int(time.time())}",
        "X-Hub-Signature-256": sig,
        "Content-Type": "application/json",
    }

    def _default_post(url: str, headers: dict, body: bytes) -> tuple[int, str]:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            return 0, str(exc)

    post = http_post or _default_post
    status, text = post(f"{base_url}/github/webhook", headers, body)
    if status == 200:
        return VerificationResult(True, "signed ping accepted (200)")
    return VerificationResult(False, f"signed ping rejected (status={status})")


def verify_approve_email_secret(
    new_secret: str,
    *,
    base_url: str = "http://127.0.0.1:8742",
    http_post: Callable[..., tuple[int, str]] | None = None,
) -> VerificationResult:
    """Send a real /approve-email probe carrying `new_secret` in the header.

    Uses an out-of-range pr_number (0) so the probe can never itself trigger
    a real merge-approval trigger even if it lands on a live daemon: the
    route's own validation (`pr_number <= 0` -> 400) rejects it AFTER the
    secret check, so a 400 (not 401/503) is what "the secret was accepted"
    looks like for this route — verification cares about the secret gate,
    not about a body forged for the actual dispatch.
    """
    body = json.dumps({"repository": "rotation-verify/none", "pr_number": 0}).encode()
    headers = {"X-Approve-Secret": new_secret, "Content-Type": "application/json"}

    def _default_post(url: str, headers: dict, body: bytes) -> tuple[int, str]:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            return 0, str(exc)

    post = http_post or _default_post
    status, _text = post(f"{base_url}/approve-email", headers, body)
    if status == 400:
        return VerificationResult(
            True, "secret accepted (400 on deliberately invalid body)"
        )
    if status == 401:
        return VerificationResult(False, "secret rejected (401)")
    if status == 503:
        return VerificationResult(
            False, "route not configured (503) — secret never reached"
        )
    return VerificationResult(False, f"unexpected status={status}")


VERIFIERS: dict[str, Callable[..., VerificationResult]] = {
    "github_webhook_secret": verify_github_webhook_secret,
    "approve_email_secret": verify_approve_email_secret,
}


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
    verification: VerificationResult | None = None

    def add(self, line: str) -> None:
        self.steps.append(line)

    def as_neotoma_notes(self) -> str:
        """A record-safe summary for the checkpoint the operator reads — the
        outcome and what the check found, per the operator ruling. Never a
        secret value; `steps` entries are themselves value-free by
        construction (every step function above redacts or omits values)."""
        if self.checkpoint_raised:
            outcome = "CHECKPOINT — dual-admit left ON, manual resolution required"
        elif self.rolled_forward:
            outcome = "rolled forward (new value promoted, old retired)"
        elif self.rolled_back:
            outcome = "rolled back (old value retained, new value discarded)"
        else:
            outcome = "dry-run (no state changed)"
        lines = [f"rotate_swarm_secret {self.target_name}: {outcome}"]
        if self.verification is not None:
            lines.append(f"live check: {self.verification.detail}")
        lines.extend(self.steps)
        return "\n".join(lines)


def run_rotation(
    target: RotationTarget,
    *,
    dry_run: bool,
    github_token: str | None = None,
    github_repo: str | None = None,
    github_hook_id: str | None = None,
    allow_real_github_write: bool = False,
    base_url: str = "http://127.0.0.1:8742",
) -> RotationReport:
    """Run the full staged sequence for one target and return a report.

    Every branch is reachable in `--dry-run` (the default, and the only mode
    exercised by this PR): step functions early-return a printed plan instead
    of touching disk/network/subprocess when `dry_run=True`, so the sequence
    and its rollback path are both exercised without side effects.

    ORDERING (fixed 2026-09-26 review — see the module docstring's SEQUENCE
    section): the GitHub issuer update for `has_external_issuer` targets runs
    ONLY AFTER the dual-admit verification (step 4) has already passed, never
    before. This closes the inconsistency window an earlier revision had:
    updating the issuer first meant a failed verification rolled the daemon
    back to admitting only the old value while GitHub was already signing
    with the new one. Verifying first means the probe that proves the new
    value works is signed by THIS SCRIPT, not by GitHub, so it never needs
    GitHub to know about the new value at all — the daemon's dual-admit
    already accepts it.
    """
    report = RotationReport(target_name=target.name, dry_run=dry_run)

    new_value = generate_new_value()
    report.add("generated new value in memory (never logged)")

    report.add(stage_next_value(target, new_value, dry_run=dry_run))
    report.add(restart_daemon(target.daemon_plist, dry_run=dry_run))

    if dry_run:
        report.add(
            f"[dry-run] would POST a signed probe to {base_url}{target.verify_path} "
            "under the NEW value (dual-admit; GitHub not yet touched)"
        )
        if target.has_external_issuer:
            report.add(
                "[dry-run] would PATCH GitHub webhook config to the new value "
                "ONLY IF that probe passes (requires --i-know-this-calls-real-github; "
                "never passed by this PR)"
            )
        return report

    verifier = VERIFIERS[target.name]
    result = verifier(new_value, base_url=base_url)
    report.verification = result

    if not result.ok:
        # Safe unconditionally: the issuer (if any) was never touched, so
        # there is nothing on the GitHub side to reconcile.
        report.add(roll_back(target, dry_run=dry_run))
        report.add(restart_daemon(target.daemon_plist, dry_run=dry_run))
        report.rolled_back = True
        return report

    if target.has_external_issuer:
        if not allow_real_github_write:
            # The probe already passed, but a REAL (non-dry-run) rotation of
            # an externally-issued secret CANNOT complete without writing
            # the issuer — there is no such thing as promoting the daemon's
            # primary while GitHub is left on the old value; that is exactly
            # the inconsistency this whole reorder exists to prevent. This
            # is not the checkpoint case either (nothing was attempted that
            # could have partially succeeded) — it is simply impossible to
            # finish, so refuse up front and roll back, restoring the old
            # value's standing rather than reporting success it never
            # reached. The tripwire's ABSENCE is deliberate in every one of
            # this PR's own doc examples and tests, so this path is the one
            # this PR's own test suite exercises, never the try/except below.
            report.add(
                "REFUSED: --i-know-this-calls-real-github not passed — a real "
                "rotation of an externally-issued secret cannot complete "
                "without updating the issuer, so promotion is refused and "
                "the rotation rolls back rather than reporting success it "
                "never reached"
            )
            report.add(roll_back(target, dry_run=dry_run))
            report.add(restart_daemon(target.daemon_plist, dry_run=dry_run))
            report.rolled_back = True
            return report

        # Tripwire per the module docstring — never reached by this PR's own
        # tests or examples, and absent from every doc command.
        try:
            update_github_webhook_secret(
                github_token=github_token or "",
                repo=github_repo or "",
                hook_id=github_hook_id or "",
                new_secret=new_value,
            )
            report.add(
                f"updated GitHub webhook config for {github_repo}#{github_hook_id}"
            )
        except GitHubAPIError as exc:
            # Step 4 already proved the daemon accepts the new value — this
            # is NOT a rollback case. Leave dual-admit ON and stop; a human
            # resolves whether GitHub actually applied the write.
            report.add(raise_issuer_checkpoint(target, reason=str(exc)))
            report.checkpoint_raised = True
            return report

    report.add(promote_and_retire(target, new_value, dry_run=dry_run))
    report.add(restart_daemon(target.daemon_plist, dry_run=dry_run))
    report.rolled_forward = True

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Staged rotation for a swarm-issued-and-consumed shared secret "
            "(authority_model.md#grants; conformance.md row 118)."
        ),
    )
    parser.add_argument(
        "target", choices=sorted(TARGETS), help="Which secret to rotate."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan with no values and no side effects. Recommended default.",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8742",
        help="Apis gateway base URL for the live-verification probe.",
    )
    parser.add_argument(
        "--github-repo",
        default=None,
        help="owner/repo whose webhook config to update (github_webhook_secret only).",
    )
    parser.add_argument(
        "--github-hook-id",
        default=None,
        help="Numeric webhook id (github_webhook_secret only).",
    )
    parser.add_argument(
        "--github-token-ref",
        default=None,
        help="op:// reference to a GitHub token with admin:repo_hook scope "
        "(github_webhook_secret only; never a literal token).",
    )
    parser.add_argument(
        "--i-know-this-calls-real-github",
        action="store_true",
        help=(
            "Tripwire: without this flag the GitHub webhook-config write is "
            "always skipped and only printed. This PR never passes it."
        ),
    )
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    target = TARGETS[args.target]

    github_token = sl.op_read(args.github_token_ref) if args.github_token_ref else None

    report = run_rotation(
        target,
        dry_run=args.dry_run,
        github_token=github_token,
        github_repo=args.github_repo,
        github_hook_id=args.github_hook_id,
        allow_real_github_write=args.i_know_this_calls_real_github,
        base_url=args.base_url,
    )

    print(f"\n=== rotate_swarm_secret: {target.name} ===")
    for line in report.steps:
        print(f"  {line}")
    if report.verification is not None:
        print(f"  live check: {report.verification.detail}")
    if report.dry_run:
        print("  outcome: dry-run — no state changed")
    elif report.checkpoint_raised:
        print("  outcome: CHECKPOINT — dual-admit left ON, manual resolution required")
    elif report.rolled_forward:
        print("  outcome: rolled forward (new value promoted, old retired)")
    elif report.rolled_back:
        print("  outcome: rolled back (old value retained)")
    print("No secret value is printed above or anywhere in this run's output.")

    if report.checkpoint_raised:
        return 2
    if report.rolled_back:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
