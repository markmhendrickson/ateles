"""One Neotoma REST helper for the capability client.

Replaces three hand-rolled ``urllib`` calls. Rules:

* No hardcoded host. ``NEOTOMA_BASE_URL`` must be set in the calling process
  (fork test: another operator supplies their own instance). Unset refuses.
* The bearer token comes from ``NEOTOMA_BEARER_TOKEN`` in the calling process.
  It is never printed or placed in an error message.
* Redirects are NEVER followed: a redirect would forward the Authorization
  header to a host the operator did not configure.
* Cloudflare 1010-blocks urllib's default User-Agent, so the client names itself.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

USER_AGENT = "ateles-capabilities/1.0"


class NeotomaConfigError(RuntimeError):
    """The calling process lacks the Neotoma base URL or token."""


class NeotomaRequestError(RuntimeError):
    """The store answered with an HTTP error (body text is server-provided)."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any):
        return None


def base_url() -> str:
    url = os.environ.get("NEOTOMA_BASE_URL", "").strip().rstrip("/")
    if not url:
        raise NeotomaConfigError("NEOTOMA_BASE_URL is not set in this process")
    if not url.startswith("https://") and not url.startswith("http://localhost"):
        raise NeotomaConfigError("NEOTOMA_BASE_URL must be an https URL")
    return url


def bearer_token() -> str:
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "").strip()
    if not token:
        raise NeotomaConfigError("NEOTOMA_BEARER_TOKEN is not set in this process")
    return token


def request_json(
    method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float = 15.0
) -> dict[str, Any]:
    url = base_url() + path
    token = bearer_token()
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as resp:  # noqa: S310
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        text = (exc.read() or b"")[:600].decode("utf-8", "replace")
        raise NeotomaRequestError(exc.code, text.replace(token, "[REDACTED]")) from None
