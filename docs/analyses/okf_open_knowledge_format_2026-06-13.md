# Analysis — Open Knowledge Format (OKF) & Marie Haynes amplification

- **Kind:** mixed (relevance + competitive)
- **Target type:** content / thought-leadership describing a format-product
- **Date:** 2026-06-13
- **Sources:**
  - Google Cloud blog — *How the Open Knowledge Format can improve data sharing* — https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing/
  - X / Marie Haynes — https://x.com/marie_haynes/status/2065531158356717721 *(returned HTTP 403; verbatim text not retrieved — see caveat)*
- **Primary comparator:** `markmhendrickson/neotoma` (the truth layer). Secondary: `ateles`, `foundation`.

> **Source caveat.** The Marie Haynes post could not be fetched (X returns 403 to unauthenticated fetch, and no X/scraper MCP is wired into this session). All claims about *her* framing are inference from her public role (SEO / Google-search / AI-search authority) and are marked `confidence: low`. The Google blog post was fetched in full and is the analyzable primary source.

---

## Headline

- OKF v0.1 formalizes Andrej Karpathy's "LLM-wiki" into a portable, vendor-neutral interchange format: a directory of **markdown files + YAML frontmatter + markdown links**, with `type` as the *only* required field. It is a **serialization format, not a knowledge system**.
- OKF deliberately omits exactly what Neotoma is built on — **provenance, corrections, multi-observer reconciliation, confidence, typed relationships**. Its only temporal field is a single `timestamp`; its "graph" is untyped markdown links. This is the cleanest articulation yet of the gap Neotoma fills *beneath* a format like this.
- The relationship is **more complementary than competitive**: OKF is a plausible **import/export and publish target** for Neotoma, not a replacement for it. The competitive risk is mindshare — Google bundling Knowledge Catalog ingestion could make "markdown-wiki = knowledge for agents" the default mental model and commoditize the file layer.

---

## What OKF is (from the primary source)

- **Origin:** Google Cloud (Data Cloud team; Sam McVeety, Amir Hormati). Published as an **open standard** — "the value of a knowledge format comes from how many parties speak it, not from who owns it."
- **Representation:** "OKF v0.1 represents knowledge as a directory of markdown files with YAML frontmatter… that let wikis written by different producers be consumed by different agents without translation."
- **Minimal schema:** required frontmatter is essentially just `type` (e.g. `BigQuery Table`); everything else (`title`, `description`, `resource`, `tags`, `timestamp`) is producer-determined. "Minimally opinionated."
- **Graph:** relationships are expressed as **markdown links** between concept files — an untyped, link-based graph.
- **Design stance:** *format, not platform* — vendor-neutral, portable as a tarball / git repo / filesystem mount, no SDK or runtime required. "Anyone can produce, without an SDK. Anyone can consume, without an integration."
- **Producer/consumer split:** clean separation between who writes knowledge and who consumes it.
- **Reference implementations shipped:** an enrichment agent (walks BigQuery, drafts OKF docs), a static HTML graph visualizer (no backend), and three sample bundles (GA4 e-commerce, Stack Overflow, Bitcoin public datasets). Google Cloud's **Knowledge Catalog** was updated to ingest OKF and serve it to agents.
- **Explicitly provisional:** "OKF v0.1 is a starting point, not a finished standard… the format will evolve as we collectively learn what knowledge representations agents actually need."

### The Karpathy framing it builds on
> "LLMs don't get bored, don't forget to update a cross-reference, and can touch 15 files in one pass. The bookkeeping that causes humans to abandon personal wikis is exactly what LLMs are good at."

OKF is the standardization of the now-popular "LLM-wiki / knowledge-is-markdown / metadata-as-code" pattern that spread through mid-2026.

---

## Relevance / competitive section (private)

### Where OKF and Neotoma overlap
Both answer the same headline problem the blog states: *"Every agent builder is solving the same context-assembly problem from scratch."* Both give an AI agent durable, reusable knowledge that compounds over sessions instead of evaporating.

### Where they differ — and why it favors Neotoma's wedge
OKF is a **flat document format**; Neotoma is a **truth-reconciliation system**. The differences map almost one-to-one onto Neotoma's core primitives:

| Dimension | OKF v0.1 | Neotoma |
|---|---|---|
| Unit | markdown file w/ YAML frontmatter | typed entity with schema'd fields |
| Relationships | untyped markdown links | typed relationships (`CORRECTS`, `PART_OF`, `REFERS_TO`, `DEPENDS_ON`, …) |
| Provenance | none (a `timestamp` field, by convention) | per-field observation provenance, source rows, content-hash dedup |
| Conflicting facts | last writer / human edit wins | multiple observations, source priority, reconciliation |
| Correction | edit the file (history only in git) | first-class `CORRECTS` / supersession with audit trail |
| Confidence | none | confidence on observations/findings |
| Multi-party sync | git merge | peer sync with loop prevention |
| Identity | file path | content-addressed entity resolution + idempotency |

OKF's own "minimally opinionated" posture is the tell: it punts on precisely the hard problems (whose claim is right, when, and has it been corrected) that constitute a *truth layer*. A markdown wiki cannot tell you which of two contradictory `description:` values is current and authoritative; Neotoma's observation/correction model is designed for exactly that.

