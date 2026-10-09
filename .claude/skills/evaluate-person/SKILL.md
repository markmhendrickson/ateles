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

Before the first write of the run, **probe the write target and assert what it
actually is**. The tool prefix is a name, and a name is not a behaviour.

This is the same "prove the instrument" discipline §3.1 demands for counts,
applied to the single most consequential decision in the skill — and it is here
because the weaker nominal check failed in exactly the way §3.1 exists to
prevent. On 2026-09-22 `mcp__mcpsrv_neotoma__*` **was** the operator's prefix,
so a prefix check passed, and `get_authenticated_user` on that same prefix
returned `storage_backend: "local"` with `sqlite_db:
/Users/.../data/neotoma.db` — a local SQLite database holding 6 rendered pages
while the operator's hosted instance held 285. The prefix, the MCP server name,
the wrapper filename and `NEOTOMA_ENV=production` all said hosted prod; the
running `neotoma mcp proxy` had been launched without `--downstream-url` and
fell back to `http://localhost:3080`. Nothing nominal could have revealed that,
and the run wrote the evaluation where the operator would never look —
`publish_rendered_page` then returned "rendered_page not found" for an entity
the hosted REST API served correctly, because the two were different stores.

Procedure, before the first write:

1. Call `get_authenticated_user` (or equivalent) **through the same channel the
   writes will use**. A probe through a different channel proves nothing about
   this one.
2. Assert the returned **storage backend and origin** are the intended instance:
   `scripts/instance_check.py assert --prefix <p> --intended-origin <url>
   --probe-json -`.
3. **If the assertion fails, the run aborts.** Do not write anywhere — not a
   draft, not a note. A wrong write must afterwards be found and deleted, which
   is strictly worse than not having started.
4. Name the **observed** store in the run summary, not the prefix.

An unreadable probe is a failed assertion, not a passing one: a check that
cannot tell hosted from local is the defect, not a mitigation of it.

**Test:** the run summary names the probed storage backend/origin, and
`instance_check.py summary` exits zero. If any write used a client prefix, the
run is void — the entity must be deleted from the client instance and re-created
on the operator's.

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

#### 1.3.1 The counterpart table — what the SUBJECT was owed

**Build this at the same time as the expectation table, not afterwards.** An
expectation table lists what the subject owed. Whether she could meet it almost
always depended on things she was owed back — an answer, a sign-off, a price, a
correction of a misunderstanding. Sweep for those explicitly, because no other
guard in this skill will catch their absence.

| # | What the subject was owed | Owed by | Requested | In force from | To | Delivered | What its absence blocked |
|---|---|---|---|---|---|---|---|

This is the one gap that passes every other check. §4.3's direction tripwire
counts corrections to **subject-authored** citations only, so a one-sided
expectation table never trips it — a run can be materially unfair and clean.

In the dogfood run three owed-to-subject items were decisive: a reference price
range owed by the CEO, open about six weeks, gating four named leads and every
referral handoff; a website sign-off owed by two people, requested with a
24-hour ask, blocking her outreach while outstanding; and a two-month
misunderstanding about whether she could pull the operator and the CEO into
conversations, uncorrected until late August and the single largest identified
cause of the pipeline shortfall. Without the first, the unsent-work finding
reads as pure negligence. A run that followed this skill literally — build the
subject-expectation table, test the subject against it — would have produced a
materially harsher and less accurate evaluation **and passed every check in the
skill.**

`scripts/reciprocity_check.py owed` records these rows and
`reciprocity_check.py check` gates on them at §3.3.

**Test:** the counterpart sweep ran and its result is recorded — including when
the result is "nothing was owed". That is a legitimate answer; not having looked
is not, and "nothing was owed" is a claim about your sweep before it is a claim
about the operator, so it names the surfaces searched (§2.3).

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
`excluded_by_filter` for every surface. `sweep_manifest.py check --path M` fails
when any `swept` surface has `total_read < total_matched` or a null
denominator. Run it before step 3 and paste the output.

