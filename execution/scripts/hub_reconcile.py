#!/usr/bin/env python3
r"""
hub_reconcile.py — add hub cards and check rendered-page reachability.

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

Global options go before `add`; `check` also accepts --hub after its name.
`add` is a dry run unless --apply is passed.
The dry run prints only the hub id, page id, marker name, and proposed body
byte length; it never prints the card, body, or token-bearing href.

After a write, `add` re-reads the stored hub body and requires exact string
equality. It deliberately does not fetch guest/share URLs; open the new card's
link as a guest before sharing.

    hub_reconcile.py check --hub ent_… [--exempt ent_… ...]

`check` lists rendered pages and follows hub cards and their page links up to
two clicks from the hub. It prints only entity IDs and counts, never titles.

Exit codes: 0 ok; 1 findings; 2 inconclusive enumeration or reachability;
3 tool error (bad input, missing credentials, HTTP failure, refused write).

Env: NEOTOMA_BASE_URL + NEOTOMA_BEARER_TOKEN (both, or neither; the fallback is
a complete pair from ~/.config/neotoma/<profile>.env, never a mix),
ATELES_HUB_ENTITY_ID, PAGE_GUEST_TOKEN (`add` only).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

# Cloudflare 1010-blocks urllib's default User-Agent on hosted instances, so
# every request must carry an explicit one.
USER_AGENT = "ateles-hub-reconcile/1.0"

EXIT_OK, EXIT_FINDINGS, EXIT_INCONCLUSIVE, EXIT_ERROR = 0, 1, 2, 3

ENTITY_ID_RE = re.compile(r"ent_[0-9a-f]{24}")
RENDERED_PAGE_PATH_RE = re.compile(r"/entities/(ent_[0-9a-f]{24})/html/?")
PROFILE_RE = re.compile(r"[A-Za-z0-9_-]+")
BADGE_CLASS_RE = re.compile(r"[a-z0-9-]+")
MARKER_NAME_RE = re.compile(r"[a-z0-9-]{1,64}")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
MAX_LIST_PAGES = 100
BASE_URL_INPUT_ERROR = (
    "NEOTOMA_BASE_URL must not contain credentials, a query, "
    "a fragment or control characters"
)


def fail(message: str):
    """Tool error: distinct from findings so callers can tell them apart."""
    print(message, file=sys.stderr)
    raise SystemExit(EXIT_ERROR)


class _SafeArgumentParser(argparse.ArgumentParser):
    """Reject invalid arguments without echoing argv or usage."""

    def error(self, _message):
        self.exit(EXIT_ERROR, "invalid arguments; run with --help\n")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: the default handler forwards Authorization to the target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirectHandler
)


def _read_env_file(path: Path) -> dict:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _contains_non_printable_ascii(value: str) -> bool:
    return any(not 0x21 <= ord(char) <= 0x7E for char in value)


def _contains_unsafe_free_text(value: str) -> bool:
    return any(
        category.startswith("C") or category in {"Zl", "Zp"}
        for category in (unicodedata.category(char) for char in value)
    )


def _check_token(variable_name: str, token: str) -> str:
    if _contains_non_printable_ascii(token):
        fail(f"{variable_name} must not contain control characters or whitespace")
    return token


def _check_base_url(base_url: str) -> str:
    if _contains_non_printable_ascii(base_url):
        fail(BASE_URL_INPUT_ERROR)
    parsed = urllib.parse.urlparse(base_url)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        fail(BASE_URL_INPUT_ERROR)
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
    if not PROFILE_RE.fullmatch(profile):
        fail("profile must match [A-Za-z0-9_-]+")
    base_url = os.environ.get("NEOTOMA_BASE_URL", "")
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    if base_url and token:
        return _check_base_url(base_url), _check_token("NEOTOMA_BEARER_TOKEN", token)
    for name in (f"{profile}.env", f"{profile}-token.env"):
        path = Path.home() / ".config" / "neotoma" / name
        if not path.exists():
            continue
        values = _read_env_file(path)
        f_base = values.get("NEOTOMA_BASE_URL", "")
        f_token = values.get("NEOTOMA_BEARER_TOKEN", "")
        if f_base and f_token:
            return _check_base_url(f_base), _check_token(
                "NEOTOMA_BEARER_TOKEN", f_token
            )
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
    if not isinstance(entity, dict):
        raise ValueError("unreadable snapshot")
    snapshot = entity.get("snapshot", entity)
    if not isinstance(snapshot, dict):
        raise ValueError("unreadable snapshot")
    if isinstance(snapshot.get("snapshot"), dict):
        snapshot = snapshot["snapshot"]
    snapshot = dict(snapshot)
    if "entity_type" in entity:
        snapshot["entity_type"] = entity["entity_type"]
    if "entity_version" in entity:
        snapshot["entity_version"] = entity["entity_version"]
    return snapshot


def _decode_probe(value: str) -> str | None:
    """Apply the same bounded, browser-equivalent decoding as add's reference probe."""
    probe = value
    for _ in range(8):
        normalized = urllib.parse.unquote(html.unescape(probe)).translate(
            {ord(char): None for char in "\t\n\r"}
        )
        if normalized == probe:
            return probe.lower()
        probe = normalized
    return None


