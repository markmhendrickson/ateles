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

Per-identity Apps (operator ruling 2026-10-14): a lens may have an App of its
own (`lib.github_app_token`, identity `reviewer_<lens>`). Which comment counts
as which lens is decided by the author AND the whole body, never by the author
alone and never by the opening marker alone:

  - Once a lens HAS an App (or `ATELES_EXPECTED_LENS_APPS` says it must), its
    verdict (`review:<lens>`) is read only from that App's bot login. A shared
    identity (the accounts above) can never speak for it: any shared-identity
    comment that names such a lens ANYWHERE in its body is refused.
  - A lens App speaks for its own lens only. Every swarm marker anywhere in its
    comment (`_VERDICT_SHAPED_RE`) must be `review:<its lens>` or that marker's
    `-superseded` form. The aggregation, a deferral and the dispatcher's notice
    markers are never a lens App's to write, nor is another lens's marker.
  - A retired verdict (`compose_superseded_verdict` puts a `>` banner line ahead
    of it) is judged exactly like the verdict it retired: `leading_lens` looks
    past the banner, so an original and its retired form get the same answer for
    every author.
  - A lens whose App is declared but whose login cannot be read admits nobody
    for that lens (fail closed on the field that carries the safety meaning).
    So does a lens whose bot login is also another identity's (the PR opener,
    another lens, the swarm App): two identities that resolve to one login are
    not two identities, and the collision is reported in `failed`.
  - The PR-opener App is never an author of any lens-shaped comment. If it is
    declared but its login cannot be read, no bot other than the swarm App is
    admitted for a lens-shaped comment while that lasts, so the opener cannot
    slip in through the operator's setting.

A lens with no App of its own is read exactly as before.

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
# The note `swarm_dispatch.compose_superseded_verdict` puts ahead of a retired
# verdict: one or more `>` lines, then the `review:<lens>-superseded by=<sha>`
# marker. The look-ahead means only a real retirement (not any quoted line)
# is looked past.
_RETIREMENT_BANNER_RE = re.compile(
    r"\A\s*(?:>[^\n]*\n\s*)+"
    r"(?=<!--\s*review:[a-z0-9_]+-superseded\s+by=[0-9a-f]{40}\s*-->)",
    re.IGNORECASE,
)


def claimed_lenses(body: object) -> frozenset[str]:
    """Every lens named by a `review:<lens>` marker anywhere in the body."""
    return frozenset(
        m.group("lens").lower() for m in _LENS_MARKER_RE.finditer(str(body or ""))
    )


def leading_lens(body: object) -> str:
    """The lens named by the body's FIRST marker (the head marker), or "".

    A retired verdict opens with a retirement banner, which is looked past, so
    a verdict and its retired form name the same lens.
    """
    text = _RETIREMENT_BANNER_RE.sub("", str(body or ""), count=1)
    match = _LEADING_LENS_MARKER_RE.match(text)
    return match.group("lens").lower() if match else ""


def _swarm_marker_tokens(body: object) -> list[str]:
    """Every swarm marker anywhere in the body, lowercased (`_VERDICT_SHAPED_RE`)."""
    return [m.group(0).lower() for m in _VERDICT_SHAPED_RE.finditer(str(body or ""))]


def _names_lens(token: str, lens: str) -> bool:
    """True when a `review:...` marker token names *lens*: the head marker, the
    retired marker, or any hyphen-suffixed variant of either."""
    if not token.startswith("review:"):
        return False
    name = token[len("review:"):]
    return name == lens or name.startswith(lens + "-")


@dataclass(frozen=True)
class LensAppScope:
    """Which App speaks for which lens.

    `lens_logins` maps a lens to its App's bot login; "" means the lens's App
    is declared (or expected) but its login could not be used, so nobody speaks
    for the lens. `other_app_logins` are Apps that are never a lens-shaped
    author (the PR opener). The shared swarm identities are in neither.

    `opener_unreadable` is set when the PR-opener App is declared but its login
    cannot be read: the opener then cannot be told apart from any other bot, so
    no bot except `shared_app_logins` (the swarm App) is admitted for a
    lens-shaped comment.
    """

    lens_logins: tuple[tuple[str, str], ...] = ()
    other_app_logins: frozenset[str] = frozenset()
    opener_unreadable: bool = False
    shared_app_logins: frozenset[str] = frozenset()

    @property
    def by_lens(self) -> dict[str, str]:
        return dict(self.lens_logins)

    def admits(self, author: str, body: object) -> bool:
        """May *author* (a casefolded login already in the admitted set) have
        written this lens-shaped *body*? Author and the WHOLE body decide."""
        by_lens = self.by_lens
        owned = {login: lens for lens, login in by_lens.items() if login}
        if author in owned:
            # A lens App speaks only for its own lens: its first marker names
            # that lens, and EVERY swarm marker anywhere in the comment is that
            # lens's own head or retired marker. A reader that finds a marker by
            # substring must find no other lens's marker, no aggregation and no
            # notice marker in this comment.
            lens = owned[author]
            own = {f"review:{lens}", f"review:{lens}-superseded"}
            return (
                leading_lens(body) == lens
                and claimed_lenses(body) == {lens}
                and all(token in own for token in _swarm_marker_tokens(body))
            )
        if author in self.other_app_logins:
            return False
        if (
            self.opener_unreadable
            and author.endswith("[bot]")
            and author not in self.shared_app_logins
        ):
            return False
        # A shared swarm identity can never speak for a lens that has an App of
        # its own, wherever in the body it names that lens: readers find a
        # marker by substring, not only at the start of the comment.
        if claimed_lenses(body) & set(by_lens):
            return False
        tokens = _swarm_marker_tokens(body)
        return not any(_names_lens(token, lens) for token in tokens for lens in by_lens)


