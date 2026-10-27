# Install: getting Ateles and Neotoma into each harness

## Purpose

How an operator gets each repository's payloads — tools, skills, rules, session-start context, guards —
into each harness they use: Claude Code, Claude chat (desktop and web), ChatGPT and Codex, and Cursor.
For each harness it names what gets installed, the command that installs it where one exists, how to
verify it took, and the manual route where no command applies.

Which carrier each harness gets, and why, is design, and lives in
[`foundation/harness_carriers.md`](foundation/harness_carriers.md): its capability matrix and its
`#targeting` section decide the rung, and this guide only carries it out. When the two disagree, the
foundation document is right and this guide is stale.

## Scope

Commands that exist in the two repositories' `main` branches, and the vendors' own commands, as of
2026-09-25. Where a path does not exist yet, this guide says so under **Open items** rather than naming a
command that would fail. It does not cover provisioning an operator's record (`ateles provision`, see
[`installability.md`](installability.md)) or deploying daemons (see [`setup.md`](setup.md)).

Placeholders: `<ateles-checkout>` is the dedicated deployment checkout the Ateles daemons run from — never
the shared clone sessions work in, whose branch changes under other sessions. `<tool>` is the harness name
a command expects.

## The default command

| Repository | Default command | State |
|---|---|---|
| Neotoma | `npm install -g neotoma` then `neotoma setup --tool <tool> --yes` | Exists. Detects the harness when `--tool` is omitted and `neotoma status` can supply a hint. |
| Ateles | none yet | Open: there is no single command that detects the harness and installs its rung. The nearest today is `python3 -m ateles doctor` from the checkout, which reports what is missing. |

The intended Ateles command extends the existing `ateles` command line (`ateles init` / `doctor` /
`provision`, [`installability.md`](installability.md)) with an install verb that detects the harness and
installs the highest rung the foundation matrix allows, reading the matrix as data so this guide and the
installer cannot disagree. It does not exist yet.

## Ateles, per harness

### Claude Code — target rung: bundle with a hook

