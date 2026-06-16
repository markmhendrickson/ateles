# formica

GitHub issue/PR automation daemon for the ateles repo. SSE subscriber to ateles GitHub events; dispatches issues and PRs to T4 invocable workers (Gryllus for implementation, Vanellus for PR review). Symmetric to neotoma-agent (which handles the neotoma repo). Currently JS; Phase 5 Python rewrite using lib/daemon_runtime/.

| Field | Value |
| --- | --- |
| Tier | T3 |
| Genus | Formica |
| Status | active |
| AAuth sub | formica@ateles-swarm |
| Agent grant | service |
| Allowed tools | github |
| Entity ID | ent_d62f1df8784b7f4fcadc7d74 |

---

Operational prompt: [`.claude/skills/formica/SKILL.md`](../../.claude/skills/formica/SKILL.md)

*Reference card mirrored from Neotoma `agent_definition` `ent_d62f1df8784b7f4fcadc7d74`. Do not edit directly — correct the entity and run `python3 execution/scripts/render_agent_docs.py`.*
