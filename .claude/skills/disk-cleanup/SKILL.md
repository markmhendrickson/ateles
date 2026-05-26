---
name: disk-cleanup
description: "Analyze disk space and surface highest-leverage, safest cleanup candidates in ordered tiers. Asks for user approval before executing each tier. Use when the user asks about disk space, freeing space, running disk cleanup, clearing caches, or reports low disk space."
triggers:
  - disk space
  - freeing space
  - disk cleanup
  - clearing caches
  - low disk space
user_invocable: true
entity_id: ent_65f75bed40fb6da5e217759b
---

# Disk Cleanup

## Core behavior

1. **Run analysis** to discover candidates
2. **Surface candidates by tier** (highest leverage, safest first)
3. **Ask for approval** before executing each tier
4. **Execute only approved tiers**; then present the next tier

## Step 1: Run analysis

```bash
./execution/scripts/preview_deletions.sh
du -sh ~/Library/Caches/* 2>/dev/null | sort -hr | head -25
du -sh ~/.npm ~/Library/Application\ Support/Google/Chrome ~/Library/Containers/com.docker.docker/Data 2>/dev/null
docker system df 2>/dev/null
```

## Step 2: Present tiers and ask for approval

Present each tier with estimated size and risk. Wait for user approval before executing. Only proceed to the next tier after the current one is approved and run.

### Tier 1 — Automated script (zero risk)

Script clears Homebrew, pip, npm, old logs, trash. All reproducible.

```
Approve Tier 1? (yes/no)
```

**If approved:**
```bash
./execution/scripts/disk_cleanup.sh --yes
```

### Tier 2 — Large app caches (zero risk, ~3–7 GB)

Apps re-download as needed. Present current sizes from analysis, then ask approval.

| Cache | Command |
|-------|--------|
| CloudKit | `rm -rf ~/Library/Caches/CloudKit/*` |
| Telegram | `rm -rf ~/Library/Caches/ru.keepcoder.Telegram/*` |
| OpenAI Atlas | `rm -rf ~/Library/Caches/com.openai.atlas/*` |
| npm | `npm cache clean --force` |

```
Approve Tier 2? (yes/no)
```

### Tier 3 — Medium app caches (zero risk, ~2–3 GB)

| Cache | Command |
|-------|--------|
| Mozilla | `rm -rf ~/Library/Caches/Mozilla/*` |
| Granola | `rm -rf ~/Library/Caches/com.granola.app.ShipIt/*` |
| Brave | `rm -rf ~/Library/Caches/BraveSoftware/*` |
| Yarn | `rm -rf ~/Library/Caches/Yarn/*` |
| Google | `rm -rf ~/Library/Caches/Google/*` |

```
Approve Tier 3? (yes/no)
```

### Tier 4 — Smaller app caches (zero risk, ~0.5–1 GB)

| Cache | Command |
|-------|--------|
| Ledger Live | `rm -rf ~/Library/Caches/ledger-live-desktop-updater/*` |
| node-gyp | `rm -rf ~/Library/Caches/node-gyp/*` |
| Loom | `rm -rf ~/Library/Caches/loom-updater/*` |
| Playwright | `rm -rf ~/Library/Caches/ms-playwright` |
| Loom ShipIt | `rm -rf ~/Library/Caches/com.loom.desktop.ShipIt` |

```
Approve Tier 4? (yes/no)
```

### Tier 5 — Docker (moderate risk, ~10 GB)

Removes images not used by any container. Supabase/active containers are kept.

```
Approve Tier 5? (yes/no)
```

**If approved:**
```bash
docker image prune -a -f
```

### Tier 6 — Manual / review required

- **Chrome**: Clear via Settings → Cached images and files (user does this)
- **Old downloads**: `find ~/Downloads -type f -mtime +90` — list for review
- **Old snapshots**: Requires DATA_DIR — list for review
- **node_modules**: Per-project deletion — list projects for review

Present as recommendations; do not execute. User chooses what to do.

## Protected items (never delete)

- Cursor files (`~/Library/Application Support/Cursor`, `~/.cursor`, `.cursor/`)
- `data/[type]/[type].parquet`
- Supabase Docker volumes and in-use containers
- Active project source files

## Script locations

- `execution/scripts/preview_deletions.sh` — analysis
- `execution/scripts/disk_cleanup.sh` — Tier 1 (`--dry-run`, `--yes`)
