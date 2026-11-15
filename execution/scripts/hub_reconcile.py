#!/usr/bin/env python3
r"""
hub_reconcile.py — add a card to a shared-materials hub by exact byte splice.

A hub is a `rendered_page` whose whole value is being the canonical index of a
page set. This script deliberately does not interpret the hub's arbitrary HTML.
The hub owner places one explicit marker where a card sequence ends:

    <!-- hub-cards:materials -->

The tool inserts one escaped card immediately before that exact marker and
verifies that the stored body equals the proposed body byte for byte.
It refuses to write when the page is already referenced anywhere in the hub.

    PAGE_GUEST_TOKEN=… \
    hub_reconcile.py --hub ent_… add --page ent_… \
        --marker materials \
        --title "…" --desc "…" --badge-class b-mark --badge-text "Recap" [--apply]

Global options (--hub) go BEFORE the subcommand.
`add` is a dry run unless --apply is passed.

After a write, `add` re-reads the stored hub body and requires exact string
equality. It deliberately does not fetch guest/share URLs; open the new card's
link as a guest before sharing.

Exit codes: 0 ok; 1 verification mismatch; 3 tool error (bad input, missing
credentials, HTTP failure, refused write).

Env: NEOTOMA_BASE_URL + NEOTOMA_BEARER_TOKEN (both, or neither; the fallback is
a complete pair from ~/.config/neotoma/<profile>.env, never a mix),
ATELES_HUB_ENTITY_ID, PAGE_GUEST_TOKEN (`add` only).
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Cloudflare 1010-blocks urllib's default User-Agent on hosted instances, so
# every request must carry an explicit one.
USER_AGENT = "ateles-hub-reconcile/1.0"

EXIT_OK, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 3

ENTITY_ID_RE = re.compile(r"^ent_[0-9a-f]{24}$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
BADGE_CLASS_RE = re.compile(r"^[a-z0-9-]+$")
MARKER_NAME_RE = re.compile(r"^[a-z0-9-]{1,64}$")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def fail(message: str):
    """Tool error: distinct from findings so callers can tell them apart."""
    print(message, file=sys.stderr)
    raise SystemExit(EXIT_ERROR)


class _SafeArgumentParser(argparse.ArgumentParser):
    """Reject invalid arguments without echoing argv or usage."""

    def error(self, _message):
        self.exit(2, "invalid arguments; run with --help\n")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: the default handler forwards Authorization to the target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _read_env_file(path: Path) -> dict:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _check_base_url(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname or ""
    if not host:
        fail("base URL has no host")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and host in LOOPBACK_HOSTS
    ):
        fail(
            "base URL must be https (http is allowed only for localhost, 127.0.0.1, ::1)"
        )
    return base_url.rstrip("/")


def _load_env(profile: str) -> tuple:
    """Resolve a base URL + bearer token as ONE pair from ONE provenance.

    The environment pair is used only when both halves are set. Otherwise a
    complete pair from a single profile file is used. Halves are never mixed,
    so a token is never sent to a host it was not issued for.
    """
    if not PROFILE_RE.match(profile):
        fail("profile must match [A-Za-z0-9_-]+")
    base_url = os.environ.get("NEOTOMA_BASE_URL", "")
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    if base_url and token:
        return _check_base_url(base_url), token
    for name in (f"{profile}.env", f"{profile}-token.env"):
        path = Path.home() / ".config" / "neotoma" / name
        if not path.exists():
            continue
        values = _read_env_file(path)
        f_base = values.get("NEOTOMA_BASE_URL", "")
        f_token = values.get("NEOTOMA_BEARER_TOKEN", "")
        if f_base and f_token:
            return _check_base_url(f_base), f_token
    fail(
        "no complete NEOTOMA_BASE_URL + NEOTOMA_BEARER_TOKEN pair found "
        "(set both in the environment, or use a profile file holding both)"
    )


def _request(url: str, token: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(payload).encode()
    with _OPENER.open(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def _fetch_snapshot(base_url: str, token: str, entity_id: str) -> dict:
    entity = _request(f"{base_url}/entities/{entity_id}", token)
    snapshot = entity.get("snapshot", entity)
    if isinstance(snapshot.get("snapshot"), dict):
        snapshot = snapshot["snapshot"]
    snapshot = dict(snapshot)
    if "entity_type" in entity:
        snapshot["entity_type"] = entity["entity_type"]
    if "entity_version" in entity:
        snapshot["entity_version"] = entity["entity_version"]
    return snapshot


def build_card(url: str, title: str, desc: str, badges: list[tuple[str, str]]) -> str:
    """Build a card matching the hub's existing markup contract exactly."""
    esc = html.escape
    for cls, _ in badges:
        if not BADGE_CLASS_RE.match(cls):
            raise ValueError(f"badge class must match [a-z0-9-]+: {cls!r}")
    row = "".join(
        f'<span class="badge {cls}">{esc(text, quote=True)}</span>'
        for cls, text in badges
    )
    return (
        f'\n<a class="card" href="{esc(url, quote=True)}">\n'
        f'<div class="row">{row}</div>\n'
        f'<div class="t">{esc(title, quote=True)}</div>\n'
        f'<div class="d">{esc(desc, quote=True)}</div></a>'
    )


