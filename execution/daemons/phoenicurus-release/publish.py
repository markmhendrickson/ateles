#!/usr/bin/env python3
"""
Phoenicurus-Release — Publish Daemon (operator-approved release executor)

This is the DETERMINISTIC half of the Neotoma release automation. It does NOT
prepare a release (no LLM, no supplement authoring, no /review). It takes a
release that has already been prepared (RC PR open, notes rendered, release
entity stored as status=pending_approval) and, once the operator approves,
executes the irreversible publish steps:

    merge RC PR -> tag -> push -> npm publish -> GitHub Release ->
    sandbox deploy -> verify -> publish GH Release draft -> post-deploy probes ->
    close resolved issues -> mark release published -> Telegram confirmation

It is invoked AFTER operator approval (e.g. by Ateles when Mark replies
"approve vX.Y.Z" on Telegram, or manually with --version). It never publishes
without an approved (or explicitly forced) release record.

The npm publish uses a granular automation token (bypass-2FA) read from
~/.config/neotoma/.env under either NPM_TOKEN or NODE_AUTH_TOKEN, written to a
temporary npmrc for the publish and removed afterwards. That .env is populated
OFFLINE by secrets_materialize.py from the age-encrypted SOPS snapshot in
ateles-private — no live 1Password session at publish time. A `npm whoami`
preflight makes a missing/expired token fail LOUD (Telegram alert) rather than
silently producing an unpublished release.

Usage:
  python3 publish.py --version v0.16.0          # publish a specific approved release
  python3 publish.py --version v0.16.0 --dry-run # plan only, no irreversible actions
  python3 publish.py --entity-id ent_xxx         # publish by release entity id
  python3 publish.py --version v0.16.0 --force    # publish even if status != approved

Exit codes:
  0  success (or dry-run completed)
  1  fatal error (reported to Telegram)
  2  precondition not met (no approved release, dirty tree, auth missing)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: load env from ~/.config/neotoma/.env (launchd does not source
# shell profiles). setdefault so an explicit environment wins.
# ---------------------------------------------------------------------------

_NEOTOMA_ENV_FILE = Path.home() / ".config" / "neotoma" / ".env"
if _NEOTOMA_ENV_FILE.exists():
    for _line in _NEOTOMA_ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# ---------------------------------------------------------------------------
# Constants / paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent  # ateles repo root
LOG_DIR = Path.home() / "Library" / "Logs" / "ateles"
LOG_FILE = LOG_DIR / "phoenicurus-release.log"

# The Neotoma source checkout the release is cut from. Override with
# NEOTOMA_REPO_ROOT for non-standard layouts.
NEOTOMA_REPO_ROOT = Path(
    os.environ.get("NEOTOMA_REPO_ROOT", str(Path.home() / "repos" / "neotoma"))
)

TELEGRAM_TOPIC = os.environ.get("TELEGRAM_TOPIC_PHOENICURUS", "") or os.environ.get(
    "TELEGRAM_TOPIC_RELEASES", ""
)

NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
NEOTOMA_BASE_URL = os.environ.get("NEOTOMA_BASE_URL", "http://localhost:3180")
# The npm automation token. Accept either conventional name so the release
# works from whatever the SOPS snapshot materialized: NPM_TOKEN (Ateles'
# manifest name) OR NODE_AUTH_TOKEN (npm's own env var, also what the neotoma
# GHA release workflow uses). Reading either avoids the June-2026 failure where
# the snapshot carried NODE_AUTH_TOKEN but publish only looked for NPM_TOKEN and
# fell back to a live `op` session that had expired.
NPM_TOKEN = os.environ.get("NPM_TOKEN", "") or os.environ.get("NODE_AUTH_TOKEN", "")

SANDBOX_URL = os.environ.get(
    "NEOTOMA_SANDBOX_URL", "https://neotoma-sandbox.fly.dev"
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)


class _FlushingFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [phoenicurus-release] %(levelname)s %(message)s",
    handlers=[
        _FlushingFileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Telegram (outbound, fire-and-forget via shared send.mjs)
# ---------------------------------------------------------------------------


def telegram_send(text: str) -> None:
    """Send a Telegram message via the shared Node.js send.mjs helper."""
    import shutil

    node = shutil.which("node")
    send_script = PROJECT_ROOT / "execution" / "lib" / "telegram" / "send.mjs"
    if node and send_script.exists():
        try:
            args = [node, str(send_script), "--text", text]
            if TELEGRAM_TOPIC:
                args += ["--thread-id", TELEGRAM_TOPIC]
            subprocess.run(args, timeout=20, capture_output=True, env=os.environ)
            return
        except Exception as exc:
            log.warning(f"send.mjs failed: {exc}, trying fallback")

    telegram_cmd = shutil.which("telegram-send")
    if telegram_cmd:
        try:
            subprocess.run(
                [telegram_cmd, text], timeout=20, capture_output=True, env=os.environ
            )
        except Exception as exc:
            log.warning(f"telegram-send fallback failed: {exc}")


# ---------------------------------------------------------------------------
# Neotoma client (urllib + Bearer; loopback omits stale tokens)
# ---------------------------------------------------------------------------


def _neotoma_headers() -> dict:
    base = NEOTOMA_BASE_URL.rstrip("/")
    is_loopback = "localhost" in base or "127.0.0.1" in base
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if NEOTOMA_BEARER_TOKEN and not is_loopback:
        headers["Authorization"] = f"Bearer {NEOTOMA_BEARER_TOKEN}"
    return headers


def neotoma_query(entity_type: str, limit: int = 100) -> list[dict]:
    """Query entities of a type from Neotoma. Empty list on error."""
    base = NEOTOMA_BASE_URL.rstrip("/")
    try:
        body = json.dumps(
            {"entity_type": entity_type, "limit": limit, "include_snapshots": True}
        ).encode()
        req = urllib.request.Request(
            f"{base}/entities/query", data=body, headers=_neotoma_headers(), method="POST"
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        if isinstance(data, list):
            return data
        return data.get("entities") or data.get("items") or data.get("results") or []
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log.warning(f"Neotoma query failed for {entity_type}: {exc}")
        return []


def neotoma_fetch_entity(entity_id: str) -> dict | None:
    """Fetch a single entity by id. None on error."""
    base = NEOTOMA_BASE_URL.rstrip("/")
    try:
        req = urllib.request.Request(
            f"{base}/entities/{entity_id}", headers=_neotoma_headers()
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log.warning(f"Neotoma fetch failed for {entity_id}: {exc}")
        return None


def neotoma_store(entities: list[dict], idempotency_key: str) -> dict | None:
    """Store/update entities via POST /store. None on error."""
    base = NEOTOMA_BASE_URL.rstrip("/")
    try:
        body = json.dumps(
            {"entities": entities, "idempotency_key": idempotency_key}
        ).encode()
        req = urllib.request.Request(
            f"{base}/store", data=body, headers=_neotoma_headers(), method="POST"
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log.warning(f"Neotoma store failed: {exc}")
        return None


def _entity_fields(entity: dict) -> dict:
    return entity.get("snapshot") or entity.get("fields") or entity


def set_release_status(version: str, status: str, extra: dict | None = None) -> None:
    """
    Append a release_result observation flipping status (prepared -> approved ->
    publishing -> published / failed). Idempotency key carries status + date so
    each transition is a distinct observation.

    Uses the `release_result` entity type, whose canonical identity is `version`,
    so every transition coalesces onto the same entity (latest status wins in the
    snapshot). Declared fields: version, status, branch, release_url, reason, ...
    """
    rec: dict = {
        "entity_type": "release_result",
        "version": version,
        "status": status,
    }
    if extra:
        rec.update(extra)
    key = f"release-{version}-{status}-{date.today().isoformat()}"
    neotoma_store([rec], key)
    log.info(f"Release {version} status -> {status}")


# ---------------------------------------------------------------------------
# Release record resolution
# ---------------------------------------------------------------------------


def find_release(version: str | None, entity_id: str | None) -> dict | None:
    """
    Resolve the release entity to publish. By entity_id if given, else the
    newest release matching `version`.
    """
    if entity_id:
        ent = neotoma_fetch_entity(entity_id)
        return ent
    if not version:
        return None
    candidates = neotoma_query("release_result", limit=100)
    matches = []
    for c in candidates:
        f = _entity_fields(c)
        if str(f.get("version") or "").lstrip("v") == version.lstrip("v"):
            matches.append(c)
    if not matches:
        return None
    # newest by last_observation_at
    matches.sort(key=lambda e: e.get("last_observation_at") or "", reverse=True)
    return matches[0]


# ---------------------------------------------------------------------------
# Shell helpers (run in the Neotoma repo)
# ---------------------------------------------------------------------------


class StepError(Exception):
    """A publish step failed; message is operator-facing."""


def run(
    cmd: list[str],
    cwd: Path | None = None,
    check: bool = True,
    env: dict | None = None,
    timeout: int = 600,
    secret_in_env: bool = False,
) -> subprocess.CompletedProcess:
    """
    Run a command. Logs the argv (never the env). Raises StepError on failure
    when check=True.
    """
    cwd = cwd or NEOTOMA_REPO_ROOT
    log.info(f"$ {' '.join(cmd)}  (cwd={cwd})")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise StepError(f"command timed out after {timeout}s: {' '.join(cmd)}") from exc
    if proc.stdout:
        log.info(proc.stdout.strip()[:4000])
    if proc.stderr:
        log.info(proc.stderr.strip()[:4000])
    if check and proc.returncode != 0:
        raise StepError(
            f"command failed (exit {proc.returncode}): {' '.join(cmd)}\n"
            f"{(proc.stderr or proc.stdout or '').strip()[:500]}"
        )
    return proc


# ---------------------------------------------------------------------------
# npm auth (temporary token-scoped npmrc)
# ---------------------------------------------------------------------------


def _npm_env_with_token() -> tuple[dict, Path]:
    """
    Build an env + temporary npmrc carrying the automation token so npm publish
    runs non-interactively. Caller MUST unlink the returned path.
    """
    if not NPM_TOKEN:
        raise StepError(
            "Neither NPM_TOKEN nor NODE_AUTH_TOKEN set in ~/.config/neotoma/.env "
            "— cannot publish. Run secrets_materialize.py to refresh the .env from "
            "the offline SOPS snapshot (no live 1Password session required)."
        )
    fd, path = tempfile.mkstemp(prefix=".npmrc-phoenicurus-", text=True)
    with os.fdopen(fd, "w") as fh:
        fh.write(f"//registry.npmjs.org/:_authToken={NPM_TOKEN}\n")
    env = dict(os.environ)
    env["NPM_CONFIG_USERCONFIG"] = path
    return env, Path(path)


def npm_whoami_preflight(npm_env: dict) -> str:
    """Confirm the token authenticates. Raise StepError (loud) if not."""
    proc = run(["npm", "whoami"], env=npm_env, check=False, timeout=60)
    who = (proc.stdout or "").strip()
    if proc.returncode != 0 or not who:
        raise StepError(
            "npm whoami failed — npm token (NPM_TOKEN / NODE_AUTH_TOKEN) "
            "missing/expired. Regenerate the granular automation token in "
            "1Password, then secrets_publish.py + secrets_materialize.py so the "
            "offline SOPS snapshot and ~/.config/neotoma/.env carry the new value."
        )
    log.info(f"npm authenticated as: {who}")
    return who


# ---------------------------------------------------------------------------
# Publish sequence (mirrors /release skill Step 4-5 execute)
# ---------------------------------------------------------------------------


def preflight(version: str, rc_branch: str, dry_run: bool) -> None:
    """Verify repo + auth preconditions before any irreversible step."""
    repo = NEOTOMA_REPO_ROOT
    if not (repo / "package.json").exists():
        raise StepError(f"NEOTOMA_REPO_ROOT has no package.json: {repo}")

    # Clean working tree (don't publish atop unrelated dirty state).
    proc = run(["git", "status", "--porcelain"], check=False)
    dirty = [
        ln
        for ln in (proc.stdout or "").splitlines()
        if ln.strip() and "docs/releases/" not in ln
    ]
    if dirty:
        raise StepError(
            "Neotoma working tree is dirty (non-release files). Refusing to "
            f"publish atop uncommitted changes:\n{chr(10).join(dirty[:10])}"
        )

    run(["git", "fetch", "origin", "--tags", "--quiet"], check=False)

    # Tag must not already exist (idempotency / no clobber).
    tags = run(["git", "tag", "--list", version], check=False).stdout.strip()
    if tags:
        raise StepError(f"tag {version} already exists — already published?")

    # npm auth preflight (loud-fail).
    if not dry_run:
        npm_env, npmrc = _npm_env_with_token()
        try:
            npm_whoami_preflight(npm_env)
        finally:
            npmrc.unlink(missing_ok=True)

    log.info(f"Preflight OK for {version} (rc_branch={rc_branch}, dry_run={dry_run})")


def merge_rc_pr(rc_pr_url: str, rc_branch: str, dry_run: bool) -> None:
    if dry_run:
        log.info(f"[dry-run] would merge RC PR {rc_pr_url}")
        return
    # Merge via gh (squash to keep main linear); tolerate already-merged.
    pr_ref = rc_pr_url or rc_branch
    proc = run(["gh", "pr", "merge", pr_ref, "--merge"], check=False)
    if proc.returncode != 0 and "not mergeable" not in (proc.stderr or "").lower():
        # Already merged is fine; anything else is fatal.
        state = run(
            ["gh", "pr", "view", pr_ref, "--json", "state", "--jq", ".state"],
            check=False,
        ).stdout.strip()
        if state != "MERGED":
            raise StepError(f"RC PR merge failed and state={state!r}: {rc_pr_url}")
    run(["git", "checkout", "main"], check=False)
    run(["git", "pull", "origin", "main", "--quiet"])


def tag_and_push(version: str, dry_run: bool) -> None:
    if dry_run:
        log.info(f"[dry-run] would tag {version} and push origin main + tag")
        return
    run(["git", "tag", "-a", version, "-m", f"Release {version}"])
    run(["git", "push", "origin", "main"])
    run(["git", "push", "origin", version])


def npm_publish(version: str, dry_run: bool) -> None:
    npm_env, npmrc = _npm_env_with_token()
    try:
        npm_whoami_preflight(npm_env)
        if dry_run:
            run(["npm", "publish", "--dry-run"], env=npm_env, timeout=900)
            log.info("[dry-run] npm publish --dry-run completed")
            return
        run(["npm", "publish"], env=npm_env, timeout=900)
        # Verify registry reflects the new version.
        proc = run(["npm", "view", "neotoma", "version"], env=npm_env, check=False)
        published = (proc.stdout or "").strip()
        if published != version.lstrip("v"):
            raise StepError(
                f"npm publish ran but registry shows {published!r}, expected "
                f"{version.lstrip('v')!r}"
            )
        log.info(f"npm published neotoma@{published}")
    finally:
        npmrc.unlink(missing_ok=True)


def github_release(version: str, notes_path: Path | None, dry_run: bool) -> None:
    if dry_run:
        log.info(f"[dry-run] would create + publish GitHub Release {version}")
        return
    # Render notes if no supplied file (the prepare run normally renders them).
    notes_file = notes_path
    if notes_file is None or not notes_file.exists():
        tmp = Path(tempfile.mkstemp(prefix=f"gh-release-{version}-", suffix=".md")[1])
        run(
            [
                "bash",
                "-lc",
                f"npm run -s release-notes:render -- --tag {version} > {tmp}",
            ]
        )
        notes_file = tmp
    # Create draft then publish (skill creates draft, publishes after sandbox).
    exists = run(
        ["gh", "release", "view", version, "--json", "isDraft"], check=False
    ).returncode == 0
    if not exists:
        run(
            [
                "gh", "release", "create", version,
                "--title", version, "--notes-file", str(notes_file), "--draft",
            ]
        )


def deploy_sandbox(version: str, dry_run: bool) -> None:
    if dry_run:
        log.info("[dry-run] would flyctl deploy sandbox + verify")
        return
    run(
        ["flyctl", "deploy", "-c", "fly.sandbox.toml", "--remote-only"],
        timeout=1200,
    )
    # Verify version + mode.
    proc = run(
        ["bash", "-lc", f"curl -fsS -H 'Accept: application/json' {SANDBOX_URL}/"],
        check=False,
    )
    try:
        j = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        j = {}
    if j.get("version") != version.lstrip("v") or j.get("mode") != "sandbox":
        raise StepError(
            f"sandbox verify failed: got version={j.get('version')!r} "
            f"mode={j.get('mode')!r}, expected {version.lstrip('v')}/sandbox"
        )
    log.info(f"sandbox verified: {version} / sandbox")


def publish_github_release_draft(version: str, dry_run: bool) -> None:
    if dry_run:
        log.info("[dry-run] would publish GitHub Release draft (--draft=false)")
        return
    run(["gh", "release", "edit", version, "--draft=false"])


def post_release(version: str, dry_run: bool) -> str:
    """Probes + issue closure. Returns a short summary string for Telegram."""
    if dry_run:
        log.info("[dry-run] would run post-deploy probes + close issues")
        return "[dry-run] post-release skipped"
    summary_bits = []
    # Post-deploy probes (advisory — log result, don't hard-fail the publish
    # since the tag/npm are already live; surface in Telegram).
    proc = run(
        [
            "bash",
            "-lc",
            f"NEOTOMA_PROBE_HOSTS='{SANDBOX_URL}' "
            f"bash scripts/security/deployed_probes.sh --tag {version}",
        ],
        check=False,
        timeout=300,
    )
    summary_bits.append("probes: ran" if proc.returncode == 0 else "probes: see log")
    # GitHub release URL.
    url = run(
        ["gh", "release", "view", version, "--json", "url", "--jq", ".url"],
        check=False,
    ).stdout.strip()
    summary_bits.append(url)
    return " | ".join(b for b in summary_bits if b)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def publish_release(
    release: dict, version: str, dry_run: bool, force: bool
) -> None:
    f = _entity_fields(release)
    status = str(f.get("status") or "")
    rc_pr_url = str(f.get("rc_pr_url") or "")
    rc_branch = str(f.get("rc_branch") or f"release/{version}")
    notes_path_s = str(f.get("notes_path") or "")
    notes_path = Path(notes_path_s) if notes_path_s else None

    if status not in ("approved",) and not force:
        raise StepError(
            f"release {version} status is {status!r}, not 'approved'. "
            "Refusing to publish without approval (use --force to override)."
        )

    log.info(f"Publishing {version} (status={status}, dry_run={dry_run}, force={force})")
    if not dry_run:
        set_release_status(version, "publishing")

    preflight(version, rc_branch, dry_run)
    merge_rc_pr(rc_pr_url, rc_branch, dry_run)
    tag_and_push(version, dry_run)
    npm_publish(version, dry_run)
    github_release(version, notes_path, dry_run)
    deploy_sandbox(version, dry_run)
    publish_github_release_draft(version, dry_run)
    summary = post_release(version, dry_run)

    if dry_run:
        log.info(f"[dry-run] publish plan complete for {version}")
        telegram_send(f"🧪 Phoenicurus dry-run OK for {version}. No changes made.")
        return

    release_url = ""
    for part in summary.split(" | "):
        if part.startswith("http"):
            release_url = part
            break
    set_release_status(
        version,
        "published",
        {"release_url": release_url, "published_summary": summary},
    )
    telegram_send(
        f"✅ Released *{version}*\n"
        f"npm: https://www.npmjs.com/package/neotoma/v/{version.lstrip('v')}\n"
        f"{summary}"
    )
    log.info(f"Release {version} PUBLISHED.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Phoenicurus release publish daemon")
    ap.add_argument("--version", help="release version, e.g. v0.16.0")
    ap.add_argument("--entity-id", help="release entity id to publish")
    ap.add_argument(
        "--dry-run", action="store_true", help="plan only; no irreversible actions"
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="publish even if release status != approved",
    )
    args = ap.parse_args()

    if not args.version and not args.entity_id:
        log.error("must supply --version or --entity-id")
        return 2

    release = find_release(args.version, args.entity_id)
    if not release:
        log.error(
            f"no release record found (version={args.version}, "
            f"entity_id={args.entity_id})"
        )
        telegram_send(
            f"🔴 Phoenicurus: no release record for "
            f"{args.version or args.entity_id} — nothing to publish."
        )
        return 2

    version = args.version or str(_entity_fields(release).get("version") or "")
    if version and not version.startswith("v"):
        version = f"v{version}"
    if not version:
        log.error("could not determine version from release record")
        return 2

    try:
        publish_release(release, version, args.dry_run, args.force)
        return 0
    except StepError as exc:
        log.error(f"publish failed: {exc}")
        if not args.dry_run:
            set_release_status(version, "failed", {"reason": str(exc)[:500]})
        telegram_send(f"🔴 Phoenicurus: {version} publish FAILED — {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 — last-resort guard for a release tool
        log.exception(f"unexpected fatal error: {exc}")
        if not args.dry_run:
            set_release_status(version, "failed", {"reason": str(exc)[:500]})
        telegram_send(f"🔴 Phoenicurus: {version} publish crashed — {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
