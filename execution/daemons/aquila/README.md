# Aquila — Cofounder Report Daemon

Aquila is the swarm's **cofounder & strategic adversary**. It exists to interrogate
the work on Neotoma at its foundations and surface the operator's blind spots —
not to validate. Two surfaces, one posture (adversarial by construction,
evidence-or-silence):

- **Monthly report** (this daemon) — a scheduled deep pass over the operator's
  Neotoma record that produces a nine-section cofounder report.
- **On-demand consult** (the `aquila` skill) — invoke `/aquila` (or ask Ateles a
  fundamental business question) for a grounded, opinionated, self-adversarial read.

Canonical definition: `agent_definition` `ent_2d342f332f73bd60bfbee5a6` (mirrored to
`.claude/skills/aquila/SKILL.md`). The prompt is public and PII-free; all
operator-specific reasoning is built from Neotoma entities at runtime.

## How it works

`aquila.py` does **no reasoning**. It is a thin scheduler:

1. **Monthly idempotency** — `.aquila_last_run` stores `YYYY-MM`; re-launches in the
   same month exit immediately (override with `--force`).
2. **Dispatch** — invokes the `aquila` skill as a T4 agent via the Apis
   `skill_runner.run_skill`, which spawns `claude --print` with the agent's
   `prompt_markdown` + `SKILL.md`. The skill reads the corpus, writes a
   `cofounder_report` entity linked `PART_OF` the plan, and returns the report
   markdown as its final message.
3. **Deliver** — the returned markdown is sent to the operator over Telegram,
   chunked under the 4096-char limit.
4. **Paper trail** — emits a `daemon_report` (Anthus surfaces error/critical to
   Ateles).

## Output

Each run stores one `cofounder_report` (`report_key: monthly-<YYYY-MM>`) with the
nine sections, an `open_findings` array carried forward month-to-month, and the
full markdown. The monthly **scorecard delta** section diffs against the prior
report and escalates findings the operator ignored. The report is
**operator-confidential** — never published externally.

## Run

```bash
python3 aquila.py            # current month, idempotent
python3 aquila.py --force    # ignore the monthly guard
python3 aquila.py --period 2026-06
```

## Install (launchd, macOS)

```bash
./install.sh                 # monthly on the 1st at 06:00 Madrid
```

## Environment

Reads from `~/.config/neotoma/.env`:

| Var | Purpose |
|---|---|
| `NEOTOMA_BEARER_TOKEN` | Neotoma API auth (daemon_report) |
| `NEOTOMA_BASE_URL` | Neotoma endpoint |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | report delivery (via `lib/notify`) |
| `APIS_CLAUDE_BIN` | `claude` binary for skill dispatch (else on PATH) |
| `AQUILA_TIMEOUT_SECONDS` | skill dispatch timeout (default 1800) |
