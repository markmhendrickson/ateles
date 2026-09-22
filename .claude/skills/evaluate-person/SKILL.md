---
name: evaluate-person
description: >-
  Build a performance review of a named person from first principles — define the
  expectations that person was actually working against, sweep every surface that
  holds material by or about them, test the evidence against those expectations,
  and render it as a standardized rendered_page short-then-long. Use whenever the
  operator asks to evaluate, review, assess, or write up how someone is doing —
  a contractor, a collaborator, a hire, a business-development partner — or says
  "how is <name> working out", "write up <name>'s performance", "I need to give
  someone a read on <name>", "evaluate <name> against what we agreed". Also use
  when re-opening a prior evaluation to interrogate or revise it. Reads other
  Neotoma instances as context; writes only to the operator's own. Companion to
  `rendered-pages`, which supplies every render mechanic this skill depends on.
  NOT `process-feedback`, which handles feedback arriving INBOUND about a product.
triggers:
  - evaluate person
  - /evaluate-person
  - performance review
  - evaluate how someone is doing
  - write up someone's performance
  - review a contractor
  - assess a collaborator
user_invocable: true
supported_harnesses:
  - claude-code
---

# evaluate-person

An evaluation of a person is an argument that their conduct did or did not meet a
standard. It has two failure modes and only one of them is about writing.

The first is a **weak argument** — vague expectations, cherry-picked evidence, a
conclusion that rests on impressions. The second is a **confidently wrong
argument** — a sweep that missed most of the corpus, a count read off a broken
instrument, a document by the subject read as saying the opposite of what it says.
The second is worse, because it survives review. It reads as rigorous.

This skill exists because the second happened. Every numbered check below traces
to a specific error in one ad-hoc review session: an evaluation drafted from 3 of
24 Slack conversations, Neotoma field counts taken before a schema migration, and
five separate misreadings of the subject's own to-do list — all five in the same
direction, all reading her accounting of what she owed the operator as complaints
about him.

**Read the checks as bug reports, not as style guidance.** Each one has a stated
test with a pass condition. A step whose test has not been run is not done.

**Cross-references:** `rendered-pages` for every render mechanic (§5 here delegates
to it wholesale — do not reimplement); `writing-voice` for the prose; `query-memory`
for Neotoma retrieval patterns.

---

## 0. Before anything: the two boundaries

### 0.1 Instance boundary — read wide, write narrow

Material about the subject may live in **any** instance the operator can reach,
including a client's. Read them all.

**Every write lands in the operator's own instance.** The evaluation entity, the
rendered page, any derived record, any note. No exception, including when the
evaluation was requested *by* the client whose instance holds the evidence.

```
READ:  operator instance + any client instance + Gmail + Slack + Drive + calendar + local files
WRITE: operator instance ONLY
```

Before the first write of the run, state which instance you are writing to and
confirm it is the operator's. In a session with more than one Neotoma MCP server
configured, the tool prefix is the only thing distinguishing them — check it, do
not assume the default is the operator's.

**Test:** name the MCP tool prefix used for every write in the run summary. If any
write used a client prefix, the run is void — the entity must be deleted from the
client instance and re-created on the operator's.

### 0.2 RGPD Art. 6(1)(f) — this is people-data processing

A durable evaluation profile of a named third party is not the household
exemption. It runs under legitimate interest, which constrains what may be kept.

- **Minimize at capture.** Store what bears on the working relationship: role,
  commitments, delivery, communication, outcomes. Not everything the sweep saw.
- **Purpose-bind.** The corpus is read to evaluate the working relationship. A
  detail that does not bear on it does not enter the evaluation, however
  interesting.
- **Do not persist incidental sensitive disclosures.** Health, family situation,
  finances, religion, politics, sexuality — RGPD Art. 9 categories. If one
  *explains* a performance fact (an illness explaining a delivery gap), record the
  effect, not the cause: "unavailable 12–26 March, reason known to the operator
  and not recorded here." Never transcribe the disclosure.
- **Honor objection.** If the subject has asked not to be tracked, stop and
  surface it to the operator. Do not argue them down.

**Test:** grep the finished `html_body` and the evaluation entity for Art. 9
terms before publishing. `scripts/sensitive_scan.py` does this; run it and record
the result. A hit is a blocker, not a warning.

### 0.3 Access control is deliberate, and must be stated

