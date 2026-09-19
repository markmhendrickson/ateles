# Daemon config inventory (SSE subscription ids)

Stub inventory for ateles#642 Phase 1. Full ~293-variable inventory lands in the
follow-up issue that completes daemon migration.

| Daemon label | Env var (POSIX-safe) | Notes |
|---|---|---|
| com.ateles.apis | `NEOTOMA_SSE_SUBSCRIPTION_ID_APIS` | Repo plist drift vs installed tracked by `check_daemon_config_parity.py` |
| com.ateles.formica | `NEOTOMA_SSE_SUBSCRIPTION_ID_FORMICA` | |
| com.ateles.anthus | `NEOTOMA_SSE_SUBSCRIPTION_ID_ANTHUS` | Often loaded via `~/.config/neotoma/.env` instead of plist |
| com.ateles.neotoma-agent | `NEOTOMA_SSE_SUBSCRIPTION_ID_NEOTOMA_AGENT` | Live plist uses invalid hyphenated key `...NEOTOMA-AGENT` |

Run `python execution/scripts/check_daemon_config_parity.py` to compare repo plists
against `~/Library/LaunchAgents/` for subscription id parity.
