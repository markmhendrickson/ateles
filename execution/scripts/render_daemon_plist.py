#!/usr/bin/env python3
"""Render a launchd plist from a repo template with no secret values in it.

An installed plist is readable by every process the operator runs, and
`launchctl print` / `plutil -p` print its ``EnvironmentVariables`` verbatim, so
a credential written there ends up in any transcript that inspects the job.
Daemons do not need credentials in the plist: ``lib/daemon_runtime`` loads the
materialized secrets file (written from the encrypted snapshot by
``secrets_materialize.py``) into the process environment at start, filling any
variable that is absent, empty, or an ``__PLACEHOLDER__``.

Every installer goes through this renderer, so a plist can only come out of an
install in the shape this module allows:

* ``<HOME>`` (the placeholder the templates use) is replaced by ``--home``
  (default: the invoking user's home directory), XML-escaped.
* Credential-named ``EnvironmentVariables`` keys (see
  ``lib/credential_env_names.py``) are DROPPED, whatever their value, and
  reported by name only. The daemon fills them from the secrets store.
* Any other ``EnvironmentVariables`` value that looks like key material (long,
  opaque, not a path or URL) is a hard error: the render fails and writes
  nothing, rather than guessing which half of the file is safe.
* A ``ProgramArguments`` flag whose name is credential-shaped and carries a
  value (``--bearer-token <value>`` / ``--api-key=<value>``) is a hard error
  for the same reason.

The renderer never reads the process environment, so ambient secrets cannot
reach the output. It prints names, never values. The output file is written
atomically with mode 0600.

Usage:
    python3 render_daemon_plist.py TEMPLATE OUT [--home DIR]
    python3 render_daemon_plist.py TEMPLATE - [--home DIR]      # to stdout
"""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.credential_env_names import is_credential_env_name  # noqa: E402

HOME_PLACEHOLDER = "<HOME>"

# Opaque material that is long enough to be a key, with no path/URL shape.
_MIN_OPAQUE_LENGTH = 24
_OPAQUE_RE = re.compile(
    r"^[A-Za-z0-9+/=_\-.!@#$%^&*~]{" + str(_MIN_OPAQUE_LENGTH) + r",}$"
)
_PLACEHOLDER_RE = re.compile(r"^__[A-Z0-9_]+__$")


class PlistRenderError(Exception):
    """The template cannot be rendered safely. The message names keys, never values."""


@dataclass(frozen=True)
class RenderResult:
    content: bytes
    dropped_credential_keys: tuple[str, ...]


_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _looks_like_opaque_secret(value: str) -> bool:
    if not value or _PLACEHOLDER_RE.match(value):
        return False
    if value.startswith("eyJ"):
        return True  # the header of a signed token, whatever follows
    if "/" in value or "://" in value or " " in value or ":" in value:
        return False  # a path, a URL or a path list
    if _HOSTNAME_RE.match(value) or _EMAIL_RE.match(value):
        return False
    return bool(_OPAQUE_RE.match(value))


def _credential_flag_with_value(arguments: list) -> str | None:
    """Return the flag name when a credential-shaped flag carries a value."""
    for index, argument in enumerate(arguments):
        if not isinstance(argument, str):
            continue
        # A credential assignment carried as one argument: `GITHUB_PAT=value`,
        # `--env=API_TOKEN=value`. The name is judged by the same classifier.
        for assignment in (argument.lstrip("-"), argument.partition("=")[2]):
            key, equals, value = assignment.partition("=")
            if (
                equals
                and value
                and key
                and re.fullmatch(r"[A-Za-z0-9_.-]+", key)
                and is_credential_env_name(key.replace("-", "_"))
            ):
                return key
        if not argument.startswith("--"):
            continue
        name, equals, inline_value = argument[2:].partition("=")
        # Same classifier as the environment check (substring words AND the PAT
        # segment), with dashes read as underscores so ``--github-pat`` and
        # ``--token-file`` classify exactly as ``GITHUB_PAT`` / ``TOKEN_FILE``.
        if not is_credential_env_name(name.replace("-", "_")):
            continue
        has_value = (
            bool(inline_value)
            if equals
            else (
                index + 1 < len(arguments)
                and isinstance(arguments[index + 1], str)
                and not arguments[index + 1].startswith("--")
            )
        )
        if has_value:
            return f"--{name}"
    return None


def render(template_text: str, *, home: str) -> RenderResult:
    """Render *template_text*; raise ``PlistRenderError`` rather than emit a secret."""
    substituted = template_text.replace(HOME_PLACEHOLDER, escape(home))
    try:
        data = plistlib.loads(substituted.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — plistlib raises several types
        raise PlistRenderError(
            f"template is not a valid plist after substitution ({type(exc).__name__})"
        ) from exc
    if not isinstance(data, dict):
        raise PlistRenderError("template root is not a dictionary")

    dropped: list[str] = []
    environment = data.get("EnvironmentVariables")
    if environment is not None:
        if not isinstance(environment, dict):
            raise PlistRenderError("EnvironmentVariables is not a dictionary")
        for key in list(environment):
            if is_credential_env_name(key):
                dropped.append(key)
                del environment[key]
                continue
            value = environment[key]
            if isinstance(value, str) and _looks_like_opaque_secret(value):
                raise PlistRenderError(
                    f"EnvironmentVariables[{key!r}] holds an opaque value that looks "
                    "like key material; refusing to write it"
                )

    arguments = data.get("ProgramArguments")
    if isinstance(arguments, list):
        flag = _credential_flag_with_value(arguments)
        if flag:
            raise PlistRenderError(
                f"ProgramArguments passes a value to {flag}; refusing to write a "
                "credential into the job definition"
            )

    return RenderResult(
        content=plistlib.dumps(data, sort_keys=False),
        dropped_credential_keys=tuple(sorted(dropped)),
    )


def render_file(template: Path, out: Path | None, *, home: str) -> RenderResult:
    """Render *template* and write it atomically (mode 0600) to *out* unless None."""
    result = render(template.read_text(encoding="utf-8"), home=home)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=out.parent, prefix=f".{out.name}.")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(result.content)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, out)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a launchd plist from a template with no secret values.",
    )
    parser.add_argument("template", type=Path)
    parser.add_argument("out", help="output path, or - for stdout")
    parser.add_argument("--home", default=str(Path.home()))
    args = parser.parse_args(argv)

    try:
        out = None if args.out == "-" else Path(args.out)
        result = render_file(args.template, out, home=args.home)
    except (PlistRenderError, OSError) as exc:
        print(f"render_daemon_plist: refused: {exc}", file=sys.stderr)
        return 1
    if args.out == "-":
        sys.stdout.buffer.write(result.content)
    if result.dropped_credential_keys:
        print(
            "render_daemon_plist: left out credential-named variables (the daemon "
            "loads them from the secrets store at start): "
            + ", ".join(result.dropped_credential_keys),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
