#!/usr/bin/env python3
"""
render_plan_page.py — mirror a hand-authored HTML page onto a Neotoma
rendered_page entity, and verify by read-back that the served page changed.

Same contract as render_plan_docs.py, pointed at a rendered_page instead of a
repo doc: one source of truth, a --check mode for CI, and no hand-republishing.

    repo file                              →  rendered_page field
    ------------------------------------      -------------------
    docs/pages/<slug>.html                 →  html_body
    docs/pages/<slug>.css   (optional)     →  custom_css

WHY THE HTML LIVES IN THE REPO, NOT GENERATED FROM THE PLAN
-----------------------------------------------------------
The page is deliberately hand-shaped — problems-and-fixes cards, per-strand
tables, an honest-gap block. The plan's `next_steps` is free prose (~18k chars),
not structured data. Parsing it was measured, not assumed:

    all 4 STRAND headings parse, all 24 steps parse contiguously  ✓
    BUT steps 12-15 carry no "Blocked by:" clause at all          ✗

The hand-made page handles that correctly: its Strand B table simply has no
"Blocked by" column, because a human saw there was nothing to put in it. A
generic parser emits four empty cells and quietly looks broken. The format is
convention, enforced by nothing — one session writing "Blocked on:" or renaming
a strand degrades the page with no error.

So the repo file is canonical and a human edits it. This script removes the
*publishing* toil (the actual ask) without pretending prose is a schema.

If the plan later gains structured steps, generating this HTML becomes correct
and this script keeps working — only the source of the file changes.

HOW UPDATING ACTUALLY WORKS (verified live 2026-09-02)
-------------------------------------------------------
`publish_rendered_page` with an existing entity_id does NOT update content. It
returns success with `created: false` and a NEW access token while continuing to
serve the old bytes — it is a create-and-mint call, not an update call.

The update path is `POST /correct` on the page's fields, which the live page
picks up immediately. Verified end-to-end: a marker written via /correct raised
the served page from 13509 to 13568 bytes on the same URL and token.

Because that distinction is invisible in a success code, this script NEVER
trusts the write response. It re-fetches the live page over HTTP and asserts the
new content is actually being served, exiting non-zero if not.

Usage:
    render_plan_page.py                # repo file → Neotoma, then verify
    render_plan_page.py --check        # exit 1 if the served page is stale
    render_plan_page.py --pull         # Neotoma → repo file (recover an
                                       # out-of-band edit made elsewhere)

Env: NEOTOMA_BASE_URL, NEOTOMA_BEARER_TOKEN (falls back to
~/.config/neotoma/.env), ATELES_PLAN_PAGE_ID to override the target page.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# The page this script maintains. Keyed by slug so a second page is one entry,
# not a fork of this file.
PAGES: dict[str, dict[str, str]] = {
    "unblock-swarm-throughput": {
        "entity_id": "ent_d6a9e133e1da93c009ac4b76",
        "html": "docs/pages/unblock-swarm-throughput.html",
        "css": "docs/pages/unblock-swarm-throughput.css",
    },
}

USER_AGENT = "ateles-plan-page-sync/1.0"


def _load_env() -> tuple[str, str]:
    base_url = os.environ.get("NEOTOMA_BASE_URL", "")
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    env_path = Path.home() / ".config" / "neotoma" / ".env"
    if (not base_url or not token) and env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            if key == "NEOTOMA_BASE_URL" and not base_url:
                base_url = value
            elif key == "NEOTOMA_BEARER_TOKEN" and not token:
                token = value
    if not base_url:
        sys.exit("NEOTOMA_BASE_URL not set (env or ~/.config/neotoma/.env)")
    if not token:
        sys.exit("NEOTOMA_BEARER_TOKEN not set (env or ~/.config/neotoma/.env)")
    return base_url.rstrip("/"), token


def _request(url: str, token: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(url)
    # Cloudflare 1010-blocks urllib's default UA against the hosted instance.
    req.add_header("User-Agent", USER_AGENT)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(payload).encode()
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _fetch_page_fields(base_url: str, token: str, page_id: str) -> dict[str, str]:
    entity = _request(f"{base_url}/entities/{page_id}", token)
    snapshot = entity.get("snapshot", entity)
    if isinstance(snapshot.get("snapshot"), dict):
        snapshot = snapshot["snapshot"]
    return {
        "html_body": snapshot.get("html_body") or "",
        "custom_css": snapshot.get("custom_css") or "",
    }


def _fetch_served_html(base_url: str, token: str, page_id: str) -> str:
    """Fetch the page through the same /html route a viewer hits.

    This is the only check that proves a write reached the served page. The
    snapshot can be correct while the rendered route serves something else,
    which is precisely the failure this script exists to catch.
    """
    req = urllib.request.Request(f"{base_url}/entities/{page_id}/html")
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode()


def _local(page: dict[str, str]) -> dict[str, str]:
    html_path = REPO_ROOT / page["html"]
    if not html_path.exists():
        sys.exit(f"missing source file: {page['html']}")
    out = {"html_body": html_path.read_text()}
    css_path = REPO_ROOT / page["css"]
    if css_path.exists():
        out["custom_css"] = css_path.read_text()
    return out


def _fingerprint(html: str) -> str:
    """A short content fingerprint embedded in the page and asserted after write.

    Byte-length alone is a weak signal: an edit that swaps equal-length text
    leaves it unchanged. Comparing full text against the served page is not
    possible either, since the server wraps html_body in its own template. A
    marker derived from the content gives an exact, template-independent check.
    """
    return hashlib.sha256(html.encode()).hexdigest()[:16]


MARKER_RE = re.compile(r"<!-- page-content-hash: ([0-9a-f]{16}) -->")


def _with_marker(html: str) -> tuple[str, str]:
    """Strip any previous marker, then append one for the current content."""
    stripped = MARKER_RE.sub("", html).rstrip() + "\n"
    digest = _fingerprint(stripped)
    return f"{stripped}<!-- page-content-hash: {digest} -->\n", digest


def _served_hash(served: str) -> str | None:
    match = MARKER_RE.search(served)
    return match.group(1) if match else None


def render(base_url: str, token: str, slug: str, page: dict[str, str]) -> int:
    local = _local(page)
    body, digest = _with_marker(local["html_body"])
    page_id = page["entity_id"]

    fields = {"html_body": body}
    if "custom_css" in local:
        fields["custom_css"] = local["custom_css"]

    for field, value in fields.items():
        key = f"render-plan-page-{slug}-{field}-{_fingerprint(value)}"
        _request(
            f"{base_url}/correct",
            token,
            {
                "entity_id": page_id,
                "entity_type": "rendered_page",
                "field": field,
                "value": value,
                "idempotency_key": key,
            },
        )
        print(f"corrected {field} ({len(value)} chars)")

    # The write "succeeded". That proves nothing — verify through the viewer's
    # own path before reporting success.
    served = _fetch_served_html(base_url, token, page_id)
    got = _served_hash(served)
    if got != digest:
        print(
            f"VERIFY FAILED: wrote {digest} but the live page serves "
            f"{got or '<no marker>'} ({len(served)} bytes).\n"
            f"  The correction reported success and the page did NOT change.\n"
            f"  Do not trust the write; investigate before assuming it landed.",
            file=sys.stderr,
        )
        return 1

    print(f"verified: live page serves {digest} ({len(served)} bytes)")
    return 0


def check(base_url: str, token: str, slug: str, page: dict[str, str]) -> int:
    local = _local(page)
    _, digest = _with_marker(local["html_body"])
    served = _fetch_served_html(base_url, token, page["entity_id"])
    got = _served_hash(served)
    if got == digest:
        print(f"{slug}: live page matches {page['html']} ({digest})")
        return 0
    print(
        f"PAGE CHECK FAILED: {slug} serves {got or '<no marker>'} but "
        f"{page['html']} hashes to {digest}.\n"
        f"  Run: execution/scripts/render_plan_page.py",
        file=sys.stderr,
    )
    return 1


def pull(base_url: str, token: str, slug: str, page: dict[str, str]) -> int:
    remote = _fetch_page_fields(base_url, token, page["entity_id"])
    if not remote["html_body"]:
        print(f"{slug}: html_body empty in Neotoma, file left untouched")
        return 1
    html_path = REPO_ROOT / page["html"]
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(MARKER_RE.sub("", remote["html_body"]).rstrip() + "\n")
    print(f"wrote {page['html']} ({len(remote['html_body'])} chars)")
    if remote["custom_css"]:
        css_path = REPO_ROOT / page["css"]
        css_path.write_text(remote["custom_css"])
        print(f"wrote {page['css']} ({len(remote['custom_css'])} chars)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="exit 1 if the served page is stale")
    mode.add_argument("--pull", action="store_true", help="Neotoma → repo file")
    parser.add_argument("--slug", help="only this page (default: all)")
    args = parser.parse_args()

    base_url, token = _load_env()
    override = os.environ.get("ATELES_PLAN_PAGE_ID")

    pages = PAGES
    if args.slug:
        if args.slug not in PAGES:
            sys.exit(f"unknown slug {args.slug!r}; known: {', '.join(PAGES)}")
        pages = {args.slug: PAGES[args.slug]}

    rc = 0
    for slug, page in pages.items():
        page = dict(page)
        if override and (args.slug or len(PAGES) == 1):
            page["entity_id"] = override
        try:
            if args.pull:
                rc |= pull(base_url, token, slug, page)
            elif args.check:
                rc |= check(base_url, token, slug, page)
            else:
                rc |= render(base_url, token, slug, page)
        except urllib.error.HTTPError as exc:
            print(f"{slug}: HTTP {exc.code} {exc.reason}", file=sys.stderr)
            rc = 1
        except urllib.error.URLError as exc:
            print(f"{slug}: network error: {exc.reason}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
