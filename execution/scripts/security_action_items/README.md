# Neotoma security and migration action-item renderer

Regenerates the **Neotoma security and migration — live status** rendered_page
in Neotoma (`ent_a85f2b280fa4233291f6f81a`) from live GitHub state.

The page is the **standing, cross-incident tracker** for Neotoma security work
**and the client-instance migration that work prompted**, not a per-incident
list. Migration items live here rather than on their own page because they
share the security gates: a patched release, an owner that is not the nil UUID,
a tested restore, and a log drain before any data is written. It began with the work surfaced by the August 2026
findings; later findings join the same list rather than starting a new page. It
is shared with third parties, including a business partner, so treat the
public-safety rule below as load-bearing.

## Why this exists

Action items from the security incidents were scattered across two write-up
pages, GitHub issues, private advisories, and Neotoma task entities. No single
surface answered "what is still outstanding?" A hand-maintained list would
answer it once and then rot — the same generated-file-drift class as
`capability_manifest.json` (neotoma#2234). So status is **never stored**: every
issue/PR row is resolved from the GitHub API at render time.

## Contract (read this before changing anything)

- `manifest.json` holds **membership, grouping, and the page framing** — which
  items belong on the page and under which heading, plus `title`, `subtitle`,
  and `scope_note`. It must NOT record status for anything GitHub can answer.
- **Everything the page displays is generated**, including the `<h1>`, the
  subtitle, the scope note, and the closing "How this page is generated"
  section. On the first publish those were hand-written onto the page instead,
  which put them outside the generator's reach — so a regeneration would have
  silently dropped them. If you find yourself hand-editing the page, that is the
  bug; change the source.
### Known gap: `manual_status` is the last hand-typed status (ateles#516)

Everything GitHub can answer for is resolved live. The six rows it cannot —
four operator actions and two private advisories — carry a hand-typed
`manual_status`, and that string drifted within an hour of the page first
shipping: the client-instance deploy row read `pending` after the deploy had
completed and been verified. [ateles#516](https://github.com/markmhendrickson/ateles/issues/516)
proposes resolving operator-action rows from Neotoma `task` entities, the same
way issue rows resolve from GitHub, with honest per-row degradation when
Neotoma is unreachable. Until that lands, verify a `manual_status` against
reality before you write it.

Note the issue also records what was **rejected**: moving membership and
grouping into Neotoma. Status resolution belongs there; editorial curation of a
public page does not.

- `manual_status` is permitted **only** for items with no public tracker:
  advisories deliberately held private until their fix ships, and operator
  actions (deploys, secret rotation, allowlist changes) that are not code
  changes. Adding a `manual_status` to a GitHub-tracked item defeats the point.
- The rendered page is **public-safe**: no client names, no partner names, no
  instance identifiers, no exploit mechanics. Both `ateles` and `neotoma` are
  public repos and the page itself is shareable with third parties. Describe
  things generically ("a hosted client instance", "the partner's requested
  address").

## Usage

```bash
python3 render_action_items.py --out body.html   # render to a file
python3 render_action_items.py                   # render to stdout
```

Then publish the result onto the existing page entity (do NOT create a new one —
that orphans the share URL people already hold):

- via MCP: `correct` the `html_body` field of `ent_a85f2b280fa4233291f6f81a`
- or `publish_rendered_page` with the same `slug`
  (`security-incident-action-items`)

## When to regenerate

Whenever an item is added, closed, or its manual status changes. The page shows
its own generation timestamp, so a stale timestamp is the signal.

## Related

- Incident write-ups: `ent_9d73f40bf0557aceb62fdddc` (2026-07),
  `ent_447958916969eaa5722c9791` (2026-08)
- Requires `gh` authenticated against the tracked repo.
