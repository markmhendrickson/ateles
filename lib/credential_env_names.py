"""Which environment-variable names carry a credential, defined once.

Several places must agree on this: the environment allowlist
(``lib/environment_allowlist.py``, which never lets a credential-named name
pass even when it is declared), the root ``conftest.py`` and
``execution/scripts/render_daemon_plist.py`` (which drops these by name). A copy in each would drift; this
module is import-light (stdlib only, no package ``__init__``) so both can use
it without pulling anything else in.

A name is a credential when it contains TOKEN, SECRET, KEY, MNEMONIC, PASSWORD,
PRIVATE or BEARER anywhere, or has a ``PAT`` segment. ``PAT`` is matched as a
segment and not as a substring because a substring match would sweep in
``PATH``. Names that match those words but hold a file-system location rather
than a secret are not credentials: anything ending ``_DIR``, ``_PATH`` or
``_FILE``, plus the explicit allowlist below. Add to the allowlist only with a
reason beside the name.
"""

from __future__ import annotations

import re

CREDENTIAL_NAME_SUBSTRINGS: tuple[str, ...] = (
    "TOKEN",
    "SECRET",
    "KEY",
    "MNEMONIC",
    "PASSWORD",
    "PRIVATE",
    "BEARER",
)

_PAT_SEGMENT_RE = re.compile(r"(^|[^A-Za-z0-9])PAT($|[^A-Za-z0-9])", re.IGNORECASE)

# Value is a location, never key material.
_LOCATION_SUFFIXES: tuple[str, ...] = ("_DIR", "_PATH", "_FILE")

# Names that match a credential word but carry no secret and do not end in a
# location suffix. Empty today; add an entry only with a reason beside it.
NON_SECRET_NAMES: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# Value classifiers, used by the plist renderer as a SECOND layer behind the
# environment allowlist (``lib/environment_allowlist.py``) and for program
# arguments, which have no allowlist. Which environment NAMES may pass at all
# is decided by the allowlist, not here: a classifier can only recognise the
# carriers it was written for, and an allowlist does not need to.
# ---------------------------------------------------------------------------

# Configuration sections whose every entry concerns authentication or loads
# another file.
_SENSITIVE_GIT_SECTIONS: tuple[str, ...] = ("credential", "include", "includeif")
# Words in a git configuration KEY that mean the entry carries or obtains
# authentication.
_SENSITIVE_GIT_KEY_WORDS: tuple[str, ...] = (
    "EXTRAHEADER", "ASKPASS", "PASS", "SSHCOMMAND", "GITPROXY", "PROXYCOMMAND",
    "COOKIEFILE", "SSLCERT",
)  # fmt: skip
# Substrings that make a configuration VALUE credential-bearing.
_SENSITIVE_VALUE_WORDS: tuple[str, ...] = (
    "token", "secret", "password", "passwd", "bearer", "authorization", "apikey",
    "api_key", "api-key", "mnemonic",
)  # fmt: skip
_URL_USERINFO_RE = re.compile(r"://[^/\s@]+@")
_OPAQUE_RUN_RE = re.compile(r"[A-Za-z0-9_\-]{32,}|\beyJ[A-Za-z0-9_\-]{10,}")


def text_looks_credential_bearing(text: str) -> bool:
    """True when *text* (a configuration value) could carry key material."""
    lowered = text.lower()
    return bool(
        any(word in lowered for word in _SENSITIVE_VALUE_WORDS)
        or _URL_USERINFO_RE.search(text)
        or _OPAQUE_RUN_RE.search(text)
    )


def git_config_entry_is_sensitive(key: str, value: str | None) -> bool:
    """True when a ``key=value`` git configuration entry (a ``git -c``
    argument) could carry or obtain a credential."""
    value = "" if value is None else value
    if not key:
        return True
    section = key.split(".", 1)[0].lower()
    if section in _SENSITIVE_GIT_SECTIONS:
        return True
    normalized = re.sub(r"[.\-]", "_", key)
    if is_credential_env_name(normalized):
        return True
    upper = key.upper()
    if any(word in upper for word in _SENSITIVE_GIT_KEY_WORDS):
        return True
    return text_looks_credential_bearing(value) or text_looks_credential_bearing(key)


def is_credential_env_name(name: str) -> bool:
    """True when *name* looks like it carries a credential."""
    if not name or name in NON_SECRET_NAMES:
        return False
    upper = name.upper()
    if upper.endswith(_LOCATION_SUFFIXES):
        return False
    if any(word in upper for word in CREDENTIAL_NAME_SUBSTRINGS):
        return True
    return bool(_PAT_SEGMENT_RE.search(name))
