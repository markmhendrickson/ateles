"""Publish a Neotoma release when the operator approves its release PR on GitHub.

OFF BY DEFAULT. Only ``ATELES_PUBLISH_ON_OPERATOR_REVIEW=1`` (that exact value)
turns it on; unset or any other value leaves the dispatcher's behaviour exactly
as before and this module makes no network call.

When it is on, a review on a release pull request of the release repository
(head branch ``release/<version>``) never reaches the dispatcher's
approve-and-merge path. This module handles it instead, and the comment and
email approval commands are refused for a release pull request too, so the only
way such an approval publishes is the operator's own GitHub review.

This is a consent gate in front of an irreversible publish, so every doubt
refuses. Every condition below is read live at decision time; nothing is taken
from the webhook payload except the review id, which is only a key to re-read:

  - the review with that id is by the operator login (not a bot), is APPROVED,
    and its commit is the pull request's current head;
  - no later review by that login is CHANGES_REQUESTED or DISMISSED, and no
    later comment or review body by that login starts with hold, skip, stop or
    do not publish;
  - the pull request is open, based on the release branch, its head branch is
    ``release/<version>`` in the same repository, and it is not behind the base;
  - exactly one release_result matches the version and the pull request URL,
    with status pending_approval and rc_head_sha equal to the head;
  - the panel gate that ``execution/scripts/approve_pr_as_app.py`` evaluates
    passes on this head (its own ``run`` in dry-run mode, not a copy);
  - the swarm App's approval review exists on this head.

On a pass, ``publish.py --version <v> --entity-id <id> --from-github-review
<review id>`` runs exactly once under a per-version lock, after a final re-read
of the head. Every refusal publishes nothing, is logged, and is reported through
the notifier and one pull request comment posted with the App identity. A
failed notice never leads to a publish.

Design basis: docs/foundation/principles.md#1-a-mechanism-that-does-not-bind-is-not-a-control
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

log = logging.getLogger("apis.release_review_approval")

FLAG_ENV = "ATELES_PUBLISH_ON_OPERATOR_REVIEW"
RELEASE_HEAD_RE = re.compile(r"^release/(v[0-9][0-9A-Za-z.\-+]*)$")
HOLD_PREFIXES = ("hold", "skip", "stop", "do not publish")
# Marker on this module's own notices. It carries no command token, so a
# notice can never re-trigger a comment command.
NOTICE_MARKER = "<!-- release-review-approval -->"
_PER_PAGE = 100
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def enabled(env: dict[str, str] | None = None) -> bool:
    """True only when the flag is the exact string ``1``."""
    source = os.environ if env is None else env
    return source.get(FLAG_ENV) == "1"


class Refusal(Exception):
    """A condition failed; the message names it in plain words."""


class NotReleasePR(Exception):
    """The pull request is not a release pull request; the normal path applies."""


@dataclass
class Deps:
    """Everything with a side effect, injected so tests use fakes.

    ``github_get(path, params)`` returns ``(status, json, headers)``.
    ``release_results()`` returns ``(rows, complete)``.
    ``panel_gate(repo, pr, head)`` returns ``(passed, reason)``.
    ``app_bot_login()`` returns the App's bot login (empty when unreadable).
    ``notify(text)`` and ``post_comment(repo, pr, body)`` report a refusal.
    ``run_publish(argv)`` returns ``(returncode, last_output_line)`` and
    raises ``asyncio.TimeoutError`` on timeout.
    """

    github_get: Callable[[str, dict], Awaitable[tuple[int, Any, dict]]]
    release_results: Callable[[], Awaitable[tuple[list[dict], bool]]]
    panel_gate: Callable[[str, int, str], Awaitable[tuple[bool, str]]]
    app_bot_login: Callable[[], Awaitable[str]]
    notify: Callable[[str], None]
    post_comment: Callable[[str, int, str], Awaitable[bool]]
    run_publish: Callable[[list[str]], Awaitable[tuple[int, str]]]
    operator_login: str
    release_repo: str
    release_branch: str = "main"
    publish_script: str = ""
    python: str = sys.executable


@dataclass
class _Decision:
    version: str
    entity_id: str
    review_id: int
    head_sha: str


def is_release_repo(repository: str, release_repo: str) -> bool:
    return bool(repository) and repository.casefold() == (release_repo or "").casefold()


def _is_bot(user: dict) -> bool:
    login = str((user or {}).get("login") or "")
    return (
        not login
        or str((user or {}).get("type") or "") != "User"
        or login.casefold().endswith("[bot]")
    )


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise Refusal(f"a timestamp could not be read ({value!r})")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise Refusal(f"a timestamp could not be read ({value!r})") from exc
    if parsed.tzinfo is None:
        raise Refusal(f"a timestamp carries no time zone ({value!r})")
    return parsed


def _starts_with_hold(body: object) -> bool:
    text = str(body or "").strip().casefold()
    return any(text.startswith(p) for p in HOLD_PREFIXES)


def _norm_version(value: object) -> str:
    v = str(value or "").strip()
    return v if not v or v.startswith("v") else f"v{v}"


def _fields(row: dict) -> dict:
    return row.get("snapshot") or row.get("fields") or row


class ReleaseReviewApproval:
    """The release-review publish gate. One instance per dispatcher."""

    def __init__(self, deps: Deps) -> None:
        self.deps = deps
        self._locks: dict[str, asyncio.Lock] = {}
        self._started_versions: set[str] = set()
        self._seen_reviews: set[int] = set()

    # ── GitHub reads (fail closed) ──────────────────────────────────────────

    async def _get(self, path: str, what: str, params: dict | None = None) -> Any:
        try:
            status, data, _ = await self.deps.github_get(path, params or {})
        except Exception as exc:
            raise Refusal(f"{what} could not be read ({exc.__class__.__name__})") from exc
        if status != 200 or data is None:
            raise Refusal(f"{what} could not be read (HTTP {status})")
        return data

    async def _get_all(self, path: str, what: str) -> list[dict]:
        """Every page of a list endpoint; an unreadable or malformed page refuses."""
        rows: list[dict] = []
        for page in range(1, 51):
            data = await self._get(path, what, {"per_page": _PER_PAGE, "page": page})
            if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
                raise Refusal(f"{what} came back in an unexpected shape")
            rows.extend(data)
            if len(data) < _PER_PAGE:
                return rows
        raise Refusal(f"{what} has more pages than this check reads")

    async def _fetch_pr(self, repo: str, pr: int) -> dict:
        data = await self._get(f"/repos/{repo}/pulls/{pr}", "the pull request")
        if not isinstance(data, dict):
            raise Refusal("the pull request came back in an unexpected shape")
        return data

    def _release_version(self, repo: str, pr_data: dict) -> str:
        head = pr_data.get("head") or {}
        match = RELEASE_HEAD_RE.match(str(head.get("ref") or ""))
        if not match:
            raise NotReleasePR()
        return match.group(1)

    # ── public entrances ────────────────────────────────────────────────────

    async def handle_review(self, trigger: Any) -> bool:
        """Handle a pr_review trigger on the release repository.

        Returns False only when the live pull request is not a release pull
        request (the caller then takes its normal path). Returns True when this
        module handled it: published, or refused and reported.
        """
        repo, pr = trigger.repository, int(trigger.number or 0)
        try:
            pr_data = await self._fetch_pr(repo, pr)
            try:
                self._release_version(repo, pr_data)
            except NotReleasePR:
                return False
            decision = await self._evaluate(trigger, pr_data)
        except Refusal as exc:
            await self._refuse(repo, pr, str(exc))
            return True
        await self._publish(repo, pr, decision)
        return True

    async def guard_other_entrance(self, trigger: Any, source: str) -> bool:
        """True when a comment or email approval must not reach the merge path.

        A release pull request is published only from the operator's GitHub
        review; an unreadable pull request is treated as one (fail closed).
        """
        repo, pr = trigger.repository, int(trigger.number or 0)
        try:
            pr_data = await self._fetch_pr(repo, pr)
            self._release_version(repo, pr_data)
        except NotReleasePR:
            return False
        except Refusal as exc:
            await self._refuse(repo, pr, f"the {source} was not acted on: {exc}")
            return True
        await self._refuse(
            repo,
            pr,
            f"the {source} was not acted on: a release pull request is "
            "published only from the operator's approving review in GitHub",
        )
        return True

    # ── the decision ────────────────────────────────────────────────────────

    async def _evaluate(self, trigger: Any, pr_data: dict) -> _Decision:
        d = self.deps
        repo, pr = trigger.repository, int(trigger.number or 0)
        raw_review = (getattr(trigger, "raw", None) or {}).get("review") or {}
        review_id = raw_review.get("id")
        if not isinstance(review_id, int) or isinstance(review_id, bool) or review_id <= 0:
            raise Refusal("the review id in the event could not be read")
        if review_id in self._seen_reviews:
            raise Refusal(f"review {review_id} was already handled once")

        version = self._release_version(repo, pr_data)
        head = pr_data.get("head") or {}
        base = pr_data.get("base") or {}
        head_sha = str(head.get("sha") or "")
        if not _FULL_SHA_RE.match(head_sha):
            raise Refusal("the pull request head commit could not be read")
        if pr_data.get("state") != "open" or pr_data.get("merged"):
            raise Refusal("the pull request is not open")
        if str(base.get("ref") or "") != d.release_branch:
            raise Refusal(f"the pull request is not based on {d.release_branch}")
        head_repo = str((head.get("repo") or {}).get("full_name") or "")
        base_repo = str((base.get("repo") or {}).get("full_name") or "")
        if not head_repo or head_repo.casefold() != repo.casefold() or base_repo.casefold() != repo.casefold():
            raise Refusal("the head branch is not in the release repository itself")

        operator = (d.operator_login or "").casefold()
        if not operator:
            raise Refusal("the operator login is not configured")

        # The review, re-read by id.
        review = await self._get(f"/repos/{repo}/pulls/{pr}/reviews/{review_id}", "the review")
        if not isinstance(review, dict):
            raise Refusal("the review came back in an unexpected shape")
        user = review.get("user") or {}
        if _is_bot(user):
            raise Refusal("the review is not by a person")
        if str(user.get("login") or "").casefold() != operator:
            raise Refusal("the review is not by the operator")
        if str(review.get("state") or "") != "APPROVED":
            raise Refusal("the review is not an approval")
        if str(review.get("commit_id") or "") != head_sha:
            raise Refusal("the approval is not on the pull request's current head")
        if _starts_with_hold(review.get("body")):
            raise Refusal("the approving review asks to hold")
        approved_at = _parse_time(review.get("submitted_at"))

        # Later reviews by the operator; and the App's own approval on this head.
        reviews = await self._get_all(f"/repos/{repo}/pulls/{pr}/reviews", "the review list")
        app_login = ""
        try:
            app_login = (await d.app_bot_login() or "").casefold()
        except Exception:  # noqa: BLE001 — unreadable refuses below
            app_login = ""
        app_approved = False
        for r in reviews:
            rid = r.get("id")
            if not isinstance(rid, int):
                raise Refusal("a review in the list has no readable id")
            r_login = str((r.get("user") or {}).get("login") or "").casefold()
            r_state = str(r.get("state") or "")
            if r_login == operator and rid != review_id:
                if r_state == "PENDING":
                    continue
                later = rid > review_id or _parse_time(r.get("submitted_at")) > approved_at
                if later and r_state in ("CHANGES_REQUESTED", "DISMISSED"):
                    raise Refusal("the operator later requested changes or dismissed a review")
                if later and _starts_with_hold(r.get("body")):
                    raise Refusal("a later review by the operator asks to hold")
            if (
                app_login
                and r_login == app_login
                and r_state == "APPROVED"
                and str(r.get("commit_id") or "") == head_sha
            ):
                app_approved = True
        if not app_login:
            raise Refusal("the swarm App's login could not be read")
        if not app_approved:
            raise Refusal("the swarm App's panel approval is not on this head")

        # Later comments by the operator (conversation and inline).
        for path, what in (
            (f"/repos/{repo}/issues/{pr}/comments", "the comment list"),
            (f"/repos/{repo}/pulls/{pr}/comments", "the inline comment list"),
        ):
            for c in await self._get_all(path, what):
                if str((c.get("user") or {}).get("login") or "").casefold() != operator:
                    continue
                if _parse_time(c.get("created_at")) > approved_at and _starts_with_hold(c.get("body")):
                    raise Refusal("a later comment by the operator asks to hold")

        # Not behind the base branch.
        compare = await self._get(
            f"/repos/{repo}/compare/{d.release_branch}...{head_sha}", "the branch comparison"
        )
        behind = compare.get("behind_by") if isinstance(compare, dict) else None
        if not isinstance(behind, int) or isinstance(behind, bool):
            raise Refusal("how far the branch is behind could not be read")
        if behind != 0:
            raise Refusal(f"the release branch is behind {d.release_branch}")

        # Exactly one matching release record, pending approval, on this head.
        try:
            rows, complete = await d.release_results()
        except Exception as exc:
            raise Refusal(f"the release records could not be read ({exc.__class__.__name__})") from exc
        if not complete:
            raise Refusal("the release record list may be incomplete")
        pr_url = str(pr_data.get("html_url") or "")
        if not pr_url:
            raise Refusal("the pull request URL could not be read")
        matches = []
        for row in rows:
            if not isinstance(row, dict):
                raise Refusal("a release record came back in an unexpected shape")
            f = _fields(row)
            if _norm_version(f.get("version")) != version:
                continue
            if pr_url not in (str(f.get("rc_pr_url") or ""), str(f.get("release_url") or "")):
                continue
            matches.append(row)
        if len(matches) != 1:
            raise Refusal(f"{len(matches)} release records match {version} and this pull request, not exactly one")
        record = matches[0]
        f = _fields(record)
        entity_id = str(record.get("entity_id") or record.get("id") or "")
        if not entity_id:
            raise Refusal("the release record id could not be read")
        if str(f.get("status") or "") != "pending_approval":
            raise Refusal(f"the release record status is {f.get('status')!r}, not pending_approval")
        if str(f.get("rc_head_sha") or "") != head_sha:
            raise Refusal("the release record was prepared for a different head")

        # The panel gate approve_pr_as_app.py evaluates, on this head.
        try:
            passed, reason = await d.panel_gate(repo, pr, head_sha)
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise Refusal(f"the review panel gate could not be evaluated ({exc.__class__.__name__})") from exc
        if not passed:
            raise Refusal(f"the review panel gate did not pass ({reason})")

        return _Decision(version=version, entity_id=entity_id, review_id=review_id, head_sha=head_sha)

    # ── effects ─────────────────────────────────────────────────────────────

    async def _publish(self, repo: str, pr: int, decision: _Decision) -> None:
        d = self.deps
        version = decision.version
        lock = self._locks.setdefault(version, asyncio.Lock())
        if lock.locked():
            await self._refuse(repo, pr, f"a publish of {version} is already running")
            return
        async with lock:
            if version in self._started_versions or decision.review_id in self._seen_reviews:
                await self._refuse(repo, pr, f"a publish of {version} was already started once")
                return
            # Final re-read of the head, immediately before starting.
            try:
                fresh = await self._fetch_pr(repo, pr)
            except Refusal as exc:
                await self._refuse(repo, pr, str(exc))
                return
            if fresh.get("state") != "open" or str((fresh.get("head") or {}).get("sha") or "") != decision.head_sha:
                await self._refuse(repo, pr, "the pull request head changed before publishing")
                return
            if not d.publish_script or not os.path.exists(d.publish_script):
                await self._refuse(repo, pr, "the publish script is missing")
                return
            self._started_versions.add(version)
            self._seen_reviews.add(decision.review_id)
            argv = [
                d.python,
                d.publish_script,
                "--version",
                version,
                "--entity-id",
                decision.entity_id,
                "--from-github-review",
                str(decision.review_id),
            ]
            log.info(f"operator GitHub review approved release {version} on {repo}#{pr} — publishing")
            try:
                rc, last = await d.run_publish(argv)
            except TimeoutError:
                log.error(f"publish {version} timed out")
                self._safe_notify(
                    f"🔴 Release {version} publish TIMED OUT after a GitHub review "
                    "approval — it may be partially done; check the release log "
                    "and the registry before retrying."
                )
                return
            except Exception as exc:  # noqa: BLE001
                log.error(f"publish {version} error: {exc}")
                self._safe_notify(
                    f"🔴 Release {version} approved by GitHub review but publish "
                    f"errored: {exc}. Publish may not have run."
                )
                return
            if rc == 0:
                log.info(f"publish {version} succeeded: {last}")
            else:
                log.error(f"publish {version} FAILED rc={rc}: {last}")
                self._safe_notify(
                    f"🔴 Release {version} approved by GitHub review but publish "
                    f"FAILED (rc={rc}): {last[:300]}. See phoenicurus-release.log."
                )

    def _safe_notify(self, text: str) -> None:
        try:
            self.deps.notify(text)
        except Exception as exc:  # noqa: BLE001 — a notice never changes the outcome
            log.warning(f"release review notice could not be sent: {exc}")

    async def _refuse(self, repo: str, pr: int, reason: str) -> None:
        """Publish nothing; log, notify and comment once. Failures are logged only."""
        log.warning(f"release review on {repo}#{pr}: not publishing — {reason}")
        self._safe_notify(f"Release pull request {repo}#{pr} was not published: {reason}.")
        body = (
            f"{NOTICE_MARKER}\n"
            f"Not published: {reason}.\n\n"
            "Nothing was released. Once the condition is met, a new approving "
            "review on the current head is needed."
        )
        try:
            ok = await self.deps.post_comment(repo, pr, body)
            if not ok:
                log.warning(f"release review notice comment not posted on {repo}#{pr}")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"release review notice comment failed on {repo}#{pr}: {exc}")


# ── real dependencies, built from the dispatcher ────────────────────────────


def _publish_script_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "phoenicurus-release",
        "publish.py",
    )


def _load_publish_module():
    import importlib.util

    path = _publish_script_path()
    spec = importlib.util.spec_from_file_location("_phoenicurus_publish", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("publish module could not be loaded")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _release_results_blocking(limit: int = 1000) -> tuple[list[dict], bool]:
    """All release_result rows, and whether the server says the list is complete."""
    import json
    import urllib.request

    pub = _load_publish_module()
    body = json.dumps(
        {"entity_type": "release_result", "limit": limit, "include_snapshots": True}
    ).encode()
    req = urllib.request.Request(
        f"{pub._neotoma_base()}/entities/query",
        data=body,
        headers=pub._neotoma_headers(),
        method="POST",
    )
    req.add_header("User-Agent", pub.NEOTOMA_USER_AGENT)
    data = pub._neotoma_request_json(req, "query release_result")
    if not isinstance(data, dict):
        return [], False
    rows = data.get("entities")
    if not isinstance(rows, list):
        return [], False
    total = data.get("total")
    complete = data.get("has_more") is False or (isinstance(total, int) and total == len(rows))
    return rows, bool(complete) and len(rows) < limit


async def _panel_gate_real(repo: str, pr: int, head_sha: str) -> tuple[bool, str]:
    """Run approve_pr_as_app.run in dry-run mode; pass only on rc 0 for this head."""
    scripts = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "scripts",
    )
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import approve_pr_as_app

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = await approve_pr_as_app.run(
            repo,
            pr,
            [],
            apply=False,
            **({"panel_mode": "path"} if approve_pr_as_app._default_panel() == "path" else {}),
            panel_all=approve_pr_as_app._default_panel() == "all",
        )
    text = out.getvalue()
    if f"head_sha={head_sha}" not in text.splitlines():
        return False, "the gate evaluated a different head"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if rc != 0:
        refusing = next((ln for ln in lines if ln.startswith("refusing")), "")
        return False, refusing or "overall FAIL"
    return True, ""


def build_for_dispatcher(dispatcher: Any, sd: Any) -> ReleaseReviewApproval:
    """Wire the real GitHub, Neotoma, panel gate, notifier and publish runner.

    ``sd`` is the swarm_dispatch module (passed in to avoid a circular import).
    """
    import httpx

    async def github_get(path: str, params: dict) -> tuple[int, Any, dict]:
        url = f"https://api.github.com{path}"
        repo = "/".join(path.split("/")[2:4])
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params=params, headers=dispatcher._github_headers(repo))
            try:
                data = resp.json()
            except ValueError:
                data = None
            return resp.status_code, data, dict(resp.headers)

    async def release_results() -> tuple[list[dict], bool]:
        return await asyncio.to_thread(_release_results_blocking)

    async def app_bot_login() -> str:
        return await asyncio.to_thread(sd._app_bot_login_blocking)

    def notify(text: str) -> None:
        dispatcher.notifier.send(text, priority=sd.Priority.BLOCKER, handler=sd.DAEMON_NAME)

    async def post_comment(repo: str, pr: int, body: str) -> bool:
        # App identity only: no PAT fallback, never an empty token.
        token = await sd._ensure_app_write_token(repo)
        if not token:
            log.warning(f"no App token for {repo}; release review notice not posted")
            return False
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://api.github.com/repos/{repo}/issues/{pr}/comments",
                json={"body": body},
                headers=headers,
            )
            return 200 <= resp.status_code < 300

    async def run_publish(argv: list[str]) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=sd.PHOENICURUS_PUBLISH_TIMEOUT_S)
        tail = (out or b"").decode(errors="replace").strip().splitlines()
        rc = proc.returncode if proc.returncode is not None else -1
        return rc, (tail[-1] if tail else "")

    return ReleaseReviewApproval(
        Deps(
            github_get=github_get,
            release_results=release_results,
            panel_gate=_panel_gate_real,
            app_bot_login=app_bot_login,
            notify=notify,
            post_comment=post_comment,
            run_publish=run_publish,
            operator_login=sd._OPERATOR_LOGIN,
            release_repo=sd.PHOENICURUS_RELEASE_REPO,
            release_branch=sd.PHOENICURUS_RELEASE_BRANCH,
            publish_script=_publish_script_path(),
        )
    )


__all__ = [
    "FLAG_ENV",
    "Deps",
    "Refusal",
    "ReleaseReviewApproval",
    "build_for_dispatcher",
    "enabled",
    "is_release_repo",
]
