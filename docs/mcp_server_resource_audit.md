# MCP Server Resource Audit

Date: 2025-01-15

## Executive Summary

Resources are a core MCP capability that provides read-only, discoverable data/templates to AI assistants. Currently, only 1 out of 9 MCP servers implements resources (Instagram). This audit identifies resource opportunities for all servers to improve discoverability and usability.

## Audit Findings

### Servers with Resources

#### Instagram (`mcp/instagram/`) ✅
**Status:** Fully implemented

**Resources:**
1. `instagram://profile` - Current Instagram business profile information
2. `instagram://media/recent` - Recent Instagram posts with engagement metrics
3. `instagram://insights/account` - Account-level analytics and insights
4. `instagram://pages` - Facebook pages connected to the account

**Assessment:** Excellent implementation. Profile and account resources provide context without requiring tool calls.

---

#### Parquet (`mcp/parquet/`) ⚠️
**Status:** Documented but NOT implemented

**Documented Resources (from README):**
1. `parquet://tools/{tool_name}` - JSON schemas for all tools
2. `parquet://catalog/data_types` - List of all available data types with descriptions
3. `parquet://examples/patterns` - Examples of common query patterns

**Issue:** README documents resources and includes usage examples, but `list_resources()` and `read_resource()` handlers are NOT implemented in code.

**Priority:** HIGH - Fix documentation/code mismatch

---

### Servers Missing Resources

#### Gmail (`mcp/gmail/`) - TypeScript
**Priority:** HIGH

**Recommended Resources:**
1. `gmail://profile` - Current user profile (email, name, quotas)
2. `gmail://labels` - List of all Gmail labels with message counts
3. `gmail://filters` - List of configured filters
4. `gmail://config/template` - Example configuration JSON
5. `gmail://tools/{tool_name}` - Tool schemas for all available tools

**Rationale:** Gmail has many discoverable entities (labels, filters) that are frequently needed for context. Profile info helps understand quota limits and account state.

---

#### Google Calendar (`mcp/google-calendar/`) - TypeScript
**Priority:** HIGH

**Recommended Resources:**
1. `calendar://profile` - Current user profile and settings
2. `calendar://calendars` - List of all calendars with metadata
3. `calendar://events/today` - Today's events across all calendars
4. `calendar://config/template` - Example configuration JSON
5. `calendar://tools/{tool_name}` - Tool schemas

**Rationale:** Calendar list is frequently needed for context. Today's events provide quick overview without tool calls. Multi-account setup makes profile info valuable.

---

#### DNSimple (`mcp/dnsimple/`) - Python
**Priority:** MEDIUM

**Recommended Resources:**
1. `dnsimple://accounts` - List of DNSimple accounts
2. `dnsimple://domains` - List of all domains with expiration dates
3. `dnsimple://tools/{tool_name}` - Tool schemas
4. `dnsimple://config/template` - Example configuration JSON

**Rationale:** Account and domain lists provide context for domain management operations. Domains resource is particularly valuable for quick status checks.

---

#### Minted (`mcp/minted/`) - Python
**Priority:** MEDIUM

**Recommended Resources:**
1. `minted://profile` - Current account profile
2. `minted://contacts/recent` - Recently accessed contacts
3. `minted://orders/pending` - Pending orders
4. `minted://tools/{tool_name}` - Tool schemas
5. `minted://config/template` - Example configuration JSON

**Rationale:** Order and contact context frequently needed. Profile info helps understand account state.

---

#### WhatsApp (`mcp/whatsapp/`) - Python
**Priority:** MEDIUM

**Recommended Resources:**
1. `whatsapp://profile` - Business profile information
2. `whatsapp://conversations/recent` - Recent conversations with metadata
3. `whatsapp://tools/{tool_name}` - Tool schemas
4. `whatsapp://config/template` - Example configuration JSON

**Rationale:** Recent conversations provide context for messaging operations. Profile info shows business account details.

---

#### HomeKit (`mcp/homekit/`) - Python
**Priority:** HIGH