**What gets installed:** the Ateles MCP server (reach), the repository's skills and hooks (method,
session-start context, guards), and — once [#1268](https://github.com/markmhendrickson/ateles/pull/1268)
merges — the session-start rule index.

1. Register the server at user scope, pointing at the deployment checkout's launcher:

   ```bash
   claude mcp add --scope user ateles -- <ateles-checkout>/execution/mcp/ateles/run_ateles_mcp.sh
   ```

   The launcher reads the Neotoma credentials from the operator's local Neotoma env file and builds its own
   interpreter on first start ([`execution/mcp/ateles/README.md`](../execution/mcp/ateles/README.md) lists
   the variables).
2. Skills and hooks: sessions opened inside an Ateles checkout pick up its repository-level
   `.claude/skills/` and `.claude/settings.json` hooks with no install step. For sessions opened in other
   repositories, user-level wiring is manual (below, open item).

**Verify:** `claude mcp get ateles` shows the server connected; in a new session the `mcp__ateles__*` tools
are listed and `get_swarm_roster` returns the roster. The server's `instructions` arrive whole (about 1.1 KB);
if a session reports no Ateles pointer in its instructions, check that other connected servers are not
exhausting the shared instructions budget (`foundation/harness_carriers.md`, measured constraint 1).

**Manual route:** none needed beyond the above. The bundle that would make this one install does not exist
(open item).

### Claude chat (desktop and web) — target rung: integration

**Desktop app, local server:** the desktop app can launch a local stdio server from its own configuration
file. Add an `ateles` entry under `mcpServers` whose `command` is
`<ateles-checkout>/execution/mcp/ateles/run_ateles_mcp.sh`, then restart the app. **Verify:** the Ateles
tools appear in the app's tool list. No hooks and no rules delivery exist on this surface.

**Web:** no path. An integration needs a remote HTTPS MCP endpoint with OAuth, and the Ateles server speaks
stdio only (open item).

### ChatGPT — target rung: integration

No path. ChatGPT apps reach only a remote MCP endpoint, which Ateles does not serve (open item).

### Codex — target rung: bundle with a hook

**What gets installed:** the Ateles MCP server and the repository-owned user-scope hook definition. Add the
server to the user's Codex configuration
(`~/.codex/config.toml`):

```toml
[mcp_servers.ateles]
command = "<ateles-checkout>/execution/mcp/ateles/run_ateles_mcp.sh"
```

Install the hook definition from the checkout whose code Codex should run:

```sh
python3 <ateles-checkout>/execution/scripts/install_codex_hooks.py
```

The installer merges only the Ateles handlers into the user's existing `hooks.json`, resolves their scripts
to that checkout, and leaves unrelated hooks intact. It copies no rule body: `SessionStart`,
`SubagentStart`, and `UserPromptSubmit` render the canonical live `agent_policy` rows, while `PreToolUse`
uses the shared Ateles guards. Codex requires a separate trust decision for non-managed hooks; open
`/hooks`, review the Ateles definitions, and trust them.

Codex also discovers the committed project-level `.codex/hooks.json`, and its documented composition
model runs matching user- and project-level commands concurrently. The rule-delivery hooks therefore use
one locked receipt under the Codex home: identical content for the same lifecycle revision is injected
once, while a later startup, resume, compaction, subagent, or prompt revision remains deliverable. Keep both
carriers installed; the project file is the portable binding and the user install remains the outside-repo
carrier.

**Verify:** start a new Codex session and list its MCP servers; the Ateles tools are available. The hook
panel shows the trusted Ateles entries, and session start reports `Loading live Ateles rules`. Then run:

```sh
python3 <ateles-checkout>/execution/scripts/install_codex_hooks.py --check
python3 <ateles-checkout>/execution/scripts/test_codex_rule_hooks.py
```

The first command reports the installed entries current. The second executes the configured commands
against a synthetic local rule corpus and checks both model-context renderers and blocking guard decisions;
config presence alone is not treated as proof. The user-scope path is the measured path in Codex CLI
0.153.4. A generated `AGENTS.md` remains a fallback for installations that have not trusted the hook, not a
second policy source.

### Cursor — target rung: protocol, beside a generated rules file

**What gets installed:** the Ateles MCP server. Add an `ateles` entry to `mcpServers` in `~/.cursor/mcp.json`
(user) or `.cursor/mcp.json` (project) with the launcher as its `command`. **Verify:** Cursor's MCP settings
show the server enabled with its tools. **Missing:** the generated rules file (open item). Whether Cursor's
session-start hooks can deliver the rule index instead is unmeasured.

## Neotoma, per harness

Neotoma already ships the per-harness command-line path; its own install guide is canonical
(`install.md` in the Neotoma repository) and this section only indexes it.

| Harness | Command | Verify |
|---|---|---|
| Claude Code | `neotoma setup --tool claude-code --yes`; hooks as a bundle: `/plugin marketplace add markmhendrickson/neotoma` then `/plugin install neotoma` in Claude Code, or `neotoma hooks install --tool claude-code`, which prints the snippet to apply | `neotoma setup` prints a line beginning `Neotoma installed at`; `neotoma status --json` reports the MCP entry and hook state |
| Cursor | `neotoma setup --tool cursor --yes`; hooks: `neotoma hooks install --tool cursor` (opt-in) | as above |
| Codex | `neotoma setup --tool codex --yes`; hooks: `neotoma hooks install --tool codex` (opt-in) | as above |
| Claude desktop | `neotoma setup --tool claude-desktop --yes` (writes the MCP entry only) | the Neotoma tools appear in the app |
| Claude web, ChatGPT | no command: add the instance's remote MCP endpoint as an integration in the app's settings, per the Neotoma repository's remote-MCP and ChatGPT-apps guides | the integration shows connected and its tools are callable |

Neotoma installs hooks only on an explicit opt-in after activation, never as part of `setup`; follow its
install guide's activation step rather than running `hooks install` unprompted.

## Open items

Each is a path the design assigns and the repository does not yet provide. `foundation/status.md`
(revision 124) records the checkout state behind each.

- **An Ateles install verb.** One command that detects the harness and installs its rung, reading the
  payload-by-carrier-by-harness matrix as data.
- **An Ateles Claude Code bundle.** One install carrying the server registration, skills, the session-start
  hooks, and the pre-action hooks.
- **User-level hook wiring for Claude Code.** Needed so the rule index reaches sessions opened outside an
  Ateles checkout; today a manual edit of the user's settings, specified in the rule-index pull request.
- **A remote transport for the Ateles MCP server.** Required before Claude web or ChatGPT can reach Ateles at
  all.
- **A generated Cursor rules file,** rendered from the record by the installer and never edited by hand.
  Codex has a measured user-scope hook path; its local instruction file is only a pre-install fallback.
- **A freshness check on the MCP server's checkout.** The launcher path is set on the host; nothing reports
  when the checkout it points at drifts from `main`.