Rendered-page guest tokens are documented as one-entity-scoped but **that scope is
not enforced** (`rendered-pages` §5). For a page naming a person and judging them,
treat the token as a bearer credential that may reach anyone it is forwarded to.

Therefore, before publishing, get an explicit answer to: **who is this link going
to?** Write the answer into the page footer as an audience line. Then:

- The page must contain **zero cross-links** to other rendered pages, hubs, or
  entities. A named-person evaluation is standalone. (`rendered-pages` §7 warns
  about hub links leaking third-party commercials; here the leak runs the other
  way — the evaluation itself is the confidential thing.)
- If the subject is not among the recipients, say so in the run summary so the
  operator is deciding knowingly.
- Never email the link without the operator's per-send approval.

---

## 1. Step 1 — Define expectations

You cannot evaluate against a standard you have not written down. Produce the
standard **before** reading the corpus for performance signal, so the corpus
cannot quietly author the standard it is then measured against.

### 1.1 What counts as an expectation

This is the first judgment definition the skill must make explicit, because for
most subjects there is no contract — only statements over time.

An **expectation** is a claim about what the subject would do that satisfies all
four:

1. **Sourced.** It is traceable to an artifact — a contract clause, a message, a
   meeting transcript line, a document, a stated role. Not a recollection.
2. **Addressed or ambient.** Either communicated to the subject (they could know
   it), or a standard so ordinary for the role that a competent professional would
   assume it. Nothing else qualifies.
3. **Behavioural.** It describes conduct or output, not a disposition. "Send a
   weekly pipeline update" qualifies; "be proactive" does not until you can name
   what proactive would have looked like.
4. **Dated.** It has a start, and an end if superseded.

A statement failing (1) is an **impression** and goes in the operator-impression
register (§1.4), never in the expectation table. A statement failing (2) is an
**unstated hope** — record it as such, and note that measuring against it is
unfair. A statement failing (3) needs operationalizing before use. A statement
failing (4) cannot be used to judge conduct outside its window.

### 1.2 The two classes

**Explicit** — stated to the subject. Contract clauses where a contract exists;
otherwise the statements made when the relationship was set up and as it ran. For
most subjects this is the scope-of-work email, the kickoff call transcript, and
the messages where goals were named.

**Implicit** — the professional standard for the role, which the subject is
assumed to meet without being told. Responsiveness, not going dark, flagging
blockers before a deadline rather than after, basic artifact hygiene. Derive these
from the **role as it was described**, not from what the operator wished for.

### 1.3 Expectations evolve — timeline them, do not flatten them

A role at month one is not the role at month nine. Flattening the timeline is how
a subject gets judged for month-one conduct against a month-nine standard.

Build the expectation table with explicit windows:

| # | Expectation | Class | Source | In force from | To | Superseded by |
|---|---|---|---|---|---|---|

**Test:** every row has a resolvable source citation — a message id, transcript
timestamp, file path, or entity id. Spot-check three rows by opening the source
and confirming it says what the row claims. Record which three.

**Test:** any evaluation finding must name the expectation row it is measured
against, and the finding's date must fall inside that row's window. A finding
that cannot cite a row is an impression and moves to §1.4.

### 1.4 Register the operator's own impressions, separately, up front

Step 6 exists as much to correct the operator's impressions as the agent's. That
only works if the impressions were written down before the evidence was read.

Ask the operator, before the sweep: **what do you already believe about how this
is going?** Record each belief verbatim in an `operator_impressions` list with a
status of `untested`. Do not argue with them and do not use them to shape the
sweep.

At §4 each impression resolves to `supported`, `contradicted`, `unsupported`
(nothing found either way), or `unfalsifiable`. **Render this table in the page.**
It is frequently the most valuable output, and it is the mechanism by which the
evaluation can tell the operator he was wrong.

---

## 2. Step 2 — Sweep the corpus, honestly

### 2.1 Enumerate surfaces first, then sweep

Write the surface list **before** searching, so the list is not retro-fitted to
what happened to be reachable. Every surface gets one of four dispositions:
`swept`, `partial`, `unreachable`, or `not-applicable` — with a reason for
anything that is not `swept`.

Default surface list (extend per subject; never shorten silently):

