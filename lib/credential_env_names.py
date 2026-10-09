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


GIT_CONFIG_COUNT_NAME = "GIT_CONFIG_COUNT"
GIT_CONFIG_PARAMETERS_NAME = "GIT_CONFIG_PARAMETERS"
_GIT_CONFIG_MEMBER_RE = re.compile(r"^GIT_CONFIG_(KEY|VALUE)_(\d+)$")

# Words in a git configuration KEY that mean the entry carries authentication.
_SENSITIVE_GIT_KEY_WORDS: tuple[str, ...] = ("EXTRAHEADER", "ASKPASS")
# Substrings that make a configuration VALUE credential-bearing.
_SENSITIVE_VALUE_WORDS: tuple[str, ...] = (
    "token", "secret", "password", "passwd", "bearer", "authorization", "apikey",
    "api_key", "api-key", "mnemonic",
)  # fmt: skip
_URL_USERINFO_RE = re.compile(r"://[^/\s@]+@")
_OPAQUE_RUN_RE = re.compile(r"[A-Za-z0-9_\-]{32,}|\beyJ[A-Za-z0-9_\-]{10,}")


def is_git_config_group_name(name: str) -> bool:
    """True for ``GIT_CONFIG_COUNT`` and ``GIT_CONFIG_KEY_<n>`` / ``..._VALUE_<n>``."""
    return name == GIT_CONFIG_COUNT_NAME or bool(_GIT_CONFIG_MEMBER_RE.match(name or ""))


def text_looks_credential_bearing(text: str) -> bool:
    """True when *text* (a configuration value) could carry key material."""
    lowered = text.lower()
    return bool(
        any(word in lowered for word in _SENSITIVE_VALUE_WORDS)
        or _URL_USERINFO_RE.search(text)
        or _OPAQUE_RUN_RE.search(text)
    )


def git_config_entry_is_sensitive(key: str, value: str) -> bool:
    """True when a ``key=value`` git configuration entry could carry a credential:
    a credential-shaped key, an authentication key, or a credential-bearing value."""
    normalized = re.sub(r"[.\-]", "_", key)
    if is_credential_env_name(normalized):
        return True
    upper = key.upper()
    if any(word in upper for word in _SENSITIVE_GIT_KEY_WORDS):
        return True
    # The key can carry material too (a URL rewrite rule keyed by a URL with
    # credentials in it).
    return text_looks_credential_bearing(value) or text_looks_credential_bearing(key)


def plan_env_scrub(environ) -> tuple[list[str], dict[str, str]]:
    """What a credential scrub must do to *environ*: ``(names_to_remove,
    values_to_set)``. Credential-named variables are removed; the grouped git
    configuration is rewritten as a unit (sensitive entries dropped, the rest
    renumbered with a matching count, the whole group removed when nothing is
    left or the count is unusable), so ``git`` stays executable."""
    names = list(environ)
    group = {n for n in names if is_git_config_group_name(n)}
    remove = {n for n in names if n not in group and is_credential_env_name(n)}
    updates: dict[str, str] = {}
    if group:
        kept: list[tuple[str, str]] = []
        try:
            count = int(str(environ.get(GIT_CONFIG_COUNT_NAME, "")).strip())
        except ValueError:
            count = 0
        for index in range(max(count, 0)):
            key = environ.get(f"GIT_CONFIG_KEY_{index}")
            value = environ.get(f"GIT_CONFIG_VALUE_{index}")
            if key is None or value is None or not key:
                continue
            if not git_config_entry_is_sensitive(key, value):
                kept.append((key, value))
        wanted: dict[str, str] = {}
        if kept:
            wanted[GIT_CONFIG_COUNT_NAME] = str(len(kept))
            for index, (key, value) in enumerate(kept):
                wanted[f"GIT_CONFIG_KEY_{index}"] = key
                wanted[f"GIT_CONFIG_VALUE_{index}"] = value
        remove.update(n for n in group if n not in wanted)
        updates.update({n: v for n, v in wanted.items() if environ.get(n) != v})
    parameters = environ.get(GIT_CONFIG_PARAMETERS_NAME)
    if parameters and text_looks_credential_bearing(str(parameters)):
        remove.add(GIT_CONFIG_PARAMETERS_NAME)
    return sorted(remove), updates


def is_credential_env_name(name: str) -> bool:
    """True when *name* looks like it carries a credential."""
    if not name or name in NON_SECRET_NAMES:
        return False
    if is_git_config_group_name(name):
        return False  # judged as a group by plan_env_scrub
    upper = name.upper()
    if upper.endswith(_LOCATION_SUFFIXES):
        return False
    if any(word in upper for word in CREDENTIAL_NAME_SUBSTRINGS):
        return True
    return bool(_PAT_SEGMENT_RE.search(name))
