# Agent Development Workflows

**Purpose:** Rules for website development and MCP server development.

**Last Updated:** 2025-01-23

---

## Website Development

**MANDATORY:** Always create websites in their own repositories as git submodules. Never create website files directly in the parent repository.

**Process:**
1. **Create new private GitHub repository** for the website (use simple domain name, e.g., `dionysiandesigns` for dionysiandesigns.com)
2. **Initialize repository** with website files (HTML, CSS, assets, README.md)
3. **Add as submodule** in `execution/website/[name]/` directory
4. **Commit and push** website changes within the submodule repository
5. **Update parent repository** if submodule reference changed

**Repository Structure:**
- Website files (index.html, privacy-policy.html, etc.) in submodule root
- README.md with deployment instructions and contact information
- Submodule tracked in parent repository at `execution/website/[name]/`

**Location:** `execution/website/` - Websites are execution artifacts (deployment/automation) and belong in the Execution layer, consistent with the three-layer architecture.

**Examples:**
- ✅ `execution/website/dionysiandesigns/` - Submodule for dionysiandesigns.com website
- ✅ `execution/website/hendricksonserrano/` - Submodule for hendricksonserrano.com website
- ❌ Creating website files in root `websites/` directory is WRONG
- ❌ Creating website files directly in parent repository is WRONG

**Rationale:** Websites are independent projects that should be versioned separately, deployed independently, and can be shared or transferred without affecting the parent repository structure.

---

## MCP Server Development

**MANDATORY:** When making enhancements to MCP servers (located in `truth/mcp-servers/` for data access or `execution/mcp-servers/` for external integrations), always:
1. Commit changes within the MCP server's repository/submodule
2. Push changes to the remote repository immediately after committing
3. Update parent repository if submodule reference changed

**Rationale:** MCP servers are independent repositories/submodules that should be kept in sync with their remotes. Changes must be pushed to make them available for use.

### MCP Configuration

- **MANDATORY:** Never use absolute paths in MCP configuration files (e.g., `.cursor/mcp.json`)
- **REQUIRED:** Always use relative paths for commands and args (relative to repository root)
- **REQUIRED:** Never use absolute paths in environment variables - use relative paths, default paths, or system environment variables instead
- **Rationale:** Absolute paths make configurations non-portable and user-specific. Relative paths ensure configurations work across different systems and users.

### MCP Server Bug Fixing

- **MANDATORY:** Always fix MCP server bugs when encountered
- **MANDATORY:** Never read MCP server underlying files directly (e.g., Python source files in submodules)
- **REQUIRED:** Use MCP tools themselves or appropriate diagnostic methods to identify and fix issues
- **Rationale:** MCP servers are encapsulated interfaces - bugs should be fixed through their proper interfaces, not by directly accessing implementation files

### MCP Server Organization

- **`truth/mcp-servers/`** - Data access servers (parquet) that provide access to Truth Layer data substrate
- **`execution/mcp-servers/`** - External API integration servers (gmail, dnsimple, google-calendar, instagram, minted) that perform actions on external systems

### MANDATORY Documentation Requirements

All MCP servers MUST include comprehensive README.md documentation with:

1. **Tool Documentation**: Detailed documentation for each tool including:
   - Parameter descriptions with types and requirements
   - Example JSON requests for each tool
   - Example JSON responses
   - Use cases and notes
2. **Configuration Examples**: 
   - Cursor MCP configuration (JSON)
   - Claude Desktop configuration (JSON)
   - Environment variable setup
   - Authentication methods
3. **Integration Instructions**:
   - Installation steps
   - Setup requirements
   - Authentication setup (all supported methods)
4. **Error Handling**: Common errors and troubleshooting
5. **Security Notes**: Authentication, credential handling, security considerations
6. **Troubleshooting**: Common issues and solutions

**Reference:** Use `execution/mcp-servers/dnsimple/README.md` or `truth/mcp-servers/parquet/README.md` as templates for comprehensive documentation.