### 2.3 Sparse and negative findings must name the surfaces

"No evidence of X" is a claim about the sweep before it is a claim about the
person, and it is the single most damaging sentence an evaluation can get wrong.

Every negative or sparse finding in the output renders as:

> No evidence of weekly pipeline updates. Searched: Gmail (312 messages,
> full range), Slack (24 conversations, full range), Drive (41 documents).
> Not searched: the client CRM (no operator access).

Never as "she did not send updates." `scripts/sweep_manifest.py cite --path M
--surfaces <surfaces>` emits this block.

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

**No count is reportable until the query that produced it has returned non-zero
on a case known to be positive — in the same invocation that produced the
count.**

Three separate requirements, and the last two were added because the first alone
proved satisfiable by a run that proved nothing:

Procedure, per query:

1. Name a **known-positive case** — an item you have already seen with your own
   eyes that this query must match. Not a hypothetical.
2. Run the query scoped to that case **in the same run as the count**. It must
   return the case.
3. If it does not, the instrument is broken. Fix it and restart the count. Do not
   report the number.
4. Record the run, its anchors and their observed counts, the population
   searched, and the query in the instrument log.

#### 3.1.1 Same run, or it is not an anchor

An anchor proven once and cited for a later count says nothing about the run
that emitted that count. Four background scans ran in the dogfood; **two were
broken, and they failed in opposite directions.**

- One reported a name **absent**. Right answer, zero evidence: its input file
  list was deleted mid-run, so every lookup after the first read a missing path
  and returned 0. The only thing that exposed it was that the **anchor names
  collapsed to 0 in the same execution**. Against a true negative it was
  indistinguishable.
- One reported the same name present in **all 414 files searched**, and another
  name **513 times against 414 files**. Wrong answer: `grep -lic | wc -l` emits
  a line per file including zero-match files, so it counted the file population
  rather than the matches.

The structural tell on the second was **a match count exceeding the population**
— impossible by construction. Not that any individual row looked wrong: the
plausible rows were equally void, because the instrument producing them was
counting the wrong thing.

#### 3.1.2 The anchor guards every number, not just zeros

D16 was filed as a rule about zeros and the completed evidence widened it. **A
non-zero is exactly as capable of being fabricated as a zero, and a confident
wrong non-zero is more likely to be believed.** Two of four runs were wrong;
without same-run anchors either could have shipped as fact, and the two
supported opposite findings with equal apparent confidence.

So every count carries a run id, and `instrument_log.py check` refuses:

- a count with no run, or citing a run with no anchor block;
- a run whose anchors **all** came back zero — the instrument saw nothing it was
  proven to see, so every number from it is a measurement of the instrument;
- a run whose anchor count **exceeds the population searched** — void including
  its plausible rows.

A zero additionally needs one of the §3.2 hazards named as ruled out: a
well-formed query against an undeclared field returns a true zero.

`scripts/instrument_log.py` records these. Every count reaching the page must
have a log entry and a live anchor from its own run.

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
same-run known-positive anchor that came back non-zero, and a pass.
`instrument_log.py check` fails otherwise.

### 3.3 Review the corpus against the expectations

Only now. For each expectation row from §1.3, assemble the evidence inside its
window and reach a finding:

| Field | Meaning |
|---|---|
| `expectation_id` | Row from §1.3. Mandatory. |
| `verdict` | `met`, `partially met`, `not met`, `insufficient evidence` |
| `evidence` | Citations. Each resolvable. |
| `counter_evidence` | Material cutting the other way. **Mandatory field** — if empty, say you looked and found none. |
| `counterpart_account` | What the subject was owed in the same window, by whom, whether it arrived. **Mandatory on every `not met` and `partially met`** — see below. |
| `confidence` | With what would raise it |

`insufficient evidence` is a real verdict and must be used rather than rounded to
`not met`. An expectation you could not measure is a gap in the sweep, not a
failure by the subject.

