"""Resolve a generation credential VALUE, inside the capability client only.

A ``vendor_binding`` carries ``credential_location`` as a reference (a key
name, or ``oauth:<route>``), never a value. This module turns the reference
into a ``Secret`` for the calling adapter and nothing else.

Rules this module enforces
--------------------------
* The credential file (``constraints.credential_env_file``) is parsed HERE,
  into a local dict. Nothing is written to the process environment and
  nothing is exported to a subprocess. There is no code path in this package
  that starts a subprocess with the credential in its environment.
* The file is the ONLY source. There is no fallback to the client's own process
  environment: one place a key can live, one place to audit.
* The file must live under the operator's Ateles credential directory
  (``~/.config/ateles`` unless ``ATELES_GENERATION_CREDENTIAL_DIR`` says
  otherwise) and must not be readable by group or other (mode 0600). A binding
  is data in a shared store; letting it name an arbitrary file would let a
  poisoned binding pull an unrelated secret into a vendor request.
* The key NAME must be in the calling adapter's allowlist for the same reason
  (a Google adapter only ever reads ``GEMINI_API_KEY``/``GOOGLE_API_KEY``).
* A missing, empty, unreadable or wrongly-permissioned source refuses with
  ``CREDENTIAL_UNRESOLVED``. The hint never tells the caller to put a key into
  an agent or process environment.
* ``Secret`` has a redacting ``repr``/``str``; ``redact`` scrubs a value out of
  vendor error text before it can reach an exception, log, or record.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from .errors import CREDENTIAL_UNRESOLVED, GenerationRefused

CREDENTIAL_DIR_ENV = "ATELES_GENERATION_CREDENTIAL_DIR"
_KEY_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_MIN_SECRET_LEN = 8


class Secret:
    """A credential value that will not print itself."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(<redacted>)"

    __str__ = __repr__

    def __bool__(self) -> bool:
        return bool(self._value)


def redact(text: str, hidden: "Secret | str | None") -> str:
    """Remove a credential value from ``text``."""
    if not text or hidden is None:
        return text
    value = hidden.reveal() if isinstance(hidden, Secret) else str(hidden)
    if len(value) < _MIN_SECRET_LEN:
        return text
    return text.replace(value, "[REDACTED]")


def credential_dir() -> Path:
    override = os.environ.get(CREDENTIAL_DIR_ENV)
    base = Path(override) if override else Path.home() / ".config" / "ateles"
    return base.expanduser().resolve()


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=value`` lines into a dict, leaving the process untouched."""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def _refuse(slot: str, message: str, hint: str) -> GenerationRefused:
    return GenerationRefused(CREDENTIAL_UNRESOLVED, slot, message, hint)


def _load_file(slot: str, credential_file: str, key_name: str) -> dict[str, str]:
    path = Path(credential_file).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise _refuse(
            slot,
            f"credential file {path} does not exist (looking for key {key_name})",
            f"Operator: materialize the generation credentials to {path} on the host "
            "that runs the capability client (see "
            "docs/dev/generation_capability_client.md). "
            "Agents never hold this key; they call the capability client.",
        ) from None
    base = credential_dir()
    if base != resolved and base not in resolved.parents:
        raise _refuse(
            slot,
            f"credential file {resolved} is outside the Ateles credential directory {base}",
            "Operator: correct the binding's credential_env_file to a path under "
            f"the credential directory ({CREDENTIAL_DIR_ENV} overrides the default).",
        )
    mode = stat.S_IMODE(resolved.stat().st_mode)
    if mode & 0o077:
        raise _refuse(
            slot,
            f"credential file {resolved} is accessible to group or other (mode {mode:04o})",
            f"Operator: run `chmod 600 {resolved}` and retry.",
        )
    try:
        return parse_env_file(resolved)
    except (OSError, UnicodeDecodeError):
        raise _refuse(
            slot,
            f"credential file {resolved} could not be read",
            "Operator: fix the file's permissions or encoding and retry.",
        ) from None


def resolve_credential(
    slot: str,
    *,
    credential_location: str,
    allowed_names: tuple[str, ...],
    credential_file: str | None = None,
) -> Secret:
    """Return the credential as a ``Secret`` or raise ``CREDENTIAL_UNRESOLVED``.

    ``allowed_names`` is the adapter's allowlist of key names. The ONLY source
    is the credential file the binding names; the client does not fall back to
    its own process environment, so there is one place a key can live and one
    place to audit.
    """
    location = (credential_location or "").strip()
    if not location or location.startswith("oauth:") or " " in location:
        raise _refuse(
            slot,
            "the binding's credential route is not a key the client can read "
            "(it is an OAuth/subscription route, or unset)",
            "This route has no host-held API key. Operator: bind an API-key "
            "route, or wait for the OAuth adapter (#1352).",
        )
    if not _KEY_NAME_RE.match(location):
        raise _refuse(
            slot,
            "the binding's credential_location is not a plain key name",
            "Operator: set credential_location to the key NAME only, never a value.",
        )
    if location not in allowed_names:
        raise _refuse(
            slot,
            f"the binding names credential key {location}, which this vendor adapter may not read",
            "Operator: correct credential_location to one of: " + ", ".join(allowed_names) + ".",
        )
    if not credential_file:
        raise _refuse(
            slot,
            f"the binding names no credential_env_file, so key {location} cannot be located",
            "Operator: add credential_env_file (a path under the Ateles credential "
            "directory) to the binding's constraints.",
        )
    value = (_load_file(slot, credential_file, location).get(location) or "").strip()
    if not value:
        raise _refuse(
            slot,
            f"key {location} is missing or empty in {Path(credential_file).expanduser()}",
            f"Operator: add a {location}=... line to that file (see "
            "docs/dev/generation_capability_client.md). Agents must call the "
            "capability client; they never hold this key.",
        )
    return Secret(value)
