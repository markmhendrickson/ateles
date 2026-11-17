# Neotoma schema definitions

Versioned Neotoma entity-type definitions owned by Ateles. Each `<entity_type>.json` holds the exact `/register_schema` body (minus `activate`/`user_specific`), and is applied by its `execution/scripts/apply_<entity_type>_schema.py` tool (dry run by default; `--apply` writes and reads back). `payment_event.json`: identity is `approval_id + payment_date`, falling back to `primary_session_key + payment_date`; never `btc_address`.