**The counter-evidence field is not optional.** A finding with an empty
counter-evidence field and no statement that it was searched for is incomplete.
This is the cheapest available guard against motivated reading.

**Every shortfall verdict states what the subject was owed in the same window.**
`not met` and `partially met` are claims against a person, and a claim against a
person who was waiting on an input she never got is a claim about the wrong
party. So each one carries its `counterpart_account` from the §1.3.1 table:
which counterpart obligations were in force, whether they were delivered, and
how the verdict accounts for them. Naming an unmet dependency and then scoring
the subject as if it had been met is the same failure with extra steps.

Where the counterpart sweep genuinely found nothing owed, record that, naming
the surfaces swept. `met` and `insufficient evidence` are exempt — neither
asserts a shortfall.

**Test:** `reciprocity_check.py check` exits zero before rendering. It fails a
shortfall verdict with no counterpart account, a counterpart item cited with no
`owed` row behind it, and — the case that actually bites — an undelivered
obligation recorded in the window that the finding does not mention.

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

1. **Who SENT it, and who AUTHORED it?** The sender is confirmed from the
   artifact's own metadata — sender field, file owner, message author — never
   inferred from content or from where it sits. **Authorship is a separate
   question, and metadata cannot answer it.** Where the text is machine output
   the subject relayed — agent output pasted into her channel, a model's
   diagnosis read aloud while screen-sharing, a generated report forwarded
   without comment — the metadata says her and the reasoning is not hers.
   Content inference is the *only* way to catch that, so it is required here
   rather than forbidden. Name what shows the text is machine-authored.
2. **What genre is it?** A self-accounting (task list, status update, personal
   notes, a log of one's own commitments), a communication to the operator, a
   communication to a third party, a record of someone else's words, or
   **relayed machine output**. **A personal to-do list is a self-accounting, and
   its default reading is what the author owes — not what the author is owed.**
   **Relayed machine output is never the subject's own reasoning or her own
   commitment** — it cannot support a finding against her, and an obligation
   appearing in it is not one she took on. Cite her own words adopting it, or
   downgrade the citation to context.
3. **Who does each named obligation run to and from?** For every item naming a
   person: is that person the one who owes it, or the one it is owed to? A name
   beside a task is ambiguous by construction — it can mean assignee, requester,
   or blocker. **Resolve it from the document's own convention** (how do the other
   items in the same list use names?), and state the convention you inferred.
4. **What is the opposite reading, and what would distinguish them?** Write the
   inverted reading out. Name the artifact that would settle it. If nothing
   available settles it, the citation is **ambiguous** and cannot support a
   finding — downgrade it to context or drop it.
5. **How well sourced is the underlying fact?** Grade it `first-hand-documented`
   (the artifact *is* the fact — a signed PDF, a commit, a transaction record),
   `corroborated` (a report, independently confirmed — name what confirms it),
   `single-verbal-report` (someone said it and it was written down), or
   `unsourced`. **Genre reliability and sourcing reliability are different
   axes**, and confusing them is how a reliable format launders an unreliable
   claim. See §4.2.1.

#### 4.2.1 A trustworthy format is not a trustworthy fact

Questions 1–4 are all about authorship and obligation direction. A citation can
pass every one of them and still be one person's unrecorded say-so.

Live case: "PROPOSAL AND NDA SIGNED" sits in a written Google Sheet. Because it
is written rather than transcribed, the ASR hazard — "NDA" is usually a
mistranscription of a similar-sounding product name — does not apply at all; the
word is certainly the word. All four questions pass. And the Neotoma record of
the same fact grades it: not corroborated by any document on the instance. It is
one person's verbal claim, faithfully written down.

**A written artifact is a reliable record of WHAT WAS SAID. It is no evidence
whatsoever that the thing said is true.** So:

- A claim traceable to a single verbal report is **not corroborated**, however
  high-trust the format carrying it.