| Surface | How |
|---|---|
| Operator Neotoma | `retrieve_entities` by subject name, plus related-entity walk |
| Client Neotoma(s) | same, per instance — read-only |
| Gmail | `gws gmail users messages list` — to, from, and mentioning |
| Slack / messaging | full conversation list, then each conversation |
| Meeting transcripts | transcription entities + local recordings |
| Calendar | `gws calendar` — meetings with the subject |
| Drive / documents | `gws drive files list` — authored by or shared with |
| Repos / issue trackers | `gh` — commits, PRs, issues, comments |
| External tooling | whatever the role used (CRM, project tracker) |
| Local files | the operator's own notes |

`scripts/sweep_manifest.py` maintains this as a JSON manifest and renders it into
the page. Use it; do not keep the list in your head.

### 2.2 The completeness test — when is a sweep done?

This is the second judgment definition. The motivating failure drafted conclusions
from 3 of 24 Slack conversations behind an arbitrary date floor, and never noticed
because 3 conversations felt like enough material.

A surface is `swept` only when **all** hold:

1. **Denominator known.** You have counted the total items on that surface
   matching the subject — 24 conversations, 312 messages, 41 documents — *before*
   reading any. If you cannot state the denominator, the surface is `partial`.
2. **No unjustified filter.** Every filter applied (date floor, channel subset,
   keyword) is either (a) derived from the evaluation window agreed with the
   operator, or (b) recorded in the manifest with its justification and its
   excluded count. **A date floor chosen because it seemed recent enough is the
   archetypal failure.** If a filter's excluded count is unknown, the surface is
   `partial`.
3. **Read to the boundary, not to sufficiency.** You stopped because you reached
   the end of the matched set, not because you had enough to conclude. "I had
   enough" is the failure condition, not the success condition.
4. **Denominator re-checked after the last read.** Corpora grow mid-run.

**Test:** the manifest records `total_matched`, `total_read`, and
`excluded_by_filter` for every surface. `sweep_manifest.py --check` fails when any
`swept` surface has `total_read < total_matched` or a null denominator. Run it
before step 3 and paste the output.

### 2.3 Sparse and negative findings must name the surfaces

"No evidence of X" is a claim about the sweep before it is a claim about the
person, and it is the single most damaging sentence an evaluation can get wrong.

Every negative or sparse finding in the output renders as:

> No evidence of weekly pipeline updates. Searched: Gmail (312 messages,
> full range), Slack (24 conversations, full range), Drive (41 documents).
> Not searched: the client CRM (no operator access).

Never as "she did not send updates." `scripts/sweep_manifest.py --cite <surfaces>`
emits this block.

**Test:** every negative finding in the page carries a surface citation. Grep the
finished body for negative-claim patterns without an adjacent `Searched:` block.

### 2.4 Minimize while collecting

Collect the corpus, but carry forward only what bears on the working relationship
(§0.2). Do not copy the corpus into Neotoma. The evaluation cites sources by
reference — message id, transcript timestamp, entity id — it does not embed them.

---

## 3. Step 3 — Validate instruments, then review

### 3.1 Prove the instrument before trusting a zero

Every query is a claim about your tooling before it is a claim about the world.
The motivating session took Neotoma field counts **before a schema migration** and
concluded from them.

**No zero, and no surprisingly-low count, is reportable until the query that
produced it has returned non-zero on a case known to be positive.**

Procedure, per query:

1. Name a **known-positive case** — an item you have already seen with your own
   eyes that this query must match. Not a hypothetical.
2. Run the query scoped to that case. It must return the case.
3. If it does not, the instrument is broken. Fix it and restart the count. Do not
   report the number.
4. Record the query, the known-positive case, and the result in the instrument log.

`scripts/instrument_log.py` records these and refuses to emit a zero that has no
passing anchor. Every count reaching the page must have a log entry.

### 3.2 The four instrument failures to check for by name

Each is live in this stack and each produced a wrong number:

- **Undeclared schema fields.** Neotoma `/store` accepts fields not declared on
  the schema and routes them to `raw_fragments` — they read back empty on the
  snapshot. A zero on such a field means "not declared", not "not present". Check
  `describe_entity_type` before counting on any field.
- **Pre-migration snapshots.** A count taken before a schema fix measures the
  bug. Record `computed_at` with every count and confirm no migration landed
  between the count and the conclusion.
- **Stale trackers.** A project board days behind reality reports absence of work
  that happened. Check the tracker's last-updated against the evaluation window.
- **Field-name variants.** The same fact stored under `issue_number`,
  `github_number`, and `number` across rows — a query on one name reports a near-
  zero that is an artifact of naming. Enumerate the variants before counting.

**Test:** for every count in the page, the instrument log shows a query, a
known-positive anchor, and a pass. `instrument_log.py --check` fails otherwise.