class ScopedAuthors(frozenset):
    """The admitted identities, carrying which App speaks for which lens.

    Behaves as a `frozenset` of logins everywhere. `is_swarm_comment` (and so
    `scope_lens_comments` / `ignored_lens_comments`) also applies `scope`. Every
    set operator and method that builds a new set returns a `ScopedAuthors`
    with the SAME scope, so deriving a set cannot silently restore the flat
    admit-every-shared-account rule. Two things this cannot reach: `frozenset(x)`
    on one, and a plain mutable `set` on the LEFT of an operator (`set | authors`
    is a `set`'s own operator and yields a plain `set`). Pass the object through,
    or put it on the left.
    """

    scope: LensAppScope

    def __new__(cls, members: Iterable[str] = (), scope: LensAppScope | None = None):
        obj = super().__new__(cls, members)
        obj.scope = scope or LensAppScope()
        return obj

    def _same_scope(self, members: Iterable[str]) -> "ScopedAuthors":
        return ScopedAuthors(members, self.scope)

    def _op(self, base_op, other):
        result = base_op(self, other)
        return result if result is NotImplemented else self._same_scope(result)

    def __or__(self, other):
        return self._op(frozenset.__or__, other)

    __ror__ = __or__

    def __and__(self, other):
        return self._op(frozenset.__and__, other)

    __rand__ = __and__

    def __sub__(self, other):
        return self._op(frozenset.__sub__, other)

    def __rsub__(self, other):
        return self._op(frozenset.__rsub__, other)

    def __xor__(self, other):
        return self._op(frozenset.__xor__, other)

    __rxor__ = __xor__

    def union(self, *others):
        return self._same_scope(frozenset.union(self, *others))

    def intersection(self, *others):
        return self._same_scope(frozenset.intersection(self, *others))

    def difference(self, *others):
        return self._same_scope(frozenset.difference(self, *others))

    def symmetric_difference(self, other):
        return self._same_scope(frozenset.symmetric_difference(self, other))

    def copy(self):
        return self._same_scope(self)


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


def _usable_login(raw: object) -> str:
    login = _norm(raw)
    return login if login and _LOGIN_RE.fullmatch(login) else ""


def _identity_login(identity: str, what: str) -> str:
    """An App's bot login, or "" when it cannot be read (logged by class only)."""
    try:
        return _usable_login(_github_app_token.identity_bot_login(identity))
    except Exception as exc:  # noqa: BLE001 — an unreadable App admits nobody
        log.warning(
            "lens authors: the %s's login is unreadable (%s)", what, exc.__class__.__name__
        )
        return ""


def _per_identity_apps(
    shared_logins: Iterable[str] = (),
) -> tuple[LensAppScope, set[str], list[str]]:
    """(scope, readable lens-App logins, failed sources) for the per-identity Apps.

    `shared_logins` are the logins of the shared identities that come from an
    identity SOURCE (the swarm App, each agent token's account), as opposed to
    one the operator typed. A login claimed by two identities is not two
    identities: every lens App that shares one admits nobody (fail closed,
    decision 143), and the collision is reported in `failed`.
    """
    opener = _github_app_token.PR_OPENER_IDENTITY
    # identity name -> login ("" = declared/expected but unusable)
    logins: dict[str, str] = {}
    failed: list[str] = []
    for lens in _github_app_token.reviewer_lenses_in_scope():
        identity = _github_app_token.reviewer_identity(lens)
        login = ""
        if _github_app_token.identity_configured(identity):
            login = _identity_login(identity, f"{lens} lens App")
        if not login:
            reason = (
                "is expected here (ATELES_EXPECTED_LENS_APPS) but not declared"
                if not _github_app_token.identity_declared(identity)
                else "bot login could not be read"
            )
            failed.append(f"the {lens} lens App ({reason})")
        logins[identity] = login
    opener_declared = _github_app_token.identity_declared(opener)
    opener_login = ""
    if opener_declared:
        opener_login = (
            _identity_login(opener, "PR opener App")
            if _github_app_token.identity_configured(opener)
            else ""
        )
        if not opener_login:
            failed.append("the PR opener App's bot login")
        logins[opener] = opener_login

    claims: dict[str, list[str]] = {}
    for identity, login in logins.items():
        if login:
            claims.setdefault(login, []).append(identity)
    for login in {_usable_login(x) for x in shared_logins} - {""}:
        claims.setdefault(login, []).append("the swarm App or a shared account")
    collided = {login for login, who in claims.items() if len(who) > 1}
    for login in sorted(collided):
        failed.append(
            "two identities resolve to one bot login ("
            + ", ".join(sorted(claims[login]))
            + ")"
        )

    lens_logins: list[tuple[str, str]] = []
    members: set[str] = set()
    for lens in _github_app_token.reviewer_lenses_in_scope():
        login = logins[_github_app_token.reviewer_identity(lens)]
        if login in collided:
            login = ""
        if login:
            members.add(login)
        lens_logins.append((lens, login))
    others = {opener_login} if opener_login else set()
    scope = LensAppScope(
        tuple(sorted(lens_logins)),
        frozenset(others),
        opener_unreadable=opener_declared and not opener_login,
        shared_app_logins=frozenset(_usable_login(x) for x in shared_logins) - {""},
    )
    return scope, members, failed


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
                + " (see: python3 lib/github_app_token.py status)"
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
    source_logins: set[str] = set(app_logins)
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
            source_logins.add(login)
        else:
            failed.append(f"the account behind {name}")
    scope, lens_members, app_failed = _per_identity_apps(source_logins)
    failed.extend(app_failed)
    if not scope.lens_logins and not scope.other_app_logins and not scope.opener_unreadable:
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
    per-identity Apps are configured, the author may speak for the lens the
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
