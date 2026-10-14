"""Who may author a lens verdict comment the swarm reads as a verdict.

A lens verdict is a PR comment that opens with a head marker
(`swarm_dispatch.compose_lens_review_marker`). On a public repository any
account can post a comment carrying such a marker, so the marker alone cannot
decide whether a comment is a lens's verdict: the AUTHOR has to be one of the
identities the swarm itself posts lens comments as. Every reader of lens
verdict comments goes through `scope_lens_comments` (or `is_swarm_comment`)
before it parses anything.

Design basis: docs/foundation/principles.md#5-fail-closed-on-the-field-that-carries-the-safety-meaning
The field that carries the safety meaning here is the comment's author. A
comment with no readable author, an author outside the set, or a set that
cannot be established at all is NOT a lens verdict: the lens then has no
verdict, which every consumer already treats as "not cleared".

Where the identities come from (never a login typed into this repo, so a fork
supplies its own accounts):

  - `ATELES_LENS_COMMENT_AUTHORS`: a comma- or space-separated list of GitHub
    logins. This is where an operator names the accounts the swarm (and a
    bootstrap-mode hand-run panel) posts lens comments as.
  - the swarm GitHub App's bot login (`lib.github_app_token.bot_login`),
    when the App is configured: the same App the approval tool and binding
    reviews already act as.
  - the account each agent token resolves to, read live from GitHub
    (`GET /user`): the shared `ATELES_AGENT_PAT`, `NEOTOMA_AGENT_PAT` and
    `GITHUB_TOKEN`, plus each lens agent's own `<AGENT>_AGENT_PAT` (the caller
    names those in `extra_token_envs`). This is the same "the login is the one
    the posting token resolves to, read live" reading the dispatcher already
    uses for its own notices. A login is never guessed from an agent name: a
    token that does not resolve admits nobody.

Every source that cannot be read contributes nothing, and an empty result
means no comment is read as a verdict. `resolve` also reports which sources
failed, so a caller on a path where a missed identity would clear something
(auto-merge) can hold instead of proceeding on a partial set.

Per-lens Apps (operator ruling 2026-10-14): a lens may have an App of its own
(`lib.github_app_token`, identity `reviewer_<lens>`). Once a lens HAS one, its
verdict (`review:<lens>`) is read only from that App's bot login: neither the
shared identities above nor another lens's App can speak for it. A per-lens
App is admitted ONLY for its own lens: it is never a general swarm identity,
so it cannot author another lens's verdict, the aggregation comment or a
dispatcher notice. A lens whose App is declared but whose login cannot be
read admits nobody for that lens (fail closed on the field that carries the
safety meaning). A lens with no App of its own is read exactly as before. The
PR-opener App is never an author of any lens-shaped comment.

The same author rule covers every other swarm-written marker comment the
dispatcher reads back as state (the aggregation comment, a review deferral,
and the notice markers it uses to avoid posting twice): see
`_VERDICT_SHAPED_RE`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib import github_app_token as _github_app_token  # noqa: E402

log = logging.getLogger("lens_authors")

ENV_AUTHORS = "ATELES_LENS_COMMENT_AUTHORS"

# Shared agent tokens whose owning account posts lens comments when a lens has
# no per-agent token of its own (swarm_dispatch._token_for_repo).
PAT_ENV_NAMES = ("ATELES_AGENT_PAT", "NEOTOMA_AGENT_PAT", "GITHUB_TOKEN")

# A GitHub login, or an App's `<slug>[bot]`. Anything else in the configured
# list (a wildcard, a path, a sentence) is not an identity and is dropped, so
# a malformed setting can only ever admit fewer accounts, never more.
_LOGIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\[bot\])?$")

# Any comment the dispatcher or approval tool reads back as swarm state: a
# `review:<lens>` form (head marker, superseded marker, legacy bare line), the
# Vanellus aggregation marker, a review-deferral marker, or the marker of a
# notice the dispatcher posts once (swarm-run confirmation, pipeline-bypass
# notice, gate-waive result). Deliberately wide: a comment of this shape from
# a non-swarm account is ignored by the readers, which costs nothing, while a
# narrower pattern could let a variant shape through.
_VERDICT_SHAPED_RE = re.compile(
    r"review:[a-z0-9_-]+"
    r"|vanellus-aggregation"
    r"|review-deferred-until"
    r"|swarm-run-confirmation"
    r"|pipeline-bypass-notice"
    r"|swarm-gate-waive-result",
    re.IGNORECASE,
)

# `review:<lens>` as a marker (head marker, superseded marker or legacy bare
# line). `-superseded` is a suffix of the marker, not part of the lens name.
_LENS_MARKER_RE = re.compile(
    r"review:(?P<lens>[a-z0-9_]+?)(?:-superseded)?(?![a-z0-9_-])", re.IGNORECASE
)
_LEADING_LENS_MARKER_RE = re.compile(
    r"\A\s*(?:<!--\s*)?review:(?P<lens>[a-z0-9_]+?)(?:-superseded)?(?![a-z0-9_-])",
    re.IGNORECASE,
)


def claimed_lenses(body: object) -> frozenset[str]:
    """Every lens named by a `review:<lens>` marker anywhere in the body."""
    return frozenset(
        m.group("lens").lower() for m in _LENS_MARKER_RE.finditer(str(body or ""))
    )


def leading_lens(body: object) -> str:
    """The lens named by the body's FIRST marker (the head marker), or ""."""
    match = _LEADING_LENS_MARKER_RE.match(str(body or ""))
    return match.group("lens").lower() if match else ""