### 3.3 Review the corpus against the expectations

Only now. For each expectation row from §1.3, assemble the evidence inside its
window and reach a finding:

| Field | Meaning |
|---|---|
| `expectation_id` | Row from §1.3. Mandatory. |
| `verdict` | `met`, `partially met`, `not met`, `insufficient evidence` |
| `evidence` | Citations. Each resolvable. |
| `counter_evidence` | Material cutting the other way. **Mandatory field** — if empty, say you looked and found none. |
| `confidence` | With what would raise it |

`insufficient evidence` is a real verdict and must be used rather than rounded to
`not met`. An expectation you could not measure is a gap in the sweep, not a
failure by the subject.

**The counter-evidence field is not optional.** A finding with an empty
counter-evidence field and no statement that it was searched for is incomplete.
This is the cheapest available guard against motivated reading.

---

## 4. Step 4 — The misreading check (run before rendering)

This is the third judgment definition and **the most important check in the
skill**. In the motivating session, five separate misattributions all ran the same
direction: the subject's own to-do list — "Mark, four items, two weeks late, all
blocking him" — was read as a complaint about the operator when it was her own
accounting of what she owed him.

Five errors in one direction is not noise. It is a systematic reading bias, and it
was only caught because the operator happened to know the documents.

### 4.1 Why this class of error is invisible

A document written by the subject about her own obligations, and a document
written by the subject complaining about the operator, can be **lexically almost
identical**. Both contain the operator's name, a list of items, and overdue dates.
The difference is the direction of obligation, which is carried by document
genre and context — not by the words. A reader who arrives expecting complaint
finds complaint.

### 4.2 The check: re-derive attribution for every subject-authored citation

For **every** citation in the evaluation drawn from material the subject wrote,
answer these four in writing before the citation may stand:

1. **Who wrote it?** Confirmed from the artifact's own metadata — sender field,
   file owner, message author — not inferred from content or from where it sits.
2. **What genre is it?** A self-accounting (task list, status update, personal
   notes, a log of one's own commitments), a communication to the operator, a
   communication to a third party, or a record of someone else's words. **A
   personal to-do list is a self-accounting, and its default reading is what the
   author owes — not what the author is owed.**
3. **Who does each named obligation run to and from?** For every item naming a
   person: is that person the one who owes it, or the one it is owed to? A name
   beside a task is ambiguous by construction — it can mean assignee, requester,
   or blocker. **Resolve it from the document's own convention** (how do the other
   items in the same list use names?), and state the convention you inferred.
4. **What is the opposite reading, and what would distinguish them?** Write the
   inverted reading out. Name the artifact that would settle it. If nothing
   available settles it, the citation is **ambiguous** and cannot support a
   finding — downgrade it to context or drop it.

### 4.3 The direction test — the systematic-bias tripwire

Individual checks catch individual errors. They do not catch a bias, because a
biased reader passes each check the same wrong way. So count.

After §4.2, tabulate every re-derived attribution by whether the correction (if
any) moved the reading toward or away from the subject's fault.

**If corrections run predominantly in one direction — three or more with none the
other way — stop.** Do not proceed to render. That asymmetry is evidence of
systematic misreading, the same signature as the motivating failure. Re-read the
unchanged citations in that class with the opposite prior, and report the
asymmetry to the operator in the run summary regardless of what the re-read finds.

`scripts/attribution_check.py` maintains the table and implements this tripwire.
It exits non-zero on an unresolved asymmetry.

**Test:** the script's table is complete (one row per subject-authored citation)
and it exits zero before the page is rendered. Both facts go in the run summary.

### 4.4 Resolve the operator's impressions

Now mark each §1.4 impression `supported`, `contradicted`, `unsupported`, or
`unfalsifiable`, with citations. An impression the corpus contradicts is reported
plainly. Softening it defeats the purpose of the step.

---

## 5. Step 5 — Render the page

**Delegate every mechanic to `rendered-pages`.** Load it. This section adds only
what is specific to an evaluation; it does not restate render rules.

From `rendered-pages`, the ones that bite hardest here:

- `publish_rendered_page` on an existing entity **only mints a token** — it does
  not update content. Use `correct(entity_id, field: "html_body", ...)`.
- Publish can return success while **discarding the body**. `curl` the share URL
  and check the byte count (~1845 bytes means empty) plus distinctive strings.
