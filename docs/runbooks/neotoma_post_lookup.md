# Neotoma post lookup: difficulties and workarounds

## Purpose

Document the difficulties that arise when resolving a Neotoma post entity_id by slug, and provide reliable workarounds for agents updating post content.

## Scope

Applies when an agent needs to correct or update a post entity in Neotoma and must resolve the `entity_id` from a known slug using MCP or local export files.

When updating a post in Neotoma by slug (e.g. to correct excerpt or title), you may need the post's `entity_id`. The following difficulties can occur when resolving slug to entity_id via Neotoma MCP.

## Difficulties

1. **`retrieve_entity_by_identifier` with slug** — Called with `identifier="we-are-all-centaurs-now"` and `entity_type="post"` can return a very large response (e.g. hundreds of entities written to a file). The target post may not appear in the first batch, and slug may not be treated as the primary identifier for matching, so the desired entity is not reliably at the top.

2. **`retrieve_entities` with search** — `entity_type="post"` and `search="we-are-all-centaurs-now"` can return 0 entities. Search may be semantic/embedding-based, so a slug string may not match the post in the index.

3. **Large responses** — `list_entity_types` and large `retrieve_*` results are written to files (e.g. under `.cursor/.../agent-tools/`). Entity_id cannot be read from chat; you must parse the file or use another source.

4. **Export file as source of entity_id** — The Neotoma export pipeline (e.g. script that writes `data/tmp/neotoma_posts_raw.json`) includes both `entity_id` and `slug` per post. That file is a reliable way to resolve slug to entity_id when MCP lookup by slug is unreliable.

## Workaround: get entity_id from export file

If you have the post slug but not entity_id:

1. Check for a recent Neotoma posts export that includes entity_id, e.g. `data/tmp/neotoma_posts_raw.json`.
2. Parse the JSON and find the entity whose `snapshot.slug` equals the post slug.
3. Use that entity's `entity_id` in `mcp_neotoma_prod_correct` (or other Neotoma MCP calls).

Example (Python):

```python
import json
with open("data/tmp/neotoma_posts_raw.json") as f:
    data = json.load(f)
for e in data.get("entities", []):
    if (e.get("snapshot") or {}).get("slug") == "we-are-all-centaurs-now":
        print(e["entity_id"])  # e.g. ent_6a645400c99f23029d839d12
        break
```

If the export file does not exist or does not contain the post, run the export step that fetches posts from Neotoma and writes `neotoma_posts_raw.json` (or equivalent), then repeat the lookup.

## Related

- `.cursor/rules/persistence.mdc` — Post data changes (Neotoma first), Website Post Cache Rebuild
- `.cursor/rules/neotoma_parquet_migration.mdc` — Data layer and Neotoma-first writes
