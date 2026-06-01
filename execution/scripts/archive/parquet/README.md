# Archived Parquet scripts

These scripts are retained for historical reference only. They were used during the migration of normalized data from Parquet files (previously under `$DATA_DIR`) to Neotoma.

**Do not run these scripts.** Parquet is no longer a supported data store in this repository. Neotoma is the single source of truth for all normalized data. See `.cursor/rules/neotoma_harness.mdc`.

If you need to resurrect any of this logic, port it to read from and write to Neotoma exclusively via the MCP or `neotoma` CLI. Do not reintroduce Parquet as a dependency.
