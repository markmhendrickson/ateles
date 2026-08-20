# Agent Prompt Instruction Integration

**Purpose:** Auto-integrate persistent instructions from user prompts.

**MANDATORY:** When user provides guidance indicating persistent behavior ("always do X", "when Y happens, do Z", "follow this rule"), automatically integrate as ongoing rules.

**Detection:** Phrases like "always", "whenever", "when X happens", "follow this", "going forward". Instructions describing behavior patterns, not one-time tasks.

**Process:** Identify intent → Integrate immediately → Treat as rule → No confirmation needed.

**Examples:** "always use normalized data files" → persistent rule. "when creating reports, include X" → apply to all reports. "going forward, do Y" → ongoing behavior.

**Reference:** `/shared/docs/agent/rules/persistence-requirements.md` for instruction persistence.

