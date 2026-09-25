"""Per-agent AAuth-signed Neotoma requests for Ateles daemons.

Two transports live here, sharing one key resolution (:func:`agent_identity`):

* :class:`NeotomaWriter` — **the signed write client for daemons** (ateles#1270
  step 2a). It signs in-process with the native RFC 9421 signer in
  ``aauth_httpsig.py`` (contract-tested against Neotoma's verifier in
  ``test_neotoma_aauth_contract.py``), so a signed write costs no ``node``
  process. It carries two rules:

  - **Governance writes never use the bearer token.** A write to a type in
    :data:`GOVERNANCE_ENTITY_TYPES` (or a field in :data:`GOVERNANCE_FIELDS`),
    or one whose entity type cannot be read from the body, is always signed
    and sent with the signature as its only credential. If it cannot be
    signed, or the server refuses the signature, it raises
    :class:`SignedWriteError`. There is no fallback, whatever the daemon's
    switch says. Classification fails closed at every input:

    - type names are compared folded (:func:`canonical_entity_type`: case,
      separators, accents, simple plurals), because Neotoma files a variant
      spelling under the registered type it folds onto;
    - the write is classified by endpoint (:data:`SUPPORTED_WRITE_PATHS`:
      ``store``, ``correct``, ``create_relationship(s)``), each reading only
      the keys Neotoma reads for it; any other endpoint is refused unsent;
    - an existing entity the body names by id (a store entity's
      ``target_id``, a correct's ``entity_id``, a relationship endpoint) has
      its real type looked up first, and every field written onto it is
      judged against that type, because neither the store's extend path nor
      ``/correct`` checks the declared type; a failed lookup counts as
      governance;
    - a caller's ``entity_types`` can only add types to the check, never
      replace the body's.
  - **Every other write follows the daemon's switch,**
    ``ATELES_SIGNED_WRITES_<DAEMON>`` (see :func:`signing_mode`): ``off``
    (the default, today's bearer write), ``shadow`` (sign; on a signing
    failure log at ERROR and fall back to the bearer) or ``on`` (sign; a
    signing failure raises). An unrecognised value reads as ``on``.

  The signing identity comes from the key file and this module only: the
  ``sub`` is pinned to ``<agent>@ateles-swarm``, and the issuer to the key
  file's ``iss`` (else the default), never an ambient ``NEOTOMA_AAUTH_*``.

  :meth:`NeotomaWriter.confirm_attribution` reads a written observation back
  and checks it carries the expected ``agent_sub`` at a signed tier, because a
  2xx says nothing about who the server recorded as the writer.

* :func:`signed_request` — the older per-request ``node signed_fetch.mjs``
  path. Its remaining callers (``gate_waive.IssueGateStore.sign_off`` and the
  ``NEOTOMA_AAUTH_VIA_CLI`` branch in ``agent_loader.py``) move onto
  :class:`NeotomaWriter` in a later step; it is kept unchanged here so this
  change does not touch the live gate sign-off path.

Server notes (neotoma ``origin/main``): a verified signature whose ``sub`` /
``iss`` match an active ``agent_grant`` is **admitted** and is a complete auth
path with no bearer; the grant's capabilities then bound which ops and entity
types the write may touch. ``NEOTOMA_STRICT_AAUTH_SUBS`` does **not** promote a
sub's tier: it pins an identity, refusing an unsigned request whose
``X-Agent-Label`` names a listed sub. Tier promotion to ``operator_attested``
comes from ``NEOTOMA_OPERATOR_ATTESTED_SUBS``, and swarm identities stay at
``software`` by operator ruling (ateles#1270, 2026-09-25).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote

import httpx

try:  # package import (production) and bare import (in-dir tests) both work
    from .aauth_httpsig import (
        DEFAULT_AAUTH_ISSUER,
        AAuthSigningError,
        HttpSigSigner,
        load_http_sig_signer,
    )
except ImportError:  # pragma: no cover
    from aauth_httpsig import (
        DEFAULT_AAUTH_ISSUER,
        AAuthSigningError,
        HttpSigSigner,
        load_http_sig_signer,
    )

log = logging.getLogger("daemon_runtime.neotoma_signed")

NODE_BIN = os.environ.get("NODE_BIN", "node")
NEOTOMA_RC_DIR = os.environ.get("NEOTOMA_RC_DIR", str(Path.home() / "neotoma-rc-src"))
AAUTH_KEYS_DIR = os.environ.get(
    "ATELES_AAUTH_KEYS_DIR", str(Path.home() / "repos" / "ateles-private" / "keys")
)
_HELPER = Path(__file__).resolve().parent / "signed_fetch.mjs"


def via_cli_enabled() -> bool:
    """True when per-agent CLI signing is switched on (default off)."""
    return os.environ.get("NEOTOMA_AAUTH_VIA_CLI", "").lower() not in ("", "0", "false", "no")


def agent_identity(
    agent_name: str,
    *,
    sub: "str | None" = None,
    keys_dir: "str | Path | None" = None,
) -> "dict[str, str] | None":
    """Resolve {key, sub, kid} for an agent, or None if it has no JWK key.

    Returning None is the signal to fall back to the unsigned/bearer path.

    ``sub`` is the EXPLICIT-subject override (ateles#795 constraint 2). Pass it
    when the caller is signing on behalf of a specific principal — e.g. the
    dispatcher recording a lens's gate verdict — so the resolved subject can
    never be swapped out by an ambient ``NEOTOMA_AAUTH_SUB`` the calling
    process happens to carry for its OWN identity. When ``sub`` is omitted,
    behavior is unchanged: ``NEOTOMA_AAUTH_SUB`` if set, else
    ``<agent>@ateles-swarm`` — the ambient ladder is only safe for a process
    signing as itself, which is the sole existing caller (`agent_loader.py`).
    """
    if not agent_name:
        return None
    key = Path(keys_dir if keys_dir is not None else AAUTH_KEYS_DIR) / f"{agent_name}.jwk.json"
    if not key.exists():
        return None
    try:
        kid = json.loads(key.read_text()).get("kid")
    except Exception:
        return None
    if not kid:
        return None
    resolved_sub = sub or os.environ.get("NEOTOMA_AAUTH_SUB") or f"{agent_name}@ateles-swarm"
    return {"key": str(key), "sub": resolved_sub, "kid": str(kid)}


def signed_request(
    method: str,
    url: str,
    body: "dict | None" = None,
    agent_name: str = "",
    timeout: int = 20,
    *,
    sub: "str | None" = None,
) -> "tuple[int, dict]":
    """Perform a per-agent AAuth-signed request. Returns (status, parsed_json).

    Raises RuntimeError on signing/transport failure or when the agent has no
    key — callers should catch and fall back to their existing path. This
    function is NOT gated on ``via_cli_enabled()`` — that flag is applied by
    the two existing callers in `agent_loader.py`, which sign a daemon's OWN
    traffic and fall back to the bearer path when the flag is off. A caller
    that must never fall back to the bearer (ateles#795 `sign_off`) calls this
    directly and treats any exception as a hard failure, not a signal to
    degrade to bearer auth.

    ``sub`` is passed straight through to :func:`agent_identity` as the
    EXPLICIT subject — see its docstring for why this must not be left to the
    ambient ``NEOTOMA_AAUTH_SUB`` when signing on behalf of a principal other
    than the calling process's own identity.
    """
    ident = agent_identity(agent_name, sub=sub)
    if ident is None:
        raise RuntimeError(f"no AAuth key for agent {agent_name!r}")
    spec: dict = {"url": url, "method": method.upper(), "headers": {"content-type": "application/json"}}
    if body is not None:
        spec["body"] = json.dumps(body)
    env = dict(
        os.environ,
        NEOTOMA_RC_DIR=NEOTOMA_RC_DIR,
        NEOTOMA_AAUTH_PRIVATE_JWK_PATH=ident["key"],
        NEOTOMA_AAUTH_SUB=ident["sub"],
        NEOTOMA_AAUTH_KID=ident["kid"],
    )
    proc = subprocess.run(
        [NODE_BIN, str(_HELPER)],
        input=json.dumps(spec),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    try:
        out = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"signed_fetch returned non-JSON: {proc.stderr.strip()[:200]}") from exc
    if out.get("error"):
        raise RuntimeError(out["error"])
    data = json.loads(out["body"]) if out.get("body") else {}
    return int(out.get("status", 0)), data


# ── Signed write client (ateles#1270 step 2a) ────────────────────────────────

SWARM_SUB_SUFFIX = "@ateles-swarm"
DEFAULT_NEOTOMA_BASE_URL = "https://neotoma.markmhendrickson.com"
_BEARER_ENV = "NEOTOMA_BEARER_TOKEN"  # gitleaks:allow — the env var's name, not a value

# Governance types: a write to one of these is an instruction to the swarm or a
# grant of authority, so it must never be attributable only to the shared
# bearer. The first seven are the design's governance types
# (docs/foundation/conformance_suite.md WM-22, the set render_data_model.py
# parses); the next three are the names the live record still uses for three of
# them (docs/foundation/migration.md); the last two are named beyond WM-22 by
# the step-1 inventory on ateles#1270. test_neotoma_writer.py asserts this set
# covers WM-22, so a type added to the design fails a test until it is added
# here.
GOVERNANCE_ENTITY_TYPES: frozenset[str] = frozenset(
    {
        # design names (WM-22)
        "agent",
        "agent_policy",
        "agent_grant",
        "workflow",
        "action_policy",
        "swarm_roster",
        "intake_rule",
        # live-record names for design types
        "agent_definition",
        "workflow_definition",
        "execution_policy",
        # named by the ateles#1270 inventory
        "task_policy",
        "checkpoint_brief",
    }
)
# Authority carried by one field of an otherwise ordinary type.
GOVERNANCE_FIELDS: frozenset[tuple[str, str]] = frozenset({("issue", "gate_status")})

# Tiers Neotoma assigns only to a verified signature.
TRUSTED_ATTRIBUTION_TIERS: frozenset[str] = frozenset(
    {"software", "operator_attested", "hardware"}
)

SIGNED_WRITES_ENV_PREFIX = "ATELES_SIGNED_WRITES_"


class SigningMode(str, Enum):
    """Per-daemon switch for NON-governance writes. Governance writes ignore it."""

    OFF = "off"  # bearer, today's behaviour (the default)
    SHADOW = "shadow"  # sign; on a signing failure log ERROR and use the bearer
    ON = "on"  # sign; a signing failure raises


class NeotomaWriteError(RuntimeError):
    """A write did not land. ``status`` is the HTTP status, or None if none was sent."""

    def __init__(self, message: str, *, status: "int | None" = None) -> None:
        super().__init__(message)
        self.status = status


class SignedWriteError(NeotomaWriteError):
    """A write that had to be signed was not: refused rather than sent with the bearer."""


def signed_writes_env_var(daemon: str) -> str:
    """``ATELES_SIGNED_WRITES_<DAEMON>``, the daemon name upper-cased, ``-`` → ``_``."""
    norm = "".join(c if c.isalnum() else "_" for c in str(daemon).strip().upper())
    return f"{SIGNED_WRITES_ENV_PREFIX}{norm}"


def signing_mode(daemon: str, env: "Mapping[str, str] | None" = None) -> SigningMode:
    """Read a daemon's switch. Unset or empty is ``off``; an unrecognised value is ``on``.

    An unrecognised value reads as the restrictive branch: a typo in a switch
    someone meant to turn on must not silently leave the daemon on the bearer.
    """
    name = signed_writes_env_var(daemon)
    raw = (os.environ if env is None else env).get(name, "")
    value = str(raw).strip().lower()
    if value in ("", "off", "0", "false", "no"):
        return SigningMode.OFF
    if value == "shadow":
        return SigningMode.SHADOW
    if value in ("on", "1", "true", "yes"):
        return SigningMode.ON
    log.error("%s=%r is not off|shadow|on; treating it as 'on'", name, raw)
    return SigningMode.ON


# Word forms Neotoma's singulariser leaves alone (entity_type_guard.ts
# IRREGULAR_SINGULAR_NAMES, the entries that could end a type name).
_NOT_PLURAL = frozenset({"news", "data", "analytics", "status", "address"})


def canonical_entity_type(entity_type: str) -> str:
    """The spelling a type name is compared under: folded the way Neotoma folds it, and further.

    Neotoma's store does not match a declared type exactly: a type with no
    exact active schema is filed under a registered one that matches it
    case-insensitively, by singular form, or by schema alias
    (``entity_type_equivalence.ts``), and alias matching also drops accents.
    So ``Agent_Policy``, ``AGENT_POLICY`` and ``agent_policies`` can all land
    as ``agent_policy``. This folds at least as far: compatibility-normalise,
    drop accents and invisible format characters, case-fold, turn every run of
    anything but ``[a-z0-9]`` into ``_``, and trim ``_``. Folding further than
    the server does only ever classes more writes as governance, which is the
    restrictive direction.
    """
    s = unicodedata.normalize("NFKD", str(entity_type))
    s = "".join(
        c for c in s if not unicodedata.combining(c) and unicodedata.category(c) != "Cf"
    )
    return re.sub(r"[^a-z0-9]+", "_", s.casefold()).strip("_")


def _singular(name: str) -> str:
    """Neotoma's ``suggestSingular`` rules, applied to a canonical name."""
    if name in _NOT_PLURAL or name.split("_")[-1] in _NOT_PLURAL:
        return name
    if re.search(r"[^aeiou]ies$", name):
        return name[:-3] + "y"
    if re.search(r"(s|x|z|ch|sh)es$", name):
        return name[:-2]
    if re.search(r"[a-z]s$", name) and not name.endswith("ss"):
        return name[:-1]
    return name


def _type_forms(canonical: str) -> set[str]:
    """Every spelling of ``canonical`` a governance type is matched against."""
    forms = {canonical, _singular(canonical)}
    # Separators folded away too, so ``agentpolicy`` or ``agent__policy``
    # cannot slip past on spacing alone.
    return forms | {f.replace("_", "") for f in forms}


_GOVERNANCE_FORMS: frozenset[str] = frozenset(
    form for t in GOVERNANCE_ENTITY_TYPES for form in _type_forms(canonical_entity_type(t))
)
_GOVERNANCE_FIELD_FORMS: frozenset[tuple[str, str]] = frozenset(
    (form, canonical_entity_type(f))
    for t, f in GOVERNANCE_FIELDS
    for form in _type_forms(canonical_entity_type(t))
)


def is_governance_type(entity_type: object) -> bool:
    """Whether ``entity_type`` names a governance type under any spelling Neotoma folds.

    A missing, non-string or blank type counts as governance: a write whose
    type cannot be read is not known to be safe. Schema aliases a user
    registers are not knowable here without a registry read; Neotoma's built-in
    definitions declare none for these types (``agent_grant`` declares
    ``aliases: []``), and the server-side floor (neotoma#2497) must classify on
    the type the server resolves, not the one declared.
    """
    if not isinstance(entity_type, str):
        return True
    canonical = canonical_entity_type(entity_type)
    if not canonical:
        return True
    return bool(_type_forms(canonical) & _GOVERNANCE_FORMS)


def _is_governance_field(entity_type: object, field_name: object) -> bool:
    if not isinstance(entity_type, str) or field_name is None:
        return False
    fld = canonical_entity_type(str(field_name))
    return any(
        (form, fld) in _GOVERNANCE_FIELD_FORMS
        for form in _type_forms(canonical_entity_type(entity_type))
    )


def is_governance_write(entity_type: object, field_name: "str | None" = None) -> bool:
    """True when a write to ``entity_type`` (and ``field_name``) must be signed.

    The type is matched under :func:`canonical_entity_type`, not exactly, and
    an unreadable type counts as governance (see :func:`is_governance_type`).
    """
    if is_governance_type(entity_type):
        return True
    return bool(field_name) and _is_governance_field(entity_type, field_name)


# The write endpoints this client sends, each with the keys by which its body
# names an entity already on record (neotoma ``origin/main``: the request
# schemas in ``action_schemas.ts`` and the store resolver):
#
# - ``store``: ``entities[].target_id`` (extend mode: the resolver returns the
#   target as the entity to write, without comparing its stored type with the
#   declared one) and ``relationships[].{source,target}_entity_id``;
# - ``correct``: ``entity_id`` (the declared ``entity_type`` is not checked
#   against it);
# - ``create_relationship``: ``source_entity_id`` / ``target_entity_id``;
# - ``create_relationships``: the same, per item of ``relationships``.
#
# Any other endpoint is refused before anything is sent: an endpoint whose id
# keys are not enumerated here cannot be classified, so it is not guessed at.
SUPPORTED_WRITE_PATHS: frozenset[str] = frozenset(
    {"store", "correct", "create_relationship", "create_relationships"}
)


def _write_path(path: object) -> str:
    return str(path or "").strip().strip("/")


def _endpoint_ids_ok(rel: Mapping[str, Any]) -> bool:
    return all(
        isinstance(rel.get(k), str) and bool(rel.get(k))
        for k in ("source_entity_id", "target_entity_id")
    )


def _store_touches_governance(body: Mapping[str, Any]) -> bool:
    entities = body.get("entities")
    if not isinstance(entities, list) or not entities:
        return True
    for ent in entities:
        if not isinstance(ent, Mapping):
            return True
        et = ent.get("entity_type")
        if is_governance_write(et):
            return True
        if any(_is_governance_field(et, k) for k in ent):
            return True
        tid = ent.get("target_id")
        if "target_id" in ent and not (isinstance(tid, str) and tid):
            return True  # an extend whose target cannot be read
    rels = body.get("relationships")
    if rels is None:
        return False
    if not isinstance(rels, list):
        return True
    for rel in rels:
        if not isinstance(rel, Mapping):
            return True
        for end in ("source", "target"):
            idx = rel.get(f"{end}_index")
            eid = rel.get(f"{end}_entity_id")
            if idx is None and not eid:
                return True
            if idx is not None and not (
                isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(entities)
            ):
                return True
    return False


def write_touches_governance(path: str, body: Mapping[str, Any]) -> bool:
    """Whether a write to ``path`` with ``body`` declares a governance type or field.

    Classified by endpoint, never by body shape: each endpoint in
    :data:`SUPPORTED_WRITE_PATHS` reads only the keys Neotoma reads for it. A
    body without its endpoint's shape counts as governance, and so does any
    other endpoint. An existing entity the body names by id is resolved
    separately (:func:`referenced_entities`); this judges only what the body
    declares.
    """
    path = _write_path(path)
    if not isinstance(body, Mapping):
        return True
    if path == "store":
        return _store_touches_governance(body)
    if path == "correct":
        eid = body.get("entity_id")
        if not (isinstance(eid, str) and eid):
            return True
        return is_governance_write(body.get("entity_type"), body.get("field"))
    if path == "create_relationship":
        return not _endpoint_ids_ok(body)
    if path == "create_relationships":
        rels = body.get("relationships")
        if not isinstance(rels, list) or not rels:
            return True
        return not all(isinstance(r, Mapping) and _endpoint_ids_ok(r) for r in rels)
    return True


def _inferred_path(body: Mapping[str, Any]) -> "str | None":
    if isinstance(body.get("entities"), list):
        return "store"
    if "entity_type" in body or "field" in body:
        return "correct"
    return None


def body_touches_governance(body: Mapping[str, Any]) -> bool:
    """:func:`write_touches_governance` with the endpoint inferred from the body.

    A body with an ``entities`` list is read as a store, one with an
    ``entity_type`` or ``field`` as a correct; any other shape counts as
    governance. :class:`NeotomaWriter` does not use this: it is told the
    endpoint and classifies on that.
    """
    path = _inferred_path(body)
    return True if path is None else write_touches_governance(path, body)


def referenced_entities(
    body: Mapping[str, Any], path: "str | None" = None
) -> list[tuple[str, "str | None"]]:
    """``(entity_id, field)`` for every existing entity a write body touches by id.

    Read per endpoint (see :data:`SUPPORTED_WRITE_PATHS`), so a key that
    belongs to another endpoint's shape can neither add an id nor hide one:

    - ``store``: each entity's ``target_id``, once with no field and once per
      key the entity carries, so a governance field (``gate_status`` onto an
      issue reached through ``target_id``) is judged against the target's
      real type; and each relationship's ``*_entity_id``;
    - ``correct``: ``entity_id`` with the corrected field, always, whatever
      else the body carries;
    - ``create_relationship`` / ``create_relationships``: both endpoints of
      every edge.

    With no ``path`` the endpoint is inferred as in :func:`body_touches_governance`.
    """
    path = _write_path(path if path is not None else (_inferred_path(body) or ""))
    out: list[tuple[str, "str | None"]] = []

    def edge_ids(rels: object) -> None:
        for rel in rels if isinstance(rels, list) else []:
            if isinstance(rel, Mapping):
                for end in ("source", "target"):
                    eid = rel.get(f"{end}_entity_id")
                    if isinstance(eid, str) and eid:
                        out.append((eid, None))

    if path == "store":
        for ent in body.get("entities") or []:
            if not isinstance(ent, Mapping):
                continue
            tid = ent.get("target_id")
            if isinstance(tid, str) and tid:
                out.append((tid, None))
                out.extend(
                    (tid, str(k)) for k in ent if k not in ("entity_type", "target_id")
                )
        edge_ids(body.get("relationships"))
    elif path == "correct":
        eid = body.get("entity_id")
        if eid:
            fld = body.get("field")
            out.append((str(eid), str(fld) if fld is not None else None))
    elif path == "create_relationship":
        edge_ids([body])
    elif path == "create_relationships":
        edge_ids(body.get("relationships"))
    return out


@dataclass
class WriteResult:
    """What a write returned, and how it was authenticated."""

    status: int
    data: dict
    signed: bool
    sub: "str | None"  # the identity it was signed as; None for a bearer write

    def observations(self) -> list[tuple[str, str]]:
        """``(entity_id, observation_id)`` pairs the response names."""
        out: list[tuple[str, str]] = []
        ents = self.data.get("entities")
        if isinstance(ents, list):
            for e in ents:
                if isinstance(e, Mapping) and e.get("entity_id") and e.get("observation_id"):
                    out.append((str(e["entity_id"]), str(e["observation_id"])))
        elif self.data.get("entity_id") and self.data.get("observation_id"):
            out.append((str(self.data["entity_id"]), str(self.data["observation_id"])))
        return out

    def entity_ids(self) -> list[str]:
        ents = self.data.get("entities")
        if isinstance(ents, list):
            return [str(e["entity_id"]) for e in ents if isinstance(e, Mapping) and e.get("entity_id")]
        return [str(self.data["entity_id"])] if self.data.get("entity_id") else []


@dataclass
class AttributionCheck:
    """Whether a written observation reads back as signed by the expected identity."""

    ok: bool
    reason: str
    observation_id: "str | None" = None
    agent_sub: "str | None" = None
    attribution_tier: "str | None" = None


def check_observation_attribution(
    observations: Iterable[Mapping[str, Any]],
    observation_id: str,
    expected_sub: str,
) -> AttributionCheck:
    """Find ``observation_id`` among ``observations`` and check who the server recorded.

    Passes only when the observation is present, its ``provenance.agent_sub``
    equals ``expected_sub``, and its tier is one only a verified signature gets.
    """
    for obs in observations:
        if str(obs.get("id") or "") != observation_id:
            continue
        prov = obs.get("provenance") or {}
        if not isinstance(prov, Mapping):
            prov = {}
        sub = prov.get("agent_sub")
        tier = prov.get("attribution_tier")
        if sub != expected_sub:
            return AttributionCheck(
                False, f"observation carries agent_sub {sub!r}, expected {expected_sub!r}",
                observation_id, sub, tier,
            )
        if tier not in TRUSTED_ATTRIBUTION_TIERS:
            return AttributionCheck(
                False, f"observation tier {tier!r} is not a verified-signature tier",
                observation_id, sub, tier,
            )
        return AttributionCheck(True, "signed as expected", observation_id, sub, tier)
    return AttributionCheck(False, "observation not found on read-back", observation_id)


@dataclass
class _Attempt:
    url: str
    headers: dict[str, str]
    content: bytes
    signed: bool
    governance: bool
    fallback_allowed: bool
    path: str = ""
    body: dict = field(default_factory=dict)


class NeotomaWriter:
    """Neotoma write client for one daemon, signing as one named swarm agent.

    ``agent_name`` picks the key (``<agent>.jwk.json`` in the agent keys dir,
    resolved by :func:`agent_identity`) and the identity
    ``<agent>@ateles-swarm``; the key file's own ``sub`` must match it, so an
    ambient ``NEOTOMA_AAUTH_SUB`` can never swap the identity. ``daemon`` names
    the switch (``ATELES_SIGNED_WRITES_<DAEMON>``) and defaults to the agent.

    A signed request carries the signature as its only credential. The bearer is
    deliberately left off: Neotoma treats a signature that fails verification
    as no signature, so a request carrying both would land as a bearer write
    when the signature was bad, which is the fallback this client exists to
    prevent.
    """

    READBACK_PAGE = 100
    READBACK_MAX_PAGES = 5

    def __init__(
        self,
        agent_name: str,
        *,
        daemon: "str | None" = None,
        mode: "SigningMode | None" = None,
        base_url: "str | None" = None,
        bearer: "str | None" = None,
        keys_dir: "str | Path | None" = None,
        timeout: float = 15.0,
    ) -> None:
        name = str(agent_name or "").strip().lower()
        if not name:
            raise ValueError("NeotomaWriter needs an agent name")
        self.agent_name = name
        self.sub = f"{name}{SWARM_SUB_SUFFIX}"
        self.daemon = str(daemon or name)
        self.mode = mode if mode is not None else signing_mode(self.daemon)
        self.base_url = str(
            base_url or os.environ.get("NEOTOMA_BASE_URL") or DEFAULT_NEOTOMA_BASE_URL
        ).rstrip("/")
        self._bearer = bearer if bearer is not None else os.environ.get(_BEARER_ENV)
        self._keys_dir = keys_dir
        self.timeout = timeout
        self._signer: "HttpSigSigner | None" = None

    # ── request construction (shared by the sync and async paths) ──

    def _load_signer(self) -> HttpSigSigner:
        if self._signer is not None:
            return self._signer
        ident = agent_identity(self.agent_name, sub=self.sub, keys_dir=self._keys_dir)
        if ident is None:
            raise AAuthSigningError(f"no usable AAuth JWK for agent {self.agent_name!r}")
        key = Path(ident["key"])
        self._signer = load_http_sig_signer(
            key, expected_sub=self.sub, issuer=_pinned_issuer(key)
        )
        return self._signer

    def _bearer_attempt(self, path: str, body: dict, governance: bool) -> _Attempt:
        if governance:  # defence in depth: nothing builds this, and nothing may
            raise SignedWriteError(f"refusing a bearer write to a governance type ({path})")
        if not self._bearer:
            raise NeotomaWriteError(f"no {_BEARER_ENV} for an unsigned write to /{path}")
        return _Attempt(
            url=f"{self.base_url}/{path}",
            headers={
                "Authorization": f"Bearer {self._bearer}",
                "Content-Type": "application/json",
            },
            content=json.dumps(body).encode("utf-8"),
            signed=False,
            governance=False,
            fallback_allowed=False,
            path=path,
            body=body,
        )

    def _signed_attempt(self, path: str, body: dict, governance: bool) -> _Attempt:
        signer = self._load_signer()
        url = f"{self.base_url}/{path}"
        content = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        # One lower-case header map: the signer covers `content-type`, and a
        # second, differently-cased copy would be sent as a combined value the
        # signature does not cover.
        headers = {
            k.lower(): v
            for k, v in signer.sign_headers(
                method="POST", url=url, body=content, content_type="application/json"
            ).items()
        }
        headers["content-type"] = "application/json"
        headers["x-agent-label"] = self.sub
        return _Attempt(
            url=url,
            headers=headers,
            content=content,
            signed=True,
            governance=governance,
            fallback_allowed=(not governance and self.mode == SigningMode.SHADOW),
            path=path,
            body=body,
        )

    def _fall_back_or_raise(self, attempt_path: str, body: dict, governance: bool,
                            allowed: bool, reason: str) -> _Attempt:
        if not allowed:
            raise SignedWriteError(
                f"{self.sub}: {reason}; not falling back to the bearer "
                f"({'governance write' if governance else f'mode {self.mode.value}'})"
            )
        log.error(
            "%s: %s; shadow mode — falling back to the bearer for /%s",
            self.sub, reason, attempt_path,
        )
        return self._bearer_attempt(attempt_path, body, governance)

    def _first_attempt(
        self,
        path: str,
        body: dict,
        entity_types: "Iterable[str] | None",
        referenced: "Iterable[tuple[str | None, str | None]]" = (),
    ) -> _Attempt:
        """Classify the write and build its first request.

        ``body`` is always inspected. ``entity_types`` and ``referenced`` (the
        resolved ``(type, field)`` of every existing entity the body names by
        id; a type of None means the lookup failed) can only ADD to what makes
        a write governance: nothing a caller passes can declassify a body that
        writes a governance type.
        """
        path = _write_path(path)
        governance = write_touches_governance(path, body)
        if entity_types is not None:
            governance = governance or any(is_governance_write(t) for t in entity_types)
        governance = governance or any(is_governance_write(t, f) for t, f in referenced)
        if not governance and self.mode == SigningMode.OFF:
            return self._bearer_attempt(path, body, governance)
        try:
            return self._signed_attempt(path, body, governance)
        except AAuthSigningError as exc:
            return self._fall_back_or_raise(
                path, body, governance,
                allowed=(not governance and self.mode == SigningMode.SHADOW),
                reason=f"could not sign ({exc})",
            )

    def _after_response(
        self, attempt: _Attempt, status: int, text: str
    ) -> "WriteResult | _Attempt":
        if attempt.signed and _refuses_signed_identity(status, text):
            return self._fall_back_or_raise(
                attempt.path, attempt.body, attempt.governance,
                allowed=attempt.fallback_allowed,
                reason=f"server refused the signed write (HTTP {status}: {text[:200]})",
            )
        if status >= 400:
            raise NeotomaWriteError(
                f"/{attempt.path} -> HTTP {status}: {text[:300]}", status=status
            )
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {}
        return WriteResult(
            status=status,
            data=data if isinstance(data, dict) else {},
            signed=attempt.signed,
            sub=self.sub if attempt.signed else None,
        )

    # ── async API ──

    async def apost(
        self, path: str, body: dict, *, entity_types: "Iterable[str] | None" = None
    ) -> WriteResult:
        """POST a write.

        The types are always read from ``body``. ``entity_types`` names extra
        types to class the write by; it can make a write governance, never
        ordinary. Every existing entity the body names by id (a correct's
        target, a relationship endpoint) has its real type looked up first, and
        a failed lookup counts as governance.
        """
        _require_supported(path)
        entity_types = None if entity_types is None else list(entity_types)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            ids = self._ids_to_resolve(path, body, entity_types)
            types: dict[str, "str | None"] = {}
            for eid, _ in ids:
                if eid not in types:
                    types[eid] = await self._alookup_type(client, eid)
            referenced = [(types[eid], fld) for eid, fld in ids]
            attempt = self._first_attempt(path, body, entity_types, referenced)
            while True:
                resp = await client.post(attempt.url, headers=attempt.headers, content=attempt.content)
                nxt = self._after_response(attempt, resp.status_code, resp.text)
                if isinstance(nxt, WriteResult):
                    return nxt
                attempt = nxt

    async def astore(
        self,
        entities: list[dict],
        *,
        idempotency_key: str,
        relationships: "list[dict] | None" = None,
        strict: "bool | None" = None,
        commit: bool = True,
    ) -> WriteResult:
        return await self.apost("store", _store_body(entities, idempotency_key, relationships, strict, commit))

    async def acorrect(
        self, entity_type: str, entity_id: str, field_name: str, value: Any, *, idempotency_key: str
    ) -> WriteResult:
        return await self.apost("correct", _correct_body(entity_type, entity_id, field_name, value, idempotency_key))

    async def aconfirm_attribution(
        self, result: WriteResult, expected_sub: "str | None" = None
    ) -> AttributionCheck:
        """Read back every observation ``result`` names; pass only if all carry ``expected_sub``."""
        pre = _precheck(result)
        if pre is not None:
            return pre
        want = expected_sub or self.sub
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for entity_id, observation_id in result.observations():
                check = AttributionCheck(False, "observation not found on read-back", observation_id)
                for page in range(self.READBACK_MAX_PAGES):
                    attempt = self._readback_attempt(entity_id, page)
                    resp = await client.post(attempt.url, headers=attempt.headers, content=attempt.content)
                    obs = _observations_from(resp.status_code, resp.text)
                    check = check_observation_attribution(obs, observation_id, want)
                    if check.ok or check.reason != "observation not found on read-back" or len(obs) < self.READBACK_PAGE:
                        break
                if not check.ok:
                    return check
        return AttributionCheck(True, "every observation signed as expected")

    # ── sync API (same rules; for daemons without an event loop) ──

    def post(
        self, path: str, body: dict, *, entity_types: "Iterable[str] | None" = None
    ) -> WriteResult:
        """POST a write; same classification as :meth:`apost`."""
        _require_supported(path)
        entity_types = None if entity_types is None else list(entity_types)
        with httpx.Client(timeout=self.timeout) as client:
            ids = self._ids_to_resolve(path, body, entity_types)
            types: dict[str, "str | None"] = {}
            for eid, _ in ids:
                if eid not in types:
                    types[eid] = self._lookup_type(client, eid)
            referenced = [(types[eid], fld) for eid, fld in ids]
            attempt = self._first_attempt(path, body, entity_types, referenced)
            while True:
                resp = client.post(attempt.url, headers=attempt.headers, content=attempt.content)
                nxt = self._after_response(attempt, resp.status_code, resp.text)
                if isinstance(nxt, WriteResult):
                    return nxt
                attempt = nxt

    def store(
        self,
        entities: list[dict],
        *,
        idempotency_key: str,
        relationships: "list[dict] | None" = None,
        strict: "bool | None" = None,
        commit: bool = True,
    ) -> WriteResult:
        return self.post("store", _store_body(entities, idempotency_key, relationships, strict, commit))

    def correct(
        self, entity_type: str, entity_id: str, field_name: str, value: Any, *, idempotency_key: str
    ) -> WriteResult:
        return self.post("correct", _correct_body(entity_type, entity_id, field_name, value, idempotency_key))

    def confirm_attribution(
        self, result: WriteResult, expected_sub: "str | None" = None
    ) -> AttributionCheck:
        pre = _precheck(result)
        if pre is not None:
            return pre
        want = expected_sub or self.sub
        with httpx.Client(timeout=self.timeout) as client:
            for entity_id, observation_id in result.observations():
                check = AttributionCheck(False, "observation not found on read-back", observation_id)
                for page in range(self.READBACK_MAX_PAGES):
                    attempt = self._readback_attempt(entity_id, page)
                    resp = client.post(attempt.url, headers=attempt.headers, content=attempt.content)
                    obs = _observations_from(resp.status_code, resp.text)
                    check = check_observation_attribution(obs, observation_id, want)
                    if check.ok or check.reason != "observation not found on read-back" or len(obs) < self.READBACK_PAGE:
                        break
                if not check.ok:
                    return check
        return AttributionCheck(True, "every observation signed as expected")

    # ── target-type lookup (a correct's declared type is not checked by /correct) ──

    @staticmethod
    def _ids_to_resolve(
        path: str, body: dict, entity_types: "Iterable[str] | None"
    ) -> list[tuple[str, "str | None"]]:
        """Entities whose real type must be read before classifying; none if already governance.

        A write the body (or the caller's extra types) already classes as
        governance is signed whatever its targets are, so it costs no read.
        """
        if write_touches_governance(path, body):
            return []
        if entity_types is not None and any(is_governance_write(t) for t in entity_types):
            return []
        return referenced_entities(body, path)

    def _type_lookup_request(self, entity_id: str) -> "tuple[str, dict[str, str]] | None":
        """A GET of one entity: bearer when present, else signed; None if neither is possible."""
        url = f"{self.base_url}/entities/{quote(str(entity_id), safe='')}"
        if self._bearer:
            return url, {"Authorization": f"Bearer {self._bearer}"}
        try:
            signer = self._load_signer()
        except AAuthSigningError:
            return None
        headers = {
            k.lower(): v
            for k, v in signer.sign_headers(method="GET", url=url, body=None, content_type=None).items()
        }
        headers["x-agent-label"] = self.sub
        return url, headers

    def _lookup_type(self, client: httpx.Client, entity_id: str) -> "str | None":
        req = self._type_lookup_request(entity_id)
        if req is None:
            return None
        try:
            resp = client.get(req[0], headers=req[1])
        except httpx.HTTPError as exc:
            log.warning("type lookup for %s failed: %s", entity_id, exc)
            return None
        return _entity_type_from(resp.status_code, resp.text)

    async def _alookup_type(self, client: httpx.AsyncClient, entity_id: str) -> "str | None":
        req = self._type_lookup_request(entity_id)
        if req is None:
            return None
        try:
            resp = await client.get(req[0], headers=req[1])
        except httpx.HTTPError as exc:
            log.warning("type lookup for %s failed: %s", entity_id, exc)
            return None
        return _entity_type_from(resp.status_code, resp.text)

    def _readback_attempt(self, entity_id: str, page: int) -> _Attempt:
        """A read of one entity's observations: bearer when present, else signed."""
        body = {
            "entity_id": entity_id,
            "limit": self.READBACK_PAGE,
            "offset": page * self.READBACK_PAGE,
        }
        if self._bearer:
            return _Attempt(
                url=f"{self.base_url}/observations/query",
                headers={"Authorization": f"Bearer {self._bearer}", "Content-Type": "application/json"},
                content=json.dumps(body).encode("utf-8"),
                signed=False, governance=False, fallback_allowed=False,
                path="observations/query", body=body,
            )
        return self._signed_attempt("observations/query", body, governance=False)


def _require_supported(path: str) -> None:
    """Refuse, before anything is sent, a write to an endpoint the classifier does not know."""
    if _write_path(path) not in SUPPORTED_WRITE_PATHS:
        raise NeotomaWriteError(
            f"/{_write_path(path)} is not a write endpoint this client classifies "
            f"(supported: {', '.join(sorted(SUPPORTED_WRITE_PATHS))})"
        )


def _pinned_issuer(key_path: Path) -> str:
    """The issuer to sign under: the key file's own ``iss``, else the default.

    Never the ambient ``NEOTOMA_AAUTH_ISS``, which :func:`load_http_sig_signer`
    would otherwise consult: like the ``sub``, the identity a daemon signs as
    comes from its key file and this module, never from its environment.
    """
    try:
        raw = json.loads(key_path.read_text())
    except Exception as exc:  # noqa: BLE001 — name the file, never its contents
        raise AAuthSigningError(f"could not load AAuth JWK from {key_path.name}") from exc
    iss = str(raw.get("iss") or "").strip() if isinstance(raw, dict) else ""
    return iss or DEFAULT_AAUTH_ISSUER


def _entity_type_from(status: int, text: str) -> "str | None":
    """The ``entity_type`` of a ``GET /entities/{id}`` response, or None when unreadable."""
    if status >= 400:
        return None
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    for src in (data, data.get("entity"), data.get("snapshot")):
        if isinstance(src, Mapping):
            et = src.get("entity_type")
            if isinstance(et, str) and et.strip():
                return et
    return None


def _refuses_signed_identity(status: int, text: str) -> bool:
    """Whether the server refused the write because of the signing identity.

    401/403 are the documented refusals. Neotoma ``origin/main`` also surfaces a
    grant capability denial (``AgentCapabilityError``) as HTTP 500
    ``DB_QUERY_FAILED`` with "is not permitted to" in the message, observed live
    on 2026-09-25, so that shape is a refusal too.
    """
    if status in (401, 403):
        return True
    return status >= 500 and "is not permitted to" in (text or "")


def _store_body(entities, idempotency_key, relationships, strict, commit) -> dict:
    body: dict[str, Any] = {"entities": list(entities), "idempotency_key": idempotency_key}
    if relationships:
        body["relationships"] = list(relationships)
    if strict is not None:
        body["strict"] = bool(strict)
    if not commit:
        body["commit"] = False
    return body


def _correct_body(entity_type, entity_id, field_name, value, idempotency_key) -> dict:
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "field": field_name,
        "value": value,
        "idempotency_key": idempotency_key,
    }


def _precheck(result: WriteResult) -> "AttributionCheck | None":
    if not result.signed:
        return AttributionCheck(False, "write was sent with the bearer, not signed")
    if not result.observations():
        return AttributionCheck(False, "write response names no observation to read back")
    return None


def _observations_from(status: int, text: str) -> list[dict]:
    if status >= 400:
        raise NeotomaWriteError(f"observations read-back -> HTTP {status}: {text[:200]}", status=status)
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        return []
    obs = data.get("observations") if isinstance(data, dict) else None
    return [o for o in obs if isinstance(o, dict)] if isinstance(obs, list) else []