- **A finding resting on one must say so in its own text**, at the point of the
  finding — not in a methods appendix. "Reported signed (single verbal report,
  2026-09-17; no countersigned document located on the instance)" is the shape.
- Corroboration means an **independent** artifact. The same claim restated by
  the same person in a second place is one report, not two.

`attribution_check.py` records the sourcing grade alongside the genre and
refuses a weakly-sourced citation that the finding does not disclose as such.

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
3. **Feedback to deliver** — §5.1a. What must actually be said.
4. **Discussion topics** — §5.1b. What is genuinely open.
5. **Overview** — three short paragraphs. Readable alone.
6. **Expectations** — the §1.3 table.
7. **Findings** — one block per expectation, with verdict, evidence, counter-evidence.
8. **Operator impressions tested** — the §1.4/§4.4 table.
9. **Method and coverage** — sweep manifest, instrument log, attribution check.
10. **Footer** — audience line, generation date, revision note.

Sections 1–5 must stand alone: a reader who stops after the overview should have
the evaluation's actual conclusion, not a teaser.

### 5.1a Feedback to deliver — what must actually be said

The six sections this template originally carried were **all retrospective** —
what happened, and how well we know it. None of them said what to *do* with it.
The motivating use case was being asked to give a colleague's 90-day review: the
evaluation answers "what is true" and leaves "what do I actually say" to be
derived by hand, which is the manual step this workflow exists to remove.

So this section holds the small number of things that must actually be said to
the subject. It sits immediately after Key takeaways because it is the most
actionable content on the page, and every item links **down** because it is also
the most derived — inferences on inferences. Short-then-long still holds: these
are short pointers into the long evidence, not standalone assertions.

**Voice — write the substance, not the script.** Each item is a **neutral point
the operator will phrase themselves**. Not a scripted sentence, not a quotable
line, not the operator's voice. A performance review is spoken aloud, and
pre-written phrasing either sounds unlike them or gets discarded, which wastes
the section. Write the substance to convey plus what it rests on:

> **Yes.** Four items she logged as two weeks late and blocking you, closed
> since / not closed.
>
> **No.** "I want to talk about some things that slipped."

Rules, each gated by `scripts/deliverable_check.py`:

- **An item may only cite a citation that survived the §4 attribution checks.**
  Nothing gets delivered that the evidence layer would not support. An
  `ambiguous` final reading, an `unresolved` obligation direction, or a
  `relayed-machine-output` genre disqualifies the citation from carrying an item
  — it could not support a finding, so it certainly cannot be said to her face.
- **Ordered by what the subject can act on, not by severity.** A severe finding
  she can do nothing about belongs in Findings, or in §5.1b if the decision is
  someone else's.
- **Every item whose finding is a shortfall carries its counterpart account**
  (D14). If the shortfall was partly caused by something the subject was
  **owed**, the item says so in the same breath, so it lands as a shared problem
  rather than as pure fault. The account is not re-typed here: the gate reads it
  from the existing `counterpart_account` machinery in `reciprocity_check.py`,
  by expectation id, so one table serves both gates and the two cannot drift.
- **Each item links down to the finding it derives from** — an in-page `#anchor`,
  which is not a cross-link (§0.3).

**Test:** `deliverable_check.py check --path D --attribution A --reciprocity R`
exits zero before the page is rendered, with the attribution and reciprocity
ledgers passed in. Run it after `attribution_check.py check` and
`reciprocity_check.py check`, never instead of them.

### 5.1b Discussion topics — what is genuinely open

Genuinely open questions. **These are NOT feedback and must never be rendered as
such.**

They are exactly three things: where the evaluation reached `insufficient
evidence`; where two readings both fit the evidence; and where a decision is
owed **to** the subject rather than by them.

**The separation is the whole point.** Collapsing the two sections produces the
exact failure this skill was built to prevent — open questions delivered as
criticisms. The same fact takes both shapes and only one of them is honest:

