# Switching a daemon to signed Neotoma writes

`lib/daemon_runtime/neotoma_signed.NeotomaWriter` is the write client daemons use
to reach Neotoma with a verified identity (ateles#1270). This page is how to move
one daemon's writes from the shared bearer token onto its own signature. It is
written for Anthus, the first daemon on the client; the steps are the same for
any other, with the daemon's name substituted.

## What the client does before you touch anything

- **Governance writes are always signed.** A write to a governance type
  (`GOVERNANCE_ENTITY_TYPES`, matched under any spelling Neotoma folds: case,
  separators, accents, simple plurals), to `issue.gate_status`, onto a
  governance entity by relationship, or onto an existing entity named by id
  whose real type turns out to be governance, is signed or refused. The ids
  resolved are a store entity's `target_id` (fields written through it are
  judged against the target's real type, so `gate_status` onto an issue
  counts), a correct's `entity_id`, and every relationship endpoint. A failed
  lookup counts as governance. The switch below does not apply to it and there
  is no bearer fallback.
- **Only known endpoints.** The client sends `store`, `correct`,
  `create_relationship` and `create_relationships`, each classified on the
  keys Neotoma reads for that endpoint. Any other path is refused before
  anything is sent.
- **Every other write follows the daemon's switch.** Unset, the daemon keeps
  writing with the bearer exactly as before.
- **The identity is pinned.** The daemon signs as `<agent>@ateles-swarm`, with
  the key file's own `sub` required to match and the issuer taken from the key
  file (else the default). Ambient `NEOTOMA_AAUTH_SUB` / `NEOTOMA_AAUTH_ISS`
  are ignored.

## The switch

`ATELES_SIGNED_WRITES_<DAEMON>`, the daemon's name upper-cased (Anthus:
`ATELES_SIGNED_WRITES_ANTHUS`).

| Value | Non-governance writes |
|---|---|
| unset / `off` | Bearer, as today. The default. |
| `shadow` | Signed. If signing fails or the server refuses the signed identity, log at ERROR and retry once with the bearer. |
| `on` | Signed. Any failure raises; nothing falls back. |
| anything else | Read as `on`, the restrictive branch. |

Set it in the daemon's LaunchAgent `EnvironmentVariables`, or in the shared env
file `lib/daemon_runtime/__init__.py` loads at import. That file is materialized
from the SOPS snapshot (see [secrets management](../secrets_management.md)), so
a hand edit there can be overwritten; change the snapshot instead.

## Before switching on

1. **Key.** `<agent>.jwk.json` exists in the agent keys directory in the
   canonical format ([keys](keys.md)): EC P-256, `sub: <agent>@ateles-swarm`,
   a `kid`. Check field names only; never print the file.
2. **Grant.** The daemon's `agent_grant` (matched on its `sub` and issuer) must
   allow every operation and type the daemon writes. For Anthus:
   - `store_structured` on `daemon_report`, `strategy_revision_proposal` and
     `strategy_drift_signal`;
   - `correct` on `strategy_revision_proposal` (the generalizer adds new
     evidence to an open proposal's `drift_signal_refs`);
   - **never** `agent_policy`: the generalizer only proposes rules, and a
     signed write to `agent_policy` must stay refused.
   - **When widening this (or any) grant, pin `match_thumbprint` to the
     agent's own key**, not `sub` alone. `sub` is a label the agent's own
     token claims and Neotoma does not verify it against any key; a grant
     matched only on `sub`/issuer would admit a signature from a different
     key that happened to carry the same label. `match_thumbprint` is the
     RFC 7638 thumbprint of the agent's public key — the same value the
     approval-rule signer check pins (see `docs/data_types.md`'s
     `strategy_revision_proposal` section, and Falco's PR #1274 round-3
     finding).

   Probe before switching: a signed dry-run store (`commit: false`, nothing
   persisted) through the client for each type. An admitted type answers
   `would_create`; a type the grant lacks is refused, currently as HTTP 500
   with "is not permitted to" in the message, which the client treats as a
   refusal of the signed identity.
3. **What happens if the grant is short.** Under `on`, refused writes raise and
   the daemon logs them; for Anthus, generalization stops until the grant is
   widened (it fails closed). Under `shadow`, they fall back to the bearer.

## Order

1. Widen the grant (operator).
2. Set the switch to `shadow` and restart the daemon from its deployment
   checkout. Verify the new process: a new pid, and the variable present in
   the running process's environment.
3. Confirm attribution on the next write (below).
4. Set `on`, restart, confirm again.

## Confirming a write is signed

In code, `NeotomaWriter.confirm_attribution(result)` reads each written
observation back and passes only when `provenance.agent_sub` is the expected
sub AND `provenance.agent_thumbprint` equals this writer's own key thumbprint
(`NeotomaWriter.thumbprint`), at a verified-signature tier (`software`,
`operator_attested`, `hardware`). The thumbprint check is required, not
optional: `agent_sub` is a label the writer's own token claims, and Neotoma
does not verify it against any key, so a different key whose token happened
to carry the same `sub` would pass a sub-only check. The thumbprint is what
the signature actually proves. By hand, for the entity the daemon just wrote:

```sh
# substitute the entity id the daemon just wrote
curl -s -H "Authorization: Bearer $NEOTOMA_BEARER_TOKEN" \
  -H 'content-type: application/json' "$NEOTOMA_BASE_URL/observations/query" \
  -d '{"entity_id":"<ent_id>","limit":5}' | \
  jq '.observations | sort_by(.observed_at) | last | .provenance | {agent_sub, attribution_tier, agent_thumbprint}'
```

Expect `{"agent_sub": "anthus@ateles-swarm", "attribution_tier": "software",
"agent_thumbprint": "<Anthus's own key thumbprint>"}`. A `null` sub, an
unsigned tier, or a thumbprint that does not match Anthus's key file means
the write did not verifiably come from Anthus's own key.

## Rolling back

Unset the variable (or set `off`) and restart. Governance writes stay signed
regardless; everything else returns to the bearer.
