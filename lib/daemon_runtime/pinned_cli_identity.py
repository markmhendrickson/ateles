"""Pin a daemon's Neotoma CLI children to the daemon's OWN AAuth identity.

Tyto's transcription path does this inline; piculet's importer chain needs the
same guarantee, so the logic lives here. What the deployed CLI does (read from
its source; line numbers are for ``~/neotoma-rc-src`` at the time of writing):

* A generic command takes its token from ``getCliToken()``
  (``src/cli/index.ts:1849-1855``): ``NEOTOMA_BEARER_TOKEN`` first, then the
  ``access_token`` stored in ``<home>/.config/neotoma/config.json``. It is sent
  as ``Authorization: Bearer`` BESIDE the request signature, and the bearer
  wins (``src/cli/index.ts:17674``). The config dir is ``os.homedir()``
  (``src/cli/config.ts:38``); there is no XDG variable and no explicit
  config-path variable, so ``HOME`` is the only lever.
* Signing is switched off by ``NEOTOMA_CLI_AAUTH_DISABLE=1`` or
  ``NEOTOMA_CLI_AAUTH_ENABLE=0`` (``src/shared/api_client.ts:102-105``), after
  which only the bearer remains.
* The signed claims come from ``NEOTOMA_AAUTH_*`` / ``NEOTOMA_CLI_AAUTH_*``
  and then ``<home>/.neotoma/aauth/config.json``
  (``src/cli/aauth_signer.ts:38-41,203-222``).
* ``src/config.ts:139-149`` loads ``<project root>/.env`` with ``override`` when
  ``NEOTOMA_ENV`` is not production. The root is ``NEOTOMA_PROJECT_ROOT`` when
  that directory is a Neotoma checkout (``isNeotomaRepoRoot``, ``:98-108,
  122-124``), else cwd, else ``repo_root`` from the HOME config, else the CLI's
  own install directory. ``encryption.enabled`` (``:401-406``) would derive a
  bearer from a key.

So pinning means, at once:

1. key, subject, kid and issuer are the daemon's own, set explicitly (issuer
   from the key file, like `neotoma_signed`), and every override is cleared;
2. every bearer-style variable, and everything that would switch signing off or
   encryption on, is removed;
3. the dotenv bootstrap that ``import lib.daemon_runtime`` runs is switched off
   (``ATELES_SKIP_DOTENV``), so importing this very module cannot re-add the
   bearer to the child (``lib/daemon_runtime/__init__.py:53-90``);
4. the CLI -- and only the CLI, via an ``env`` prefix on its argv -- runs with a
   private ``HOME`` whose config holds no token, and with
   ``NEOTOMA_PROJECT_ROOT`` pointing at a private empty "checkout" so no ``.env``
   is loaded; the rest of the chain keeps the real ``HOME``;
5. the child is told it is pinned, and refuses if anything above is not true.

Nothing here reads key material (only the public ``kid``/``iss`` fields) and
nothing here prints a token; the stored-token check is boolean only.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from lib.daemon_runtime.neotoma_signed import (
    NEOTOMA_RC_DIR,
    _pinned_issuer,
    agent_identity,
)

PIN_ENV = "ATELES_PINNED_NEOTOMA_SUB"
CLI_HOME_ENV = "ATELES_PINNED_CLI_HOME"
CLI_PROJECT_ROOT_ENV = "ATELES_PINNED_CLI_PROJECT_ROOT"

# Credentials the Neotoma CLI would send instead of (or beside) the signature.
BEARER_ENV_KEYS = (
    "NEOTOMA_BEARER_TOKEN",
    "NEOTOMA_TOKEN",
    "NEOTOMA_ACCESS_TOKEN",
    "NEOTOMA_MNEMONIC",
    "NEOTOMA_MNEMONIC_PASSPHRASE",
    "NEOTOMA_KEY_FILE_PATH",
    "NEOTOMA_ENCRYPTION_ENABLED",
)

# Switches that stop the CLI signing (leaving only a bearer).
SIGNING_DISABLE_KEYS = ("NEOTOMA_CLI_AAUTH_DISABLE", "NEOTOMA_CLI_AAUTH_ENABLE")

# Ambient claim overrides: the pinned values are set explicitly instead.
CLAIM_OVERRIDE_KEYS = (
    "NEOTOMA_CLI_AAUTH_SUB",
    "NEOTOMA_CLI_AAUTH_KID",
    "NEOTOMA_CLI_AAUTH_ISS",
    "NEOTOMA_CLI_AAUTH_TOKEN_TTL_SEC",
    "NEOTOMA_AAUTH_TOKEN_TTL_SEC",
)


class PinnedIdentityError(RuntimeError):
    """The daemon cannot present its own identity; callers must not proceed."""


def default_state_dir(daemon: str) -> Path:
    return Path.home() / ".ateles-work" / "pinned-cli" / daemon


def _private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) owner-only and refuse a symlink or a stranger's."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if path.is_symlink() or info.st_uid != os.getuid():
        raise PinnedIdentityError("the private CLI state directory is not trustworthy")
    path.chmod(0o700)
    return path


def prepare_cli_isolation(daemon: str, state_dir: "str | Path | None" = None) -> "tuple[Path, Path]":
    """(private HOME, private project root) for the daemon's CLI children.

    The HOME holds an empty CLI config (no token, no base url); the project root
    is an empty directory carrying only the ``package.json`` the CLI needs to
    accept it as a checkout, so no ``.env`` can be found there.
    """
    base = _private_dir(Path(state_dir) if state_dir is not None else default_state_dir(daemon))
    home = _private_dir(base / "home")
    config_dir = _private_dir(home / ".config" / "neotoma")
    config = config_dir / "config.json"
    if not config.is_file() or config.read_text().strip() != "{}":
        config.write_text("{}")
        config.chmod(0o600)
    project = _private_dir(base / "project")
    manifest = project / "package.json"
    if not manifest.is_file():
        manifest.write_text('{"name": "neotoma"}')
        manifest.chmod(0o600)
    return home, project


def build_pinned_env(
    daemon: str,
    base_env: "dict[str, str] | None" = None,
    *,
    keys_dir: "str | Path | None" = None,
    rc_dir: "str | Path | None" = None,
    state_dir: "str | Path | None" = None,
) -> dict[str, str]:
    """Environment for a child that must write as ``<daemon>@ateles-swarm``.

    Raises PinnedIdentityError when the key is missing or an ambient
    ``NEOTOMA_AAUTH_SUB`` names a different principal.
    """
    env = dict(os.environ if base_env is None else base_env)
    expected = f"{daemon}@ateles-swarm"
    ambient = env.get("NEOTOMA_AAUTH_SUB")
    if ambient and ambient != expected:
        raise PinnedIdentityError(
            f"{daemon} must write as {expected}, but NEOTOMA_AAUTH_SUB names "
            "a different principal; remove it"
        )
    identity = agent_identity(daemon, sub=expected, keys_dir=keys_dir)
    if identity is None:
        raise PinnedIdentityError(
            f"{daemon} has no AAuth key; configure ATELES_AAUTH_KEYS_DIR "
            f"with {daemon}.jwk.json"
        )
    try:
        issuer = _pinned_issuer(Path(identity["key"]))
    except Exception as exc:  # noqa: BLE001 — name the failure class only
        raise PinnedIdentityError(f"{daemon}'s key file has no readable issuer") from exc
    home, project = prepare_cli_isolation(daemon, state_dir)
    for key in (*BEARER_ENV_KEYS, *SIGNING_DISABLE_KEYS, *CLAIM_OVERRIDE_KEYS):
        env.pop(key, None)
    root = Path(rc_dir if rc_dir is not None else NEOTOMA_RC_DIR)
    env.update(
        NEOTOMA_AAUTH_PRIVATE_JWK_PATH=identity["key"],
        NEOTOMA_AAUTH_SUB=identity["sub"],
        NEOTOMA_AAUTH_KID=identity["kid"],
        NEOTOMA_AAUTH_ISS=issuer,
        NEOTOMA_CLI_SCRIPT=str(root / "dist" / "cli" / "bootstrap.js"),
        NEOTOMA_PROJECT_ROOT=str(project),
        # `import lib.daemon_runtime` loads the operator dotenv into os.environ
        # (lib/daemon_runtime/__init__.py), which would put the shared bearer
        # back into this child before any check ran. The child needs nothing
        # from it: it inherits every non-credential setting from the parent.
        ATELES_SKIP_DOTENV="1",
        **{PIN_ENV: identity["sub"], CLI_HOME_ENV: str(home), CLI_PROJECT_ROOT_ENV: str(project)},
    )
    return env


def cli_isolation_prefix(env: "dict[str, str] | None" = None) -> list[str]:
    """argv prefix that runs ONLY the CLI under the private HOME and project root.

    Empty when the run is not pinned (nothing changes for other callers).
    """
    env = os.environ if env is None else env
    home = env.get(CLI_HOME_ENV, "").strip()
    project = env.get(CLI_PROJECT_ROOT_ENV, "").strip()
    if not (env.get(PIN_ENV, "").strip() and home and project):
        return []
    return ["/usr/bin/env", f"HOME={home}", f"NEOTOMA_PROJECT_ROOT={project}"]


def verify_cli_runtime(env: dict[str, str]) -> None:
    """Fail before any write if the deployed CLI cannot honor a per-agent key."""
    script = Path(env["NEOTOMA_CLI_SCRIPT"])
    if not script.is_file():
        raise PinnedIdentityError("the deployed Neotoma CLI bootstrap is missing")
    signer = script.with_name("aauth_signer.js")
    check = """
        const { pathToFileURL } = await import('node:url');
        const { resolve } = await import('node:path');
        const signer = await import(pathToFileURL(process.argv[1]).href);
        if (typeof signer.resolveCliPrivateJwkPath !== 'function' ||
            resolve(signer.resolveCliPrivateJwkPath()) !==
            resolve(process.env.NEOTOMA_AAUTH_PRIVATE_JWK_PATH)) process.exit(1);
    """
    try:
        result = subprocess.run(
            [env.get("NODE_BIN", "node"), "--input-type=module", "-e", check, str(signer)],
            env=env, capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PinnedIdentityError(
            f"could not verify the deployed Neotoma CLI ({type(exc).__name__})"
        ) from exc
    if result.returncode:
        raise PinnedIdentityError(
            "the deployed Neotoma CLI does not support per-agent key paths; "
            "update NEOTOMA_RC_DIR"
        )


def stored_token_reachable(home: "str | Path") -> bool:
    """Boolean only: does ``<home>/.config/neotoma/config.json`` hold a token?

    An unreadable or unparseable config counts as reachable (fail closed): the
    CLI might still parse something this cannot.
    """
    config = Path(home) / ".config" / "neotoma" / "config.json"
    if not config.exists():
        return False
    try:
        data = json.loads(config.read_text())
    except Exception:  # noqa: BLE001
        return True
    if not isinstance(data, dict):
        return True
    return bool(str(data.get("access_token") or "").strip())


def pinned_child_problem(env: "dict[str, str] | None" = None) -> "str | None":
    """For use INSIDE a pinned child: why it must refuse, or None if consistent.

    A child that finds a bearer credential, a switch that disables signing, a
    reachable stored token or a loadable project ``.env``, or a key/sub that is
    not the one it was pinned to, must stop; the CLI would otherwise prefer the
    bearer. Names variables and conditions, never values.
    """
    env = os.environ if env is None else env
    pinned = env.get(PIN_ENV, "").strip()
    if not pinned:
        return None  # not a pinned run
    if env.get("NEOTOMA_AAUTH_SUB", "").strip() != pinned:
        return "the signing subject differs from the pinned identity"
    key = env.get("NEOTOMA_AAUTH_PRIVATE_JWK_PATH", "").strip()
    if not key or not Path(key).is_file():
        return "the pinned signing key is missing"
    if not env.get("NEOTOMA_AAUTH_ISS", "").strip():
        return "the pinned issuer is not set"
    for name in SIGNING_DISABLE_KEYS:
        if name in env:
            return f"{name} is set, which would stop the CLI signing"
    for name in CLAIM_OVERRIDE_KEYS:
        if env.get(name, "").strip():
            return f"{name} would override a pinned claim"
    for name in BEARER_ENV_KEYS:
        if env.get(name, "").strip():
            return f"a bearer-style credential ({name}) is present beside the pinned identity"
    if (env.get("ATELES_SKIP_DOTENV") or "").strip().lower() not in ("1", "true", "yes"):
        return "the operator dotenv bootstrap is not disabled (ATELES_SKIP_DOTENV)"
    home = env.get(CLI_HOME_ENV, "").strip()
    project = env.get(CLI_PROJECT_ROOT_ENV, "").strip()
    if not home or not project:
        # No isolation was prepared: the CLI would read the real HOME's config.
        if stored_token_reachable(env.get("HOME", "") or Path.home()):
            return "a stored CLI token is reachable from the home directory"
        return "the CLI is not isolated from the home directory's config and project .env"
    if stored_token_reachable(home):
        return "a stored CLI token is reachable from the private CLI home"
    if env.get("NEOTOMA_PROJECT_ROOT", "").strip() != project:
        return "the CLI project root is not the private one"
    if any(p.name.startswith(".env") for p in Path(project).iterdir()):
        return "the private CLI project root contains a .env file"
    return None


def load_dotenv_unless_pinned_bearer(load_dotenv, path, **kwargs):
    """`load_dotenv`, but a pinned run does not take a bearer from the file.

    The repo ``.env`` may carry the shared bearer; python-dotenv adds any key
    the environment lacks. In a pinned run, bearer-style keys that the file
    added are removed again. One the parent passed itself is left in place so
    the preflight can refuse it.
    """
    before = {k for k in BEARER_ENV_KEYS if k in os.environ}
    result = load_dotenv(path, **kwargs)
    if os.environ.get(PIN_ENV, "").strip():
        for key in BEARER_ENV_KEYS:
            if key not in before:
                os.environ.pop(key, None)
    return result
