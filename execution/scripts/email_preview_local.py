#!/usr/bin/env python3
"""
Local email preview: render draft HTML in the default browser.

Use for quick layout/CSS checks. Rendering is browser-only; for accurate
Gmail/Outlook/Apple Mail preview, use email_preview_send.py to send a test
email and open it in each client.

Usage:
  python execution/scripts/email_preview_local.py path/to/draft.html
  python execution/scripts/email_preview_local.py path/to/draft.html --serve   # serve on localhost:8765
  echo '<html><body>Hello</body></html>' | python execution/scripts/email_preview_local.py -
"""

import argparse
import sys
import webbrowser
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent.parent


def main():
    parser = argparse.ArgumentParser(
        description="Preview email HTML locally in browser (or serve on localhost)"
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="-",
        help="Path to HTML file, or '-' for stdin",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Serve on http://127.0.0.1:8765 instead of opening a file (avoids file:// restrictions)",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open browser automatically (only write file or start server)",
    )
    args = parser.parse_args()

    if args.input == "-":
        html = sys.stdin.read()
        out_path = _repo_root / "tmp" / "email_preview.html"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")
    else:
        in_path = Path(args.input)
        if not in_path.is_absolute():
            in_path = _repo_root / in_path
        if not in_path.exists():
            print(f"Error: file not found: {in_path}", file=sys.stderr)
            sys.exit(1)
        html = in_path.read_text(encoding="utf-8")
        out_path = _repo_root / "tmp" / "email_preview.html"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")

    if args.serve:
        try:
            import http.server
            import socketserver

            class Handler(http.server.SimpleHTTPRequestHandler):
                def __init__(self, *a, **k):
                    super().__init__(*a, directory=str(out_path.parent), **k)

                def log_message(self, format, *args):
                    print(f"[serve] {args[0]}")

            with socketserver.TCPServer(("127.0.0.1", 8765), Handler) as httpd:
                url = "http://127.0.0.1:8765/email_preview.html"
                print(f"Serving at {url}")
                if not args.no_open:
                    webbrowser.open(url)
                httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
        return

    file_url = out_path.as_uri()
    print(f"Wrote {out_path}")
    if not args.no_open:
        webbrowser.open(file_url)


if __name__ == "__main__":
    main()
