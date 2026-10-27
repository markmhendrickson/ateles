<!-- ateles-turn-guard:$automation_id -->
Heartbeat guard for automation $automation_id. The job brief is not in this message.

1. Before anything else, do not load context: no AGENTS.md, skills, memory, Neotoma or repositories.
2. Read the brief once, from this file: $brief_path
3. Follow the brief. If it shows nothing has changed since the last run, end with an empty final message and make no further tool call.

This message is an instruction, not a mechanism. The binding check is the Ateles codex_turn_guard hook, which blocks an idle heartbeat before the model runs; if the hook is not installed and trusted, this stub only shortens the prompt.
