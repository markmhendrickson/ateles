"""The one shared place a script's settings come from, and loud failure when absent.

Every operator credential and setting a script needs lives in ONE file, the
SOPS-materialized dotenv at ``~/.config/neotoma/.env`` (override with
``ATELES_DOTENV_FILE``; ``docs/secrets_management.md``). It is host-level, so a
git worktree, the shared clone and a runtime checkout all resolve the same
settings with no per-checkout ``.env`` or ``config.py`` to materialize by hand.

Rules this module enforces (ateles#885, and the worktree failure it shares a
root with):

* ONE file is consulted, never several merged first-wins. A stale value in a
  second file therefore cannot shadow the authoritative one.
* The process environment wins over the file (explicit override). An empty or
  plist-placeholder (``__X__``) value counts as unset.
* A file that cannot be read is an error naming the file. A malformed line is
  skipped and reported once, by file and line NUMBER only (never its text, which
  may be a credential); every well-formed line still loads, so one odd line
  cannot strip a daemon of its other settings. ``strict=True`` raises instead.
* A required setting that is absent is an error naming the setting and where it
  was looked for. There is no default.

Stdlib-only. Never logs or returns a value in an error.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

DOTENV_OVERRIDE_ENV = "ATELES_DOTENV_FILE"
SKIP_ENV = "ATELES_SKIP_DOTENV"

_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


class SharedEnvError(RuntimeError):
    """The shared dotenv exists but cannot be used. Never carries a value."""


class MissingSettingError(SharedEnvError):
    """A required setting is absent from the environment and the shared dotenv."""


@dataclass
class LoadReport:
    path: Path
    found: bool
    loaded: list[str] = field(default_factory=list)  # names only


def shared_env_path(environ: dict | None = None) -> Path:
    env = os.environ if environ is None else environ
    override = (env.get(DOTENV_OVERRIDE_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "neotoma" / ".env"


def should_load(environ: dict | None = None, modules: dict | None = None) -> bool:
    """False under pytest or ``ATELES_SKIP_DOTENV`` (ateles#1285): a test must
    not inherit the operator's real settings unless it opts in itself."""
    env = os.environ if environ is None else environ
    mods = sys.modules if modules is None else modules
    if (env.get(SKIP_ENV) or "").strip().lower() in ("1", "true", "yes"):
        return False
    return not ("pytest" in mods or env.get("PYTEST_CURRENT_TEST") is not None)


_PLACEHOLDER = re.compile(r"^__[A-Za-z0-9_]+__$")
_reported: set[tuple[str, int]] = set()


def _unset(value: str | None) -> bool:
    """Empty, whitespace-only, or an exact ``__NAME__`` plist placeholder."""
    v = (value or "").strip()
    return not v or bool(_PLACEHOLDER.match(v))


def parse_dotenv_lenient(path: Path) -> tuple[dict[str, str], list[int]]:
    """Parse ``path``; return (settings, malformed line numbers).

    Raises ``SharedEnvError`` only when the file cannot be read. Accepts
    ``NAME=value`` and ``export NAME=value`` (the old loader stored the latter
    under the name ``export NAME``), a UTF-8 byte-order mark, and one matching
    pair of surrounding quotes, which is stripped.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise SharedEnvError(
            f"Cannot read the settings file {path}: {type(exc).__name__}. "
            "Fix its permissions or encoding; no settings were loaded from it."
        ) from None
    values: dict[str, str] = {}
    bad: list[int] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if not match:
            bad.append(number)
            continue
        name, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values, bad


def _bad_lines_message(path: Path, bad: list[int]) -> str:
    return (
        f"settings file {path} has {len(bad)} malformed line(s): "
        f"{', '.join(str(n) for n in bad)} (line numbers only; expected "
        "NAME=value, where NAME has only letters, digits and underscores, on "
        "one line). They were skipped; every other setting loaded."
    )


def parse_dotenv(path: Path) -> dict[str, str]:
    """Strict parse: raises ``SharedEnvError`` naming file and line numbers."""
    values, bad = parse_dotenv_lenient(path)
    if bad:
        raise SharedEnvError("Malformed " + _bad_lines_message(path, bad))
    return values


def _warn_bad(path: Path, bad: list[int]) -> None:
    fresh = [n for n in bad if (str(path), n) not in _reported]
    if not fresh:
        return
    _reported.update((str(path), n) for n in fresh)
    print(f"WARNING: {_bad_lines_message(path, fresh)}", file=sys.stderr)


def load_shared_env(
    environ: dict | None = None, *, path: Path | None = None, strict: bool = False
) -> LoadReport:
    """Fill ``environ`` (default ``os.environ``) from the shared dotenv.

    Existing set values win. Every well-formed line loads; malformed lines are
    reported once on stderr by number (or raise, with ``strict=True``). A missing
    file is not an error here (a caller that needs a setting uses
    :func:`require_settings`, which names the file). An unreadable file raises
    when strict, else warns.
    """
    env = os.environ if environ is None else environ
    target = path or shared_env_path(env)
    if not target.exists():
        return LoadReport(path=target, found=False)
    try:
        values, bad = parse_dotenv_lenient(target)
    except SharedEnvError as exc:
        if strict:
            raise
        print(f"WARNING: {exc}", file=sys.stderr)
        return LoadReport(path=target, found=True)
    if bad:
        if strict:
            raise SharedEnvError("Malformed " + _bad_lines_message(target, bad))
        _warn_bad(target, bad)
    loaded = []
    for name, value in values.items():
        if _unset(env.get(name)):
            env[name] = value
            loaded.append(name)
    return LoadReport(path=target, found=True, loaded=loaded)


def load_fallback_file(path: Path, environ: dict | None = None) -> list[str]:
    """Last-resort source: fill ONLY names unset in ``environ``, warning per name.

    For a legacy per-checkout file whose contents cannot be audited. Callers
    run it after :func:`load_shared_env`, so the environment and the shared file
    both win. Each name resolved only from here is reported on stderr by name
    and file, never value.
    """
    env = os.environ if environ is None else environ
    if not path.exists():
        return []
    try:
        values, bad = parse_dotenv_lenient(path)
    except SharedEnvError as exc:
        print(f"WARNING: {exc}", file=sys.stderr)
        return []
    _warn_bad(path, bad)
    used = []
    for name, value in values.items():
        if _unset(env.get(name)) and not _unset(value):
            env[name] = value
            used.append(name)
            print(
                f"WARNING: setting {name} resolved only from the legacy file "
                f"{path}; add it to the shared settings file "
                f"{shared_env_path(env)} (the fallback will be removed).",
                file=sys.stderr,
            )
    return used


def require_settings(
    *names: str, purpose: str, environ: dict | None = None
) -> dict[str, str]:
    """Return the named settings, or raise ``MissingSettingError`` naming each."""
    env = os.environ if environ is None else environ
    missing = [n for n in names if _unset(env.get(n))]
    if missing:
        path = shared_env_path(env)
        state = "exists but does not define it" if path.exists() else "does not exist"
        raise MissingSettingError(
            f"Missing setting {', '.join(missing)} (needed to {purpose}). It is "
            f"not in the environment and the shared settings file {path} {state}. "
            f"Add {', '.join(n + '=<value>' for n in missing)} to that file "
            "(materialize it with the secrets tooling, docs/secrets_management.md) "
            "or export it for this run. There is no default."
        )
    return {n: env[n] for n in names}