- Scripts never run in the sandboxed iframe. CSS-only interaction.
- House visual standard, theme toggle, host-template `!important` overrides.
- Mint the guest token as part of building the page; surface the link with its
  `?access_token=`.

Evaluation-specific overrides:

- **Zero cross-links** (§0.3). Not even a markdown-view link — it 401s under a
  guest token anyway.
- **Audience line in the footer**, naming who the link is for, per §0.3.
- **A "how this was built" section is mandatory**, carrying the sweep manifest,
  the instrument log summary, and the attribution-check outcome. An evaluation
  that does not show its method cannot be audited by the person it judges.

### 5.1 Template

`references/evaluation_page_template.html` is the standardized template — one
structure across everyone evaluated, specialized only by role and objectives. It
carries the house CSS, the theme toggle, and placeholder sections in short-then-long
order. Fill it; do not hand-roll a one-off.

Section order is fixed (this is step 5's short-then-long requirement):

1. **Header** — subject, role, evaluation window, who requested it, date.
2. **Key takeaways** — at most five, each one sentence, each linking down to its
   evidence.
3. **Overview** — three short paragraphs. Readable alone.
4. **Expectations** — the §1.3 table.
5. **Findings** — one block per expectation, with verdict, evidence, counter-evidence.
6. **Operator impressions tested** — the §1.4/§4.4 table.
7. **Method and coverage** — sweep manifest, instrument log, attribution check.
8. **Footer** — audience line, generation date, revision note.

Sections 1–3 must stand alone: a reader who stops after the overview should have
the evaluation's actual conclusion, not a teaser.

### 5.2 Where it is stored

Operator's instance (§0.1). Store an `evaluation` entity carrying the structured
findings and link the `rendered_page` to it with `REFERS_TO`. Link the evaluation
`REFERS_TO` the subject's `contact` entity. Use `strict: true` on the store —
thin schemas merge heuristically on name collision (`rendered-pages` §8).

**Read back every write** and assert the field you wrote is present with the value
you wrote. A 2xx is not evidence. Undeclared fields accept writes and read back
empty.

---

## 6. Step 6 — The interrogation loop

The evaluation is a draft until the operator has pushed on it and the agent has
re-checked. This step is not polish and is not optional.

### 6.1 Run it explicitly

Present the page, then ask for pushback in these terms: **which findings feel
wrong, and which of your own prior impressions does this contradict?**

For each challenge:

1. **Re-open the source material.** Not your summary of it, not the page — the
   artifact. A summary entity is not a source.
2. **Re-run §4.2** on any citation the challenge touches. Challenges cluster on
   exactly the citations most likely to be misread.
3. **Resolve**: the finding stands (say what re-checking confirmed), the finding
   changes (say what changed it), or it becomes ambiguous (downgrade it).
4. **Record the challenge and its resolution** in the evaluation entity's revision
   log. This is the audit trail for why a finding survived pushback.

### 6.2 The loop corrects the operator too

The operator was explicit that this step exists as much to correct **his**
impressions as the agent's. So:

- When the corpus contradicts an operator impression, **say so directly** and show
  the evidence. Do not bury it or hedge it into agreement.
- When the operator pushes back and the re-check **confirms the original finding**,
  report that the finding stands and why. Do not fold to pressure — an evaluation
  that yields to whoever spoke last is worthless.
- When the operator's pushback reveals an actual misreading, fix it, and **check
  whether the same misreading class appears elsewhere** (§4.3). One found error of
  this type predicts others.

### 6.3 Exit condition

The loop ends when the operator has no outstanding challenges **and** every
challenge raised has a recorded resolution. Then re-run the §0.2 sensitive scan
and the `rendered-pages` pre-share verification sweep, confirm the audience line
still matches who the link is going to, and hand over the link.

Sending it to anyone is the operator's approval, per turn, per recipient.

---

## 7. Run summary — what to report back

Every run of this skill ends with:

- The page link, with `?access_token=`.
- Write-instance confirmation (§0.1) — the MCP prefix used.
- Sweep manifest: surfaces swept / partial / unreachable, with denominators.
- Instrument log: how many counts, how many anchored, any instrument found broken.
- Attribution check: citations checked, corrections made, direction table, tripwire
  result.
- Operator impressions: how many supported, contradicted, unsupported.
- Sensitive scan: clean or blocked.
- Open ambiguities — citations that could not be resolved and the artifact that
  would settle each.

An evaluation handed over without these is not finished, it is just written.
