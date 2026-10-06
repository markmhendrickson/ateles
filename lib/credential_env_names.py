"""Which environment-variable names carry a credential, defined once.

Several places must agree on this: the root ``conftest.py`` (strips these from
the process environment before any test runs, so an assertion that prints the
environment cannot print a secret) and ``execution/scripts/render_daemon_plist.py``
(never writes these into a launchd plist). A copy in each would drift; this
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