@dataclass(frozen=True)
class LensAppScope:
    """Which App speaks for which lens.

    `lens_logins` maps a lens to its App's bot login; "" means the lens's App
    is declared but its login could not be read, so nobody speaks for the lens.
    `other_app_logins` are Apps that are never a lens-shaped author (the PR
    opener). The shared swarm identities are in neither.
    """

    lens_logins: tuple[tuple[str, str], ...] = ()
    other_app_logins: frozenset[str] = frozenset()

    @property
    def by_lens(self) -> dict[str, str]:
        return dict(self.lens_logins)

    def admits(self, author: str, body: object) -> bool:
        """May *author* (a casefolded login already in the admitted set) have
        written this lens-shaped *body*?"""
        by_lens = self.by_lens
        owned = {login: lens for lens, login in by_lens.items() if login}
        if author in owned:
            # A lens App speaks only for its own lens, and only in a comment
            # that names no other lens: a reader that finds a marker by
            # substring must not find another lens's marker in this comment.
            lens = owned[author]
            return leading_lens(body) == lens and claimed_lenses(body) == {lens}
        if author in self.other_app_logins:
            return False
        # A shared swarm identity: its leading marker must not claim a lens
        # that has an App of its own (that lens is read from that App only).
        lead = leading_lens(body)
        return not (lead and lead in by_lens)


class ScopedAuthors(frozenset):
    """The admitted identities, carrying which App speaks for which lens.

    Behaves as a `frozenset` of logins everywhere. `is_swarm_comment` (and so
    `scope_lens_comments` / `ignored_lens_comments`) also applies `scope`. Do
    not rebuild one with set operators: those return a plain `frozenset` and
    drop `scope`; pass the object through unchanged.
    """

    scope: LensAppScope

    def __new__(cls, members: Iterable[str] = (), scope: LensAppScope | None = None):
        obj = super().__new__(cls, members)
        obj.scope = scope or LensAppScope()
        return obj


# A successful lookup is stable; a failed one is retried soon, so a transient
# rate limit does not leave the swarm unable to read its own comments.
_LIVE_LOGIN_TTL_S = 600.0
_LIVE_LOGIN_FAILURE_TTL_S = 30.0
_LIVE_LOGIN_TIMEOUT_S = 10
_live_lock = threading.Lock()
_live_login_cache: dict[str, tuple[str, float]] = {}


def _norm(login: object) -> str:
    return str(login or "").strip().casefold()


