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

Per-identity Apps (operator ruling 2026-10-14): besides the default App above,
each swarm identity can have an App of its own, so the host's record of who
opened or approved a pull request matches who did the work. An identity name
maps to an env prefix by one rule, ``ATELES_APP_<IDENTITY>``:

    pr_opener      -> ATELES_APP_PR_OPENER      opens pull requests
    reviewer_<lens>-> ATELES_APP_REVIEWER_<LENS> (pm, arch, qa, ux, security,
                                                  content, legal) approves and
                                                  posts that lens's verdict

Each uses the same ``_ID`` / ``_PRIVATE_KEY`` / ``_PRIVATE_KEY_PATH`` /
``_INSTALLATION_ID`` variables as the default App. The PR-opener App must never
be the approving identity: an App cannot approve a pull request it opened.

Three words are used in one sense throughout. *Declared*: any of an identity's
variables is set (``identity_declared``). *Configured*: it has an App id and a
readable private key (``identity_configured``). *Unusable*: declared or
expected but not configured, or configured but its bot login cannot be read or
is another identity's. A half-configured App reads as declared and unusable
rather than being mistaken for absent, and the readers fail closed on it.

Two names that look alike and are not: the swarm App (the default, the one
every lens without an App of its own still approves as) is
``ATELES_REVIEWER_APP_*``; the per-identity Apps are ``ATELES_APP_REVIEWER_<LENS>_*``
and ``ATELES_APP_PR_OPENER_*``. A transposed name is a different, undeclared
name, so ``status`` lists both.

``ATELES_EXPECTED_LENS_APPS`` (optional) names the lenses that must have an App
on THIS host. A process that is missing a lens's variables while the setting
names that lens is not allowed to fall back to the shared identity: its
verdicts are unread, its approval is refused, and its agent gets no token, each
loudly. Set it wherever the per-lens variables are set (the daemons and any
session that runs the approval tool), from the same snapshot. An entry that is
not a known lens name (a stray character or a misspelling) is refused, never
dropped: every lens is then treated as expected, so nothing is read, approved,
or minted for a lens without an App here, and ``status`` names the entry and
exits non-zero.

The private-key variables are never handed to a child process: ``exec`` removes
every App key variable from the child's environment (it needs only the minted
token), and the dispatcher does the same for agent processes
(``is_app_key_variable``).

CLI (never prints a token):

    python3 lib/github_app_token.py check [--repo owner/name] [--identity NAME | --prefix P]
        App slug, bot login, installation, permissions, repositories, expiry.
    python3 lib/github_app_token.py exec [--repo owner/name] [--identity NAME | --prefix P] [--env NAME ...] -- CMD ...
        Run CMD with GH_TOKEN and GITHUB_TOKEN (plus any --env names) set to a
        fresh installation token. The token exists only in CMD's environment.
        Open a pull request as the PR-opener App with:
          exec --identity pr_opener --repo owner/name -- gh pr create ...
    python3 lib/github_app_token.py status
        Which per-identity Apps are configured on this host (no network), and
        what ATELES_EXPECTED_LENS_APPS says. Exits 1 when that setting has an
        entry that is not a lens name.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import pathlib
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

DEFAULT_APP_ENV_PREFIX = "ATELES_REVIEWER_APP"
# Per-identity Apps: ``<APP_IDENTITY_ENV_PREFIX>_<IDENTITY>`` (see module doc).
APP_IDENTITY_ENV_PREFIX = "ATELES_APP"
PR_OPENER_IDENTITY = "pr_opener"
REVIEWER_IDENTITY_PREFIX = "reviewer_"
# Review roles that may have an App of their own. Used only to list a row for
# each in `status`; discovery of configured reviewers scans the environment, not
# this list. A test holds it equal to `review_panel.LENSES`.
KNOWN_REVIEWER_LENSES = ("pm", "arch", "qa", "ux", "security", "content", "legal")
# Optional roster: lenses that must have an App on this host (see module doc).
ENV_EXPECTED_LENS_APPS = "ATELES_EXPECTED_LENS_APPS"
_APP_VAR_SUFFIXES = ("_INSTALLATION_ID", "_PRIVATE_KEY_PATH", "_PRIVATE_KEY", "_ID")
# A lens key: lowercase words joined by single underscores. `identity_env_prefix`
# turns every non-alphanumeric run into one underscore, so this is the shape
# for which the env name and the lens name round-trip (a hyphen would not).
_LENS_KEY_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
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


