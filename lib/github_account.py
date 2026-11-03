"""Named existing GitHub account selection for trusted control-side callers.

No login, refresh, account switch or ambient-token fallback. This module has
no daemon bootstrap, credential-file reader or import-time effects.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request

HOST = "github.com"
API = "https://api.github.com"
MAX_RESPONSE = 262144


class AccountReadError(RuntimeError):
    """Fixed nonsecret failure; provider output is never an error message."""


def validate_login(login: str) -> str:
    if (not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login)
            or login.lower() in {"operator", "unassigned", "none", "unknown"}):
        raise AccountReadError("an explicit existing operator account is required")
    return login


def control_environment() -> dict[str, str]:
    # Only host facilities needed for the existing account; never model/daemon
    # tokens, debug/header logging, proxy redirection or Python import overrides.
    return {k: v for k, v in os.environ.items()
            if k in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AccountReadError("the fixed GitHub read redirected")


def fixed_get(path: str, token: str):
    """Internal fixed-origin GET. Callers must supply an allowlisted path."""
    if not path.startswith("/") or path.startswith("//") or "#" in path:
        raise AccountReadError("invalid fixed GitHub read")
    url = API + path
    request = urllib.request.Request(url, method="GET", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ateles-control-policy-reader"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            if response.status != 200 or response.geturl() != url:
                raise AccountReadError("the fixed GitHub read was not confirmed")
            body = response.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE or response.headers.get("Link"):
                raise AccountReadError("the fixed GitHub read is incomplete or too large")
            return json.loads(body)
    except Exception:
        raise AccountReadError("the fixed GitHub read failed or is incomplete") from None


def verified_account_from_gh(login: str, *, register_secret=None) -> tuple[str, int]:
    """Extracted narrow named-account operation; only trusted callers get it.

    The policy-reader subprocess never returns this value to its parent. The
    existing rotation caller retains its in-memory/redaction contract.
    """
    validate_login(login)
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", HOST, "--user", login],
            capture_output=True, text=True, timeout=20, env=control_environment(), check=False)
        token = result.stdout.strip()
        if result.returncode != 0 or len(token) < 20 or any(c.isspace() for c in token):
            raise AccountReadError("the named existing GitHub account is unavailable")
        if register_secret is not None:
            register_secret(token)
        user = fixed_get("/user", token)
        if not isinstance(user, dict) or user.get("login") != login:
            raise AccountReadError("the named GitHub account identity did not match")
        principal_id = user.get("id")
        if isinstance(principal_id, bool) or not isinstance(principal_id, int) or principal_id <= 0:
            raise AccountReadError("the named GitHub account numeric identity is unavailable")
        return token, principal_id
    except AccountReadError:
        raise
    except Exception:
        raise AccountReadError("the named existing GitHub account could not be verified") from None


def github_token_from_gh(login: str, *, register_secret=None) -> str:
    """Preserve the existing caller's token-only/redaction return contract."""
    credential, _ = verified_account_from_gh(login, register_secret=register_secret)
    return credential