def configured_authors() -> frozenset[str]:
    """The logins named in `ATELES_LENS_COMMENT_AUTHORS` (valid ones only)."""
    raw = os.environ.get(ENV_AUTHORS, "") or ""
    out: set[str] = set()
    for part in re.split(r"[,\s]+", raw):
        login = _norm(part)
        if login and _LOGIN_RE.fullmatch(login):
            out.add(login)
    return frozenset(out)


def _app_bot_logins() -> tuple[set[str], bool]:
    """(the swarm App's bot login, whether the lookup failed).

    An App that is not configured is not a failure: it has nothing to say.
    A configured App whose login cannot be read is.
    """
    try:
        if not _github_app_token.app_configured():
            return set(), False
        login = _norm(_github_app_token.bot_login())
    except Exception as exc:  # noqa: BLE001 — an unreadable App contributes nothing
        log.warning("lens authors: swarm App login unreadable (%s)", exc.__class__.__name__)
        return set(), True
    if login and _LOGIN_RE.fullmatch(login):
        return {login}, False
    return set(), True


def _per_identity_apps() -> tuple[LensAppScope, set[str], list[str]]:
    """(scope, readable lens-App logins, failed sources) for the per-identity Apps."""
    lens_logins: list[tuple[str, str]] = []
    members: set[str] = set()
    failed: list[str] = []
    for lens in _github_app_token.declared_reviewer_lenses():
        identity = _github_app_token.reviewer_identity(lens)
        login = ""
        try:
            if _github_app_token.identity_configured(identity):
                login = _norm(_github_app_token.identity_bot_login(identity))
        except Exception as exc:  # noqa: BLE001 — an unreadable App admits nobody
            log.warning(
                "lens authors: the %s lens App's login is unreadable (%s)",
                lens,
                exc.__class__.__name__,
            )
        if login and _LOGIN_RE.fullmatch(login):
            members.add(login)
        else:
            login = ""
            failed.append(f"the {lens} lens App's bot login")
        lens_logins.append((lens, login))
    others: set[str] = set()
    opener = _github_app_token.PR_OPENER_IDENTITY
    if _github_app_token.identity_declared(opener):
        try:
            login = _norm(_github_app_token.identity_bot_login(opener))
            if login and _LOGIN_RE.fullmatch(login):
                others.add(login)
        except Exception as exc:  # noqa: BLE001 — only ever narrows what is admitted
            log.warning(
                "lens authors: the PR opener App's login is unreadable (%s)",
                exc.__class__.__name__,
            )
    return LensAppScope(tuple(sorted(lens_logins)), frozenset(others)), members, failed


def _login_for_token(token: str) -> str:
    """The account a token belongs to (`GET /user`), cached; "" when unreadable."""
    key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.monotonic()
    with _live_lock:
        cached = _live_login_cache.get(key)
    if cached is not None:
        ttl = _LIVE_LOGIN_TTL_S if cached[0] else _LIVE_LOGIN_FAILURE_TTL_S
        if now - cached[1] < ttl:
            return cached[0]
    login = ""
    try:
        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "ateles-lens-authors/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=_LIVE_LOGIN_TIMEOUT_S) as resp:
            login = _norm((json.loads(resp.read() or b"{}") or {}).get("login"))
    except Exception as exc:  # noqa: BLE001 — a token that cannot be resolved admits nobody
        log.warning("lens authors: a shared agent token's account is unreadable (%s)", exc.__class__.__name__)
    if login and not _LOGIN_RE.fullmatch(login):
        login = ""
    with _live_lock:
        _live_login_cache[key] = (login, now)
    return login


def clear_cache() -> None:
    """Drop cached token-to-login lookups (tests)."""
    with _live_lock:
        _live_login_cache.clear()


@dataclass(frozen=True)
class Resolution:
    """The admitted identities, and the sources that could not be read."""

    authors: frozenset[str]
    failed: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """True only when at least one identity resolved and no source failed."""
        return bool(self.authors) and not self.failed

    def describe_gap(self) -> str:
        if not self.authors:
            return (
                "no swarm lens-comment identity is configured or readable "
                f"(set {ENV_AUTHORS})"
            )
        if self.failed:
            return (
                "these swarm identity sources could not be resolved: "
                + ", ".join(self.failed)
            )
        return ""