def forget_token(token: str) -> bool:
    """Drop the cached installation token equal to *token* (server-side revoked).

    GitHub can invalidate an installation token before its ``expires_at`` (a
    permission change, an uninstall, a key rotation). The cache cannot know, so
    a caller that has just been answered 401 with this token forgets it here and
    the next ``installation_token_info`` mints a fresh one. Installation lookups
    are kept. Returns True when an entry was dropped.
    """
    if not token:
        return False
    with _lock:
        stale = [k for k, v in _token_cache.items() if v.token == token]
        for k in stale:
            del _token_cache[k]
    return bool(stale)


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


# ── per-identity Apps ───────────────────────────────────────────────────────


def identity_env_prefix(identity: str) -> str:
    """``pr_opener`` -> ``ATELES_APP_PR_OPENER``; the one name-to-prefix rule."""
    slug = re.sub(r"[^a-z0-9]+", "_", str(identity or "").strip().lower()).strip("_")
    if not slug:
        raise AppTokenError("an App identity name is required")
    return f"{APP_IDENTITY_ENV_PREFIX}_{slug.upper()}"


def reviewer_identity(lens: str) -> str:
    return f"{REVIEWER_IDENTITY_PREFIX}{str(lens or '').strip().lower()}"


def reviewer_prefix(lens: str) -> str:
    return identity_env_prefix(reviewer_identity(lens))


def pr_opener_prefix() -> str:
    return identity_env_prefix(PR_OPENER_IDENTITY)


def _any_app_var_set(prefix: str) -> bool:
    return any(
        (os.environ.get(f"{prefix}{suffix}") or "").strip() for suffix in _APP_VAR_SUFFIXES
    )


def identity_declared(identity: str) -> bool:
    """True when ANY of the identity's App variables is set (usable or not)."""
    return _any_app_var_set(identity_env_prefix(identity))


def identity_configured(identity: str) -> bool:
    """True when the identity has both an App id and a readable private key."""
    return app_configured(identity_env_prefix(identity))


def declared_reviewer_lenses() -> tuple[str, ...]:
    """Lenses whose own App has at least one variable set, sorted.

    Discovered from the environment, so a lens added to the panel needs no
    change here. A name that is not a plain lens key is ignored.
    """
    head = f"{APP_IDENTITY_ENV_PREFIX}_REVIEWER_"
    found: set[str] = set()
    for name, value in os.environ.items():
        if not name.startswith(head) or not (value or "").strip():
            continue
        rest = name[len(head):]
        for suffix in _APP_VAR_SUFFIXES:
            if rest.endswith(suffix):
                lens = rest[: -len(suffix)].lower()
                if _LENS_KEY_RE.fullmatch(lens):
                    found.add(lens)
                break
    return tuple(sorted(found))


def _expected_roster_entries() -> tuple[list[str], list[str]]:
    """(valid lens keys, rejected entries) from `ATELES_EXPECTED_LENS_APPS`.

    An entry is valid only when it is a lens key AND names a lens this code
    knows (`KNOWN_REVIEWER_LENSES`) or one that has App variables here. A
    misspelled name of a real lens is the same mistake as a stray semicolon: it
    would drop that lens's protection, so it is rejected.
    """
    raw = os.environ.get(ENV_EXPECTED_LENS_APPS) or ""
    known = set(KNOWN_REVIEWER_LENSES) | set(declared_reviewer_lenses())
    valid: list[str] = []
    rejected: list[str] = []
    for part in re.split(r"[,\s]+", raw):
        if not part:
            continue
        if _LENS_KEY_RE.fullmatch(part.lower()) and part.lower() in known:
            valid.append(part.lower())
        else:
            rejected.append(part)
    return valid, rejected


def _describe_rejected_entry(entry: str) -> str:
    """How one rejected entry is shown: never a value that might be a secret.

    Shown as written only when it is short, made of letters, digits and
    ``_-;:|*./``, and either contains a separator character (a typo such as
    ``pm;qa``) or is close to a known lens name (then with the suggestion).
    Anything else is described by its length, so a value pasted into the wrong
    setting is not echoed.
    """
    if len(entry) <= 32 and re.fullmatch(r"[A-Za-z0-9_\-;:|*./]+", entry):
        if _LENS_KEY_RE.fullmatch(entry.lower()):
            close = difflib.get_close_matches(entry.lower(), KNOWN_REVIEWER_LENSES, n=1, cutoff=0.7)
            if close:
                return f"{entry!r}, did you mean {close[0]!r}?"
        elif re.search(r"[;:|*./\-]", entry):
            return repr(entry)
    return f"an entry of {len(entry)} characters"


def malformed_expected_lens_entries() -> tuple[str, ...]:
    """What to print for each `ATELES_EXPECTED_LENS_APPS` entry that is not a known lens."""
    return tuple(_describe_rejected_entry(e) for e in _expected_roster_entries()[1])


