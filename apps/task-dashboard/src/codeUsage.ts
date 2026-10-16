/**
 * STATIC REPO SCAN — NOT LIVE DATA.
 *
 * Whether an entity type has a READER cannot be answered by any Neotoma query:
 * Neotoma knows what was written, never what reads it. That question is only
 * answerable by static analysis of the code, so this file is GENERATED from a
 * grep over the ateles repo and is a SNAPSHOT, not a live signal.
 *
 * Regenerate with the script noted in the Schemas view; the UI labels every
 * column sourced from here as static and shows the commit and timestamp below,
 * so a reader can tell how old it is.
 *
 * HOW IT DECIDES, and where it is weak — both stated on screen:
 *   READER  — a query/retrieval call (`retrieve_entities`, `/entities/query`, …)
 *             within 6 lines of a string literal naming the type.
 *   WRITER  — a `store`/`submit_entity`/`correct` call in the same window.
 *
 * PROXIMITY IS A HEURISTIC. A type read through a variable rather than a
 * literal is INVISIBLE to this scan, so a zero here means "no reader found by
 * this method", never "no reader exists". Counts exclude test files: a type
 * whose only reader is its own test is not read in production, and that
 * distinction is carried as `testOnlyReaders`.
 *
 * It also sees ONE repo. A reader living in neotoma, openclaw, or an operator
 * script is out of scope and cannot be counted here.
 */

export interface CodeUsage {
  readers: string[];
  writers: string[];
  readerCount: number;
  writerCount: number;
  /** Readers exist, but only inside test files — not read in production. */
  testOnlyReaders: boolean;
}

export const SCAN_META = {
  "generatedAt": "2026-08-31T14:13:12+00:00",
  "commit": "47d786e",
  "filesScanned": 312,
  "repoRoot": "ateles"
} as const;

