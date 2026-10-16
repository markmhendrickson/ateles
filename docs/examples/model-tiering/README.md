# Model tier policy

The runner reads the action policy and vendor bindings afresh for each dispatch.
Existing scalar entries name a minimum tier. An explicitly bounded entry has
both `min_tier` and `max_tier`:

```json
{
  "panel_aggregation": {"min_tier": "mid", "max_tier": "mid"},
  "lens_review:security": "top"
}
```

Automatic escalation is evaluated first, then limited by the declared maximum.
The audit records the measured signals even when the maximum prevents a tier
increase (`source=capped`). Invalid bounds, a minimum above its maximum, or a
missing model binding for a capped dispatch refuse the dispatch before launch.
They never discard the cap and use an ambient model.

The example policy maps content, aggregation, engineering, legal and generic
task dispatch to mid. Only content and aggregation have the approved mid cap.
Security and architecture remain top; blocking product findings still escalate.
Astra and Fable bindings refuse automatic routing; explicit model overrides
retain their existing semantics.

Validate both files with `model_tiering.py --check` before installation. Install
bounded entries only after the runtime supports them. The scalar mappings and
the cap implementation are separate rollout steps; changing a scalar minimum
alone does not prevent escalation.

The example Codex binding uses `gpt-5.6-terra` for mid and `gpt-6-sol`
for top. Omitting `dispatch_role --action-class` selects `task_dispatch`, whose
scalar minimum can still escalate. An explicit `--model` retains its override.
Selected configuration must be readable JSON object data; invalid input refuses
dispatch. A genuinely missing file still allows environment configuration, and
an entirely unconfigured installation retains the existing defaults.