def expected_roster_summary() -> str:
    """What is wrong with `ATELES_EXPECTED_LENS_APPS`, or "" when it is sound."""
    bad = malformed_expected_lens_entries()
    if not bad:
        return ""
    return (
        f"{ENV_EXPECTED_LENS_APPS} has {'an entry' if len(bad) == 1 else 'entries'} "
        f"that {'is' if len(bad) == 1 else 'are'} not a known lens name ({'; '.join(bad)})"
    )


def expected_roster_problem() -> str:
    """`expected_roster_summary` plus what the process does about it, or "" when sound."""
    summary = expected_roster_summary()
    if not summary:
        return ""
    return f"{summary}; every lens is treated as expected to have an App until it is fixed"


def expected_reviewer_lenses() -> tuple[str, ...]:
    """Lenses `ATELES_EXPECTED_LENS_APPS` says must have an App on this host.

    Comma- or space-separated lens names. The roster is a list of lenses that
    are REQUIRED to have an App, so a malformed entry is the unsafe direction
    to drop: a typo would silently remove that lens's protection. It fails
    closed instead: if any entry is not a known lens name (a stray character or
    a misspelling), every known lens is expected, which refuses verdict reads,
    approvals, and agent tokens for each lens that has no App here, and
    `expected_roster_problem` names the entry.
    """
    valid, rejected = _expected_roster_entries()
    if rejected:
        return tuple(sorted(set(KNOWN_REVIEWER_LENSES) | set(valid)))
    return tuple(sorted(set(valid)))


def missing_expected_lens_apps() -> tuple[str, ...]:
    """Expected lenses with no App variable at all in THIS process.

    The process is missing what the roster says it must have: it must not fall
    back to the shared identity for these lenses.
    """
    return tuple(
        lens
        for lens in expected_reviewer_lenses()
        if not identity_declared(reviewer_identity(lens))
    )


def reviewer_lenses_in_scope() -> tuple[str, ...]:
    """Every lens that has, or is expected to have, an App of its own."""
    return tuple(sorted(set(declared_reviewer_lenses()) | set(expected_reviewer_lenses())))


def lens_has_own_app(lens: str) -> bool:
    """True when *lens* has an App declared here or is expected to have one."""
    return str(lens or "").strip().lower() in reviewer_lenses_in_scope()


def is_app_key_variable(name: str) -> bool:
    """True for an App PRIVATE KEY variable (inline or path), of any App.

    The default App's ``ATELES_REVIEWER_APP_PRIVATE_KEY(_PATH)`` and every
    per-identity ``ATELES_APP_<IDENTITY>_PRIVATE_KEY(_PATH)``. These let their
    holder mint a token for an App, so no dispatched agent may hold one: the
    daemon mints, and an agent needs only the token it is handed.
    """
    upper = str(name or "").upper()
    if not upper.endswith(("_PRIVATE_KEY", "_PRIVATE_KEY_PATH")):
        return False
    return upper.startswith(f"{APP_IDENTITY_ENV_PREFIX}_") or upper.startswith(
        f"{DEFAULT_APP_ENV_PREFIX}_"
    )


def without_app_keys(variables: dict[str, str]) -> dict[str, str]:
    """A copy of a child-process variable map with every App key removed."""
    return {k: v for k, v in variables.items() if not is_app_key_variable(k)}


def identity_token(identity: str, repo: str | None = None) -> str:
    return installation_token(repo, prefix=identity_env_prefix(identity))


def identity_bot_login(identity: str) -> str:
    return bot_login(identity_env_prefix(identity))


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


def repo_scoped_token(
    repo: str, *, prefix: str = DEFAULT_APP_ENV_PREFIX
) -> InstallationToken:
    """An installation token proven to reach *repo*, or ``AppTokenError``.

    ``installation_token`` resolves the installation from the repository only
    when ``<PREFIX>_INSTALLATION_ID`` is unset; when it is pinned the token is
    that installation's whatever the repository, and a private repository the
    installation does not cover then answers 404 deep inside a clone or gh call
    with no hint of why. This asks GitHub once, with the minted token, whether
    the repository is visible to it, and says so by name when it is not. Use it
    wherever a caller reads a repository it does not own and must fail closed
    rather than fall back to an ambient credential.
    """
    if not repo or "/" not in repo:
        raise AppTokenError(f"a repository as owner/name is required, got {repo!r}")
    info = installation_token_info(repo, prefix=prefix)
    try:
        _request("GET", f"/repos/{repo}", info.token)
    except AppTokenError as exc:
        # A rejected token must not stay cached for the next caller.
        forget_token(info.token)
        raise AppTokenError(
            f"no installation of the {prefix} App covers {repo} "
            f"(installation {info.installation_id}): {exc}"
        ) from None
    return info


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


