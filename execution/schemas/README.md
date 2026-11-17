# Neotoma schema definitions

Versioned Neotoma entity-type definitions owned by Ateles. Each `<entity_type>.json` holds the exact `/register_schema` body (minus `activate`/`user_specific`), and is applied by its `execution/scripts/apply_<entity_type>_schema.py` tool (dry run by default; `--apply` writes and reads back). `payment_event.json`: identity includes `obligation_key` with `approval_id` or `primary_session_key` and `payment_date` whenever known, with legacy fallbacks when it is absent; never `btc_address`.
