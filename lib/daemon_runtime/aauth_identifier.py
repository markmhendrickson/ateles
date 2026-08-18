"""AAuth agent identifiers (draft-hardt-oauth-aauth-protocol-10 §5.1).

An agent identifier is a URI in the ``aauth`` scheme::

    aauth:local@domain

where ``domain`` is the **agent provider's** domain. The local part is limited
to lowercase ASCII letters, digits, ``-``, ``_``, ``+``, and ``.``; it must be
non-empty and at most 255 characters. ``+`` is RESERVED as the sub-agent
delimiter, so a top-level agent's local part must not contain it.

Ateles historically used bare ``<name>@ateles-swarm`` subjects. Those are not
valid identifiers under the current draft on two counts: no ``aauth:`` scheme,
and ``ateles-swarm`` is not a domain name. :func:`normalize` maps the legacy
form onto the spec form so callers can migrate without a flag day, and
:func:`is_legacy` lets callers report on what still needs converting.

The agent-provider domain is read from the environment rather than hardcoded,
so a fork can supply its own without editing code.
"""

from __future__ import annotations

import os
import re

# Agent-provider domain. Derived from the AAuth issuer, which is the agent
# provider's HTTPS URL; the identifier carries the domain without the scheme.
DEFAULT_AGENT_DOMAIN = "markmhendrickson.com"

# The legacy pseudo-domain that predates draft-10 conformance.
LEGACY_SUFFIX = "@ateles-swarm"

_SCHEME = "aauth:"
_LOCAL_RE = re.compile(r"^[a-z0-9._+-]+$")
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)


class InvalidAgentIdentifier(ValueError):
    """Raised when a string is not a valid AAuth agent identifier."""


def agent_domain() -> str:
    """The agent-provider domain used to build identifiers."""
    return os.environ.get("ATELES_AAUTH_AGENT_DOMAIN", DEFAULT_AGENT_DOMAIN).lower()


def build(local: str, domain: str | None = None) -> str:
    """Build ``aauth:<local>@<domain>``, validating both parts."""
    return validate(f"{_SCHEME}{local.lower()}@{(domain or agent_domain()).lower()}")


def is_legacy(sub: str) -> bool:
    """True if ``sub`` uses the pre-draft-10 ``<name>@ateles-swarm`` form."""
    return sub.endswith(LEGACY_SUFFIX) and not sub.startswith(_SCHEME)


def normalize(sub: str, domain: str | None = None) -> str:
    """Return the draft-10 identifier for ``sub``.

    Accepts an already-conformant identifier unchanged, and rewrites the two
    legacy shapes: ``<name>@ateles-swarm`` and a bare ``<name>@<domain>`` that
    is missing only the scheme.
    """
    sub = sub.strip()
    if sub.startswith(_SCHEME):
        return validate(sub)
    if is_legacy(sub):
        return build(sub[: -len(LEGACY_SUFFIX)], domain)
    if "@" in sub:
        local, _, existing_domain = sub.rpartition("@")
        return build(local, existing_domain)
    return build(sub, domain)


def validate(identifier: str) -> str:
    """Validate an agent identifier, returning it unchanged.

    Raises :class:`InvalidAgentIdentifier` with the specific rule that failed.
    """
    if not identifier.startswith(_SCHEME):
        raise InvalidAgentIdentifier(
            f"{identifier!r} must use the 'aauth:' scheme (draft-10 §5.1)"
        )
    body = identifier[len(_SCHEME) :]
    local, sep, domain = body.rpartition("@")
    if not sep:
        raise InvalidAgentIdentifier(f"{identifier!r} must be of the form local@domain")
    if not local:
        raise InvalidAgentIdentifier(f"{identifier!r} has an empty local part")
    if len(local) > 255:
        raise InvalidAgentIdentifier(f"{identifier!r} local part exceeds 255 characters")
    if not _LOCAL_RE.match(local):
        raise InvalidAgentIdentifier(
            f"{identifier!r} local part may contain only a-z, 0-9, '-', '_', '+', '.'"
        )
    if not _DOMAIN_RE.match(domain):
        raise InvalidAgentIdentifier(
            f"{identifier!r} domain {domain!r} is not a valid domain name"
        )
    return identifier


def local_part(identifier: str) -> str:
    """The local part of a validated identifier."""
    return validate(identifier)[len(_SCHEME) :].rpartition("@")[0]


def is_subagent(identifier: str) -> bool:
    """True if the identifier's local part carries the ``+`` sub-agent delimiter.

    Note draft-10 §5.1: parties MUST NOT parse the local part for protocol
    decisions — the ``parent_agent`` claim is the authoritative marker. This
    helper is for logging and display only.
    """
    return "+" in local_part(identifier)


def subagent(parent: str, discriminator: str) -> str:
    """Build a sub-agent identifier beneath ``parent``.

    draft-10 §5.1 allows a single level: the sub-agent's local part is the
    parent's, then ``+``, then a non-empty discriminator.
    """
    parent_local = local_part(parent)
    if "+" in parent_local:
        raise InvalidAgentIdentifier(
            f"{parent!r} is already a sub-agent; nesting is not permitted (§10.2)"
        )
    if not discriminator:
        raise InvalidAgentIdentifier("sub-agent discriminator must be non-empty")
    domain = validate(parent)[len(_SCHEME) :].rpartition("@")[2]
    return build(f"{parent_local}+{discriminator}", domain)


# Qualified aliases for the package's public API, where the bare names above
# would be ambiguous alongside the rest of daemon_runtime.
build_agent_identifier = build
is_legacy_agent_identifier = is_legacy
normalize_agent_identifier = normalize
subagent_identifier = subagent
validate_agent_identifier = validate
