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
import time
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
NEOTOMA_BASE_URL = os.environ.get("NEOTOMA_BASE_URL", "http://localhost:9180")
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

# npm publishing runs in GitHub Actions by default (neotoma#2015): the tag push
# fires .github/workflows/npm-publish.yml, which builds on a pinned Node and
# publishes with provenance. Set PHOENICURUS_NPM_PUBLISH_MODE=local to publish
# from this host instead (fallback if the workflow or its token is unavailable).
NPM_PUBLISH_MODE = os.environ.get("PHOENICURUS_NPM_PUBLISH_MODE", "ci").strip().lower()
# How long to wait for CI to land the version on the registry, and how often to
# check. The wait must cover npm ci + build + publish + registry propagation.
NPM_PUBLISH_WAIT_S = int(os.environ.get("PHOENICURUS_NPM_PUBLISH_WAIT_S", "900"))
NPM_PUBLISH_POLL_S = int(os.environ.get("PHOENICURUS_NPM_PUBLISH_POLL_S", "20"))
# Surfaced in the timeout message so the operator can jump straight to the run.
ACTIONS_RUNS_URL = os.environ.get(
    "PHOENICURUS_ACTIONS_RUNS_URL",
    "https://github.com/markmhendrickson/neotoma/actions/workflows/npm-publish.yml",
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


# HTTP statuses worth retrying: auth/availability blips that clear on their own.
# 401/403 are included deliberately — on the loopback-trusted prod path a 403 is
# almost always a momentary server restart / auth-service warm-up, NOT a real
# permission denial (a transient 403 stranded the first live release-approval,
# 2026-07-27). A genuine 404 (record absent) is NOT retried — it is a real answer.
_NEOTOMA_RETRY_STATUSES = {401, 403, 408, 425, 429, 500, 502, 503, 504}
_NEOTOMA_MAX_ATTEMPTS = int(os.environ.get("NEOTOMA_HTTP_MAX_ATTEMPTS", "5"))
_NEOTOMA_RETRY_BASE_S = float(os.environ.get("NEOTOMA_HTTP_RETRY_BASE_S", "1.5"))


def _neotoma_request_json(req: urllib.request.Request, what: str):
    """Perform a Neotoma HTTP request, retrying TRANSIENT failures with backoff.

    Returns the parsed JSON, or None if every attempt failed. Raises nothing —
    callers keep their empty/None fallbacks for a genuinely-down Neotoma, but a
    brief blip (transient 5xx / a loopback 403 during a server restart / a
    timeout) no longer strands an approved release: it retries with exponential
    backoff before giving up. A non-retryable HTTP error (e.g. 404 not-found) is
    returned as None immediately — it is a real answer, not a blip.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _NEOTOMA_MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in _NEOTOMA_RETRY_STATUSES:
                log.warning(f"Neotoma {what}: non-retryable HTTP {exc.code}")
                return None
            reason = f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_exc = exc
            reason = str(exc)
        except json.JSONDecodeError as exc:
            # Malformed body — usually a proxy/error page during a restart; retry.
            last_exc = exc
            reason = f"bad JSON: {exc}"
        if attempt < _NEOTOMA_MAX_ATTEMPTS:
            delay = _NEOTOMA_RETRY_BASE_S * (2 ** (attempt - 1))
            log.warning(
                f"Neotoma {what}: transient failure ({reason}) — "
                f"retry {attempt}/{_NEOTOMA_MAX_ATTEMPTS - 1} in {delay:.1f}s"
            )
            time.sleep(delay)
    log.error(
        f"Neotoma {what}: gave up after {_NEOTOMA_MAX_ATTEMPTS} attempts "
        f"(last: {last_exc})"
    )
    return None


def neotoma_query(entity_type: str, limit: int = 100) -> list[dict]:
    """Query entities of a type from Neotoma. Empty list on error (after retries)."""
    base = NEOTOMA_BASE_URL.rstrip("/")
    body = json.dumps(
        {"entity_type": entity_type, "limit": limit, "include_snapshots": True}
    ).encode()
    req = urllib.request.Request(
        f"{base}/entities/query", data=body, headers=_neotoma_headers(), method="POST"
    )
    data = _neotoma_request_json(req, f"query {entity_type}")
    if data is None:
        return []
    if isinstance(data, list):
        return data
    return data.get("entities") or data.get("items") or data.get("results") or []


def neotoma_fetch_entity(entity_id: str) -> dict | None:
    """Fetch a single entity by id. None on error (after retries)."""
    base = NEOTOMA_BASE_URL.rstrip("/")
    req = urllib.request.Request(
        f"{base}/entities/{entity_id}", headers=_neotoma_headers()
    )
    return _neotoma_request_json(req, f"fetch {entity_id}")


def neotoma_store(entities: list[dict], idempotency_key: str) -> dict | None:
    """Store/update entities via POST /store. None on error (after retries)."""
    base = NEOTOMA_BASE_URL.rstrip("/")
    body = json.dumps(
        {"entities": entities, "idempotency_key": idempotency_key}
    ).encode()
    req = urllib.request.Request(
        f"{base}/store", data=body, headers=_neotoma_headers(), method="POST"
    )
    return _neotoma_request_json(req, "store")


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
    Resolve the release entity to publish. By entity_id if given, else by
    `version` — REFUSING when more than one distinct in-flight RC exists.

    Duplicate-RC guard: two concurrent prepare runs can each cut an RC for the
    same version (different branch/PR — e.g. release/v0.20.0 and release/
    v0.20.0-a), storing two distinct pending_approval release_results. Silently
    publishing "the newest" would pick one candidate over another at random for
    an IRREVERSIBLE publish. Instead we HALT and force disambiguation: the
    operator (or Ateles) must pass --entity-id to name the exact RC to ship, or
    close the duplicate PR + its record. `entity_id` always bypasses this guard
    (it IS the disambiguation).

    "In-flight" = still actionable (prepared / pending_approval / approved).
    Terminal records (published / failed) for the same version number don't
    count — they're history, not a competing candidate. Distinctness is by the
    RC branch (falling back to release_url / entity_id) so the same entity
    re-observed is one candidate, not two.
    """
    if entity_id:
        ent = neotoma_fetch_entity(entity_id)
        return ent
    if not version:
        return None
    want = version.lstrip("v")
    candidates = []
    for c in neotoma_query("release_result", limit=100):
        f = _entity_fields(c)
        if str(f.get("version") or "").lstrip("v") != want:
            continue
        status = str(f.get("status") or "").lower()
        if status in ("published", "failed", "skipped"):
            continue  # terminal — not a competing in-flight candidate
        candidates.append(c)
    if not candidates:
        return None

    # Collapse to DISTINCT in-flight RCs by their branch/PR identity.
    def _rc_key(c: dict) -> str:
        f = _entity_fields(c)
        return (
            str(f.get("rc_branch") or f.get("branch") or "")
            or str(f.get("rc_pr_url") or f.get("release_url") or "")
            or str(c.get("entity_id") or "")
        )

    # Collapse per RC key, keeping each RC's NEWEST observation.
    by_rc: dict[str, dict] = {}
    for c in candidates:
        k = _rc_key(c)
        prev = by_rc.get(k)
        if prev is None or (c.get("last_observation_at") or "") > (
            prev.get("last_observation_at") or ""
        ):
            by_rc[k] = c

    if len(by_rc) > 1:
        lines = []
        for c in by_rc.values():
            f = _entity_fields(c)
            lines.append(
                f"  - {f.get('rc_branch') or f.get('branch') or '?'} "
                f"({f.get('rc_pr_url') or f.get('release_url') or '?'}) "
                f"[{c.get('entity_id')}]"
            )
        raise StepError(
            f"{len(by_rc)} distinct in-flight release candidates for {version} — "
            "refusing to guess which to publish (an irreversible action). "
            "Two concurrent prepare runs likely each cut an RC. Resolve by "
            "closing the duplicate PR + its release_result, or re-run with "
            "--entity-id <the RC to ship>:\n" + "\n".join(lines)
        )

    # Exactly one distinct in-flight RC (its newest observation).
    matches = list(by_rc.values())
    matches.sort(key=lambda e: e.get("last_observation_at") or "", reverse=True)
    return matches[0]


# ---------------------------------------------------------------------------
# Shell helpers (run in the Neotoma repo)
# ---------------------------------------------------------------------------


class StepError(Exception):
    """A publish step failed; message is operator-facing."""


class InsufficientPermissionsError(StepError):
    """gh pr merge failed because the acting account lacks merge rights.

    Distinct from a generic StepError so operators scanning logs immediately
    recognize this as an operator action (grant merge rights / merge manually
    and resume), not a merge-conflict or network failure.
    """


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


def preflight_post_merge(version: str, dry_run: bool) -> None:
    """
    Guard the irreversible steps (tag_and_push, npm_publish) against the
    version-bump commit being missing from the merged RC PR. Must run AFTER
    merge_rc_pr (the bump commit lands via that merge) and BEFORE tag_and_push
    — this is the exact class of failure that caused the v0.18.8 incident
    (neotoma#1920): the RC PR merged without a bump commit and publish.py
    tagged/would-have-published the wrong version.
    """
    if dry_run:
        log.info(f"[dry-run] would verify package.json version == {version}")
        return
    target = version.lstrip("v")
    pkg_path = NEOTOMA_REPO_ROOT / "package.json"
    try:
        actual = json.loads(pkg_path.read_text()).get("version", "")
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise StepError(
            f"preflight/version-match FAILED: could not read/parse "
            f"{pkg_path} ({exc}). Refusing to publish without confirming the "
            f"checked-out version matches the target release version."
        ) from exc
    if actual != target:
        raise StepError(
            f"Preflight FAILED: package.json version ({actual}) does not match "
            f"target release version ({target}).\n"
            f"This means the version-bump commit is missing from the merged RC "
            f"PR — publishing now would tag/publish the WRONG version (this is "
            f"the exact defect that caused the v0.18.8 incident).\n"
            f"Fix: verify the RC PR included a `chore(release): bump version to "
            f"v{target}` commit. If missing, run "
            f"`npm version {target} --no-git-tag-version`, commit as "
            f"`chore(release): bump version to v{target} + supplement`, push, "
            f"and re-merge before re-running publish.py."
        )
    log.info(f"preflight/version-match OK: package.json version == {target}")


def _owner_repo_from_remote_url(url: str) -> str:
    """Parse 'owner/repo' out of an SSH or HTTPS git remote URL."""
    url = url.strip().removesuffix(".git")
    if url.startswith("git@"):
        # git@github.com:owner/repo
        return url.split(":", 1)[-1]
    # https://github.com/owner/repo
    return "/".join(url.rsplit("/", 2)[-2:])


def merge_rc_pr(rc_pr_url: str, rc_branch: str, dry_run: bool, version: str = "") -> None:
    if dry_run:
        log.info(f"[dry-run] would merge RC PR {rc_pr_url}")
        return
    # Merge via gh (squash to keep main linear); tolerate already-merged.
    pr_ref = rc_pr_url or rc_branch
    proc = run(["gh", "pr", "merge", pr_ref, "--merge"], check=False)
    if proc.returncode != 0:
        stderr = proc.stderr or ""
        if "does not have the correct permissions" in stderr.lower():
            remote_url = run(
                ["git", "remote", "get-url", "origin"], check=False
            ).stdout.strip()
            repo = _owner_repo_from_remote_url(remote_url)
            pr_number = pr_ref.rstrip("/").rsplit("/", 1)[-1]
            raise InsufficientPermissionsError(
                f"merge_rc_pr FAILED: the gh account running this command does "
                f"not have permission to merge pull request {pr_number} on "
                f"{repo} (GitHub returned: \"{stderr.strip()[:500]}\").\n"
                f"This is an operator action, not a retryable pipeline error.\n"
                f"Fix: have an operator with merge rights run "
                f"`gh pr merge {pr_number} --merge` (or merge via the GitHub "
                f"UI), then re-run: "
                f"python publish.py --resume-from=tag_and_push "
                f"--version={version}"
            )
        if "not mergeable" not in stderr.lower():
            # Already merged is fine; anything else is fatal.
            state = run(
                ["gh", "pr", "view", pr_ref, "--json", "state", "--jq", ".state"],
                check=False,
            ).stdout.strip()
            if state != "MERGED":
                raise StepError(f"RC PR merge failed and state={state!r}: {rc_pr_url}")
    run(["git", "fetch", "origin", "main", "--quiet"])
    run(["git", "checkout", "--detach", "FETCH_HEAD"])
    sha = run(["git", "rev-parse", "--short", "HEAD"], check=False).stdout.strip()
    log.info(
        f"merge_rc_pr: checked out origin/main in detached HEAD ({sha}) — "
        f"safe under concurrent worktrees."
    )


def tag_and_push(version: str, dry_run: bool) -> None:
    if dry_run:
        log.info(f"[dry-run] would tag {version} and push tag")
        return
    run(["git", "tag", "-a", version, "-m", f"Release {version}"])
    # merge_rc_pr always merges server-side via `gh pr merge` — there is no
    # local-merge code path in this file — so origin/main is already up to
    # date and a follow-up `git push origin main` would be a no-op push.
    log.info(
        f"tag_and_push: RC PR merged server-side via gh pr merge — skipping "
        f"git push origin main (already up to date on origin). Pushing tag "
        f"{version} only."
    )
    run(["git", "push", "origin", version])


def _registry_version(npm_env: dict | None = None) -> str:
    """Current `latest` on the npm registry, or '' if it can't be read."""
    proc = run(
        ["npm", "view", "neotoma", "version"], env=npm_env, check=False, timeout=120
    )
    return (proc.stdout or "").strip()


def await_ci_npm_publish(version: str, dry_run: bool) -> None:
    """
    Wait for the `npm publish` GitHub workflow (fired by our tag push) to land
    the release on the registry.

    Publishing moved to CI (neotoma#2015) for provenance, laptop-independence
    and a reproducible build env. tag_and_push has already pushed the tag, which
    triggers .github/workflows/npm-publish.yml.

    THE RISK THIS STEP EXISTS TO CONTAIN: moving the publish off-box turns a
    synchronous failure into an asynchronous one. If this poll were quiet, a
    failed CI publish would let the release continue to github_release and
    "succeed" with nothing on npm. So the timeout is bounded and FAILS the
    release loudly (Telegram + StepError) rather than falling through.
    """
    want = version.lstrip("v")

    if dry_run:
        log.info(f"[dry-run] would await CI npm publish of neotoma@{want}")
        return

    # Already there (e.g. a --resume-from re-run) — nothing to wait for.
    if _registry_version() == want:
        log.info(f"npm already shows neotoma@{want} — skipping wait")
        return

    deadline = time.monotonic() + NPM_PUBLISH_WAIT_S
    log.info(
        f"awaiting CI npm publish of neotoma@{want} "
        f"(timeout {NPM_PUBLISH_WAIT_S}s) — workflow: {ACTIONS_RUNS_URL}"
    )
    while time.monotonic() < deadline:
        live = _registry_version()
        if live == want:
            log.info(f"npm published neotoma@{live} (via CI)")
            return
        remaining = int(deadline - time.monotonic())
        log.info(f"registry shows {live or 'none'!r}; {remaining}s left")
        time.sleep(NPM_PUBLISH_POLL_S)

    live = _registry_version()
    msg = (
        f"npm publish did not land within {NPM_PUBLISH_WAIT_S}s. Registry shows "
        f"{live or 'none'!r}, expected {want!r}. The release is TAGGED but NOT "
        f"PUBLISHED. Check the workflow run: {ACTIONS_RUNS_URL} — then re-run "
        f"`python3 publish.py --version {version} --resume-from=npm_publish` "
        f"once it succeeds, or publish locally with "
        f"PHOENICURUS_NPM_PUBLISH_MODE=local."
    )
    telegram_send(f"🔴 Phoenicurus {version}: {msg}")
    raise StepError(msg)


def npm_publish_local(version: str, dry_run: bool) -> None:
    """Publish from this machine (fallback when CI publishing is unavailable)."""
    npm_env, npmrc = _npm_env_with_token()
    try:
        npm_whoami_preflight(npm_env)
        if dry_run:
            run(["npm", "publish", "--dry-run"], env=npm_env, timeout=900)
            log.info("[dry-run] npm publish --dry-run completed")
            return
        run(["npm", "publish"], env=npm_env, timeout=900)
        # Verify registry reflects the new version.
        published = _registry_version(npm_env)
        if published != version.lstrip("v"):
            raise StepError(
                f"npm publish ran but registry shows {published!r}, expected "
                f"{version.lstrip('v')!r}"
            )
        log.info(f"npm published neotoma@{published}")
    finally:
        npmrc.unlink(missing_ok=True)


def npm_publish(version: str, dry_run: bool) -> None:
    """Land the release on npm — via CI by default, locally on request."""
    if NPM_PUBLISH_MODE == "local":
        log.info("PHOENICURUS_NPM_PUBLISH_MODE=local — publishing from this host")
        npm_publish_local(version, dry_run)
        return
    await_ci_npm_publish(version, dry_run)


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


# Fixed step order, used to resolve --resume-from to a skip-before index.
# preflight_post_merge is not independently resumable — it always runs
# immediately before tag_and_push (the first irreversible step), regardless of
# --resume-from, since it's cheap and idempotent (re-reads package.json, no
# side effects) and is the guard against publishing without the version-bump
# commit.
STEP_ORDER = [
    "preflight",
    "merge_rc_pr",
    "tag_and_push",
    "npm_publish",
    "github_release",
    "deploy_sandbox",
    "publish_github_release_draft",
    "post_release",
]


def publish_release(
    release: dict,
    version: str,
    dry_run: bool,
    force: bool,
    resume_from: str | None = None,
    email_approval: bool = False,
) -> None:
    f = _entity_fields(release)
    status = str(f.get("status") or "")
    # Field-name reconciliation: prepare.py's agent stores the RC PR URL under
    # `release_url` and the branch under `branch`, but this reader historically
    # only looked for `rc_pr_url` / `rc_branch`. That mismatch left both empty on
    # publish, so merge_rc_pr fell back to the literal `release/<version>` and
    # the PR URL was lost. Accept BOTH names (rc_* preferred when present) so a
    # release_result stored under either convention publishes correctly.
    rc_pr_url = str(f.get("rc_pr_url") or f.get("release_url") or "")
    rc_branch = str(f.get("rc_branch") or f.get("branch") or f"release/{version}")
    notes_path_s = str(f.get("notes_path") or "")
    notes_path = Path(notes_path_s) if notes_path_s else None

    # Email-approval path (Turdus -> Apis -> here): the operator replied
    # `approve <version>` to the RC email. This IS the approval, so flip
    # pending_approval -> approved here — but ONLY from pending_approval, the
    # exact gate the Ateles Telegram-approve path applies. Refuse any other
    # starting state (already publishing/published, or never prepared) so a
    # duplicate or stale email reply can't re-publish or publish an un-prepared
    # version. This runs before the approved-gate below, which then passes.
    if email_approval and not force:
        if status == "approved":
            log.info(f"{version} already approved — proceeding to publish")
        elif status == "pending_approval":
            if not dry_run:
                set_release_status(version, "approved")
            status = "approved"
            log.info(f"{version} approved via email reply -> publishing")
        else:
            raise StepError(
                f"email-approval for {version} refused: release status is "
                f"{status!r}, not 'pending_approval'. A release can only be "
                "email-approved from pending_approval (this guards against a "
                "duplicate/stale reply re-publishing or publishing an "
                "un-prepared version)."
            )

    if status not in ("approved",) and not force:
        raise StepError(
            f"release {version} status is {status!r}, not 'approved'. "
            "Refusing to publish without approval (use --force to override)."
        )

    log.info(f"Publishing {version} (status={status}, dry_run={dry_run}, force={force})")
    if not dry_run:
        set_release_status(version, "publishing")

    resume_idx = STEP_ORDER.index(resume_from) if resume_from else 0

    if resume_idx <= STEP_ORDER.index("preflight"):
        preflight(version, rc_branch, dry_run)
    if resume_idx <= STEP_ORDER.index("merge_rc_pr"):
        merge_rc_pr(rc_pr_url, rc_branch, dry_run, version=version)
    if resume_idx <= STEP_ORDER.index("tag_and_push"):
        # Always runs before the first irreversible step (tag_and_push), even
        # when resuming past merge_rc_pr (e.g. an operator merged manually and
        # resumes with --resume-from=tag_and_push) — that resume path is
        # exactly how the v0.18.8 incident's missing bump commit slipped
        # through undetected.
        preflight_post_merge(version, dry_run)
        tag_and_push(version, dry_run)
    if resume_idx <= STEP_ORDER.index("npm_publish"):
        npm_publish(version, dry_run)
    if resume_idx <= STEP_ORDER.index("github_release"):
        github_release(version, notes_path, dry_run)
    if resume_idx <= STEP_ORDER.index("deploy_sandbox"):
        deploy_sandbox(version, dry_run)
    if resume_idx <= STEP_ORDER.index("publish_github_release_draft"):
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
    ap.add_argument(
        "--resume-from",
        choices=STEP_ORDER,
        default=None,
        help="resume from this step, skipping earlier steps (e.g. after a "
        "manual fix following a merge_rc_pr/insufficient_permissions failure)",
    )
    ap.add_argument(
        "--from-email-approval",
        action="store_true",
        help="the operator approved by email reply (Turdus->Apis): flip the "
        "release from pending_approval to approved, then publish. Refuses any "
        "other starting state.",
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
        publish_release(
            release, version, args.dry_run, args.force,
            resume_from=args.resume_from,
            email_approval=args.from_email_approval,
        )
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
