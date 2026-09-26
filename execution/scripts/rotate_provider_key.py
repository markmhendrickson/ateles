#!/usr/bin/env python3
"""Rotate a provider API key: openai | elevenlabs | anthropic.

Operator ruling (master plan ent_81aadb43caf2fa493361e8ed, decision
`credential_rotation_split_by_issuer`): the operator mints provider API keys —
everything downstream belongs to the swarm. This script is the operator-run
half: it talks to the provider and to 1Password, verifies the new key with a
harmless read-only call, and then optionally hands off to the swarm's own
secrets pipeline (publish → materialize → daemon restart list) so the new
value actually reaches the running system.

SECRET-HANDLING CONTRACT (read before changing this file):
  - No secret value (admin key, new key, pasted key) may appear on argv, in an
    environment variable set on this process's own command line, in stdout,
    in stderr, in an exception message, or in a log line. Admin keys and the
    interactively-pasted Anthropic key are read via `--admin-key-ref op://...`
    (a 1Password REFERENCE, resolved in-process) or `getpass`, never as a
    plain CLI value.
  - The new key is written to 1Password via `op item edit`, fed through STDIN
    (a piped JSON template), never as an `op` command-line assignment
    (1Password's own CLI help says command arguments are visible to other
    processes on this machine).
  - Anything staged to disk (the JSON template) goes to a mode-600 temp file
    under a `finally` block that unlinks it even on error.
  - The provider's create/update HTTP calls use the `requests` library where
    available and fall back to `urllib.request` — this repo's other scripts
    are stdlib-only, but there is no stdlib HTTP client that makes secret-free
    logging as easy to audit, so we accept the `requests` dependency here and
    document it in the docstring rather than hide it.
  - Every error message built from a provider HTTP response body or an `op`
    subprocess's stderr goes through `_safe_excerpt`, which truncates AND
    redacts any secret-shaped substring (`sk-...`, `xi-...`, or any other
    long opaque token) before the message is raised, printed, or returned.
    This is defense-in-depth on top of the (well-founded, but unverifiable
    without live accounts) assumption that providers and `op` never echo a
    request's own secret value back in an error body.

VERIFY-THEN-ACT ORDER (per provider), matching the operator's spec:
  1. Create/rotate the key at the provider (openai, elevenlabs) or accept the
     pasted key (anthropic).
  2. Write the new value to 1Password (op item edit, template via stdin).
  3. Verify the NEW key with a harmless read-only provider call.
  4. Only on success, and only if --revoke-old was passed, revoke/archive the
     OLD key. Otherwise print the exact follow-up command and stop.
  5. Run the swarm's downstream steps (secrets_publish.py, secrets_materialize.py,
     list the daemons/plists that read the rotated env var) — unless
     --no-downstream is passed.
  6. Print a status summary naming which steps ran and the new key's id/hint.
     Never the value.

Usage:
    python execution/scripts/rotate_provider_key.py openai \\
        --admin-key-ref op://Private/<item>/<field> \\
        --project-id proj_abc123 \\
        --op-item-ref op://Private/<item>/<field> \\
        [--service-account-name "ateles-rotation-2026-09-26"] \\
        [--revoke-old <old_service_account_id>] [--no-downstream]

    python execution/scripts/rotate_provider_key.py elevenlabs \\
        --admin-key-ref op://Private/<item>/<field> \\
        --service-account-user-id user_abc123 \\
        --op-item-ref op://Private/<item>/<field> \\
        [--key-name "ateles-rotation-2026-09-26"] \\
        [--permission speech_to_text] [--character-limit 100000] \\
        [--revoke-old <key_id>] [--no-downstream]
        # --permission is repeatable: pass it more than once for more than
        # one permission.

    python execution/scripts/rotate_provider_key.py anthropic \\
        --op-item-ref op://Private/<item>/<field> \\
        [--archive-old <api_key_id> --admin-key-ref op://Private/<item>/<field>] \\
        [--no-downstream]
    # Anthropic has no create endpoint (per docs.anthropic.com/en/api/admin-api
    # /apikeys as of 2026-09): the new key is pasted interactively via getpass.

Never call a real provider API or 1Password during development or from tests —
this module is exercised entirely with mocked HTTP and a mocked `op` (see
test_rotate_provider_key.py). This script is written to be run by the
operator himself, never invoked unattended by the swarm.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secrets_lib as sl  # noqa: E402

try:
    import requests  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover — exercised only if requests is absent
    requests = None  # type: ignore[assignment]

import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# HTTP helpers — never log request/response bodies at the caller; return
# structured data only. Secrets appear only in headers set here, never printed.
# ---------------------------------------------------------------------------


class ProviderHTTPError(RuntimeError):
    """Raised on a non-2xx provider response. Message never includes the key."""


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """POST/GET JSON, returning the decoded response body.

    Raises ProviderHTTPError on non-2xx. The error message includes the
    status code and URL PATH ONLY (never headers, which may carry the admin
    key) and a truncated, best-effort-redacted body excerpt.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if requests is not None:
        resp = requests.request(
            method, url, headers=headers, data=data, timeout=timeout
        )
        if not (200 <= resp.status_code < 300):
            raise ProviderHTTPError(
                f"{method} {_path_only(url)} -> HTTP {resp.status_code}: "
                f"{_safe_excerpt(resp.text)}"
            )
        return resp.json() if resp.text else {}
    # urllib fallback
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        raise ProviderHTTPError(
            f"{method} {_path_only(url)} -> HTTP {exc.code}: {_safe_excerpt(raw)}"
        ) from None


