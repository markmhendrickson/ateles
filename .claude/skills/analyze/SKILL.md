---
name: analyze
description: Analyze codebase or context per foundation analyze command. Produces a comparative analysis (competitive/partnership/relevance) of a target product, content, or repo against all known repos in Neotoma; stores the full analysis plus sub-entities (tasks, findings, identified repos, proposed issues) in Neotoma; and, opt-in, opens public GitHub issues for repo-touching tasks with the competitive analysis sections redacted.
triggers:
  - analyze
  - /analyze
user_invocable: true
entity_id: ent_2abe100df424dbe78fd3c5f5
---

## Notes

- This command is generic and works for any repo using foundation as submodule
- **Comparative analysis:** All analysis is relative to **all** repos. Load repos from Neotoma (`repository` entities). If empty, run `execution/scripts/sync_repos_to_neotoma.py`.
- Output documents are confidential and stored in private docs submodule
- **Resource Type Detection:** Command automatically detects if resource is a product/project (competitive/partnership analysis) or content/thought leadership (relevance analysis)
- Templates ensure consistent, thorough analysis across all assessments
- For content/thought leadership, analysis focuses on extracting insights applicable to current repo rather than competitive positioning
- **Web Scraper Integration:** ChatGPT and Twitter/X URLs are automatically handled via web scraper MCP server when configured (see MCP configuration)
- **Browser Tools Fallback:** Non-scraper URLs and search terms use browser tools for research