def resolve(
    *, extra_logins: Iterable[str] = (), extra_token_envs: Iterable[str] = ()
) -> Resolution:
    """Every identity the swarm posts lens comments as (normalized), and the
    sources that could not be read.

    `extra_token_envs` names further environment variables holding an agent
    token (a lens agent's own `<AGENT>_AGENT_PAT`); each is resolved through
    `GET /user` exactly like the shared tokens, and one that does not resolve
    is reported in `failed`. May make a few cached GitHub reads: call it off
    the event loop (`asyncio.to_thread`) from async code.
    """
    out: set[str] = set(configured_authors())
    failed: list[str] = []
    for extra in extra_logins:
        login = _norm(extra)
        if login and _LOGIN_RE.fullmatch(login):
            out.add(login)
    app_logins, app_failed = _app_bot_logins()
    out |= app_logins
    if app_failed:
        failed.append("the swarm App's bot login")
    seen: set[str] = set()
    for name in (*PAT_ENV_NAMES, *extra_token_envs):
        if name in seen:
            continue
        seen.add(name)
        token = (os.environ.get(name) or "").strip()
        if not token:
            continue
        login = _norm(_login_for_token(token))
        if login:
            out.add(login)
        else:
            failed.append(f"the account behind {name}")
    scope, lens_members, app_failed = _per_identity_apps()
    failed.extend(app_failed)
    if not scope.lens_logins and not scope.other_app_logins:
        return Resolution(frozenset(out), tuple(failed))
    # Per-lens Apps are members (so "is anything configured" still reads true)
    # but are admitted only for their own lens (`LensAppScope.admits`). The PR
    # opener is a member only if the setting named it, and is then refused for
    # every lens-shaped comment by the scope.
    return Resolution(ScopedAuthors(out | lens_members, scope), tuple(failed))


def resolve_authors(
    *, extra_logins: Iterable[str] = (), extra_token_envs: Iterable[str] = ()
) -> frozenset[str]:
    """`resolve(...).authors`: empty means no comment is read as a verdict."""
    return resolve(
        extra_logins=extra_logins, extra_token_envs=extra_token_envs
    ).authors


def comment_author(comment: dict) -> str:
    """A comment's author login, casefolded; "" when it is not readable."""
    user = comment.get("user") if isinstance(comment, dict) else None
    if not isinstance(user, dict):
        return ""
    return _norm(user.get("login"))


def is_swarm_comment(comment: dict, authors: frozenset[str] | set[str]) -> bool:
    """True only when the comment's readable author is in `authors` and, where
    per-lens Apps are configured, the author may speak for the lens the
    comment claims (`LensAppScope.admits`)."""
    author = comment_author(comment)
    if not author or author not in authors:
        return False
    scope = getattr(authors, "scope", None)
    if scope is None:
        return True
    return scope.admits(author, comment.get("body"))


def claimed_marker(body: object) -> str:
    """The swarm marker a comment body claims (e.g. `review:arch`), or ""."""
    match = _VERDICT_SHAPED_RE.search(str(body or ""))
    return match.group(0).lower() if match else ""


def looks_like_lens_comment(body: object) -> bool:
    """True when the body is shaped like swarm-written state (see `_VERDICT_SHAPED_RE`)."""
    return bool(_VERDICT_SHAPED_RE.search(str(body or "")))


def scope_lens_comments(
    comments: list[dict], authors: frozenset[str] | set[str]
) -> list[dict]:
    """`comments` without any lens-shaped comment a swarm identity did not write.

    A comment that is not lens-shaped is returned untouched: this narrows what
    a lens reader can see, nothing else. With no `authors` every lens-shaped
    comment is dropped.
    """
    return [
        c
        for c in comments
        if not looks_like_lens_comment(c.get("body")) or is_swarm_comment(c, authors)
    ]


def ignored_lens_comments(
    comments: list[dict], authors: frozenset[str] | set[str]
) -> list[dict]:
    """The lens-shaped comments `scope_lens_comments` drops (for reporting)."""
    return [
        c
        for c in comments
        if looks_like_lens_comment(c.get("body")) and not is_swarm_comment(c, authors)
    ]