### Strategic read
1. **Complementary layering, not a head-to-head competitor.** OKF is an interchange/transport format that sits *above* a store. Neotoma can treat OKF as (a) an **ingestion source** — parse a bundle into entities + relationships, and (b) an **export/publish target** — render a Neotoma subgraph as an OKF bundle (it already has `rendered_page` / `publish_rendered_page` machinery and markdown-first docs). This is a low-friction interop story, not a rebuild.
2. **The commoditization risk is mindshare, not features.** If Google's Knowledge Catalog + OKF becomes the reflexive answer to "how do agents get context," the *file-wiki* layer gets commoditized and Neotoma must position clearly as the layer *underneath* — the system of record that a format serializes, with the provenance/correction guarantees a flat format structurally cannot provide.
3. **Vocabulary to engage.** OKF popularizes durable terms — *knowledge format, producer/consumer independence, metadata as code, format not platform, LLM-wiki*. Neotoma's narrative ("the truth layer / memory with provenance and corrections") should explicitly relate to and contrast with these, ideally via a comparative post in the existing memory-&-truth-layer series.
4. **Prior art worth mining.** OKF's reference *enrichment agent* and *static HTML visualizer* are directly analogous to Neotoma's enrichment + rendered-page paths; both are open and worth reading as design references.

### Marie Haynes angle (low confidence — tweet unretrieved)
Marie Haynes is a widely-followed SEO / Google-search / AI-search authority. Her amplifying a *data-analytics* knowledge-format post (rather than a classic ranking topic) is itself a weak signal that OKF is crossing into the **SEO / GEO (generative-engine-optimization)** discourse — i.e. practitioners reading it as "how to structure knowledge so agents and AI search surfaces consume it well." If confirmed, that widens OKF's audience beyond data engineers into the content/marketing world Neotoma also touches. **This should be verified against the actual tweet text before acting on it.**

---

## Findings

1. **OKF = portable markdown+YAML with `type` as the only required field; it formalizes Karpathy's LLM-wiki.** Evidence: blog — "represents knowledge as a directory of markdown files with YAML frontmatter"; "minimally opinionated." `confidence: high`, kind: technology.
2. **OKF deliberately omits provenance, corrections, and conflicting-observation reconciliation — Neotoma's core.** Evidence: "minimally opinionated"; only temporal field is `timestamp`; no correction or multi-source semantics in the spec. `confidence: high`, kind: competitive.
3. **OKF is a serialization format; Neotoma is a system of record — the relationship is complementary (import/export target), not head-to-head.** Evidence: "format, not platform… portable as a tarball / git repo / filesystem mount." `confidence: medium`, kind: competitive.
4. **OKF's relationship model is an untyped markdown-link graph, weaker than Neotoma's typed relationships.** Evidence: blog describes relationships as markdown links between concept files. `confidence: high`, kind: technology.
5. **Google bundling Knowledge Catalog ingestion creates platform gravity that could commoditize the file-wiki layer and anchor the default mental model for "knowledge for agents."** Evidence: "Knowledge Catalog updated to ingest OKF and serve it to agents." `confidence: medium`, kind: market.
6. **OKF popularizes durable shared vocabulary (knowledge format, producer/consumer, metadata as code, format not platform) Neotoma should engage in positioning.** Evidence: direct quotes throughout the post. `confidence: medium`, kind: market.
7. **Marie Haynes (SEO/AI-search authority) amplifying OKF signals it crossing into the SEO/GEO discourse.** Evidence: her public role; the tweet links the OKF post. Tweet text NOT retrieved (403). `confidence: low`, kind: market.

---

## Follow-up tasks

1. **[neotoma — repo]** Spec an OKF import/export adapter: ingest an OKF bundle into Neotoma entities + relationships, and export a Neotoma subgraph as an OKF bundle (markdown + YAML frontmatter). → proposed issue.
2. **[neotoma — repo]** Document the OKF↔Neotoma field mapping and the lossy boundary (what OKF cannot carry: provenance, corrections, observations, confidence, typed edges). → proposed issue.
3. **[content]** Draft a comparative post for the memory-&-truth-layer series: "A knowledge format needs a truth layer" — Neotoma as the provenance/correction substrate beneath OKF-style wikis. (Use `/draft-comparative-neotoma-post`.) Internal — not repo-touching.
4. **[neotoma — repo / research]** Review OKF's reference enrichment agent and static HTML visualizer as prior art for Neotoma enrichment + `rendered_page` features. Internal research; may fold into task 1.
5. **[internal]** Retrieve the Marie Haynes tweet verbatim via an authenticated X path (x_api MCP / scraper) to confirm or drop finding #7 before acting on the SEO/GEO angle.

---

## Proposed GitHub issues (drafts — opt-in OFF this run)

Issues are **staged only**; nothing was opened (no `--open-issues` flag, `ANALYZE_OPEN_GH_ISSUES` unset). Bodies below are pre-redacted (competitive framing stripped; read as neutral feature requests).

- **markmhendrickson/neotoma** — *Add Open Knowledge Format (OKF) import/export adapter* — backed by task 1. `competitive_content_stripped: true`.
- **markmhendrickson/neotoma** — *Document OKF ↔ Neotoma field mapping and round-trip fidelity* — backed by task 2. `competitive_content_stripped: true`.

---

## PII / sensitivity inventory

- **Names (all public figures):** Marie Haynes, Sam McVeety, Amir Hormati, Andrej Karpathy. No private-contact PII.
- **Customers:** none.
- **Internal projects to generalize before any public post/issue:** Ateles, internal agent-swarm names. (The proposed neotoma issues do not reference them.)
- **Other:** none.
