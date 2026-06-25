"""Ateles — personal agent swarm infrastructure.

This package provides the ``ateles`` CLI: the install-by-package entrypoint for
the swarm. It is the W0 foundation of the installability epic — see
``docs/installability.md`` and GitHub issue #18 for the full roadmap. The CLI
surface (``init``/``doctor``/``provision``/``run``/``deploy``/``mirror``) is the
spine; individual verbs are implemented across later workstreams (W1–W7).

Versioning is intentionally pre-release (0.0.0) until the release-signal work in
W8 (semver + tags + CHANGELOG) lands.
"""

__version__ = "0.0.0"
