"""Fail-closed URL policy for generated product sites.

Local media may only come from the generated ``/assets/`` tree. Navigation
may use root-relative paths, fragments, or HTTPS links. Everything else is
rejected before it can reach an HTML attribute.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit


def _decoded(value: str) -> str:
    decoded = value
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def local_asset_url(value: object) -> str | None:
    """Return a normalized local asset URL, or ``None`` when unsafe."""
    raw = str(value or "").strip()
    decoded = _decoded(raw)
    if not raw.startswith("/assets/") or not decoded.startswith("/assets/"):
        return None
    parsed = urlsplit(decoded)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    path = PurePosixPath(parsed.path)
    if ".." in path.parts or path.name in {"", ".", ".."} or "//" in decoded:
        return None
    return raw


def public_href(value: object) -> str | None:
    """Return a safe public navigation URL, or ``None`` when unsafe."""
    raw = str(value or "").strip()
    if not raw:
        return None
    decoded = _decoded(raw)
    if decoded.startswith("#"):
        return raw if decoded[1:] and not any(c.isspace() for c in decoded) else None
    if decoded.startswith("/"):
        parsed = urlsplit(decoded)
        path = PurePosixPath(parsed.path)
        if (
            parsed.scheme
            or parsed.netloc
            or ".." in path.parts
            or "//" in decoded
            or any(c in decoded for c in ("\r", "\n", "\x00"))
        ):
            return None
        return raw
    parsed = urlsplit(decoded)
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or any(c in decoded for c in ("\r", "\n", "\x00"))
    ):
        return None
    return raw