def _unusable_reason(prefix: str) -> str:
    """Which half of a declared-but-unusable App is missing (names only)."""
    missing = []
    if not app_id(prefix):
        missing.append(f"{prefix}_ID")
    if not app_private_key_pem(prefix):
        missing.append(f"{prefix}_PRIVATE_KEY or {prefix}_PRIVATE_KEY_PATH (readable)")
    return ", ".join(missing)


def _status_state(prefix: str, *, expected: bool = False) -> str:
    if app_configured(prefix):
        return "configured (id and key present; not tested, run: check --identity NAME)"
    if _any_app_var_set(prefix):
        return f"DECLARED BUT UNUSABLE, missing: {_unusable_reason(prefix)}"
    if expected:
        return "EXPECTED BUT NOT DECLARED in this process (see ATELES_EXPECTED_LENS_APPS)"
    return "not configured"


def _cli_status() -> int:
    """Which Apps this host has configured. Offline; prints names, never values."""
    expected = set(expected_reviewer_lenses())
    lenses = sorted(set(KNOWN_REVIEWER_LENSES) | set(declared_reviewer_lenses()) | expected)
    rows = [(PR_OPENER_IDENTITY, pr_opener_prefix(), False)]
    rows += [
        (reviewer_identity(lens), reviewer_prefix(lens), lens in expected) for lens in lenses
    ]
    print("Per-identity GitHub Apps on this host (offline; variable names only):")
    print("  not configured        that role keeps its current identity")
    print("  declared but unusable fails closed: the lens's verdicts are unread, approval is refused")
    print("  expected but not declared  ATELES_EXPECTED_LENS_APPS names it; fails closed the same way")
    roster_problem = expected_roster_problem()
    valid_entries, _ = _expected_roster_entries()
    if roster_problem:
        print(f"{ENV_EXPECTED_LENS_APPS}: MALFORMED. {roster_problem}")
    elif valid_entries:
        print(f"{ENV_EXPECTED_LENS_APPS}: {', '.join(sorted(set(valid_entries)))}")
    else:
        print(f"{ENV_EXPECTED_LENS_APPS}: unset (no lens is required to have an App here)")
    for identity, prefix, is_expected in rows:
        print(f"{identity:<18} {prefix:<32} {_status_state(prefix, expected=is_expected)}")
    print()
    print(
        "The swarm App (the default; approves for every lens without an App of its own) "
        "is ATELES_REVIEWER_APP_*, a different name from ATELES_APP_REVIEWER_<LENS>_*:"
    )
    print(
        f"{'swarm App':<18} {DEFAULT_APP_ENV_PREFIX:<32} "
        f"{_status_state(DEFAULT_APP_ENV_PREFIX)}"
    )
    return 1 if roster_problem else 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    cmd_tail: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, cmd_tail = argv[:split], argv[split + 1 :]

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=("check", "exec", "status"))
    parser.add_argument("--repo", default=None, help="owner/name (resolves the installation)")
    parser.add_argument("--prefix", default=None, help="App env prefix (default: the swarm App)")
    parser.add_argument(
        "--identity",
        default=None,
        help="per-identity App name (pr_opener, reviewer_<lens>); mutually exclusive with --prefix",
    )
    parser.add_argument(
        "--env", action="append", default=[], help="extra env var name to receive the token"
    )
    args = parser.parse_args(argv)
    if args.identity and args.prefix:
        parser.error("--identity and --prefix are mutually exclusive")
    if args.action == "status" and (args.identity or args.prefix or args.repo or args.env):
        parser.error("status lists every App; it takes no --identity, --prefix, --repo or --env")

    try:
        if args.action == "status":
            return _cli_status()
        if args.identity:
            args.prefix = identity_env_prefix(args.identity)
        elif not args.prefix:
            args.prefix = DEFAULT_APP_ENV_PREFIX
        if args.action == "check":
            return _cli_check(args.repo, args.prefix)
        if not cmd_tail:
            parser.error("exec needs a command after --")
        # The command gets the minted token and no App key: it can act as this
        # one App for the token's lifetime, and cannot mint a token for another.
        env = {
            **without_app_keys(dict(os.environ)),
            **token_env(args.repo, prefix=args.prefix, extra_names=tuple(args.env)),
        }
    except AppTokenError as exc:
        print(f"github_app_token: {exc}", file=sys.stderr)
        return 2
    os.execvpe(cmd_tail[0], cmd_tail, env)
    return 127  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())
