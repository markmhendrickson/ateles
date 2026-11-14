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
* A file that cannot be read or has a malformed line is an error naming the file
  and the line NUMBER. A line's text is never shown: it may be a credential.
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


def _unset(value: str | None) -> bool:
    v = (value or "").strip()
    return not v or (v.startswith("__") and v.endswith("__"))


def parse_dotenv(path: Path) -> dict[str, str]:
    """Parse ``path`` strictly. Raises ``SharedEnvError`` naming file and line number."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SharedEnvError(
            f"Cannot read the shared settings file {path}: {type(exc).__name__}. "
            "Fix its permissions or encoding; no settings were loaded."
        ) from None
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if not match:
            raise SharedEnvError(
                f"Malformed line {number} in the shared settings file {path} "
                "(expected NAME=value). Fix that line; no settings were loaded."
            )
        name, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def load_shared_env(
    environ: dict | None = None, *, path: Path | None = None, strict: bool = True
) -> LoadReport:
    """Fill ``environ`` (default ``os.environ``) from the shared dotenv.

    Existing non-empty values win. A missing file is not an error here (a
    caller that needs a setting uses :func:`require_settings`, which then names
    the file). ``strict=False`` reports an unusable file on stderr instead of
    raising, for import-time daemon bootstrap that must not crash.
    """
    env = os.environ if environ is None else environ
    target = path or shared_env_path(env)
    if not target.exists():
        return LoadReport(path=target, found=False)
    try:
        values = parse_dotenv(target)
    except SharedEnvError as exc:
        if strict:
            raise
        print(f"WARNING: {exc}", file=sys.stderr)
        return LoadReport(path=target, found=True)
    loaded = []
    for name, value in values.items():
        if _unset(env.get(name)):
            env[name] = value
            loaded.append(name)
    return LoadReport(path=target, found=True, loaded=loaded)


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
