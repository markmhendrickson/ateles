"""Pluggable secret backends (W4).

The daemons currently hard-assume a sibling ``ateles-private`` repo decrypted
via SOPS+age. W4 puts secret resolution behind an interface so that layout
becomes one backend among several (env, SOPS+age, 1Password). This module
introduces the interface and the environment backend; migrating the live daemon
call sites (and wrapping the SOPS+age / 1Password tooling) is a separate,
higher-blast step, deferred until the operator signs off (see
``docs/installability.md`` W4).
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class SecretBackend(Protocol):
    """Resolves secret values by key. Implementations must be side-effect free
    on construction and cheap on ``get``."""

    name: str

    def get(self, key: str) -> str | None:
        ...

    def available(self) -> bool:
        """Whether this backend can serve secrets in the current environment."""
        ...


class EnvBackend:
    """Resolve secrets from environment variables — the always-available base
    backend and the fallback every other backend degrades to."""

    name = "env"

    def __init__(self, environ: dict | None = None) -> None:
        self._env = os.environ if environ is None else environ

    def get(self, key: str) -> str | None:
        return self._env.get(key) or None

    def available(self) -> bool:
        return True


# Known backends by name. The SOPS+age and 1Password backends wrap the existing
# execution/scripts tooling and register here when the live migration lands.
_BACKENDS: dict[str, type] = {"env": EnvBackend}


def available_backends() -> tuple[str, ...]:
    return tuple(sorted(_BACKENDS))


def get_backend(name: str | None = None, **kwargs) -> SecretBackend:
    """Return a backend by name (default: ``ATELES_SECRET_BACKEND`` or ``env``)."""
    name = name or os.environ.get("ATELES_SECRET_BACKEND", "env")
    try:
        factory = _BACKENDS[name]
    except KeyError:
        raise ValueError(
            f"unknown secret backend {name!r}; known: {list(available_backends())}"
        ) from None
    return factory(**kwargs)


def resolve(key: str, *, backend: SecretBackend | None = None) -> str | None:
    """Resolve a single secret via the given (or default) backend."""
    return (backend or get_backend()).get(key)
