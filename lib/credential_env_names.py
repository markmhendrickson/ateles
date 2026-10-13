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

# ---------------------------------------------------------------------------
# Every way git takes configuration or authentication from the environment.
#
# 1. The indexed group ``GIT_CONFIG_COUNT`` + ``GIT_CONFIG_KEY_<n>`` /
#    ``GIT_CONFIG_VALUE_<n>``: each entry is judged as a key/value pair.
# 2. ``GIT_CONFIG_PARAMETERS`` (what ``git -c`` exports to child processes):
#    parsed into the same key/value pairs, in both quoting forms git writes,
#    and judged entry by entry. Text that does not parse is treated as
#    sensitive as a whole.
# 3. Configuration-file pointers (``GIT_CONFIG``, ``GIT_CONFIG_GLOBAL``,
#    ``GIT_CONFIG_SYSTEM``) and, inside 1 and 2, ``include.*`` /
#    ``includeIf.*`` entries: they hand git a file this classifier cannot
#    inspect, which can carry any setting above, so they are sensitive unless
#    they point at nothing (empty, or the null device).
# 4. Programs git runs to obtain or present credentials (``GIT_ASKPASS``,
#    ``SSH_ASKPASS``, ``GIT_SSH``, ``GIT_SSH_COMMAND``, ``GIT_PROXY_COMMAND``)
#    and their configuration twins (``core.askPass``, ``core.sshCommand``,
#    ``core.gitProxy``, every ``credential.*`` entry): a command line can
#    embed a secret, and a program can return one, so they are sensitive
#    unless they name a program known to supply nothing.
#
# A name in 3 or 4 is judged here and nowhere else; callers apply one policy
# (`plan_env_scrub` removes, the plist renderer refuses).
# ---------------------------------------------------------------------------
GIT_CONFIG_POINTER_NAMES: frozenset[str] = frozenset(
    {"GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"}
)
GIT_AUTH_PROGRAM_NAMES: frozenset[str] = frozenset(
    {"GIT_ASKPASS", "SSH_ASKPASS", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_PROXY_COMMAND"}
)
# Targets that supply no configuration and no credential.
_INERT_GIT_TARGETS: frozenset[str] = frozenset(
    {"", "/dev/null", "/usr/bin/false", "/bin/false", "/usr/bin/true", "/bin/true"}
)

# Configuration sections whose every entry concerns authentication or loads
# another file.
_SENSITIVE_GIT_SECTIONS: tuple[str, ...] = ("credential", "include", "includeif")
# Words in a git configuration KEY that mean the entry carries or obtains
# authentication (headers, password prompts and passwords, programs that
# open authenticated connections, cookie and client-certificate files).
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


def is_git_config_group_name(name: str) -> bool:
    """True for ``GIT_CONFIG_COUNT`` and ``GIT_CONFIG_KEY_<n>`` / ``..._VALUE_<n>``."""
    return name == GIT_CONFIG_COUNT_NAME or bool(_GIT_CONFIG_MEMBER_RE.match(name or ""))


def is_git_environment_name(name: str) -> bool:
    """True for every environment name through which git takes configuration
    or authentication (all four mechanisms above)."""
    return (
        is_git_config_group_name(name)
        or name == GIT_CONFIG_PARAMETERS_NAME
        or name in GIT_CONFIG_POINTER_NAMES
        or name in GIT_AUTH_PROGRAM_NAMES
    )


def text_looks_credential_bearing(text: str) -> bool:
    """True when *text* (a configuration value) could carry key material."""
    lowered = text.lower()
    return bool(
        any(word in lowered for word in _SENSITIVE_VALUE_WORDS)
        or _URL_USERINFO_RE.search(text)
        or _OPAQUE_RUN_RE.search(text)
    )


def git_config_entry_is_sensitive(key: str, value: str | None) -> bool:
    """True when a ``key=value`` git configuration entry could carry or obtain
    a credential: an authentication section or key, a file include, a
    credential-shaped key, or a credential-bearing key or value."""
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
    # The key can carry material too (a URL rewrite rule keyed by a URL with
    # credentials in it).
    return text_looks_credential_bearing(value) or text_looks_credential_bearing(key)


def parse_git_config_parameters(text: str) -> list[tuple[str, str | None]] | None:
    """Parse ``GIT_CONFIG_PARAMETERS`` into ``(key, value)`` entries, or None
    when it is not in a form git writes.

    Entries are separated by whitespace. Each is single-quoted, with a quote
    inside written as ``'\\''``. Git writes two forms: ``'key'='value'``
    (the ``=`` outside the quotes) and the older ``'key=value'``; a bare
    ``'key'`` is a boolean with no value."""
    entries: list[tuple[str, str | None]] = []
    index, length = 0, len(text)
    while index < length:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            break
        buffer: list[str] = []
        key: str | None = None
        while index < length and not text[index].isspace():
            char = text[index]
            if char == "'":
                end = text.find("'", index + 1)
                if end < 0:
                    return None
                buffer.append(text[index + 1 : end])
                index = end + 1
            elif char == "\\" and index + 1 < length:
                buffer.append(text[index + 1])
                index += 2
            elif char == "=" and key is None:
                key = "".join(buffer)
                buffer = []
                index += 1
            else:
                return None
        if key is None:
            raw = "".join(buffer)
            name, equals, value = raw.partition("=")
            entries.append((name, value if equals else None))
        else:
            entries.append((key, "".join(buffer)))
    return entries


def _git_sq_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def format_git_config_parameters(entries) -> str:
    """Serialize ``(key, value)`` entries the way git writes them."""
    return " ".join(
        _git_sq_quote(key) if value is None else f"{_git_sq_quote(key)}={_git_sq_quote(value)}"
        for key, value in entries
    )


def git_indirect_value_is_sensitive(value) -> bool:
    """For a configuration pointer or an authentication program: True unless
    it points at nothing (see ``_INERT_GIT_TARGETS``)."""
    return str(value if value is not None else "").strip() not in _INERT_GIT_TARGETS


def plan_env_scrub(environ) -> tuple[list[str], dict[str, str]]:
    """What a credential scrub must do to *environ*: ``(names_to_remove,
    values_to_set)``. Credential-named variables are removed. Git's
    environment is judged by mechanism (see the block above): the indexed
    group and ``GIT_CONFIG_PARAMETERS`` are rewritten entry by entry
    (sensitive entries dropped, the rest kept in a form git still reads, the
    variable or group removed when nothing safe is left or it does not parse),
    and pointers and authentication programs are removed unless inert, so
    ``git`` stays executable."""
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
            if not git_config_entry_is_sensitive(str(key), str(value)):
                kept.append((str(key), str(value)))
        wanted: dict[str, str] = {}
        if kept:
            wanted[GIT_CONFIG_COUNT_NAME] = str(len(kept))
            for index, (key, value) in enumerate(kept):
                wanted[f"GIT_CONFIG_KEY_{index}"] = key
                wanted[f"GIT_CONFIG_VALUE_{index}"] = value
        remove.update(n for n in group if n not in wanted)
        updates.update({n: v for n, v in wanted.items() if environ.get(n) != v})
    if GIT_CONFIG_PARAMETERS_NAME in environ:
        text = str(environ.get(GIT_CONFIG_PARAMETERS_NAME) or "")
        entries = parse_git_config_parameters(text)
        if entries is None:
            remove.add(GIT_CONFIG_PARAMETERS_NAME)
        else:
            safe = [(k, v) for k, v in entries if not git_config_entry_is_sensitive(k, v)]
            if len(safe) != len(entries):
                if safe:
                    updates[GIT_CONFIG_PARAMETERS_NAME] = format_git_config_parameters(safe)
                else:
                    remove.add(GIT_CONFIG_PARAMETERS_NAME)
    for name in (*GIT_CONFIG_POINTER_NAMES, *GIT_AUTH_PROGRAM_NAMES):
        if name in environ and git_indirect_value_is_sensitive(environ.get(name)):
            remove.add(name)
    for name in remove:
        updates.pop(name, None)
    return sorted(remove), updates


def is_credential_env_name(name: str) -> bool:
    """True when *name* looks like it carries a credential."""
    if not name or name in NON_SECRET_NAMES:
        return False
    if is_git_environment_name(name):
        return False  # judged by mechanism in plan_env_scrub
    upper = name.upper()
    if upper.endswith(_LOCATION_SUFFIXES):
        return False
    if any(word in upper for word in CREDENTIAL_NAME_SUBSTRINGS):
        return True
    return bool(_PAT_SEGMENT_RE.search(name))
