"""Generate codeUsage.ts — the static repo-derived reader/writer scan."""
import os, re, json, subprocess, datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
OUT  = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "codeUsage.ts"))
SKIP = {"node_modules", ".git", "dist", "build", ".venv", "__pycache__", "worktrees"}

CANONICAL = ["project","plan","task","workflow_definition","agent_definition","conversation",
 "conversation_message","checkpoint_brief","escalation","issue","session_digest","rendered_page",
 "rendered_page_template","harness_event","daemon_report","execution_policy"]
CONFIG = ["execution_policy","agent_policy","task_policy","locale_profile","vendor_binding",
 "channel_config","deployment_configuration","swarm_roster","operator_profile","priority_rubric",
 "brand_voice","payment_profile"]
EXTRA = ["github_issue","agent_message","email_message","source_owner"]
TYPES = sorted(set(CANONICAL + CONFIG + EXTRA))

READ_FN  = re.compile(r'(retrieve_entities|entities/query|list_entity|retrieve_entity_by_identifier|get_entities|query_entities|retrieve_related|/entities/)')
WRITE_FN = re.compile(r'\b(store|store_structured|submit_entity|create_entity|correct)\b')

files = []
for dp, dns, fns in os.walk(ROOT):
    dns[:] = [d for d in dns if d not in SKIP]
    for fn in fns:
        if not fn.endswith((".py", ".ts", ".tsx", ".js", ".mjs")):
            continue
        full = os.path.join(dp, fn)
        # Never scan this script or the file it generates: the type names are
        # literals in both, so including them would have every type "read" by
        # its own inventory.
        if os.path.abspath(full) in (os.path.abspath(__file__), os.path.abspath(OUT)):
            continue
        files.append(full)

res = {t: {"readers": set(), "writers": set(), "testOnly": True} for t in TYPES}
for f in files:
    try: src = open(f, encoding="utf-8", errors="ignore").read()
    except Exception: continue
    rel = os.path.relpath(f, ROOT)
    is_test = "/test" in rel or rel.split("/")[-1].startswith("test_") or ".test." in rel
    lines = src.split("\n")
    for i, line in enumerate(lines):
        for t in TYPES:
            if not re.search(r'["\']%s["\']' % re.escape(t), line): continue
            ctx = "\n".join(lines[max(0, i - 6): min(len(lines), i + 7)])
            hit = f"{rel}:{i+1}"
            if READ_FN.search(ctx):
                res[t]["readers"].add(hit)
                if not is_test: res[t]["testOnly"] = False
            if WRITE_FN.search(ctx):
                res[t]["writers"].add(hit)
                if not is_test: res[t]["testOnly"] = False

def prod(hits):
    return sorted(h for h in hits if "/test" not in h and not h.split("/")[-1].startswith("test_") and ".test." not in h)

payload = {}
for t in TYPES:
    r, w = prod(res[t]["readers"]), prod(res[t]["writers"])
    payload[t] = {
        "readers": r[:6], "writers": w[:6],
        "readerCount": len(r), "writerCount": len(w),
        "testOnlyReaders": len(res[t]["readers"]) > 0 and len(r) == 0,
    }

rev = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"],
                     capture_output=True, text=True).stdout.strip()
meta = {"generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "commit": rev, "filesScanned": len(files), "repoRoot": "ateles"}

header = '''/**
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
'''
with open(OUT, "w") as fh:
    fh.write(header)
    fh.write("\nexport interface CodeUsage {\n  readers: string[];\n  writers: string[];\n  readerCount: number;\n  writerCount: number;\n  /** Readers exist, but only inside test files — not read in production. */\n  testOnlyReaders: boolean;\n}\n\n")
    fh.write("export const SCAN_META = " + json.dumps(meta, indent=2) + " as const;\n\n")
    fh.write("export const CODE_USAGE: Record<string, CodeUsage> = " + json.dumps(payload, indent=2) + ";\n")
print("wrote", OUT, "types:", len(payload), "files:", len(files), "commit:", rev)
for t in ["harness_event","agent_message","github_issue","checkpoint_brief","task","issue"]:
    print(" ", t, payload[t]["readerCount"], "readers /", payload[t]["writerCount"], "writers")