# A link counts only when it is unambiguously a live link in a canonical form.
# Anything ambiguous is NOT counted, so the page is reported as an orphan.
# A false orphan is safe (a person looks and adds a card); a false OK is not.
class _PageLinks(HTMLParser):
    _VOID_ELEMENTS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
    _INERT_ELEMENTS = {
        "template",
        "noscript",
        "script",
        "style",
        "iframe",
        "object",
        "svg",
        "math",
    }

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        parsed = urllib.parse.urlsplit(base_url)
        authorities = {parsed.netloc}
        default_port = 443 if parsed.scheme == "https" else 80
        if parsed.port in (None, default_port):
            authorities.add(f"{parsed.hostname}:{default_port}")
            authorities.add(parsed.hostname)
        origins = {""} | {
            origin
            for authority in authorities
            for origin in (f"{parsed.scheme}://{authority}", f"//{authority}")
        }
        origin_pattern = "|".join(
            re.escape(origin) for origin in sorted(origins, reverse=True)
        )
        self.href_pattern = re.compile(
            rf"(?:{origin_pattern})/entities/({ENTITY_ID_RE.pattern})/html/?"
            r"(?:\?access_token=[A-Za-z0-9-]+)?"
        )
        self.links: set[str] = set()
        self.cards: set[str] = set()
        self.open_elements: list[tuple[str, bool]] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        inert = (
            tag in self._INERT_ELEMENTS
            or "hidden" in attributes
            or "inert" in attributes
            or attributes.get("aria-hidden") == "true"
        )
        if (
            tag == "a"
            and not inert
            and not any(ancestor_inert for _, ancestor_inert in self.open_elements)
        ):
            hrefs = [value for name, value in attrs if name == "href"]
            if len(hrefs) == 1 and isinstance(hrefs[0], str):
                match = self.href_pattern.fullmatch(hrefs[0])
                if match:
                    entity_id = match.group(1)
                    self.links.add(entity_id)
                    classes = attributes.get("class") or ""
                    if "card" in re.split(r"[ \t\n\f\r]+", classes):
                        self.cards.add(entity_id)
        if tag not in self._VOID_ELEMENTS:
            self.open_elements.append((tag, inert))

    def handle_endtag(self, tag):
        for index in range(len(self.open_elements) - 1, -1, -1):
            if self.open_elements[index][0] == tag:
                del self.open_elements[index:]
                break

    def handle_startendtag(self, tag, attrs):
        # HTML ignores the slash on non-void elements, including <template/>.
        self.handle_starttag(tag, attrs)


def _page_links(body: str, base_url: str) -> _PageLinks:
    links = _PageLinks(base_url)
    links.feed(body)
    return links