def _path_only(url: str) -> str:
    """URL path without query string, for error messages (no secrets there)."""
    return url.split("?", 1)[0]


# ---------------------------------------------------------------------------
# Redaction — round 2 rewrite.
#
# Round-1 review (Falco) noted the residual assumption that providers/`op`
# never echo a request's own secret back in an error body. Round-2 review
# added a shape-based regex net on top of that assumption — but a second
# adversarial pass (Falco again) CONFIRMED two evasion classes (short
# non-`sk-`-prefixed secrets under the length floor; any secret containing
# `.`/`/`/`+`/`=`, including Anthropic's own documented partial-key-hint
# format) and one false-positive class (real OpenAI error-type fields and
# realistic 1Password item names swallowed purely by length/character-class
# match, at the cost of exactly the diagnostic detail an error message
# exists to convey).
#
# Fix, per the coordinator's design: redact EXACT KNOWN VALUES first. This
# script always holds the actual secret value in memory at the moment it
# could leak (the admin key just read from 1Password, the new key just
# minted or pasted) — there is no need to guess a shape when the literal
# value is sitting in a local variable. `_KnownSecrets` is a small registry
# that commands register a secret into the moment they obtain it; every
# redaction call scrubs the exact value AND every substring of it 8+
# characters long (so a partial echo — e.g. a provider's own truncated hint
# of the key it rejected — is still caught, without needing to guess at
# formats). Shape-based matching survives only as a narrow SECONDARY layer,
# for the has-no-known-secret-registered case (e.g. before any secret has
# been obtained) — and is deliberately narrowed so it no longer eats
# ordinary diagnostic words.
# ---------------------------------------------------------------------------


class _KnownSecrets:
    """Registry of exact secret values seen this run, for exact-value redaction.

    Not a shape guess: every value registered here was, at some point in
    this process, an actual credential this script read or received. Reset
    with `.clear()` between independent script invocations (tests do this
    per-test via the `known_secrets_registry` fixture) so no state leaks
    across runs.
    """

    def __init__(self) -> None:
        self._values: set[str] = set()

    def register(self, value: str | None) -> None:
        if value:
            self._values.add(value)

    def clear(self) -> None:
        self._values.clear()

    def redact(self, text: str) -> str:
        """Replace every registered secret, and every 8+ char substring of
        it, with `[redacted]`. Substrings are checked longest-first so a
        shorter substring of an already-redacted region never re-matches
        leftover fragments oddly (defensive; redaction is idempotent either
        way since `[redacted]` itself is never 8+ chars of the original).
        """
        substrings: set[str] = set()
        for secret in self._values:
            substrings.add(secret)
            for length in range(
                len(secret), 7, -1
            ):  # 8 is the coordinator's stated floor
                for start in range(0, len(secret) - length + 1):
                    substrings.add(secret[start : start + length])
        for substring in sorted(substrings, key=len, reverse=True):
            if substring and substring in text:
                text = text.replace(substring, "[redacted]")
        return text


