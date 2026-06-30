# linkedin-enrichment daemon

Standing background enrichment that keeps intro-relevant contacts' LinkedIn data
current. Neotoma contact records are stale proxies (2010–14 titles); live
LinkedIn is the ground truth for current role / company / location — the data
intros turn on.

Task: `ent_3757c3fa27e81711778a6565`. Decided with operator 2026-06-30 out of the
LocalGlobe / Zowie intro session.

## What it does

1. **Queue** (`enrichment_queue.py`) — scans Gmail for genuine **two-way**
   correspondents (operator both sent to AND received from), excludes
   newsletters / one-way / no-reply, ranks by most-recent reciprocal touch,
   and matches each to a Neotoma `contact` by email.
2. **Pace** (`state.py`) — **hard 10/day cap**, persisted, **no burst-catchup**
   across a missed day. One counter increment per profile visit.
3. **Scrape seam** (`linkedin_enrichment.py`) — the daemon hands one contact to
   a `ScrapeFn` and consumes a `ProfileCapture`. The actual LinkedIn load runs
   in the **agent layer via the Chrome MCP** (`mcp__Claude_in_Chrome__*`),
   driven through the operator's logged-in Chrome session. A standalone process
   can't reach MCP tools, so the daemon never scrapes by itself.
4. **Enrich-once** — each contact is enriched a single time (dedupe marker in
   state); re-scrape only happens on-demand when an intro is actively being
   built for that person.
5. **Write-back** (`enrich.py`) — appends a dated `LinkedIn enrichment <date>:`
   line to the contact's `notes` (**never overwrites**) and refreshes
   `title` / `company` / `location` / `linkedin_url`. Silent, store-only.

## Load-bearing guardrails

- **Fail-safe on challenge.** The `ScrapeFn` MUST raise `LinkedInChallenge` on
  ANY LinkedIn challenge / checkpoint / captcha / unusual-activity prompt. The
  daemon then **hard-stops the whole run**, surfaces a blocker, and never
  auto-solves. The risk is the operator's **personal** account (restriction /
  ban); automated scraping violates LinkedIn's User Agreement.
- **Hard 10/day cap**, clamped — the cap is a ceiling, not a tunable.
- **RGPD legitimate-interest discipline.** Capture is minimized; an Art. 9
  special-category filter (`enrich.scrub_sensitive`) drops sensitive
  recent-activity lines. Internal-only; no external publication without
  per-case operator approval.
- **Operator-agnostic.** Operator email, timezone, cap, scan window all from env.

## Autonomy — checkpoint-gated

The enrichment **writes** are safe to run unattended once validated, but the
**first run must be operator-supervised** to confirm the Chrome flow, the
two-way queue logic, and the fail-safe behavior. **Do NOT auto-start scraping.**

### Supervised first run (do this with the operator watching, live Chrome)

```
# 1. Preview the queue — no scraping, no writes:
python3 linkedin_enrichment.py --preview-queue

# 2. In a Claude Code session with the Chrome MCP extension connected and
#    Chrome logged into LinkedIn, drive run_once() with a real scrape fn that:
#      - navigates to the contact's linkedin_url (or searches by name+company),
#      - reads title / employer / location / recent activity,
#      - raises daemon.LinkedInChallenge on ANY anti-automation prompt.
#    Start with --max 1 to confirm one clean end-to-end enrichment, then 10.
```

The scrape fn is intentionally NOT committed as an autonomous loop. It lives in
the supervised session until the operator signs off, then we decide scheduling
(launchd plist with randomized intervals) as a separate step.

## Environment

| var | purpose | default |
|---|---|---|
| `NEOTOMA_BASE_URL` / `NEOTOMA_BEARER_TOKEN` | Neotoma API | — |
| `GWS_OPERATOR_EMAIL` | operator's Gmail (queue reciprocity) | gws profile |
| `LINKEDIN_ENRICH_TZ` | local tz for day rollover | `Europe/Madrid` |
| `LINKEDIN_ENRICH_DAILY_CAP` | hard cap (clamped ≤ 10) | `10` |
| `LINKEDIN_ENRICH_SCAN_WINDOW` | Gmail date filter | `newer_than:3y` |
| `LINKEDIN_ENRICH_DRY_RUN` | `1` → preview writes only | `0` |

## Tests

```
python3 -m pytest test_linkedin_enrichment.py -q
```

Covers the cap, no-burst-catchup, enrich-once dedupe, notes-append-never-
overwrite, two-way reciprocity, Art. 9 scrub, and the challenge hard-stop.
