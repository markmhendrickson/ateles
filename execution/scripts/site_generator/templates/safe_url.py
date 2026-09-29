"""safe_url.py — the single href gate for the site generator.

Every link the generator emits (markdown links, page_specific CTAs, sibling
links) goes through `safe_href`, so there is one place that decides what a
link may point at. Fail closed: an absolute URL whose scheme is not on the
allowlist is replaced by an inert `#`, never passed through.

Allowed: http, https, mailto, and scheme-less references (relative paths,
`/root-relative`, `#fragment`, `?query`). Everything else, including
`javascript:`, `data:`, `vbscript:` and `file:`, is rejected regardless of
case, embedded whitespace/control characters, or HTML character references
that a browser would decode into the scheme.
"""

from __future__ import annotations

import html
import re

ALLOWED_SCHEMES = frozenset({"http", "https", "mailto"})
INERT_HREF = "#"

_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
# Whitespace, C0/C1 controls and zero-width characters that browsers strip
# from a URL before parsing its scheme.
_STRIPPED = re.compile(r"[\x00-\x20\x7f-\x9f\u00ad\u200b-\u200f\u2028\u2029\u2060\ufeff]+")


def safe_href(href: object) -> str:
    """Return `href` unchanged if it is safe to place in an `href=` attribute,
    otherwise `#`. The result is NOT HTML-escaped; callers escape it."""
    if not isinstance(href, str):
        return INERT_HREF
    probe = href
    for _ in range(3):  # decode character references that a browser would
        decoded = html.unescape(probe)
        if decoded == probe:
            break
        probe = decoded
    probe = _STRIPPED.sub("", probe)
    match = _SCHEME.match(probe)
    if match and match.group(1).lower() not in ALLOWED_SCHEMES:
        return INERT_HREF
    return href