_known_secrets = _KnownSecrets()


# Narrowed secondary layer: only provider key-prefix conventions actually
# documented for the three providers this file talks to (OpenAI: `sk-`;
# Anthropic: `sk-ant-`, a subset already covered by `sk-`). ElevenLabs has no
# publicly documented prefix, so it is NOT guessed here — the exact-value
# layer above is what protects an ElevenLabs key, since this script always
# holds the literal value. Character class now includes `.`, `/`, `+`, `=`
# (the gap Falco demonstrated with Anthropic's own dotted partial-key-hint
# format and a base64-shaped secret), and the length floor is raised to 24
# (was 20) specifically so it stops matching ordinary long diagnostic words
# like `invalid_request_error` (21 chars) while still catching realistic key
# lengths — this is a secondary net for the not-yet-registered case, not the
# primary defense, so a narrower net that preserves diagnostics is the right
# trade here.
_SECRET_SHAPED_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_./+=-]{16,}\b")


def _redact_secret_shaped_tokens(text: str) -> str:
    """Narrow secondary net: only a documented `sk-`-prefixed key shape.

    Kept deliberately narrow (see module comment above) so it no longer
    hides legitimate diagnostics — it does NOT catch an unprefixed or
    non-`sk-` secret; that is exact-value redaction's job, applied first in
    `_safe_excerpt`.
    """
    return _SECRET_SHAPED_TOKEN_RE.sub("[redacted]", text)


def _safe_excerpt(text: str, limit: int = 300) -> str:
    """Truncate and redact a provider/`op` error body for display.

    Order matters: exact-value redaction (every secret this run has
    actually seen, and its 8+ char substrings) runs FIRST, so a partial
    echo of a real secret is caught before the narrow shape-based net even
    looks at the text. Providers do not echo request secrets back in error
    bodies, and `op`'s own stderr names items/fields but never values (per
    its own CLI behavior and `secrets_lib.op_read`'s docstring) — this is
    defense-in-depth on top of, not a replacement for, that reasoning.
    """
    text = _known_secrets.redact(text.strip())
    text = _redact_secret_shaped_tokens(text)
    return text[:limit] + ("…" if len(text) > limit else "")


# ---------------------------------------------------------------------------
# 1Password: write the new secret value via a piped JSON template — never a
# command-line assignment (those are visible to other processes; op's own
# --help says so). Never read back a value we just wrote, either — the
# summary reports by NAME/HINT only.
# ---------------------------------------------------------------------------


