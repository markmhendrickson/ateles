# Agent Prompt Instruction Integration

**Purpose:** Rules for automatically integrating persistent instructions from user prompts.

**Last Updated:** 2025-01-23

---

## Prompt Instruction Integration

**MANDATORY:** When the user provides guidance or instructions in a prompt that indicate persistent behavior (e.g., "always do X", "when Y happens, do Z", "follow this rule"), automatically integrate those instructions as ongoing rules to follow.

**Detection patterns:**
- Phrases like "always", "whenever", "when X happens", "follow this", "going forward"
- Instructions that describe behavior patterns, not one-time tasks
- Rules or guidelines that apply beyond the current task
- Documentation-style instructions provided in prompts

**Process:**
1. **Identify instruction intent** - Recognize when user-provided guidance is meant to be persistent
2. **Integrate immediately** - Apply the instruction in the current task without prompting
3. **Treat as rule** - Follow the instruction in all future relevant tasks
4. **No confirmation needed** - Do not ask "should I always do this?" - if guidance suggests persistence, treat it as such

**Examples:**
- User says "always use normalized data files" → Treat as persistent rule
- User says "when creating reports, include X" → Apply to all future reports
- User provides a decision rule → Follow it whenever the condition applies
- User says "going forward, do Y" → Integrate as ongoing behavior

**Rationale:** Ensures prompt-provided instructions are automatically integrated as persistent rules, eliminating need for explicit confirmation or documentation requests.

**Reference:** `/shared/docs/agent-persistence-requirements.md` for instruction persistence requirements.