def _list_rendered_pages(base_url: str, token: str) -> tuple[set[str], bool]:
    """Return listed IDs and whether the listing is proven complete."""
    found: set[str] = set()
    visited_cursors: set[str] = set()
    cursor = ""
    expected_total = None
    for _ in range(MAX_LIST_PAGES):
        url = f"{base_url}/entities?entity_type=rendered_page&limit=200"
        if cursor:
            url += f"&cursor={urllib.parse.quote(cursor, safe='')}"
        try:
            data = _request(url, token)
        except (urllib.error.URLError, json.JSONDecodeError, UnicodeError, ValueError):
            return found, False
        if not isinstance(data, dict):
            return found, False
        if "truncated" in data and data["truncated"] is not False:
            return found, False
        entities = data.get("entities")
        total = data.get("total")
        next_cursor = data.get("next_cursor")
        if (
            not isinstance(entities, list)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total < 0
            or next_cursor is not None
            and not isinstance(next_cursor, str)
        ):
            return found, False
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            return found, False
        previous_count = len(found)
        for entity in entities:
            if not isinstance(entity, dict):
                return found, False
            entity_id = entity.get("entity_id")
            if not isinstance(entity_id, str) or not ENTITY_ID_RE.fullmatch(entity_id):
                return found, False
            found.add(entity_id)
        if len(found) > total:
            return found, False
        if len(found) == previous_count:
            return found, False
        if not next_cursor:
            return found, len(found) == total
        if next_cursor in visited_cursors or next_cursor == cursor or not entities:
            return found, False
        visited_cursors.add(next_cursor)
        cursor = next_cursor
    return found, False


def cmd_check(args, base_url, token) -> int:
    """Report pages unreachable from hub cards within two clicks."""
    if not ENTITY_ID_RE.fullmatch(args.hub):
        fail("--hub must look like ent_ followed by 24 hex characters")
    if any(not ENTITY_ID_RE.fullmatch(entity_id) for entity_id in args.exempt):
        fail("--exempt IDs must look like ent_ followed by 24 hex characters")
    exempt = set(args.exempt)
    pages, complete = _list_rendered_pages(base_url, token)
    if not complete or args.hub not in pages:
        print(
            f"checked={len(pages)} orphans=0 exempt={len(exempt)} result=INCONCLUSIVE"
        )
        return EXIT_INCONCLUSIVE

    try:
        hub = _fetch_snapshot(base_url, token, args.hub)
        if not isinstance(hub, dict):
            raise ValueError("unreadable page")
        if hub.get("entity_type") != "rendered_page":
            fail("hub is not a readable rendered_page; refusing")
        if not isinstance(hub.get("html_body"), str):
            raise ValueError("unreadable page")
        carded = _page_links(hub["html_body"], base_url).cards & pages
        reachable = {args.hub} | carded
        for entity_id in sorted(carded):
            snapshot = _fetch_snapshot(base_url, token, entity_id)
            if not isinstance(snapshot, dict):
                raise ValueError("unreadable page")
            if snapshot.get("entity_type") != "rendered_page" or not isinstance(
                snapshot.get("html_body"), str
            ):
                raise ValueError("unreadable page")
            reachable.update(_page_links(snapshot["html_body"], base_url).links & pages)
    except (
        urllib.error.URLError,
        json.JSONDecodeError,
        UnicodeError,
        TypeError,
        ValueError,
    ):
        print(
            f"checked={len(pages)} orphans=0 exempt={len(exempt)} result=INCONCLUSIVE"
        )
        return EXIT_INCONCLUSIVE

    orphans = sorted(pages - reachable - exempt)
    for entity_id in orphans:
        print(f"orphan {entity_id}")
    result = "FINDINGS" if orphans else "OK"
    print(
        f"checked={len(pages)} orphans={len(orphans)} exempt={len(exempt)} result={result}"
    )
    return EXIT_FINDINGS if orphans else EXIT_OK


