# Ateles

Personal agent swarm infrastructure — Neotoma-canonical, auditable, extensible.

## What this is

Ateles is a working, production blueprint for running a personal agent swarm where every agent action is attributed, versioned, and queryable. It is built around [Neotoma](https://github.com/markmhendrickson/neotoma) as the canonical memory and state layer.

**Not a framework. Not a toy. A reference architecture that runs in production.**

## Core principles

- **Neotoma-canonical**: agent definitions, payment profiles, and configuration live in Neotoma as entities — not hardcoded in files. Updating an agent's behaviour is a `correct()` call, not a code commit.
- **Auditable by design**: every agent action is an attributed observation. "Why did this happen?" always has a traceable answer.
- **Minimum viable**: ~1,800 LOC total. Apprise for notifications, launchd for scheduling, Claude Agent SDK for execution. No orchestration framework until the operational need is proven.
- **Public by default**: architecture docs are generated mirror artifacts. Private data stays in private repos and env vars.

## Agent taxonomy

See [docs/taxonomy.md](docs/taxonomy.md) for the full agent table.

| Tier | Role | Examples |
|---|---|---|
| **T1** | Hosts (T1 is a role — see docs/architecture.md for alternatives) | OpenClaw, launchd |
| **T2** | Resident agents (always-on) | Onychomys, Menura |
| **T3** | Daemons (event-driven background) | Formica, neotoma-agent, Apis, Apus, Piculet, Strix |
| **T4** | Invocable agents (stateless, spawned per task) | Loxia, skill-based agents |

## Structure

```
ateles/
├── docs/
│   ├── taxonomy.md          # canonical agent table
│   ├── architecture.md      # system design and Neotoma integration
│   └── phases.md            # implementation roadmap (mirror of Neotoma plan)
├── execution/
│   └── daemons/             # T3 daemon implementations
│       ├── monedula/        # recurring payment daemon (Wise + BTC)
│       ├── formica/         # GitHub issue/PR automation
│       ├── piculet/         # audio transcription daemon
│       ├── strix/           # meeting/ambient audio recorder
│       ├── neotoma-agent/   # neotoma-repo automation daemon
│       └── apus/            # HTTPS webhook receiver (Neotoma mirror triggers)
└── lib/
    ├── notify/              # Apprise-backed notification routing
    └── daemon_runtime/      # SSE subscription, agent_definition loader, AAuth signer
```

## Key dependencies

- [Neotoma](https://github.com/markmhendrickson/neotoma) — memory and state layer
- [Claude Agent SDK](https://docs.anthropic.com/claude/agent-sdk) — agent execution
- [Apprise](https://github.com/caronc/apprise) — notification delivery
- `gws` CLI — Google Calendar / Gmail integration
- `neotoma` CLI — Neotoma entity operations

## Architecture plan

The full architecture plan lives in Neotoma as entity `ent_99ace4dd6673aa36ed08b1fe` and is mirrored to [docs/phases.md](docs/phases.md).

## Positioning

> *Ateles is what personal agent infrastructure looks like when it's done right — auditable, extensible, and Neotoma-backed.*

Ateles is the reference architecture and proof artifact for Neotoma's core claim: that observation provenance makes agent behaviour explainable. See [docs/architecture.md](docs/architecture.md) for the full design rationale.

## License

MIT
