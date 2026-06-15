# monedula

Payment execution daemon. Runs once daily via launchd; checks Google Calendar (via gws) for yesterday's sessions that trigger payment obligations, and cross-references Neotoma payment tasks (created by Sylvia, Turdus, or manually). Executes Wise IBAN and BTC transfers for finance-domain tasks. Never auto-executes: every payment raises a blocking PLAN checkpoint and waits for explicit operator approval (confidence_threshold=1.0). Sends Telegram notifications on completion or failure via lib/notify/. Watches Calendar for payment triggers but does not own general task lifecycle — payment executor only.

| Field | Value |
| --- | --- |
| Tier | T3 |
| Genus | Corvus monedula |
| Status | active |
| AAuth sub | monedula@ateles-swarm |
| Agent grant | service |
| Allowed tools | btc_wallet_preview_transfer, btc_wallet_send_transfer, mcp__mcpsrv_neotoma__store, mcp__mcpsrv_neotoma__correct, mcp__mcpsrv_neotoma__retrieve_entities, mcp__Claude_in_Chrome__navigate, mcp__Claude_in_Chrome__get_page_text, mcp__Claude_in_Chrome__read_page, mcp__Claude_in_Chrome__find, mcp__Claude_in_Chrome__javascript_tool, mcp__Claude_in_Chrome__read_network_requests |
| Entity ID | ent_26e45f38f53798eb42961a69 |

---

Operational prompt: [`.claude/skills/monedula/SKILL.md`](../../.claude/skills/monedula/SKILL.md)

*Reference card mirrored from Neotoma `agent_definition` `ent_26e45f38f53798eb42961a69`. Do not edit directly — correct the entity and run `python3 execution/scripts/render_agent_docs.py`.*
