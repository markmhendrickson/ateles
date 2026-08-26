# Security incident action-item renderer

Regenerates the **Security incident response — action items** rendered_page in
Neotoma (`ent_a85f2b280fa4233291f6f81a`) from live GitHub state.

## Why this exists

Action items from the security incidents were scattered across two write-up
pages, GitHub issues, private advisories, and Neotoma task entities. No single
surface answered "what is still outstanding?" A hand-maintained list would
answer it once and then rot — the same generated-file-drift class as
`capability_manifest.json` (neotoma#2234). So status is **never stored**: every
issue/PR row is resolved from the GitHub API at render time.

## Contract (read this before changing anything)

- `manifest.json` holds **membership and grouping only** — which items belong on
  the page and under which heading. It must NOT record status for anything
  GitHub can answer.
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
