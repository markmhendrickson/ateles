# Person server evaluation — should Ateles adopt AAuth's governance layer?

**Date:** 2026-08-18 · **Spec:** `draft-hardt-oauth-aauth-protocol-10` ·
**Probed:** `person.hello.coop` (live)

## The question

AAuth defines an orthogonal *agent governance* layer hosted at a person server
(PS) the operator chooses: missions, per-action permission, audit, and consent
relay. Ateles independently grew two mechanisms that occupy the same ground:

| Ateles mechanism | AAuth counterpart |
|---|---|
| `agent_grant` — per-agent capability allowlist (ops, entity types, MCP tools) | PS-managed permission + audit |
| `execution_policy` — scoped autonomy for swarm dispatch, with checkpoints and fallbacks | Missions |

Do we converge on the standard, or keep ours and document the divergence?

## What the live PS actually implements

`https://person.hello.coop/.well-known/aauth-person.json` returns:

```json
{
  "issuer": "https://person.hello.coop",
  "token_endpoint": "https://person.hello.coop/aauth/token",
  "auth_token_endpoint": "https://person.hello.coop/aauth/token/auth",
  "person_token_endpoint": "https://person.hello.coop/aauth/token/person",
  "interaction_endpoint": "https://person.hello.coop/auth",
  "jwks_uri": "https://issuer.hello.coop/.well-known/jwks.json",
  "accept_signature_algs": ["Ed25519", "ES256", "RS256"]
}
```

Measured against the endpoints draft-10 names for a PS:

| Endpoint | Live? |
|---|---|
| `person_token_endpoint` | ✅ |
| `token_endpoint` | ✅ |
| `interaction_endpoint` | ✅ |
| `mission_endpoint` | ❌ absent |
| `permission_endpoint` | ❌ absent |
| `audit_endpoint` | ❌ absent |

**The deployed PS implements the identity half, not the governance half.** The
three endpoints that would replace `agent_grant` and `execution_policy` are not
exposed. Missions exist in the specification, not yet in this deployment.

## Finding

The choice is not "converge or diverge" — it is not yet available to make.
Adopting the PS today would buy us:

- **Person tokens and auth tokens** — a standard way to assert *which person*
  an agent acts for, to resources outside our trust domain.
- **Interaction relay** — a standard consent flow.

It would not replace `agent_grant` or `execution_policy`, because the endpoints
that would do so aren't there.

That inverts the framing from the initial review. The governance layer is the
part our homegrown mechanisms duplicate, and it is the part that is not yet
deployable. The identity layer is deployable and is the part we *don't* have.

## Recommendation

**Keep `agent_grant` and `execution_policy`. Do not attempt convergence now.**
Revisit when `mission_endpoint` / `permission_endpoint` / `audit_endpoint`
appear in the live metadata — that is the concrete trigger, and it is cheap to
re-probe.

Separately, the PS *is* worth adopting for identity if we ever need an Ateles
agent to authenticate to a third-party resource that is not Neotoma. Today
every agent talks only to Neotoma, where our own grant layer is the policy
boundary and a person token adds nothing. So this stays on the shelf until
there is a genuine cross-domain resource in play.

## Corollary already actioned

`accept_signature_algs` lists `Ed25519` first. Our Ed25519 support (shipped in
this branch) is what any future interoperation would require, and it satisfies
the draft-10 §12.8.1 MUST regardless of the PS decision. That work stands on
its own.

## Re-check procedure

```bash
curl -s https://person.hello.coop/.well-known/aauth-person.json | python3 -m json.tool
```

If `mission_endpoint` appears, reopen this evaluation.
