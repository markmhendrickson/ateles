# Local LLM stack for the `claude-local` provider

The `claude-local` harness provider (`execution/daemons/apis/local_provider.py`)
runs the ordinary `claude` CLI against a local model. Every hop is loopback:

```
claude CLI ──ANTHROPIC_BASE_URL──▶ LiteLLM :4000 ──▶ ollama_shim :11435 ──▶ Ollama :11434
```

The local model is driven through the claude CLI, not through a separate agent
loop, so the repo's PreToolUse guards (stash, sibling-repo, Gmail send, gh
identity) bind on the local path too. The provider never passes `--bare`,
because `--bare` skips every hook, including hooks loaded with `--settings`.

## Files

| File | Role |
|---|---|
| `ollama_shim.py` | Folds late system messages, defaults temperature to 0, repairs text-form tool calls for declared tools, re-emits SSE, and **refuses prompts over the context ceiling** so Ollama cannot silently truncate them. |
| `litellm_config.template.yaml` | LiteLLM proxy config (contains no secrets). `model_name` must equal the provider config's `model`. |
| `eval_lean_prompt.py` | Live correctness eval for the lean local prompt — see below. |

## The lean local prompt

A `claude-local` dispatch does NOT receive the same system prompt a frontier
dispatch does. The frontier prompt (`skill_runner.build_system_prompt`) is the
role's full `agent_definition.prompt_markdown` plus the live `agent_policy`
rendering — sized for a ~200K-token window and covering every judgement domain
the swarm has (Gmail, payments, RGPD, PR review conventions, …), none of which
a mechanical local job touches. Measured 2026-09-29 (ateles task
`ent_71387d9c1d1d3d1eef9ecc01`): that full prompt for one role cost ~44K
tokens, ~35K of it (79%, 70 policy rows) `agent_policy` content with no
relevance filter. At a 32K local ceiling it does not fit at all; forced to fit
at 64K, the model returned a confidently WRONG answer (7 prunable worktrees
claimed vs. 56 true) while still reporting `ok: true`.

`local_provider.build_lean_prompt(work_class)` replaces it, for `claude-local`
dispatches ONLY, with: a one-line role summary, and the small set of hard
rules relevant to that work class (`local_provider.MECHANICAL_WORK_CLASSES`).
It deliberately does not inline the dispatched role's SKILL.md either — every
role's SKILL.md in this repo is that role's own agent_definition mirror, not a
lean task description; the actual task instructions still reach the model in
full, unfiltered, as the ordinary stdin prompt (the user turn), separate from
the system prompt. Guard-enforced rules (never stash, never `--no-verify`,
worktree isolation) are not restated in the lean prompt either: they bind
through the `PreToolUse` hooks regardless of what the prompt says. Result: the
lean prompt for every mechanical work class is 200-300 tokens, leaving nearly
the entire 32K ceiling for tool output. Frontier dispatches
(`build_system_prompt`) are completely unchanged by this — the lean prompt is
an additive, local-only path.

## Post-condition checks

A local run exiting 0 with a plausible reply is not the same as a CORRECT
reply — that gap is exactly what produced the wrong-but-`ok`-reporting
regression above. `local_provider.verify_postcondition(work_class, stdout,
cwd=...)` re-derives the work class's ground truth independently of the
model's own words (for `worktree_hygiene`: counting `prunable` lines in `git
worktree list --porcelain` directly) and refutes a wrong answer, which
`skill_runner._run_skill_once` folds into the dispatch result the same way it
already does for a detected delivery denial — the run is marked NOT ok and
`_run_provider_attempts` fails it over to a frontier provider rather than
accepting a wrong local result. This is deliberately narrow: only a work class
with an objective, cheaply-computed ground truth gets a checker
(`local_provider._POSTCONDITION_CHECKS`); a class with none is unverified
beyond its exit code, same as before this existed.

## Correctness eval

```bash
python3 execution/local_llm/eval_lean_prompt.py
```

Requires the live local stack already running and `ATELES_REPO_PATH` pointed
at a checkout with the guard hooks wired — the same preconditions as any other
`claude-local` dispatch. It does not start, stop, or reconfigure the stack,
and never touches `~/.config/ateles/claude-local.json`. Two cases:

1. **`lean_prompt_fits_ceiling`** — every mechanical work class's lean prompt,
   measured against the host's real configured ceiling, with headroom
   reported. Static; no dispatch.
2. **`worktree_hygiene_correctness`** — builds a throwaway git repo with a
   known number of prunable and live worktrees, dispatches a real
   `dispatch_role.py --provider claude-local --work-class worktree_hygiene`
   subprocess against it, and requires both `ok: true` AND that the model's
   answer survives the post-condition check (which is itself checked against
   this script's own independently-computed ground truth, not the model's).

Exit code is 0 iff every case passed.

The guards-only Claude settings file is generated at dispatch time from the
repo's own `.claude/settings.json` (`local_provider.write_guards_file`), so it
cannot drift from the frontier path's guards. A missing required guard refuses
the local launch.

## Running the stack by hand

```bash
OLLAMA_CONTEXT_LENGTH=32768 ollama serve
SHIM_CONTEXT_CEILING=32768 python3 execution/local_llm/ollama_shim.py
litellm --config execution/local_llm/litellm_config.template.yaml --host 127.0.0.1 --port 4000
```

`SHIM_CONTEXT_CEILING` is required and must equal Ollama's context length; the
shim refuses to start without it.

This PR sets up no persistent service (launchd). That setup is a separate,
operator-approved step.

## Enabling routing

The provider is off until `~/.config/ateles/claude-local.json` (or the path in
`APIS_CLAUDE_LOCAL_CONFIG`) holds a valid, enabled config. The docstring of
`local_provider.py` gives the full schema. Only the mechanical work classes in
`local_provider.MECHANICAL_WORK_CLASSES` can be enabled. A run with an eligible
`work_class` tries `claude-local` first, and any local failure falls over to the
frontier providers, with the reason recorded as a `provider_failover`
harness_event.

```bash
python3 execution/daemons/apis/dispatch_role.py --role cicada \
  --work-class rebase --cwd <worktree> --task "Rebase this branch onto origin/main and push."
```