def build_card(url: str, title: str, desc: str, badges: list[tuple[str, str]]) -> str:
    """Build a card matching the hub's existing markup contract exactly."""
    esc = html.escape
    for cls, _ in badges:
        if not BADGE_CLASS_RE.fullmatch(cls):
            raise ValueError("badge class must match [a-z0-9-]+")
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
    if not MARKER_NAME_RE.fullmatch(marker_name):
        fail("marker name must match [a-z0-9-]{1,64}")
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
    if not ENTITY_ID_RE.fullmatch(args.hub):
        fail("--hub must look like ent_ followed by 24 hex characters")
    if _contains_unsafe_free_text(args.title):
        fail("--title must not contain control or format characters")
    if _contains_unsafe_free_text(args.desc):
        fail("--desc must not contain control or format characters")
    if _contains_unsafe_free_text(args.badge_text):
        fail("--badge-text must not contain control or format characters")
    if _contains_unsafe_free_text(args.badge2_text):
        fail("--badge2-text must not contain control or format characters")
    for cls in (args.badge_class, args.badge2_class):
        if cls and not BADGE_CLASS_RE.fullmatch(cls):
            fail("badge class must match [a-z0-9-]+")
    if not args.title.strip():
        fail("--title must not be blank")
    if not ENTITY_ID_RE.fullmatch(args.page):
        fail("--page must look like ent_ followed by 24 hex characters")
    if not MARKER_NAME_RE.fullmatch(args.marker):
        fail("--marker must match [a-z0-9-]{1,64}")
    if not args.page_token:
        fail("PAGE_GUEST_TOKEN must be non-empty")
    _check_token("PAGE_GUEST_TOKEN", args.page_token)

    badges = []
    if args.badge_text:
        badges.append((args.badge_class or "b-mark", args.badge_text))
    if args.badge2_text:
        badges.append((args.badge2_class or "b-review", args.badge2_text))

    try:
        page_snapshot = _fetch_snapshot(base_url, token, args.page)
    except (
        urllib.error.URLError,
        json.JSONDecodeError,
        UnicodeError,
        TypeError,
        ValueError,
    ):
        fail(f"page {args.page} is not a readable rendered_page; refusing")
    if page_snapshot.get("entity_type") != "rendered_page":
        fail(f"page {args.page} is not a readable rendered_page; refusing")

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

    quoted = urllib.parse.quote(args.page_token, safe="")
    href = f"{base_url}/entities/{args.page}/html?access_token={quoted}"
    probe = _decode_probe(before_body)
    if probe is None:
        fail("hub body has unresolvable encoding; refusing")
    if args.page.lower() in probe:
        fail(
            f"page {args.page} is already referenced in the hub; "
            "check or update that card by hand"
        )

    card = build_card(href, args.title, args.desc, badges)
    # The splice introduces only this tool's escaped card, whose href always
    # carries a token; exact equality proves that every other byte is unchanged.
    after_body = insert_card(before_body, args.marker, card)

    if not args.apply:
        print(
            f"dry run: hub={args.hub} page={args.page} marker={args.marker} "
            f"body_bytes={len(after_body.encode())}"
        )
        return EXIT_OK

    print(f"hub {args.hub}; marker {args.marker}; found {marker_count}")

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


def _run() -> int:
    ap = _SafeArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 ok; 1 findings; 2 inconclusive; 3 tool error.",
    )
    # No client default: this repo is public, so instance names stay out of it.
    # Pass --profile <name> for ~/.config/neotoma/<name>.env, or set the env vars.
    ap.add_argument(
        "--profile",
        default=os.environ.get("ATELES_NEOTOMA_PROFILE", "neotoma"),
        help="~/.config/neotoma/<profile>.env (or set NEOTOMA_BASE_URL/_BEARER_TOKEN)",
    )
    ap.add_argument("--hub", default=os.environ.get("ATELES_HUB_ENTITY_ID", ""))
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=_SafeArgumentParser)

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
    check = sub.add_parser("check", help="report pages unreachable from hub cards")
    check.add_argument("--hub", default=argparse.SUPPRESS)
    check.add_argument("--exempt", nargs="+", default=[], metavar="ENTITY_ID")

    args = ap.parse_args()
    if args.cmd == "add":
        args.page_token = os.environ.get("PAGE_GUEST_TOKEN", "")
    if not args.hub:
        fail("--hub or ATELES_HUB_ENTITY_ID required")
    if not ENTITY_ID_RE.fullmatch(args.hub):
        fail("--hub must look like ent_ followed by 24 hex characters")
    if args.cmd == "check" and any(
        not ENTITY_ID_RE.fullmatch(entity_id) for entity_id in args.exempt
    ):
        fail("--exempt IDs must look like ent_ followed by 24 hex characters")
    base_url, token = _load_env(args.profile)

    try:
        if args.cmd == "check":
            return cmd_check(args, base_url, token)
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


def main() -> int:
    try:
        return _run()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"unexpected error: {type(exc).__name__}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
