# The grammar by which an `agent_grant` expresses a tool

**Ratified by decision 86 (2026-09-08), and still not an implementation.** The rule this document's
mechanics serve is stated in
`foundation/authority_model.md#a-capability-names-a-tool-as-toolsurfaceoperation-and-that-is-what-a-harness-allowlist-is-compared-against`,
which ruled the grammar and named this document as the mapping's home. Read the rule there; read the
mapping, the migration path, and the parity test's two sides here. No grant is changed, no parity test is
written, and no dispatch code is touched by this document — what it makes mechanical is the test and the
migration, which are separate work.

**Derived from:** decision 42's ruling in `migration.md#where-a-skills-harness-mechanics-live`; the
parity measurement of 2026-09-07 (issue #803) and its report; `authority_model.md#grants` and `#the-tuple`;
`principles.md` invariants 5, 7, and 9; `data_model.md#concepts`; `vocabulary.md`'s `domain` and
`permission scope`. Code read on `origin/preview/foundation-all` @ `755d44a5`.

---

## 0. What was verified, and what could not be

Every premise this design rests on was checked against the code or the corpus rather than taken from the
brief. Four did not hold as stated, and one central figure could not be re-verified at all.

| Premise as given | Verdict |
|---|---|
| `GrantChecker._parse` exists and builds `tool_grants` keyed `"<server>:<tool>"` | **holds** — `grant_checker.py`, `_parse` strips the `tool:` prefix; `tool_constraints` already implements both wildcard tiers |
| The dispatcher appends `mcp__mcpsrv_neotoma__*` unconditionally to every restricted allowlist | **holds** — `skill_runner.py`, in the `--allowed-tools` block |
| `tool_allowlist == "*"` causes *no* `--allowed-tools` flag to be passed | **holds** — the guard is `provider == "claude" and tools != ["*"]`; the wildcard falls to the `else` branch |
| Only `claude` receives an allowlist; `cursor` gets `--force --trust --approve-mcps` | **holds** — `_provider_command`, read directly |
| The grant proxy has **zero references outside its own directory** | **does not hold as stated.** Zero *executable* references — no `.py`, `.json`, `.plist` or `.sh` outside its directory wires it in. But it is referenced in six documents, including `conformance.md`'s own document-to-code map, `docs/architecture.md` (which calls it "shipped"), and `docs/aauth.md`. The corpus has already assigned it an owner document. It is unwired, not unknown |
| `authority_model.md#grants` names the fail-open wildcard as a defect | **holds, and more strongly than stated** — "a stub with a wildcard tool allowlist is the fail-open shape", and separately "a capability naming every type is not an allowlist but the default-allow this rule rejects" |
| Decision 85 is on branch `docs/register-decision-85-artifact-separation` | **does not hold** — the branch is `docs/register-decision-85-artifact-**homing**`. The number is right; the branch name is not |
| Neotoma prod is flaky: query 502/401 while `/health` returns 200 | **does not hold** — `/health` returned **502** on every attempt during this pass. Prod was fully down, not partially |
| 31 `agent_grant` entities, 0 with a `tool:` capability | **could not be re-verified.** Five query attempts over roughly two minutes returned 502 on the REST path and 502 through the MCP proxy. The figure is carried from the measurement report, dated 2026-09-07, and is **not independently confirmed by this pass** |

Two findings the brief did not anticipate, both load-bearing below:

- **A harness-side grant grammar already exists and is machine-validated.** `scripts/linters/validate_tool_allowlist.py`, wired into `scripts/lint.sh` under the comment "tool_allowlist grant grammar", recognizes exactly four forms and warns on anything else: `*`, a bare tool name, `mcp__<server>__<tool-or-*>`, and `Bash(<command>)` / `Bash(<command>:*)`. It exists because an undocumented `bash:<command>` prefix was **silently dropped** by the CLI parser — a grant that looked granted and was not. The grammar this design must map onto is therefore not hypothetical and not mine to invent: it is already written down, already enforced, and already carries one scar.
- **The fail-open wildcard is manufactured in code, not only inherited from data.** `agent_loader.py` returns `tool_allowlist="*"` for a **stub** — a definition whose load failed — and defaults the field to `"*"` when absent. So a *failed read* yields the widest possible reach. That is `principles.md` invariant 5 and `authority_model.md`'s "a degraded read never synthesizes a value more permissive than success would have returned" violated in the loader, independently of any grant. A parity test that reads the field would score a stub as a legitimate wildcard.

---

## 1. The bijection

### The shape

A tool capability is an ordinary capability of the tuple, not a new kind of thing. `vocabulary.md` already
splits the two terms this needs: the **domain** is "what the principal may act on", the **permission scope**
is "what it may do there". A tool call has exactly that shape — a server is a domain, a tool is an operation
in it — so the grant expresses a tool as:

```
op = "tool:<surface>:<operation>"
```

with `param_constraints` unchanged, and both halves matching `[A-Za-z0-9_.-]+` or the single character `*`.
This is **the shape already in the code**: `GrantChecker._parse` builds `tool_grants` from exactly this op
form, and `tool_constraints` already resolves `"<surface>:<operation>"`, then `"<surface>:*"`, then `"*"`.
The grammar is therefore not introduced here. It is *specified* here, and its `<surface>` half is widened
from "MCP server" to "capability surface" so that the non-MCP tools of section 3 have a home.

### The mapping, in both directions

| Grant capability | Harness allowlist entry | Direction |
|---|---|---|
| `tool:mcpsrv_neotoma:store` | `mcp__mcpsrv_neotoma__store` | bijective |
| `tool:mcpsrv_neotoma:*` | `mcp__mcpsrv_neotoma__*` | bijective |
| `tool:harness:Read` | `Read` | bijective, via the reserved surface (section 3) |
| `tool:shell:gh` | `Bash(gh:*)` | bijective, via the reserved surface (section 3) |
| `tool:shell:*` | `Bash` | bijective — bare `Bash` is unscoped shell |
| `tool:*` | `*` | **expressible, and refused** (section 2) |

Normalization is one function, defined once, used by both sides of the parity test and by any future
derivation: `mcp__<a>__<b>` ⇄ `tool:<a>:<b>`; a bare CamelCase tool name ⇄ `tool:harness:<name>`;
`Bash(<cmd>:*)` and `Bash(<cmd>)` ⇄ `tool:shell:<cmd>`; bare `Bash` ⇄ `tool:shell:*`. Invariant 9 requires
this live in one module that both readers import, never a reimplementation on each side.

### Where it is not one-to-one, and what happens

Three asymmetries, each resolved in the closed direction per invariant 5.

**A grant entry with no harness expression.** `param_constraints` have no allowlist counterpart — the harness
can say *which* tool, never *with what arguments*. This is not a defect in the mapping; it is the reason the
proxy exists (section 7). For parity, a constrained capability is compared on its `<surface>:<operation>`
half only, and the test records that the constraint is **unenforced by the harness** for that agent. That is
a reporting-only mark in decision 42's own sense, named rather than hidden, per invariant 1.

**A harness entry with no grant expression.** Under this grammar there is none: every one of the validator's
four recognized forms maps. An *unrecognized* form — the fifth class the validator already warns on — maps
to nothing, and the parity test goes **red**, not `unknown`. An entry nobody can parse is a reach nobody can
bound, and the `bash:` incident is the proof that unparsed does not mean inert.

**Two grant entries collapsing onto one harness entry.** `tool:mcpsrv_neotoma:store` and
`tool:mcpsrv_neotoma:correct` are two capabilities; a harness that can only express
`mcp__mcpsrv_neotoma__*` widens them into one. Parity compares **sets after expansion against the
server's declared tool list**, and where the harness cannot express the narrower set, the test reports the
pair as **wider-than-grant** — which it is — rather than treating the coarser entry as equal. The harness's
inability to be narrow is not a reason to call it equal.

---

## 2. Wildcard semantics

**`tool:*` is expressible in the grammar and refused by the grant.** Both halves are needed, and they are
different claims.

Expressible, because the parser already accepts it, and a grammar in which the fail-open shape is
*inarticulable* cannot describe the state the swarm is actually in. The migration has to be able to write
down what it is migrating away from, and the parity test has to be able to report a wildcard as a wildcard
rather than as a parse error.

Refused, because the corpus rules it out twice, in terms, and neither is about tools by accident:

- `authority_model.md#grants`: "a stub with a wildcard tool allowlist is the **fail-open shape**".
- Decision 41, in the same section: "a capability naming every type is not an allowlist but the
  default-allow this rule rejects, written as a grant". Decision 42 then makes the tool dimension a
  capability of the same `capabilities[]` array, and says so: "a wildcard over tools is the same fail-open
  shape as a wildcard over types."

So the rule is: **a grant capability whose `<surface>` half is `*` is invalid, and a validator refuses it at
the write.** `tool:*` never appears in a stored grant.

**`tool:<surface>:*` — a whole surface — is admissible but bounded.** It is a real domain with a real
enumerable membership, which is what separates it from `tool:*`. Three bounds, each with a reason from the
corpus rather than from taste:

1. **The surface must be enumerable at check time.** A surface wildcard is shorthand for a set, and a
   shorthand whose set cannot be listed is a wildcard wearing a domain's clothes. Where the tool list cannot
   be read, the capability resolves `Indeterminate`, which `authority_model.md` resolves to deny at an
   enforcement point.
2. **It never spans surfaces.** `tool:mcpsrv_neotoma:*` grants Neotoma tools and nothing else. This is the
   domain term doing its job.
3. **It is a governance write like any other**, and per decision 41 it widens only by one.

**The unconditional `mcp__mcpsrv_neotoma__*` append is the thing this decision most directly condemns.**
It is a surface wildcard granted by the *dispatcher*, to every restricted agent, with no grant behind it and
no author — reaching `delete_entity`, `merge_entities`, and `register_schema` from an agent whose declared
allowlist is one tool. Under this design it becomes what it always was in substance: a capability that must
be **written into each grant that needs it**, by someone, on a date, or not held. This design does not remove
it — removing it breaks every agent's Neotoma access on the next dispatch — it names it as the largest single
unauthored grant in the swarm and puts its removal in the migration's last stage, behind the grants that
replace it.

**And the loader's stub wildcard is refused outright.** A failed load must not yield `"*"`. Whatever the
migration decides for real agents, a stub's reach is the empty set, and the parity test reads a stub as
`unknown`, never as a wildcard grant.

---

## 3. Non-MCP entries

`Bash`, `Write`, `Edit`, `WebFetch`, `Read`, and the parameterised `Bash(gh:*)` are capabilities no MCP
server mediates. Three answers were available.

**Rejected: the grant cannot name them.** This is the status quo, and it is the worst of the three. It would
leave `ateles` — an active T2 agent — holding shell and filesystem reach that no governance write ever
authorized, with the design's own position being that this reach is unnameable. Decision 42 ruled that "the
tools a principal may invoke are a dimension of its grant" without excepting the ones with no server behind
them, and the reach that most needs bounding is precisely the reach with no mediator.

**Rejected: a separate field.** A second list beside `capabilities[]` is a second home for one bound, which
invariant 9 forbids, and it would need its own parity test.

**Adopted: two reserved surfaces, `harness` and `shell`.**

- `tool:harness:<Tool>` — a tool the harness itself provides: `tool:harness:Read`, `tool:harness:Write`,
  `tool:harness:Edit`, `tool:harness:WebFetch`. The harness is the surface. It is a real domain: its
  membership is the harness's own tool list, enumerable, which satisfies bound 1 above.
- `tool:shell:<command>` — one command through the shell: `tool:shell:gh`, `tool:shell:git`. Maps to
  `Bash(<command>:*)`. `tool:shell:*` is unscoped shell and maps to bare `Bash`.

Two consequences worth stating plainly.

`tool:shell:*` is a **surface** wildcard by the letter of section 2, and it is the one whose enumerability
bound is not really satisfiable: the set of commands reachable through a shell is not a list anyone can
read back. It is admissible under the grammar and it should be treated by reviewers as the near-equivalent
of `tool:*` — a grant that means "anything this machine can do". The design does not forbid it, because a
build agent genuinely needs a shell and forbidding it would push the reach back out of the record where
nobody can see it at all. But it is the capability whose grant should always be the one someone argued for.

And `param_constraints` on `tool:shell:<command>` are enforceable **only through the proxy**, which does not
mediate the shell. A shell capability is therefore, today, unconstrainable in argument — worth writing into
the grant anyway, because the record then says what reach was held, which is decision 42's stated purpose
("a sign-off's pinned agent version pins the prompt and not the reach").

---

## 4. Provider divergence

Provider is chosen at dispatch by quota headroom, so the same agent with the same grant has different reach
depending on which harness had capacity. The corpus settles this, and it does not settle it by choosing a
verdict.

`principles.md` invariant 7 — "unknown stays distinct from a verdict" — requires a third value in every
reader of grant state, and invariant 5 requires the restrictive branch where a value is absent. Together they
give the rule:

**A grant means the same thing on every provider. What differs is whether the provider can enforce it, and a
provider that cannot is `unknown` — never green, never red.**

| Provider | Enforcement | Parity verdict |
|---|---|---|
| `claude` | `--allowed-tools`, introspectable from argv | green or red, measured |
| `codex` | a filesystem sandbox policy; no tool bound | **`unknown`**, recorded reporting-only |
| `cursor` | `--force --trust --approve-mcps`, which pre-approves every MCP server | **`unknown`**, recorded reporting-only |

Three things follow, and the third is the one that gives the row teeth.

`unknown` is not a pass. Decision 42's consequence clause already names the state — "a harness that cannot
enforce a grant-derived allowlist leaves the bound reporting-only for that harness, and principle 1 names it
as such rather than hiding it." The parity test prints the agent-provider pair by name.

**A run in which every provider is `unknown` is a red run, not an empty one.** Otherwise the test is
decoration under invariant 4: a swarm that routed all work to `cursor` would show no failures and hold no
bound.

**And the divergence is itself the reportable defect, not merely a limitation.** An agent whose grant is
enforced on Monday and ambient on Tuesday, decided by quota, has no stable reach for a sign-off to attest.
The design's position is that dispatching a T2 or T3 agent to a non-enforcing provider is a **capability
escalation performed by the router**, and the parity test should name it as such. What to *do* about it —
pin high-tier agents to enforcing providers, or accept the escalation and record it — is a routing decision
downstream of this grammar, and is left to the row rather than settled here.

---

## 5. The migration path

The measurement's figures could not be re-verified this pass (section 0). Under the figures as reported —
40 agents with `tool_allowlist`, 31 grants, 14 agents with no grant — the migration splits three ways.

### Mechanically derivable (the majority)

An agent whose allowlist consists only of the validator's recognized forms, and which has exactly one active
grant, migrates by transform: each entry through the normalizer of section 1, appended to `capabilities[]` as
`tool:` ops with empty `param_constraints`. This is a governance write per grant, reviewed as a batch, and
**it derives the grant from the allowlist — the opposite of the direction decision 42 wants**. That inversion
is acceptable exactly once, as the seeding step, and only because the alternative is authoring 40 grants from
nothing. It must be recorded as such, because a derived grant carries no evidence anyone judged the reach
appropriate: it records what the reach *was*, not what it *should be*. The narrowing pass is separate work
and is not in this design.

### Cannot be migrated mechanically

**The four wildcard agents — `aquila`, `ateles`, `concierge`, `ops`.** `"*"` carries no information about
which tools are needed. Deriving `tool:*` from it is refused by section 2; deriving a surface list from it
would be invention. Each needs its reach **authored** from what the agent actually does. `ateles` is the
urgent one: an active T2 agent whose harness allowlist is the literal string `*` and whose grant names no
tool, holding shell, filesystem, and every-MCP-server reach that no governance write authorized. It is the
largest single delta in the swarm and the first grant that should be written.

**The fourteen agents with no grant entity.** There is nothing to correct; a grant must be created, which is
a governance write of a different class from widening an existing one. Two are active — `falco` (18 named
tools, no grant) and `lanius` (the gate-reporting lens running on live PRs today).

**Any agent whose allowlist contains an unrecognized form.** The validator warns rather than fails on these,
so they may exist; each is a judgement, since the entry's *effective* reach is unknown — the `bash:` scar is
the precedent for assuming nothing.

**Every agent, for `param_constraints`.** No allowlist entry carries an argument bound, so no constraint can
be derived from one. Constraints are authored where they matter — the payment and deletion capabilities
first — or omitted, and their absence is not evidence they were considered.

### Ordering

Author the four wildcard agents first, `ateles` first among them, since they are the widest and the least
derivable. Then create grants for the fourteen ungranted, active ones first. Then the mechanical batch. Then,
last and only once every grant names the Neotoma tools it needs, remove the unconditional
`mcp__mcpsrv_neotoma__*` append — which is the step that actually closes the fail-open shape, and the step
that breaks the swarm if taken first.

---

## 6. What the parity test reads on each side

Both of the measurement's recommendations were checked against the code before adoption. Both hold, and one
needs an addition.

**Grant side: read through `GrantChecker._parse`.** Adopted, verified. `_parse` is a `@staticmethod` taking
an entity dict, so a test can call it on a query result with no network and no daemon. It is the same parser
the proxy's enforcement path uses, so the test cannot measure its own reimplementation. What it must **not**
reuse is `check_tool`, which fails open when no grant declares any tool capability — correct for advisory
enforcement, fatal for a test, which would score every unmigrated agent green. The test reads `tool_grants`
directly and treats empty as **red**, not as permitted.

**Harness side: read the argv actually built.** Adopted, verified, and the reason is stronger than stated.
The disagreement between field and argv is not a past bug that PR #800 fixed; it is **structural in the
current code**: the guard `provider == "claude" and tools != ["*"]` means the field `["*"]` produces *no
flag*, and the unconditional append means the argv always holds one entry the field never did. A test reading
`tool_allowlist` would be wrong in both directions on the same agent. It must call the command builder and
the allowlist block and parse `--allowed-tools` out of the result.

**The addition: read the stub flag.** `agent_loader` returns `tool_allowlist="*"` for a failed load, so argv
alone cannot distinguish "granted everything" from "the definition did not load". The test reads `is_stub`
and reports a stub as `unknown` with its `load_error`. Without this, an outage reads as a swarm of wildcard
grants.

### The verdict table

| Condition | Verdict |
|---|---|
| normalized harness set == normalized grant set | **green** |
| the two differ, either direction | **red**, both sets printed, the delta named |
| a dispatchable agent has no grant | **red** |
| a grant carries `tool:*` | **red** — refused by section 2 |
| an allowlist entry matches no recognized form | **red** |
| the definition is a stub | **unknown**, with `load_error` |
| the provider passes no allowlist (`codex`, `cursor`) | **unknown**, recorded reporting-only |
| every provider `unknown` | **red run** |

The **M** obligation, per invariant 4: widen one agent's harness allowlist by one tool without touching its
grant, and the row goes red. Its complement is worth stating too, since it is the direction the current
measurement cannot see at all — narrow one agent's allowlist below its grant and the row must also go red.

It runs as a `B0` row beside `PR-9` in the conformance suite, and in `scripts/lint.sh` beside
`validate_tool_allowlist.py`, which is the natural place: that validator already reads every agent's
allowlist and already owns the harness-side grammar. Neotoma read access only.

---

## 7. Whether the design routes through the existing proxy

**Yes — and the two mechanisms answer different questions, which is why building both was not duplication.**

The allowlist and the proxy are not two implementations of one control. The allowlist decides **which tools
appear in the child's menu**; it is coarse, it cannot see arguments, and it is the only thing that can bound
non-MCP reach, since no proxy mediates `Bash` or `Write`. The proxy decides **whether a particular call with
particular arguments proceeds**; it is the only place `param_constraints` can be enforced, the only place a
denial is observable as a `tool_call_observation`, and it is the enforcement point `authority_model.md`'s
"the grant is read at every enforcement point" is asking for. A grant that says
`tool:btc-wallet:btc_send_transfer` with `{max_amount_sats: 500000}` is enforced as to *tool* by the
allowlist and as to *amount* only by the proxy.

So the design routes through it: `skill_runner`'s injected `--mcp-config` should point the child at the proxy
with the downstream server behind it, rather than straight at the Neotoma endpoint. That is a one-file change
to the config the dispatcher writes, and it is the smaller half of the work — the proxy's own permissive
fallback is the larger half, since `check_tool` returns allow whenever no grant declares any tool capability,
which is every agent today. Wiring the proxy before populating grants changes nothing at all; wiring it after
is what makes the grants bind.

**Why it exists unwired**, which the brief asked directly: it was built to close its own issue, and closing
that issue was scoped to *the enforcement point*, not to *the dispatcher that would have to point at it*. The
corpus records it as shipped, `conformance.md` maps it to `authority_model.md`, and `status.md` calls it "the
nearest thing to a real enforcement point" — so it was not forgotten. It was finished at one end. Decision 42
then ruled the grant dimension that would have given it something to enforce, and the two were never joined
because nothing declared the join to be anyone's work. That is the shape invariant 1 warns about: a
mechanism that does not bind is not a control, and the proxy has been reporting rather than binding since the
day it merged.

The sequence is therefore: **grammar (this document, ruled) → grants populated → parity test green →
proxy wired → the Neotoma wildcard append removed.** Each step is inert without the one before it.

---

## 8. What this design does not settle

- **Which tools each agent should hold.** The grammar says how a reach is written down, never what reach is
  appropriate. The migration's authoring pass is separate work.
- **Whether the router may dispatch a high-tier agent to a non-enforcing provider.** Named as a capability
  escalation in section 4; the remedy is a routing decision, and is registered as part of the open row rather
  than settled here.
- **How `param_constraints` are expressed for a shell command.** Left open; the proxy does not mediate the
  shell, so the question has no enforcement point today.
- **Whether the grant should eventually *derive* the harness allowlist at load** rather than being held equal
  by a test. Decision 42 permits either. The parity test is the cheaper first step and is what the
  measurement asked for; derivation removes the drift class entirely and is the better end state. Sequencing
  one after the other is a judgement for the operator.