def insert_card(body: str, marker_name: str, card: str) -> str:
    """Insert `card` immediately before the one exact named marker."""
    marker = f"<!-- hub-cards:{marker_name} -->"
    count = body.count(marker)
    if count != 1:
        fail(
            f"marker {marker_name} must appear exactly once in the hub body "
            f"(found {count}); add it by hand"
        )
    index = body.index(marker)
    return body[:index] + card + body[index:]


def cmd_add(args, base_url, token) -> int:
    """Read-modify-write one exact card splice and verify the stored bytes."""
    if not ENTITY_ID_RE.match(args.page):
        fail("--page must look like ent_ followed by 24 hex characters")
    if not MARKER_NAME_RE.match(args.marker):
        fail("--marker must match [a-z0-9-]{1,64}")
    if not args.page_token or not args.page_token.strip():
        fail("PAGE_GUEST_TOKEN must be non-empty")

    badges = []
    if args.badge_text:
        badges.append((args.badge_class or "b-mark", args.badge_text))
    if args.badge2_text:
        badges.append((args.badge2_class or "b-review", args.badge2_text))
    for cls, _ in badges:
        if not BADGE_CLASS_RE.match(cls):
            fail(f"badge class must match [a-z0-9-]+: {cls!r}")

    snapshot = _fetch_snapshot(base_url, token, args.hub)
    if snapshot.get("entity_type") != "rendered_page":
        fail(f"hub {args.hub} is not a rendered_page; refusing")
    entity_version = snapshot.get("entity_version")
    if not isinstance(entity_version, str) or not entity_version:
        fail("hub snapshot has no entity_version; refusing an unversioned write")
    before_body = snapshot.get("html_body", "")

    marker = f"<!-- hub-cards:{args.marker} -->"
    marker_count = before_body.count(marker)
    if marker_count != 1:
        fail(
            f"marker {args.marker} must appear exactly once in the hub body "
            f"(found {marker_count}); add it by hand"
        )

    quoted = urllib.parse.quote(args.page_token.strip(), safe="")
    href = f"{base_url}/entities/{args.page}/html?access_token={quoted}"
    probe = urllib.parse.unquote(html.unescape(before_body)).lower()
    if args.page.lower() in probe:
        fail(
            f"page {args.page} is already referenced in the hub; "
            "check or update that card by hand"
        )

    print(f"hub {args.hub}; marker {args.marker}; found {marker_count}")

    card = build_card(href, args.title, args.desc, badges)
    # The splice introduces only this tool's escaped card, whose href always
    # carries a token; exact equality proves that every other byte is unchanged.
    after_body = insert_card(before_body, args.marker, card)

    if not args.apply:
        print(f"dry run: would write {len(after_body)} chars; pass --apply to write")
        if args.dry_run:
            try:
                fd = os.open(
                    args.dry_run,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                    0o600,
                )
            except OSError as exc:
                if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1)):
                    fail(f"refusing --dry-run symlink target: {args.dry_run}")
                fail(f"could not open --dry-run path {args.dry_run}: {exc.strerror}")
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w") as handle:
                    fd = -1
                    handle.write(after_body)
            finally:
                if fd != -1:
                    os.close(fd)
            print(f"proposed body written to {args.dry_run}")
        return EXIT_OK

    # `correct` replaces the whole field, so a concurrent edit between our read
    # and this write would be lost. Re-read and refuse if the body moved.
    current = _fetch_snapshot(base_url, token, args.hub).get("html_body", "")
    if current != before_body:
        fail(
            "refusing to write: the hub body changed since it was read; re-run to merge"
        )

    digest = hashlib.sha256(after_body.encode()).hexdigest()[:16]
    try:
        _request(
            f"{base_url}/correct",
            token,
            {
                "entity_id": args.hub,
                "entity_type": "rendered_page",
                "field": "html_body",
                "value": after_body,
                "expected_version": entity_version,
                "idempotency_key": f"hub-add-card-{args.page}-{digest}",
                "priority": 1000,
            },
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            fail("hub changed, re-run")
        raise

    stored_body = _fetch_snapshot(base_url, token, args.hub).get("html_body", "")
    if stored_body != after_body:
        print("write not confirmed in the stored body")
        return EXIT_FINDINGS
    print(
        "stored hub body verified; the served page was not fetched by this tool, "
        "so open the new card's link as a guest before sharing"
    )
    return EXIT_OK


def main() -> int:
    ap = _SafeArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 ok; 1 verification mismatch; 3 tool error.",
    )
    # No client default: this repo is public, so instance names stay out of it.
    # Pass --profile <name> for ~/.config/neotoma/<name>.env, or set the env vars.
    ap.add_argument(
        "--profile",
        default=os.environ.get("ATELES_NEOTOMA_PROFILE", "neotoma"),
        help="~/.config/neotoma/<profile>.env (or set NEOTOMA_BASE_URL/_BEARER_TOKEN)",
    )
    ap.add_argument("--hub", default=os.environ.get("ATELES_HUB_ENTITY_ID", ""))
    sub = ap.add_subparsers(
        dest="cmd", required=True, parser_class=_SafeArgumentParser
    )

    add = sub.add_parser("add", help="add one card, verified")
    add.add_argument("--page", required=True)
    add.add_argument("--marker", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--desc", required=True)
    add.add_argument("--badge-class", default="b-mark")
    add.add_argument("--badge-text", default="")
    add.add_argument("--badge2-class", default="b-review")
    add.add_argument("--badge2-text", default="")
    add.add_argument(
        "--apply",
        action="store_true",
        help="actually write to the live hub (default is a dry run)",
    )
    add.add_argument(
        "--dry-run", metavar="PATH", help="also save the proposed body to PATH"
    )

    args = ap.parse_args()
    args.page_token = os.environ.get("PAGE_GUEST_TOKEN", "")
    if not args.hub:
        fail("--hub or ATELES_HUB_ENTITY_ID required")
    if not ENTITY_ID_RE.match(args.hub):
        fail("--hub must look like ent_ followed by 24 hex characters")
    base_url, token = _load_env(args.profile)

    try:
        return cmd_add(args, base_url, token)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            fail(f"HTTP {exc.code}: redirect refused (credentials are never forwarded)")
        if exc.code in (401, 403, 404):
            fail(
                f"HTTP {exc.code}: the credential cannot read the hub entity; "
                "use a credential for the shared-graph user"
            )
        fail(f"HTTP {exc.code}")


if __name__ == "__main__":
    raise SystemExit(main())
