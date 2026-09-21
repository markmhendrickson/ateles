#!/usr/bin/env python3
"""
preview_server.py — serve a built static site locally for review, with no
live domain and no deploy.

WHY THIS EXISTS: every product page in this workstream had been previewed
via a Neotoma `rendered_page` (a CSP-sandboxed guest-token URL) because that
was the only preview mechanism that existed before this generator. Per the
operator's 2026-09-21 correction, the site generator IS now the deploy path,
so the skills that used to end in a `rendered_page` should end in something
previewable through THIS mechanism instead — not a sandboxed iframe page
rebuilt as a real site later.

This script does the minimum: build the requested product's site (via
build_site.py, so the preview is always a fresh build — never stale output
reviewed by accident) and serve it with Python's stdlib http.server, bound to
0.0.0.0 so a phone on the same network can open it, not just localhost.

HARD REQUIREMENTS this script satisfies:
  - No live domain touched. ateles.co has no DNS records; neotoma.io serves
    an existing static build (a different, larger Vite/React/Playwright
    pipeline this generator does not replace) that must not be disturbed.
    This server only ever reads dist/site/<product>/ on the local machine.
  - Openable from a phone. Binding 0.0.0.0 (not 127.0.0.1) means a device on
    the same Wi-Fi can open http://<this-machine's-LAN-IP>:<port>/ directly —
    the script prints that URL, not just localhost, so it doesn't have to be
    worked out by hand.

WHAT THIS DOES NOT REPLACE: a Neotoma rendered_page still gives a
shareable, hosted guest-token URL that works over the public internet with
no repo checkout, no local server, and no network-reachability requirement —
useful for sharing outside a LAN. This local preview requires the reviewer
to be on the same network as the machine running it (or use a tunnel, which
this script does not set up). See build_site.py's PR description for that
tradeoff stated explicitly, so it is the operator's call whether to keep
draft-rendered-page's guest-link path around for that use case.

Usage:
    preview_server.py <product> [--port 8143]

Prefer running this through Claude Code's `preview_start` mechanism
(.claude/launch.json entry "site-preview") rather than invoking directly, so
the harness's Browser pane opens it automatically — see that file.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import socket
import sys
from pathlib import Path

GEN_DIR = Path(__file__).resolve().parent
REPO_ROOT = GEN_DIR.parents[2]

sys.path.insert(0, str(GEN_DIR))


def _lan_ip() -> str:
    """Best-effort LAN IP so the printed URL is one a phone can actually
    open. Falls back to 127.0.0.1 if no network route is available (e.g. an
    offline sandbox) -- a local-only URL is still correct, just not
    phone-reachable in that case."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "product",
        nargs="?",
        default="ateles",
        help="product to build and preview (default: ateles)",
    )
    parser.add_argument("--port", type=int, default=8143)
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="skip the fresh build and serve dist/site/ as-is",
    )
    args = parser.parse_args()

    if not args.no_build:
        import build_site

        out_dir = build_site.DEFAULT_OUT_DIR
        try:
            blockers = build_site.build(args.product, out_dir)
        except build_site.BuildBlocker as exc:
            print(f"BUILD BLOCKED — preview not started: {exc}")
            return 1
        if blockers:
            print("BUILD BLOCKED — preview not started")
            return 1

    site_dir = REPO_ROOT / "dist" / "site" / args.product
    if not site_dir.exists():
        print(f"nothing built for '{args.product}' at {site_dir}")
        return 1

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(site_dir)
    )
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", args.port), handler)  # noqa: S104 — deliberate LAN bind, see module docstring

    lan_ip = _lan_ip()
    print(f"serving {site_dir.relative_to(REPO_ROOT)} at:")
    print(f"  http://localhost:{args.port}/")
    print(
        f"  http://{lan_ip}:{args.port}/   <- open this on a phone on the same network"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
