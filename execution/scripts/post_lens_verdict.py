#!/usr/bin/env python3
"""
execution/scripts/post_lens_verdict.py — post ONE lens verdict on ONE PR of ANY
repo, in exactly the form `approve_pr_as_app.py` reads.

A session that runs the bootstrap merge path by hand (docs/infrastructure/app_merge_path.md)
runs each lens as an independent subagent, then needs each verdict on the PR as
a comment the approval tool will accept: a head marker line
(`swarm_dispatch.compose_lens_review_marker`), the lens's attribution header,
its verdict line, the findings, posted by an identity the swarm admits as a lens
author (`lens_authors`). This tool does that, and nothing else. It needs no
daemon: no dispatcher, Apis or gate process is involved.

Usage:
    python3 execution/scripts/post_lens_verdict.py --repo <owner/name> --pr <n> \\
        --lens <pm|arch|ux|qa|security|...> --body-file <file> \\
        [--head <40-hex sha>] [--token-env NAME] [--dry-run]

The body file is the lens's reply: the attribution header line first, the
verdict line (`**SIGNED_OFF**`, `**BLOCKED**`, ...) second, then the findings.
The head marker is composed here from the PR's CURRENT head; a body that already
opens with that exact marker is accepted, one with any other marker is refused.

What it refuses, before posting anything:
  - `--head` given and different from the PR's current head (the verdict would
    be pinned to a head the PR is no longer at);
  - a closed or merged PR, an unreadable head, a lens it does not know;
  - a body the approval tool's reader cannot read: the verdict must sit at the
    fixed position (`swarm_dispatch.lens_own_verdict`);
  - a body that declares a sign-off but carries a blocking verdict token
    (BLOCKED, REQUEST_CHANGES, CHANGES_REQUESTED in upper case or bold, with any
    decoration) or a `[BLOCKING]` finding line ANYWHERE: the approval tool reads
    the whole body for those, so the sign-off would be read as blocking (known
    trap; the check calls the approval tool's own reader). Looser wording (the
    word "blocking" in prose, lower-case "blocked") is not read as blocking
    today, so it only draws a warning. Reword rather than quote the token;
  - a posting identity `lens_authors` would not admit as this lens's author on
    this host (the approval tool would then ignore the comment). Checked with
    the same resolver the approval tool uses, before posting.

Identity. By default the comment is posted AS THE SWARM GITHUB APP: an
installation token is minted in-process for the TARGET repository
(`lib.github_app_token`, the helper `approve_pr_as_app.py` uses), or as the
lens's own App when one is declared for that lens (`ATELES_APP_REVIEWER_<LENS>_*`),
exactly as the approval tool picks the identity that approves for that lens.
This works on a private repository the App is installed on, where an agent PAT
reads 404, and where the agent PAT has lost comment rights (403). An
installation token cannot call `GET /user`, so the App's login is read from the
App itself (`GET /app`), never from the token. `--token-env NAME` opts into the
PAT path instead (the account the token resolves to); `--as-app` is accepted
for clarity and is the default. A shared App can be the
verdict author and the approving reviewer on the same PR: the approval tool's
distinctness checks are about the PR's AUTHOR and the PR-opener App, not about
who commented (`approve_pr_as_app._check_approver_identities`), and a comment is
not a push, so "approval of the most recent reviewable push" is unaffected.
Only a PR the App itself opened is refused, as it should be.

Idempotent: one verdict comment per (identity, lens, head). A second post for
the same head edits that comment in place instead of adding a duplicate (the
contract `harness_lens_runner.post_verdict` follows). Two or more existing
matches are refused rather than guessed between. The comment is read back and
must match what was sent.

This tool never prints, logs, or writes a token or key. Exit 0: posted (or
dry run clear). Exit 1: refused or failed, nothing posted unless the output
says a post landed.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DAEMON_DIR = _REPO_ROOT / "execution" / "daemons" / "apis"
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import httpx  # noqa: E402

from lib import github_app_token as _github_app_token  # noqa: E402

import lens_authors  # noqa: E402
import swarm_dispatch  # noqa: E402

GITHUB_API = "https://api.github.com"
DEFAULT_TOKEN_ENV = "ATELES_AGENT_PAT"
MAX_BODY_BYTES = 256 * 1024
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MARKER_LINE_RE = re.compile(
    r"^<!--\s*review:(?P<lens>[a-z0-9_-]+)\s+commit=(?P<head>[0-9a-fA-F]{40})\s*-->$"
)
_LOOSE_BLOCKING_RE = re.compile(r"blocked|blocking|request[_ ]changes|changes[_ ]requested", re.I)


class Refusal(RuntimeError):
    """The verdict was not posted, and why."""


@dataclass(frozen=True)
class Identity:
    token: str
    login: str
    via: str  # human description; never contains the token


# ── identity ────────────────────────────────────────────────────────────────


def _app_prefix_for(lens: str) -> str:
    """The App env prefix that would speak for *lens*: the lens's own App when
    one is declared, else the swarm App. Mirrors `approve_pr_as_app.plan_approvals`
    (a declared-but-unusable or expected-but-missing lens App is a refusal,
    never a fallback to the shared App)."""
    identity = _github_app_token.reviewer_identity(lens)
    prefix = _github_app_token.reviewer_prefix(lens)
    if _github_app_token.identity_declared(identity):
        if not _github_app_token.identity_configured(identity):
            raise Refusal(
                f"the {lens} lens's App is declared ({prefix}_*) but is not usable (it "
                "needs an App id and a readable private key); not falling back to the "
                "shared App"
            )
        return prefix
    if lens in _github_app_token.expected_reviewer_lenses():
        raise Refusal(
            f"the {lens} lens is expected to have an App of its own "
            "(ATELES_EXPECTED_LENS_APPS) but this process has none of its "
            f"{prefix}_* variables; not posting in its place as the shared App"
        )
    return _github_app_token.DEFAULT_APP_ENV_PREFIX


def _resolve_app_identity(repo: str, lens: str) -> Identity:
    prefix = _app_prefix_for(lens)
    try:
        token = _github_app_token.installation_token(repo, prefix=prefix)
        login = _github_app_token.bot_login(prefix)
    except _github_app_token.AppTokenError as exc:
        raise Refusal(
            f"could not mint an installation token for {repo} ({exc}); is the App "
            "installed on that repository?"
        ) from None
    if not token or not login:
        raise Refusal(f"the App returned no usable token or login for {repo}")
    own = prefix != _github_app_token.DEFAULT_APP_ENV_PREFIX
    return Identity(token, login, f"the {lens} lens's own App" if own else "the swarm App")


def _resolve_pat_identity(client: httpx.Client, env_name: str) -> Identity:
    if not _ENV_NAME_RE.match(env_name):
        raise Refusal(f"--token-env {env_name!r} is not an environment variable name")
    token = (os.environ.get(env_name) or "").strip()
    if not token:
        raise Refusal(f"${env_name} is unset or empty; nothing to post as")
    resp = client.get(
        f"{GITHUB_API}/user",
        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"},
    )
    if resp.status_code >= 400:
        raise Refusal(f"${env_name} does not resolve to an account (HTTP {resp.status_code})")
    login = str((resp.json() or {}).get("login") or "")
    if not login:
        raise Refusal(f"${env_name} resolved to no login")
    return Identity(token, login, f"the account behind ${env_name}")


def _headers(identity: Identity) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {identity.token}",
    }


# ── the body ────────────────────────────────────────────────────────────────


def compose_body(body_text: str, *, lens: str, head: str) -> str:
    """The comment text: the head marker, then the lens's reply.

    A reply that already opens with the exact marker is accepted as is; one
    that opens with any other review marker (another lens, another head) is
    refused, since posting it would pin the verdict to the wrong thing.
    """
    marker = swarm_dispatch.compose_lens_review_marker(lens, head)
    lines = body_text.lstrip("\n").splitlines()
    rest = lines
    if lines:
        m = _MARKER_LINE_RE.match(lines[0].strip())
        if m:
            if m.group("lens") != lens or m.group("head").lower() != head:
                raise Refusal(
                    f"the body opens with a review marker for lens={m.group('lens')!r} "
                    f"commit={m.group('head').lower()[:12]}, not lens={lens!r} commit={head[:12]}"
                )
            rest = lines[1:]
    text = "\n".join(rest).strip("\n")
    if not text:
        raise Refusal("the body is empty")
    return f"{marker}\n{text}\n"


def check_readable(composed: str, *, lens: str) -> str:
    """Refuse a body the approval tool would misread; return the verdict token."""
    agent = _agent_for(lens)
    verdict = swarm_dispatch.lens_own_verdict(composed, lens_agent=agent)
    if verdict is None:
        position = swarm_dispatch.describe_gate_verdict_position(composed, lens_agent=agent)
        raise Refusal(
            "the approval tool cannot read a verdict at the fixed position "
            f"(line 1 the attribution header naming {agent}, line 2 the verdict). "
            f"Observed: {position}."
        )
    if verdict in swarm_dispatch.SIGN_OFF_CLEAR_VERDICTS and not swarm_dispatch.sign_off_is_warranted(
        composed, lens_agent=agent
    ):
        causes = []
        if swarm_dispatch.output_has_blocking_verdict(composed):
            causes.append(
                "a blocking verdict word (BLOCKED, BLOCKING, REQUEST_CHANGES or "
                "CHANGES_REQUESTED, in any case or decoration) appears in the body"
            )
        if swarm_dispatch.body_has_blocking_findings(composed):
            causes.append("a [BLOCKING] finding line appears in the body")
        raise Refusal(
            f"the declared verdict is {verdict.upper()} but the approval tool would read it as "
            "blocking: " + "; ".join(causes or ["a veto the reader applies"]) + ". "
            "Those words anywhere in a body read as blocking. Reword the text (for "
            "example 'an earlier objection', 'the previous round's finding') and post again."
        )
    if verdict in swarm_dispatch.SIGN_OFF_CLEAR_VERDICTS:
        loose = sorted({m.group(0).lower() for m in _LOOSE_BLOCKING_RE.finditer(composed)})
        if loose:
            print(
                "warning: the sign-off contains blocking-sounding wording "
                f"({', '.join(loose)}). The current reader still clears it, but a stricter "
                "reader or a human may not; reword if you can."
            )
    return verdict


def _agent_for(lens: str) -> str:
    try:
        import approve_pr_as_app  # noqa: PLC0415 — the one registry of lens -> agent
    except Exception:  # pragma: no cover
        approve_pr_as_app = None
    agents = getattr(approve_pr_as_app, "LENS_AGENTS", None)
    if agents is None:
        from review_panel import LENSES  # noqa: PLC0415

        agents = {item.lens: item.agent for item in LENSES}
    agent = agents.get(lens)
    if not agent:
        raise Refusal(f"unknown lens {lens!r} (known: {sorted(agents)})")
    return agent


def check_author_admitted(identity: Identity, composed: str) -> None:
    """Refuse unless the approval tool would read this comment, from this
    identity, as a lens verdict (same resolver, same per-lens App scope)."""
    try:
        authors = swarm_dispatch.lens_comment_authors()
    except Exception as exc:  # noqa: BLE001 — unreadable identities admit nobody
        raise Refusal(
            f"could not resolve the swarm's lens-comment identities ({exc.__class__.__name__})"
        ) from None
    probe = {"user": {"login": identity.login}, "body": composed}
    if not lens_authors.is_swarm_comment(probe, authors):
        raise Refusal(
            f"{identity.login} (via {identity.via}) is not an identity the approval tool "
            f"admits as this lens's author on this host, so it would ignore the comment. "
            f"Name the account in {lens_authors.ENV_AUTHORS}, or post as the swarm App "
            "(the default, without --token-env), then retry."
        )


# ── GitHub ──────────────────────────────────────────────────────────────────


def _fetch_pr(client: httpx.Client, repo: str, pr: int, identity: Identity) -> dict:
    resp = client.get(f"{GITHUB_API}/repos/{repo}/pulls/{pr}", headers=_headers(identity))
    if resp.status_code == 404:
        raise Refusal(
            f"{repo}#{pr} not found with {identity.via} (HTTP 404). On a private repository "
            "a PAT without access reads 404; post as the swarm App (the default) if it is installed there."
        )
    if resp.status_code >= 400:
        raise Refusal(f"could not read {repo}#{pr} (HTTP {resp.status_code})")
    return resp.json() or {}


def _comments(client: httpx.Client, repo: str, pr: int, identity: Identity) -> list[dict]:
    out: list[dict] = []
    page = 1
    while True:
        resp = client.get(
            f"{GITHUB_API}/repos/{repo}/issues/{pr}/comments",
            headers=_headers(identity),
            params={"per_page": 100, "page": page},
        )
        if resp.status_code >= 400:
            raise Refusal(f"could not list comments on {repo}#{pr} (HTTP {resp.status_code})")
        rows = resp.json() or []
        out.extend(rows)
        if len(rows) < 100:
            return out
        page += 1


def _existing_for_head(comments: list[dict], *, login: str, marker: str) -> dict | None:
    matches = [
        c
        for c in comments
        if str(((c.get("user") or {}).get("login")) or "").casefold() == login.casefold()
        and (str(c.get("body") or "").splitlines() or [""])[0].strip() == marker
    ]
    if len(matches) > 1:
        ids = ", ".join(str(c.get("id")) for c in matches)
        raise Refusal(
            f"{len(matches)} comments by {login} already carry {marker} ({ids}); "
            "refusing to choose one to edit"
        )
    return matches[0] if matches else None


def post_verdict(
    client: httpx.Client,
    *,
    repo: str,
    pr: int,
    lens: str,
    body_text: str,
    expected_head: str | None,
    identity: Identity,
    dry_run: bool = False,
) -> str:
    """Validate, then post (or edit in place) the lens verdict. Returns a result line."""
    pr_data = _fetch_pr(client, repo, pr, identity)
    state = str(pr_data.get("state") or "").lower()
    if state != "open" or pr_data.get("merged_at"):
        raise Refusal(f"{repo}#{pr} is not open (state={state!r}); a verdict on it is moot")
    live = swarm_dispatch._normalise_full_sha(str((pr_data.get("head") or {}).get("sha") or ""))
    if not live:
        raise Refusal("could not read the PR's current head SHA")
    if expected_head is not None:
        given = swarm_dispatch._normalise_full_sha(expected_head)
        if not given:
            raise Refusal(f"--head {expected_head!r} is not a full 40-hex commit SHA")
        if given != live:
            raise Refusal(
                f"the PR's head is {live[:12]}, not the {given[:12]} the review was run on; "
                "re-run the lens on the current head (nothing posted)"
            )
    composed = compose_body(body_text, lens=lens, head=live)
    if len(composed.encode("utf-8")) > 60000:
        raise Refusal("the body is larger than a GitHub comment allows (60000 bytes)")
    verdict = check_readable(composed, lens=lens)
    check_author_admitted(identity, composed)
    marker = swarm_dispatch.compose_lens_review_marker(lens, live)
    existing = _existing_for_head(_comments(client, repo, pr, identity), login=identity.login, marker=marker)
    action = "edit" if existing else "create"
    summary = f"lens={lens} head={live} verdict={verdict} as {identity.login} ({identity.via})"
    if dry_run:
        return f"dry run: would {action} a verdict comment: {summary}"

    if existing is None:
        resp = client.post(
            f"{GITHUB_API}/repos/{repo}/issues/{pr}/comments",
            headers=_headers(identity),
            json={"body": composed},
        )
    else:
        resp = client.patch(
            f"{GITHUB_API}/repos/{repo}/issues/comments/{existing.get('id')}",
            headers=_headers(identity),
            json={"body": composed},
        )
    if resp.status_code >= 400:
        raise Refusal(f"posting failed (HTTP {resp.status_code}); nothing is known to have landed")
    landed = resp.json() or {}
    comment_id = landed.get("id")
    # Read back; the write's own response is not proof it landed as sent.
    check = client.get(
        f"{GITHUB_API}/repos/{repo}/issues/comments/{comment_id}", headers=_headers(identity)
    )
    if check.status_code >= 400:
        raise Refusal(f"posted, but the read-back failed (HTTP {check.status_code}); verify by hand")
    row = check.json() or {}
    if (
        str(row.get("body") or "") != composed
        or str((row.get("user") or {}).get("login") or "").casefold() != identity.login.casefold()
    ):
        raise Refusal("posted, but the read-back does not match what was sent; verify by hand")
    return f"{'edited' if existing else 'posted'}: {row.get('html_url') or ''} ({summary})"


# ── CLI ─────────────────────────────────────────────────────────────────────


def _new_client() -> httpx.Client:
    return httpx.Client(timeout=30)


def run(args: argparse.Namespace) -> int:
    try:
        if not _REPO_RE.match(args.repo):
            raise Refusal(f"--repo {args.repo!r} is not owner/name")
        if args.pr < 1:
            raise Refusal("--pr must be a positive number")
        lens = args.lens.strip().lower()
        _agent_for(lens)
        body_path = Path(args.body_file)
        try:
            size = body_path.stat().st_size
            if size > MAX_BODY_BYTES:
                raise Refusal(f"--body-file is larger than {MAX_BODY_BYTES} bytes")
            body_text = body_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise Refusal(f"could not read --body-file ({exc.__class__.__name__})") from None
        except UnicodeDecodeError:
            raise Refusal("--body-file is not UTF-8") from None
        with _new_client() as client:
            if args.as_app and args.token_env:
                raise Refusal("--as-app and --token-env are alternatives; pass one")
            identity = (
                _resolve_pat_identity(client, args.token_env)
                if args.token_env
                else _resolve_app_identity(args.repo, lens)
            )
            line = post_verdict(
                client,
                repo=args.repo,
                pr=args.pr,
                lens=lens,
                body_text=body_text,
                expected_head=args.head,
                identity=identity,
                dry_run=args.dry_run,
            )
    except Refusal as exc:
        print(f"refusing: {exc}")
        return 1
    except httpx.HTTPError as exc:
        print(f"refusing: GitHub unreachable ({exc.__class__.__name__}); nothing is known to have landed")
        return 1
    print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Post one lens verdict on one PR of any repo, in the form approve_pr_as_app.py "
            "reads: the head marker for the PR's current head, then the lens's reply."
        )
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--lens", required=True, help="pm, arch, ux, qa, security, ...")
    parser.add_argument("--body-file", required=True, help="the lens's reply (header, verdict, findings)")
    parser.add_argument(
        "--head",
        default=None,
        help="the 40-hex head the lens reviewed; refuse if the PR is no longer at it",
    )
    parser.add_argument(
        "--token-env",
        default=None,
        help=(
            "post with the token in this environment variable instead of as the App "
            f"(for example {DEFAULT_TOKEN_ENV}); the default is the swarm App"
        ),
    )
    parser.add_argument(
        "--as-app",
        action="store_true",
        help=(
            "post as the swarm GitHub App (or the lens's own App) with an installation "
            "token minted for --repo. This is the default; it conflicts with --token-env"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run every check and say what would be posted; post nothing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