export const CODE_USAGE: Record<string, CodeUsage> = {
  "agent_definition": {
    "readers": [
      "apps/task-dashboard/server/neotomaProxy.ts:164",
      "execution/mcp/ateles/server.py:407",
      "execution/scripts/render_agent_docs.py:117",
      "lib/daemon_runtime/agent_loader.py:196"
    ],
    "writers": [],
    "readerCount": 4,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "agent_message": {
    "readers": [],
    "writers": [
      "execution/mcp/mcp_tool_grant_proxy/session_integrity.py:49",
      "lib/daemon_runtime/session_finalize.py:179"
    ],
    "readerCount": 0,
    "writerCount": 2,
    "testOnlyReaders": false
  },
  "agent_policy": {
    "readers": [
      "lib/daemon_runtime/agent_loader.py:281",
      "lib/daemon_runtime/generalizer.py:274",
      "scripts/revert_auto_policy.py:87"
    ],
    "writers": [
      "lib/daemon_runtime/generalizer.py:434",
      "scripts/revert_auto_policy.py:54"
    ],
    "readerCount": 3,
    "writerCount": 2,
    "testOnlyReaders": false
  },
  "brand_voice": {
    "readers": [],
    "writers": [],
    "readerCount": 0,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "channel_config": {
    "readers": [],
    "writers": [],
    "readerCount": 0,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "checkpoint_brief": {
    "readers": [
      "execution/mcp/ateles/server.py:469"
    ],
    "writers": [
      "execution/mcp/ateles/server.py:532"
    ],
    "readerCount": 1,
    "writerCount": 1,
    "testOnlyReaders": false
  },
  "conversation": {
    "readers": [
      "apps/task-dashboard/server/neotomaProxy.ts:266",
      "execution/daemons/riparia/riparia.py:220"
    ],
    "writers": [
      "execution/mcp/mcp_tool_grant_proxy/session_integrity.py:49"
    ],
    "readerCount": 2,
    "writerCount": 1,
    "testOnlyReaders": false
  },
  "conversation_message": {
    "readers": [],
    "writers": [
      "execution/mcp/mcp_tool_grant_proxy/session_integrity.py:49"
    ],
    "readerCount": 0,
    "writerCount": 1,
    "testOnlyReaders": true
  },
  "daemon_report": {
    "readers": [],
    "writers": [],
    "readerCount": 0,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "deployment_configuration": {
    "readers": [],
    "writers": [],
    "readerCount": 0,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "email_message": {
    "readers": [],
    "writers": [],
    "readerCount": 0,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "escalation": {
    "readers": [],
    "writers": [],
    "readerCount": 0,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "execution_policy": {
    "readers": [],
    "writers": [],
    "readerCount": 0,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "github_issue": {
    "readers": [],
    "writers": [],
    "readerCount": 0,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "harness_event": {
    "readers": [],
    "writers": [
      "execution/daemons/apis/swarm_dispatch.py:6057"
    ],
    "readerCount": 0,
    "writerCount": 1,
    "testOnlyReaders": false
  },
  "issue": {
    "readers": [
      "execution/daemons/apis/swarm_dispatch.py:5687",
      "execution/daemons/apis/swarm_dispatch.py:5833",
      "execution/mcp/ateles/server.py:694",
      "execution/mcp/ateles/server.py:700"
    ],
    "writers": [
      "execution/daemons/apis/swarm_dispatch.py:5833"
    ],
    "readerCount": 4,
    "writerCount": 1,
    "testOnlyReaders": false
  },
  "locale_profile": {
    "readers": [],
    "writers": [],
    "readerCount": 0,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "operator_profile": {
    "readers": [],
    "writers": [],
    "readerCount": 0,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "payment_profile": {
    "readers": [
      "execution/daemons/monedula/handlers/payment_profile.py:141"
    ],
    "writers": [],
    "readerCount": 1,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "plan": {
    "readers": [],
    "writers": [
      ".claude/hooks/_session_integrity.py:146",
      "execution/mcp/mcp_tool_grant_proxy/session_integrity.py:230",
      "execution/scripts/render_plan_docs.py:137"
    ],
    "readerCount": 0,
    "writerCount": 3,
    "testOnlyReaders": false
  },
  "priority_rubric": {
    "readers": [],
    "writers": [],
    "readerCount": 0,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "project": {
    "readers": [
      "execution/daemons/anthus/orchestrator.py:400"
    ],
    "writers": [],
    "readerCount": 1,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "rendered_page": {
    "readers": [],
    "writers": [],
    "readerCount": 0,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "rendered_page_template": {
    "readers": [],
    "writers": [],
    "readerCount": 0,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "session_digest": {
    "readers": [
      "apps/task-dashboard/server/neotomaProxy.ts:672"
    ],
    "writers": [],
    "readerCount": 1,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "source_owner": {
    "readers": [],
    "writers": [],
    "readerCount": 0,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "swarm_roster": {
    "readers": [
      "execution/mcp/ateles/server.py:260"
    ],
    "writers": [],
    "readerCount": 1,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "task": {
    "readers": [
      "apps/task-dashboard/server/neotomaProxy.ts:145",
      "apps/task-dashboard/server/neotomaProxy.ts:661",
      "execution/daemons/apis/task_watchdog.py:240",
      "execution/scripts/verify_aauth_signer.py:52"
    ],
    "writers": [
      "execution/mcp/ateles/server.py:538"
    ],
    "readerCount": 4,
    "writerCount": 1,
    "testOnlyReaders": false
  },
  "task_policy": {
    "readers": [],
    "writers": [],
    "readerCount": 0,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "vendor_binding": {
    "readers": [],
    "writers": [],
    "readerCount": 0,
    "writerCount": 0,
    "testOnlyReaders": false
  },
  "workflow_definition": {
    "readers": [
      "apps/task-dashboard/server/neotomaProxy.ts:634",
      "execution/daemons/anthus/orchestrator.py:321",
      "execution/daemons/apis/swarm_dispatch.py:4217"
    ],
    "writers": [
      "execution/daemons/apis/swarm_dispatch.py:4217"
    ],
    "readerCount": 3,
    "writerCount": 1,
    "testOnlyReaders": false
  }
};