> **Feedback:** "You've been ignoring the scoring model."
>
> **Discussion topic:** "Our scoring penalises the contacts you're strongest in
> — what should we do about that?"

Same fact, different act. The first is a judgement about her conduct, and it is
only deliverable if the evidence layer supports it. The second is a question
about a system, and it is open precisely because the evidence does not settle
who is right. A future run will be tempted to merge them, because both are
"things to raise in the meeting" — do not. `deliverable_check.py` rejects an
item in this section phrased as a criticism rather than as a question, and
rejects a `kind` outside the three above.

### 5.1c Both sections render with their interrogation state

§5.1a and §5.1b are the **most likely content on the page to be wrong before the
§6 interrogation loop has run** — they are inferences built on findings that
have not yet been challenged.

So **render them with a visible "not yet interrogated" state rather than
omitting them.** A draft stays usable that way, and nobody mistakes an
unchallenged recommendation for a settled one. Omission is not the safe option:
it hides the section that the reader most needs, and a reader who does not see it
cannot know it was withheld.

The page already carries a top-level "Draft: interrogation step not yet run"
banner. **These two sections carry their own inline marker as well**, because
someone may scroll straight to them and never see the top of the page. Flip both
to the `interrogation ok` state only once §6.3's exit condition is met.

`deliverable_check.py interrogation --path D --state not-yet-interrogated|interrogated`
records it and `render` emits the marker; `check` fails when the state is
missing or unrecognized.

### 5.2 Where it is stored

Operator's instance (§0.1). Store an `evaluation` entity carrying the structured
findings and link the `rendered_page` to it with `REFERS_TO`. Link the evaluation
`REFERS_TO` the subject's `contact` entity. Use `strict: true` on the store —
thin schemas merge heuristically on name collision (`rendered-pages` §8).

**Register the schema first. `evaluation` is not a built-in type.**

This step used to mandate the store without shipping a schema, which made it an
instruction to trigger the bug the skill warns about two paragraphs down:
`describe_entity_type('evaluation')` errors on a fresh instance, `/store` accepts
undeclared fields and routes every one of them to `raw_fragments`, and the
entity reads back empty. `strict: true` does not help — strict governs entity
**matching**, not field **declaration**.

So, in order:

1. `describe_entity_type('evaluation')`. If it errors or returns no active
   schema, register it from `references/evaluation_schema.json` — the declared
   field list this skill writes against — and re-run `describe_entity_type` to
   confirm it took.
2. Store, writing only fields that appear in the schema. A field you need that
   is not declared is a schema change, not a store argument.
3. **Read the entity back and assert each field you wrote is present with the
   value you wrote**, naming them individually. A 2xx is not evidence; neither
   is `success: true`. Undeclared fields accept writes and read back empty, so
   the read-back is the only thing that distinguishes a stored evaluation from a
   discarded one.
4. If any field reads back missing or empty, it was undeclared. Fix the schema
   and re-store — do not proceed with a half-written entity.

**Test:** the run summary names the fields asserted on read-back, and the
`describe_entity_type` result that preceded the store.

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

Before that hand-over, **re-derive §5.1a and §5.1b against the findings as they
now stand** — a finding that moved under challenge may have been carrying a
feedback item that no longer holds, and the item does not update itself. Then
flip the interrogation state (`deliverable_check.py interrogation --path D
--state interrogated`) and re-render both sections so their inline markers say
so. A page still showing "not yet interrogated" after the loop has closed
understates its own reliability, which is the cheaper error but is still wrong.

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
- Deliverable gate: how many feedback items and discussion topics, how many
  shortfall items and whether each carries its counterpart account, and the
  interrogation state both sections rendered with.
- Operator impressions: how many supported, contradicted, unsupported.
- Sensitive scan: clean or blocked.
- Open ambiguities — citations that could not be resolved and the artifact that
  would settle each.

An evaluation handed over without these is not finished, it is just written.