def op_write_password_field(item_ref: str, field_label: str, value: str) -> None:
    """Set `field_label` on the 1Password item identified by `item_ref`.

    `item_ref` may be an item name, item ID, or an `op://vault/item/field`
    reference (only the item portion is used for lookup; op resolves the
    rest). Uses `op item get --format=json` to fetch the current item, edits
    the named field's value in memory, writes it to a mode-600 temp file, and
    pipes that file to `op item edit --template - <item>` via stdin — never
    as a CLI argument. The temp file is unlinked in a `finally` block even on
    error. The value itself never touches argv, stdout, or stderr.
    """
    item_id = _item_id_from_ref(item_ref)
    get_result = subprocess.run(
        [sl.op_path(), "item", "get", item_id, "--format", "json"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if get_result.returncode != 0:
        raise RuntimeError(
            f"op item get failed for item {item_id!r}: {_safe_excerpt(get_result.stderr)}"
        )
    item = json.loads(get_result.stdout)

    field_found = False
    for field in item.get("fields", []):
        if field.get("label") == field_label or field.get("id") == field_label:
            field["value"] = value
            field_found = True
            break
    if not field_found:
        raise RuntimeError(
            f"field {field_label!r} not found on item {item_id!r} — "
            "refusing to guess which field to overwrite"
        )

    tmp_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(suffix=".op-item.json")
        tmp_path = Path(tmp_name)
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(item, fh)
        with open(tmp_path, "rb") as template_fh:
            edit_result = subprocess.run(
                [sl.op_path(), "item", "edit", item_id, "--template", "-"],
                stdin=template_fh,
                capture_output=True,
                text=True,
                timeout=20,
            )
        if edit_result.returncode != 0:
            raise RuntimeError(
                f"op item edit failed for item {item_id!r}: {_safe_excerpt(edit_result.stderr)}"
            )
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


def _item_id_from_ref(ref: str) -> str:
    """Extract the item identifier from an op:// reference or bare item name/id."""
    if ref.startswith("op://"):
        parts = ref[len("op://") :].split("/")
        if len(parts) < 2:
            raise ValueError(f"malformed op:// reference: {ref!r}")
        return parts[1]  # vault/item/field -> item
    return ref


def _field_label_from_ref(ref: str, default: str = "credential") -> str:
    if ref.startswith("op://"):
        parts = ref[len("op://") :].split("/")
        if len(parts) >= 3:
            return parts[2]
    return default


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


def openai_create_service_account(
    admin_key: str, project_id: str, name: str
) -> dict[str, Any]:
    """POST /v1/organization/projects/{project_id}/service_accounts.

    Verified against the official OpenAI OpenAPI spec (ProjectServiceAccount
    CreateRequest/CreateResponse), 2026-09-26. Returns the full response;
    caller extracts `api_key.value` for the new key.
    """
    url = (
        f"https://api.openai.com/v1/organization/projects/{project_id}/service_accounts"
    )
    headers = {
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }
    return _http_json("POST", url, headers=headers, body={"name": name})


def openai_verify_key(api_key: str) -> None:
    """GET /v1/models — harmless, read-only. Raises on failure."""
    url = "https://api.openai.com/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    _http_json("GET", url, headers=headers)


def openai_delete_service_account(
    admin_key: str, project_id: str, service_account_id: str
) -> None:
    """DELETE /v1/organization/projects/{project_id}/service_accounts/{id}."""
    url = (
        f"https://api.openai.com/v1/organization/projects/{project_id}"
        f"/service_accounts/{service_account_id}"
    )
    headers = {"Authorization": f"Bearer {admin_key}"}
    _http_json("DELETE", url, headers=headers)


# ---------------------------------------------------------------------------
# ElevenLabs
# ---------------------------------------------------------------------------

DEFAULT_ELEVENLABS_PERMISSIONS = ["speech_to_text"]


def elevenlabs_create_api_key(
    admin_key: str,
    service_account_user_id: str,
    name: str,
    permissions: list[str],
    character_limit: int | None,
) -> dict[str, Any]:
    """POST /v1/service-accounts/{service_account_user_id}/api-keys.

    Verified against elevenlabs.io/docs/api-reference/service-accounts/api-keys
    /create, 2026-09-26. Response key is `xi-api-key` (not `key` or `value`) —
    confirmed from the live docs, not assumed.
    """
    url = f"https://api.elevenlabs.io/v1/service-accounts/{service_account_user_id}/api-keys"
    headers = {"xi-api-key": admin_key, "Content-Type": "application/json"}
    body: dict[str, Any] = {"name": name, "permissions": permissions}
    if character_limit is not None:
        body["character_limit"] = character_limit
    return _http_json("POST", url, headers=headers, body=body)


def elevenlabs_verify_key(api_key: str) -> None:
    """GET /v1/user — harmless, read-only. Raises on failure."""
    url = "https://api.elevenlabs.io/v1/user"
    headers = {"xi-api-key": api_key}
    _http_json("GET", url, headers=headers)


def elevenlabs_delete_api_key(
    admin_key: str, service_account_user_id: str, key_id: str
) -> None:
    """DELETE /v1/service-accounts/{service_account_user_id}/api-keys/{key_id}."""
    url = f"https://api.elevenlabs.io/v1/service-accounts/{service_account_user_id}/api-keys/{key_id}"
    headers = {"xi-api-key": admin_key}
    _http_json("DELETE", url, headers=headers)


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

ANTHROPIC_VERSION = "2023-06-01"


def anthropic_verify_key(api_key: str) -> None:
    """GET /v1/models — harmless, read-only. Raises on failure."""
    url = "https://api.anthropic.com/v1/models"
    headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
    _http_json("GET", url, headers=headers)


def anthropic_archive_key(admin_key: str, api_key_id: str) -> dict[str, Any]:
    """POST /v1/organizations/api_keys/{api_key_id} with status=archived.

    Verified against platform.claude.com/docs/en/api/beta/organization/api_keys
    /update, 2026-09-26 (docs.anthropic.com redirects there). Allowed status
    values: active, archived, inactive (this always sends "archived").
    """
    url = f"https://api.anthropic.com/v1/organizations/api_keys/{api_key_id}"
    headers = {
        "x-api-key": admin_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }
    return _http_json("POST", url, headers=headers, body={"status": "archived"})


# ---------------------------------------------------------------------------
# Downstream swarm steps (shared across providers)
# ---------------------------------------------------------------------------

# env_var -> which manifest file block carries it (per manifest.env-map.json).
ENV_VAR_TO_MANIFEST_FILE = {
    "ANTHROPIC_API_KEY": "neotoma",
    "OPENAI_API_KEY": "neotoma",
    "ELEVENLABS_API_KEY": "neotoma",
}

# Daemons/plists known to read each var, by NAME only (never inline secret
# values). Kept here as a documented starting point; the manifest and
# .claude/hooks are the living source — this list is advisory for the
# operator's restart step, not authoritative.
ENV_VAR_CONSUMERS = {
    "ANTHROPIC_API_KEY": [
        "com.ateles.apis (Apis daemon — claude --print review panel)",
        "com.ateles.phoenicurus-prepare (Phoenicurus prepare agent)",
        "openclaw agent core/watchdog (via ~/repos/openclaw/.env)",
    ],
    "OPENAI_API_KEY": [
        "openclaw agent core/watchdog (via ~/repos/openclaw/.env)",
        "Bottega8 hosted instance (Fly app bottega8-neotoma — separate materialize target)",
    ],
    "ELEVENLABS_API_KEY": [
        "execution/daemons/tyto/tyto.py (explicit diarization path)",
    ],
}


def run_downstream_steps(env_var: str, repo_root: Path) -> list[str]:
    """Publish the encrypted snapshot, materialize it, list known consumers.

    Returns the list of status lines printed (for the final summary). Never
    prints a secret value — secrets_publish.py and secrets_materialize.py
    already guarantee that on their own output.
    """
    lines: list[str] = []
    manifest_file = ENV_VAR_TO_MANIFEST_FILE.get(env_var)
    if manifest_file is None:
        lines.append(
            f"[downstream] no manifest file block known for {env_var} — skipped publish/materialize"
        )
        return lines

    publish_script = repo_root / "execution" / "scripts" / "secrets_publish.py"
    materialize_script = repo_root / "execution" / "scripts" / "secrets_materialize.py"

    publish_result = subprocess.run(
        [sys.executable, str(publish_script), manifest_file],
        capture_output=True,
        text=True,
        timeout=60,
    )
    lines.append(
        f"[downstream] secrets_publish.py {manifest_file}: "
        f"{'ok' if publish_result.returncode == 0 else 'FAILED'}"
    )
    if publish_result.returncode != 0:
        lines.append(f"[downstream]   stderr: {_safe_excerpt(publish_result.stderr)}")
        return lines

    materialize_result = subprocess.run(
        [sys.executable, str(materialize_script), manifest_file],
        capture_output=True,
        text=True,
        timeout=60,
    )
    lines.append(
        f"[downstream] secrets_materialize.py {manifest_file}: "
        f"{'ok' if materialize_result.returncode == 0 else 'FAILED'}"
    )
    if materialize_result.returncode != 0:
        lines.append(
            f"[downstream]   stderr: {_safe_excerpt(materialize_result.stderr)}"
        )
        return lines

    consumers = ENV_VAR_CONSUMERS.get(env_var, [])
    if consumers:
        lines.append(
            f"[downstream] daemons/plists that read {env_var} (restart these):"
        )
        for c in consumers:
            lines.append(f"[downstream]   - {c}")
    else:
        lines.append(
            f"[downstream] no known consumers listed for {env_var} — check manifest targets by hand"
        )
    return lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _resolve_admin_key(args: argparse.Namespace) -> str:
    if not args.admin_key_ref:
        raise SystemExit("--admin-key-ref is required for this operation")
    admin_key = sl.op_read(args.admin_key_ref)
    _known_secrets.register(admin_key)
    return admin_key


def cmd_openai(args: argparse.Namespace, repo_root: Path) -> int:
    admin_key = _resolve_admin_key(args)
    name = args.service_account_name or f"ateles-rotation-{_today()}"

    print(f"[openai] creating service account {name!r} in project {args.project_id}…")
    resp = openai_create_service_account(admin_key, args.project_id, name)
    api_key_block = resp.get("api_key") or {}
    new_key = api_key_block.get("value")
    _known_secrets.register(new_key)
    key_id = api_key_block.get("id")
    service_account_id = resp.get("id")
    if not new_key:
        print("[openai] FATAL: response did not include api_key.value", file=sys.stderr)
        return 1
    print(f"[openai] created service account id={service_account_id} key_id={key_id}")

    field_label = _field_label_from_ref(args.op_item_ref)
    print(
        f"[openai] writing new key to 1Password ({_item_id_from_ref(args.op_item_ref)} / {field_label})…"
    )
    op_write_password_field(args.op_item_ref, field_label, new_key)

    print("[openai] verifying new key with GET /v1/models…")
    try:
        openai_verify_key(new_key)
    except ProviderHTTPError as exc:
        print(f"[openai] FATAL: new key failed verification: {exc}", file=sys.stderr)
        return 1
    print("[openai] new key verified.")

    if args.revoke_old:
        print(f"[openai] revoking old service account {args.revoke_old}…")
        openai_delete_service_account(admin_key, args.project_id, args.revoke_old)
        print("[openai] old service account deleted.")
    else:
        print(
            "[openai] --revoke-old not passed. To revoke the OLD service account "
            f"once you've confirmed the new one is live, run:\n"
            f"  python {Path(__file__).name} openai --admin-key-ref <ref> "
            f"--project-id {args.project_id} --op-item-ref <ref> "
            f"--revoke-old <OLD_SERVICE_ACCOUNT_ID>"
        )

    downstream_lines: list[str] = []
    if not args.no_downstream:
        downstream_lines = run_downstream_steps("OPENAI_API_KEY", repo_root)
        for line in downstream_lines:
            print(line)

    _print_summary(
        provider="openai",
        key_hint=f"key_id={key_id} service_account_id={service_account_id}",
        old_revoked=bool(args.revoke_old),
        downstream_ran=not args.no_downstream,
    )
    return 0


def cmd_elevenlabs(args: argparse.Namespace, repo_root: Path) -> int:
    admin_key = _resolve_admin_key(args)
    name = args.key_name or f"ateles-rotation-{_today()}"
    permissions = args.permission or list(DEFAULT_ELEVENLABS_PERMISSIONS)

    print(
        f"[elevenlabs] creating API key {name!r} for service account {args.service_account_user_id} "
        f"(permissions={permissions})…"
    )
    resp = elevenlabs_create_api_key(
        admin_key, args.service_account_user_id, name, permissions, args.character_limit
    )
    new_key = resp.get("xi-api-key")
    _known_secrets.register(new_key)
    key_id = resp.get("key_id")
    if not new_key:
        print(
            "[elevenlabs] FATAL: response did not include xi-api-key", file=sys.stderr
        )
        return 1
    print(f"[elevenlabs] created key_id={key_id}")

    field_label = _field_label_from_ref(args.op_item_ref)
    print(
        f"[elevenlabs] writing new key to 1Password ({_item_id_from_ref(args.op_item_ref)} / {field_label})…"
    )
    op_write_password_field(args.op_item_ref, field_label, new_key)

    print("[elevenlabs] verifying new key with GET /v1/user…")
    try:
        elevenlabs_verify_key(new_key)
    except ProviderHTTPError as exc:
        print(
            f"[elevenlabs] FATAL: new key failed verification: {exc}", file=sys.stderr
        )
        return 1
    print("[elevenlabs] new key verified.")

    if args.revoke_old:
        print(f"[elevenlabs] deleting old key {args.revoke_old}…")
        elevenlabs_delete_api_key(
            admin_key, args.service_account_user_id, args.revoke_old
        )
        print("[elevenlabs] old key deleted.")
    else:
        print(
            "[elevenlabs] --revoke-old not passed. To delete the OLD key once "
            "you've confirmed the new one is live, run:\n"
            f"  python {Path(__file__).name} elevenlabs --admin-key-ref <ref> "
            f"--service-account-user-id {args.service_account_user_id} --op-item-ref <ref> "
            f"--revoke-old <OLD_KEY_ID>"
        )

    downstream_lines: list[str] = []
    if not args.no_downstream:
        downstream_lines = run_downstream_steps("ELEVENLABS_API_KEY", repo_root)
        for line in downstream_lines:
            print(line)

    _print_summary(
        provider="elevenlabs",
        key_hint=f"key_id={key_id}",
        old_revoked=bool(args.revoke_old),
        downstream_ran=not args.no_downstream,
    )
    return 0


def cmd_anthropic(args: argparse.Namespace, repo_root: Path) -> int:
    if args.archive_old:
        if not args.admin_key_ref:
            print(
                "[anthropic] --admin-key-ref is required with --archive-old",
                file=sys.stderr,
            )
            return 1
        admin_key = sl.op_read(args.admin_key_ref)
        _known_secrets.register(admin_key)
        print(f"[anthropic] archiving old key {args.archive_old}…")
        anthropic_archive_key(admin_key, args.archive_old)
        print("[anthropic] old key archived.")
        _print_summary(
            provider="anthropic",
            key_hint=f"archived api_key_id={args.archive_old}",
            old_revoked=True,
            downstream_ran=False,
        )
        return 0

    print(
        "[anthropic] No create endpoint exists in the Anthropic Admin API docs "
        "(platform.claude.com/docs/en/api/admin-api/apikeys, checked 2026-09-26). "
        "Mint the new key yourself in the Anthropic console, then paste it below."
    )
    new_key = getpass.getpass(
        "Paste the new ANTHROPIC key (input hidden, not echoed): "
    ).strip()
    _known_secrets.register(new_key)
    if not new_key:
        print("[anthropic] FATAL: no key entered", file=sys.stderr)
        return 1

    field_label = _field_label_from_ref(args.op_item_ref)
    print(
        f"[anthropic] writing new key to 1Password ({_item_id_from_ref(args.op_item_ref)} / {field_label})…"
    )
    op_write_password_field(args.op_item_ref, field_label, new_key)

    print("[anthropic] verifying new key with GET /v1/models…")
    try:
        anthropic_verify_key(new_key)
    except ProviderHTTPError as exc:
        print(f"[anthropic] FATAL: new key failed verification: {exc}", file=sys.stderr)
        return 1
    print("[anthropic] new key verified.")

    print(
        "[anthropic] No --archive-old was given. Anthropic has no delete endpoint either — "
        "the only lifecycle action is archive/deactivate. Once you have the OLD key's "
        "api_key_id (visible in the Anthropic console), run:\n"
        f"  python {Path(__file__).name} anthropic --archive-old <OLD_API_KEY_ID> "
        "--admin-key-ref <ref> --op-item-ref <ref>"
    )

    downstream_lines: list[str] = []
    if not args.no_downstream:
        downstream_lines = run_downstream_steps("ANTHROPIC_API_KEY", repo_root)
        for line in downstream_lines:
            print(line)

    _print_summary(
        provider="anthropic",
        key_hint="(pasted key; no id available without an admin lookup)",
        old_revoked=False,
        downstream_ran=not args.no_downstream,
    )
    return 0


def _today() -> str:
    import datetime

    return datetime.date.today().isoformat()


def _print_summary(
    *, provider: str, key_hint: str, old_revoked: bool, downstream_ran: bool
) -> None:
    print("\n=== rotate_provider_key summary ===")
    print(f"provider: {provider}")
    print(f"new key: {key_hint}")
    print(f"old key revoked: {old_revoked}")
    print(f"downstream (publish/materialize/consumer list) ran: {downstream_ran}")
    print("No secret value is printed above or anywhere in this run's output.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rotate a provider API key (openai, elevenlabs, anthropic). Operator-run only.",
    )
    sub = parser.add_subparsers(dest="provider", required=True)

    common_downstream = argparse.ArgumentParser(add_help=False)
    common_downstream.add_argument(
        "--no-downstream",
        action="store_true",
        help="Skip secrets_publish.py / secrets_materialize.py / consumer listing.",
    )

    p_openai = sub.add_parser(
        "openai",
        parents=[common_downstream],
        help="Create a new OpenAI project service account + key.",
    )
    p_openai.add_argument(
        "--admin-key-ref",
        required=True,
        help="op:// reference to an OpenAI admin key. Never a literal value.",
    )
    p_openai.add_argument("--project-id", required=True)
    p_openai.add_argument(
        "--op-item-ref",
        required=True,
        help="op:// reference (or item name/id) for where the new key is written.",
    )
    p_openai.add_argument("--service-account-name", default=None)
    p_openai.add_argument(
        "--revoke-old",
        default=None,
        metavar="SERVICE_ACCOUNT_ID",
        help="Old service account id to delete after the new key verifies.",
    )
    p_openai.set_defaults(func=cmd_openai)

    p_eleven = sub.add_parser(
        "elevenlabs",
        parents=[common_downstream],
        help="Create a new ElevenLabs service-account API key.",
    )
    p_eleven.add_argument(
        "--admin-key-ref",
        required=True,
        help="op:// reference to an ElevenLabs admin key. Never a literal value.",
    )
    p_eleven.add_argument("--service-account-user-id", required=True)
    p_eleven.add_argument("--op-item-ref", required=True)
    p_eleven.add_argument("--key-name", default=None)
    p_eleven.add_argument(
        "--permission",
        action="append",
        default=None,
        help="Repeatable. Defaults to ['speech_to_text'] if omitted.",
    )
    p_eleven.add_argument("--character-limit", type=int, default=None)
    p_eleven.add_argument(
        "--revoke-old",
        default=None,
        metavar="KEY_ID",
        help="Old key id to delete after the new key verifies.",
    )
    p_eleven.set_defaults(func=cmd_elevenlabs)

    p_anthropic = sub.add_parser(
        "anthropic",
        parents=[common_downstream],
        help="Accept a pasted Anthropic key; optionally archive an old one.",
    )
    p_anthropic.add_argument("--op-item-ref", required=True)
    p_anthropic.add_argument(
        "--admin-key-ref", default=None, help="Required only with --archive-old."
    )
    p_anthropic.add_argument(
        "--archive-old",
        default=None,
        metavar="API_KEY_ID",
        help="If given, archive this key via the Admin API and do nothing else.",
    )
    p_anthropic.set_defaults(func=cmd_anthropic)

    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent.parent
    return args.func(args, repo_root)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
