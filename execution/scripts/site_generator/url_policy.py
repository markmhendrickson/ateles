"""Fail-closed URL policy for generated product sites.

Local media may only come from the generated ``/assets/`` tree. Navigation
may use root-relative paths, fragments, or HTTPS links. Everything else is
rejected before it can reach an HTML attribute.
"""

from __future__ import annotations

import unicodedata
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


def _has_control(value: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)


def local_asset_url(value: object) -> str | None:
    """Return a normalized local asset URL, or ``None`` when unsafe."""
    raw = str(value or "").strip()
    decoded = _decoded(raw)
    if not raw.startswith("/assets/") or not decoded.startswith("/assets/"):
        return None
    # A host may treat a backslash as a separator, and compatibility forms
    # (full-width dots and slashes) fold to ASCII under NFKC: judge the
    # traversal rule on the folded, backslash-normalised string, and refuse
    # control characters (including NUL) outright.
    folded = unicodedata.normalize("NFKC", decoded).replace("\\", "/")
    if _has_control(raw) or _has_control(decoded) or _has_control(folded):
        return None
    if ".." in PurePosixPath(folded).parts or "//" in folded or not folded.startswith("/assets/"):
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
    raw_parsed = urlsplit(raw)
    try:
        port = parsed.port
        raw_port = raw_parsed.port
    except ValueError:
        return None
    if (
        # The string returned is the RAW one, so the host that was validated
        # must be the host a browser will navigate to: a percent-encoded
        # netloc delimiter (%2f, %23, %40) makes the decoded and raw parses
        # disagree, which is refused rather than reconciled.
        raw_parsed.netloc != parsed.netloc
        or raw_parsed.hostname != parsed.hostname
        or raw_port != port
        or "%" in raw_parsed.netloc
        or "\\" in raw
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or any(c in decoded for c in ("\r", "\n", "\x00"))
    ):
        return None
    return raw
