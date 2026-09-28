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