**Recommended Resources:**
1. `homekit://devices` - List of all HomeKit devices with current state
2. `homekit://rooms` - List of rooms/zones with devices
3. `homekit://scenes` - Available scenes
4. `homekit://status` - Server connection status
5. `homekit://tools/{tool_name}` - Tool schemas

**Rationale:** Device and room lists are essential for HomeKit operations. Status resource helps debug connection issues. Highly valuable for discovery.

---

#### Asana (`mcp/asana/`) - Python
**Priority:** MEDIUM

**Recommended Resources:**
1. `asana://profile` - Current user profile
2. `asana://workspaces` - List of accessible workspaces
3. `asana://projects/recent` - Recently accessed projects
4. `asana://tools/{tool_name}` - Tool schemas
5. `asana://config/template` - Example configuration JSON

**Rationale:** Workspace and project lists provide context for task operations. Profile shows current user context.

---

#### Web Scraper (`mcp/web-scraper/`) - Python
**Priority:** LOW

**Recommended Resources:**
1. `webscraper://sources` - List of configured scraping sources
2. `webscraper://tools/{tool_name}` - Tool schemas
3. `webscraper://config/template` - Example configuration JSON

**Rationale:** Source list provides context for scraping operations. Relatively simple server, fewer resource needs.

**Agent policy:** For ChatGPT share URLs (`chatgpt.com/share/...`), agents MUST use this server’s `scrape_content` tool—not generic HTTP fetch—so thread text is actually loaded. See `docs/developer/chatgpt_share_url_rules.md`.

---

#### Google Search Console (`mcp/google-search-console/`) - TypeScript
**Priority:** MEDIUM

**Recommended Resources:**
1. `searchconsole://profile` - Current user profile
2. `searchconsole://sites` - List of verified sites with metadata
3. `searchconsole://tools/{tool_name}` - Tool schemas
4. `searchconsole://config/template` - Example configuration JSON

**Rationale:** Site list provides context for SEO operations. Profile shows current user and permissions.

---

## Universal Resources

All servers should consider implementing:

1. **Tool Schemas** (`{server}://tools/{tool_name}`)
   - Provides JSON schema for each tool
   - Enables tool discovery without code inspection
   - Should be dynamically generated from tool definitions

2. **Configuration Template** (`{server}://config/template`)
   - Example Cursor/Claude Desktop configuration JSON
   - Includes all environment variables and paths
   - Helps users configure the server

## Priority Summary

**HIGH Priority (implement first):**
- Parquet (fix documentation mismatch)
- Gmail (high value for label/filter discovery)
- Google Calendar (high value for calendar list)
- HomeKit (high value for device discovery)

**MEDIUM Priority:**
- DNSimple
- Minted
- WhatsApp
- Asana
- Google Search Console

**LOW Priority:**
- Web Scraper (simple server, fewer needs)

## Implementation Guidelines

### When to Implement Resources

**Always consider:**
- Tool schemas (all servers)
- Account/profile information (API-based servers)
- Configuration templates (all servers)

**Consider if relevant:**
- Lists of domain entities (labels, calendars, devices, domains)
- Recent/active items
- Status/health information
- Common query results

**Not needed:**
- Simple servers with few tools and no discoverable state
- Servers where all operations are stateless

### Resource vs Tool vs Prompt

**Use Resources for:**
- Read-only data that's frequently needed for context
- Discoverable lists (labels, calendars, devices)
- Server metadata (tool schemas, configuration templates)
- Status/health information

**Use Tools for:**
- Actions that modify state
- Operations with complex parameters
- Operations that may fail or have side effects

**Use Prompts for:**
- Formatted messages for AI assistants
- Guidance and workflows
- Template-based interactions

## Next Steps

1. **Immediate:** Fix Parquet documentation/implementation mismatch
2. **High Priority:** Implement resources for Gmail, Calendar, HomeKit
3. **Medium Priority:** Implement resources for other servers
4. **All Servers:** Add tool schema resources for discoverability
5. **Update Guide:** Ensure development guide includes resources section