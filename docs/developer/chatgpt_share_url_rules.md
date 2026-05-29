# ChatGPT share URL retrieval

## Purpose

ChatGPT conversation share pages (`chatgpt.com/share/...`) render the thread body in the client. Plain HTTP fetch tools return shell markup only, so agents cannot reliably read the conversation without a browser-capable scraper.

## Trigger Patterns

When a task requires **reading or summarizing content** from a URL that matches:

- `https://chatgpt.com/share/` …
- `http://chatgpt.com/share/` …

…agents MUST retrieve that URL through the **ateles web-scraper MCP**, not through generic web fetch or raw HTTP.

## Agent actions

1. Confirm the URL is a ChatGPT share (or conversation) link as described above.
2. Call the web-scraper MCP tool **`scrape_content`** with:
   - `url`: the full share URL
   - `method`: `auto` (tries Apify, then Playwright, then requests) unless you need to force `playwright` or `apify`
3. Use the scraped payload (title, messages, or fields returned by the server) as the source of truth for quotes, outlines, and blog-idea capture.
4. If scraping fails (auth, rate limit, missing Playwright), report the failure and ask the user to paste the thread text or export—do not pretend a plain fetch was sufficient. When Apify fails, surface the error’s **statusMessage** and console run URL if present.

## Constraints

- Agents MUST NOT use `web_fetch`, `mcp_web_fetch`, or equivalent non-browser HTTP retrieval as the primary way to load ChatGPT share URLs for content extraction.
- Agents MAY use plain fetch only for non-share ChatGPT URLs if a separate rule or task explicitly allows it; this rule applies specifically to **`/share/`** (and the same policy applies to **`/c/`** conversation URLs when the goal is to read thread content).
- Agents MUST prefer **`scrape_content`** on the web-scraper MCP because it supports ChatGPT as a first-class source (Playwright / Apify / requests per server implementation).

## Quick reference

| Domain | Required tool |
|--------|----------------|
| ChatGPT share (`chatgpt.com/share/...`) | Web-scraper MCP `scrape_content` |
| ChatGPT thread in browser (`chatgpt.com/c/...`) when reading messages | Web-scraper MCP `scrape_content` |
