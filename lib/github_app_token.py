"""Short-lived GitHub App installation tokens, minted on demand.

Operator ruling 2026-09-26 (`credential_rotation_split_by_issuer`): the swarm's
long-lived GitHub PATs are replaced by installation tokens minted from the
swarm GitHub App's private key. An installation token lives about one hour and
is never stored, so there is nothing to rotate and nothing durable to leak.

This module is the ONE place that turns an App id + private key into a token.
Every consumer takes the token at call time — as ``GH_TOKEN``/``GITHUB_TOKEN``
in a child process's environment (``token_env`` / the ``exec`` CLI), or as a
bearer header — never from a value copied into a dotenv file.

Configuration (per App, by env prefix; the default prefix is the App the swarm
already runs binding reviews and approvals as):

    <PREFIX>_ID                    App id (numeric)
    <PREFIX>_PRIVATE_KEY           PEM inline (``\\n`` escapes expanded) — wins
    <PREFIX>_PRIVATE_KEY_PATH      path to the PEM file (``~`` expanded)
    <PREFIX>_INSTALLATION_ID       optional; else resolved per repository

Caching: one token per (App, installation), reused until
``REFRESH_MARGIN`` before its ``expires_at``, then re-minted. The cache is
process-local by design — tokens are never written to disk.

Identity note: an installation token acts as ``<app-slug>[bot]``. ``GET /user``
is not available to it (403), so code that discovers "who am I" from
``/user`` must use ``bot_login()`` instead.

CLI (never prints a token):

    python3 lib/github_app_token.py check [--repo owner/name]
        App slug, bot login, installation, permissions, repositories, expiry.
    python3 lib/github_app_token.py exec [--repo owner/name] [--env NAME ...] -- CMD ...
        Run CMD with GH_TOKEN and GITHUB_TOKEN (plus any --env names) set to a
        fresh installation token. The token exists only in CMD's environment.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

DEFAULT_APP_ENV_PREFIX = "ATELES_REVIEWER_APP"
GITHUB_API = "https://api.github.com"
USER_AGENT = "ateles-github-app-token/1.0"
# Re-mint this long before expiry, so a token handed to a child process that
# runs for several minutes (a gh-driven agent turn) does not expire mid-run.
REFRESH_MARGIN = timedelta(minutes=10)
_HTTP_TIMEOUT_S = 20

_log = logging.getLogger("github_app_token")


class AppTokenError(RuntimeError):
    """The App is unconfigured, or GitHub refused to mint a token.

    The message never contains a token or key material.
    """


@dataclass(frozen=True)
class InstallationToken:
    token: str
    expires_at: datetime
    installation_id: str
    permissions: dict

    def __repr__(self) -> str:  # never render the token itself
        return (
            f"InstallationToken(installation_id={self.installation_id!r}, "
            f"expires_at={self.expires_at.isoformat()!r}, token=<redacted>)"
        )


_lock = threading.Lock()
_token_cache: dict[tuple[str, str], InstallationToken] = {}
_installation_for_repo: dict[tuple[str, str], str] = {}
_app_meta_cache: dict[str, dict] = {}


def clear_cache() -> None:
    """Drop every cached token and lookup (tests; forced re-mint)."""
    with _lock:
        _token_cache.clear()
        _installation_for_repo.clear()
        _app_meta_cache.clear()


# ── configuration ────────────────────────────────────────────────────────────


def app_id(prefix: str = DEFAULT_APP_ENV_PREFIX) -> str:
    return (os.environ.get(f"{prefix}_ID") or "").strip()


def app_private_key_pem(
    prefix: str = DEFAULT_APP_ENV_PREFIX,
    *,
    logger: logging.Logger | None = None,
    log_prefix: str = "",
) -> str:
    """Return the App's private key PEM, inline or from a file; "" if absent.

    The inline variable wins when both are set. An unreadable key path is
    logged at ERROR and answered with "" (fail closed): a misconfigured path
    must be visible, never indistinguishable from "not configured".
    """
    raw = (os.environ.get(f"{prefix}_PRIVATE_KEY") or "").strip()
    if raw:
        return raw.replace("\\n", "\n")
    path = (os.environ.get(f"{prefix}_PRIVATE_KEY_PATH") or "").strip()
    if not path:
        return ""
    try:
        return pathlib.Path(path).expanduser().read_text().strip()
    except OSError as exc:
        (logger or _log).error(
            f"{log_prefix}{prefix}_PRIVATE_KEY_PATH is set but could not be "
            f"read ({exc.__class__.__name__}); App tokens cannot be minted "
            "until it is readable"
        )
        return ""


def app_configured(prefix: str = DEFAULT_APP_ENV_PREFIX) -> bool:
    return bool(app_id(prefix)) and bool(app_private_key_pem(prefix))


def mint_app_jwt(prefix: str = DEFAULT_APP_ENV_PREFIX) -> str:
    """A 10-minute App JWT (RS256), backdated 60s for clock skew."""
    import jwt as pyjwt  # local: callers that never mint need no crypto deps

    ident = app_id(prefix)
    pem = app_private_key_pem(prefix)
    if not ident or not pem:
        raise AppTokenError(
            f"GitHub App not configured: set {prefix}_ID and "
            f"{prefix}_PRIVATE_KEY or {prefix}_PRIVATE_KEY_PATH"
        )
    now = int(time.time())
    try:
        return pyjwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": ident}, pem, algorithm="RS256"
        )
    except Exception as exc:  # noqa: BLE001 — a bad key must not leak key text
        raise AppTokenError(
            f"could not sign the App JWT with {prefix}'s key ({exc.__class__.__name__})"
        ) from None


# ── HTTP ─────────────────────────────────────────────────────────────────────


def _request(method: str, path: str, bearer: str) -> dict:
    req = urllib.request.Request(
        f"{GITHUB_API}{path}",
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {bearer}",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        # The error body is GitHub's JSON message; it carries no credential.
        detail = ""
        try:
            detail = json.loads(exc.read() or b"{}").get("message", "")
        except Exception:  # noqa: BLE001
            pass
        raise AppTokenError(f"{method} {path} -> HTTP {exc.code} {detail}".strip()) from None
    except urllib.error.URLError as exc:
        raise AppTokenError(f"{method} {path} -> {exc.reason}") from None
    return json.loads(body or b"{}")


# ── tokens ───────────────────────────────────────────────────────────────────


def _resolve_installation_id(prefix: str, repo: str | None, jwt_factory) -> str:
    configured = (os.environ.get(f"{prefix}_INSTALLATION_ID") or "").strip()
    if configured:
        return configured
    if not repo:
        raise AppTokenError(
            f"{prefix}_INSTALLATION_ID is unset and no repository was given "
            "to resolve the installation from"
        )
    key = (prefix, repo.lower())
    with _lock:
        cached = _installation_for_repo.get(key)
    if cached:
        return cached
    inst = _request("GET", f"/repos/{repo}/installation", jwt_factory())
    ident = str(inst.get("id") or "")
    if not ident:
        raise AppTokenError(f"the App is not installed on {repo}")
    with _lock:
        _installation_for_repo[key] = ident
    return ident


def installation_token_info(
    repo: str | None = None, *, prefix: str = DEFAULT_APP_ENV_PREFIX
) -> InstallationToken:
    """Return a cached-or-fresh installation token for *repo*'s installation.

    Raises ``AppTokenError`` when the App is unconfigured or GitHub refuses.
    """
    if not app_configured(prefix):
        raise AppTokenError(
            f"GitHub App not configured: set {prefix}_ID and "
            f"{prefix}_PRIVATE_KEY or {prefix}_PRIVATE_KEY_PATH"
        )
    jwt_holder: list[str] = []

    def _jwt() -> str:  # sign at most once, and only when GitHub is called
        if not jwt_holder:
            jwt_holder.append(mint_app_jwt(prefix))
        return jwt_holder[0]

    installation_id = _resolve_installation_id(prefix, repo, _jwt)
    key = (prefix, installation_id)
    now = datetime.now(timezone.utc)
    with _lock:
        cached = _token_cache.get(key)
    if cached is not None and now < cached.expires_at - REFRESH_MARGIN:
        return cached
    payload = _request(
        "POST", f"/app/installations/{installation_id}/access_tokens", _jwt()
    )
    token = str(payload.get("token") or "")
    expires_raw = str(payload.get("expires_at") or "")
    if not token or not expires_raw:
        raise AppTokenError("GitHub returned no token/expiry for the installation")
    minted = InstallationToken(
        token=token,
        expires_at=datetime.fromisoformat(expires_raw.replace("Z", "+00:00")),
        installation_id=installation_id,
        permissions=dict(payload.get("permissions") or {}),
    )
    with _lock:
        _token_cache[key] = minted
    return minted


def installation_token(
    repo: str | None = None, *, prefix: str = DEFAULT_APP_ENV_PREFIX
) -> str:
    return installation_token_info(repo, prefix=prefix).token


def token_env(
    repo: str | None = None,
    *,
    prefix: str = DEFAULT_APP_ENV_PREFIX,
    extra_names: tuple[str, ...] = (),
) -> dict[str, str]:
    """``{GH_TOKEN, GITHUB_TOKEN, *extra_names}`` → a fresh token.

    Merge into a child's env at spawn time: ``{**os.environ, **token_env(r)}``.
    ``gh`` reads GH_TOKEN before GITHUB_TOKEN, so both are set to the same value
    to stop an ambient GITHUB_TOKEN (a PAT) from winning.
    """
    tok = installation_token(repo, prefix=prefix)
    return {name: tok for name in ("GH_TOKEN", "GITHUB_TOKEN", *extra_names)}


def app_metadata(prefix: str = DEFAULT_APP_ENV_PREFIX) -> dict:
    with _lock:
        cached = _app_meta_cache.get(prefix)
    if cached is not None:
        return cached
    meta = _request("GET", "/app", mint_app_jwt(prefix))
    with _lock:
        _app_meta_cache[prefix] = meta
    return meta


def bot_login(prefix: str = DEFAULT_APP_ENV_PREFIX) -> str:
    """The login an installation token acts as: ``<slug>[bot]``."""
    slug = str(app_metadata(prefix).get("slug") or "")
    if not slug:
        raise AppTokenError("GET /app returned no slug")
    return f"{slug}[bot]"


def github_headers(
    repo: str | None = None, *, prefix: str = DEFAULT_APP_ENV_PREFIX
) -> dict[str, str]:
    """REST headers authenticated with a fresh installation token."""
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {installation_token(repo, prefix=prefix)}",
    }


def read_token(repo: str | None = None, *, fallback_env: tuple[str, ...] = ()) -> str:
    """Token for READ-ONLY GitHub calls: the App when configured, else env.

    The env fallback exists only for the migration window (hosts where the App
    is not yet configured); it returns "" rather than raising so a public-repo
    read can still proceed unauthenticated. Remove the fallback names once the
    PATs are revoked.
    """
    if app_configured():
        try:
            return installation_token(repo)
        except AppTokenError as exc:
            _log.warning(f"App installation token unavailable ({exc}); using env fallback")
    for name in fallback_env:
        value = os.environ.get(name, "")
        if value:
            return value
    return ""


def gh_read_env(repo: str | None = None) -> dict[str, str]:
    """Environment for a READ-ONLY ``gh`` subprocess.

    ``os.environ`` with GH_TOKEN/GITHUB_TOKEN set to the App's installation
    token when one can be minted; otherwise ``os.environ`` unchanged (the
    migration-window behaviour: the child inherits whatever token the daemon
    already carries). Never use for a write — writes carry an identity, and
    which identity writes is decided per consumer, not defaulted here.
    """
    env = dict(os.environ)
    if app_configured():
        try:
            env.update(token_env(repo))
        except AppTokenError as exc:
            _log.warning(f"App installation token unavailable ({exc}); gh inherits env")
    return env


# ── CLI ──────────────────────────────────────────────────────────────────────


def _cli_check(repo: str | None, prefix: str) -> int:
    meta = app_metadata(prefix)
    info = installation_token_info(repo, prefix=prefix)
    repos = _request("GET", "/installation/repositories?per_page=100", info.token)
    print(f"app: {meta.get('slug')} (id {meta.get('id')})")
    print(f"acts as: {meta.get('slug')}[bot]")
    print(f"installation: {info.installation_id}")
    print(f"permissions: {json.dumps(info.permissions, sort_keys=True)}")
    print(
        "repositories: "
        + ", ".join(r.get("full_name", "") for r in repos.get("repositories", []))
    )
    print(f"token expires_at: {info.expires_at.isoformat()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    cmd_tail: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, cmd_tail = argv[:split], argv[split + 1 :]

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=("check", "exec"))
    parser.add_argument("--repo", default=None, help="owner/name (resolves the installation)")
    parser.add_argument("--prefix", default=DEFAULT_APP_ENV_PREFIX, help="App env prefix")
    parser.add_argument(
        "--env", action="append", default=[], help="extra env var name to receive the token"
    )
    args = parser.parse_args(argv)

    try:
        if args.action == "check":
            return _cli_check(args.repo, args.prefix)
        if not cmd_tail:
            parser.error("exec needs a command after --")
        env = {
            **os.environ,
            **token_env(args.repo, prefix=args.prefix, extra_names=tuple(args.env)),
        }
    except AppTokenError as exc:
        print(f"github_app_token: {exc}", file=sys.stderr)
        return 2
    os.execvpe(cmd_tail[0], cmd_tail, env)
    return 127  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())
