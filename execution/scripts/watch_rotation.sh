#!/usr/bin/env bash
# Watch for credential rotations landing, and exit only on POSITIVE evidence.
#
# The failure this guards against: a watcher whose exit condition is satisfied by
# empty strings. If `op read` or `curl` fails transiently, every variable goes
# blank at once, and a naive test like `case "$a$n" in *PLACEHOLDER*)` passes on
# nothing — reporting success when nothing rotated. That happened on 2026-08-05.
#
# Rule enforced here: every probe must yield a value in an EXPECTED SET before it
# counts. Unknown/blank is treated as "no information", never as progress.

set -uo pipefail

OP="${OP_BIN:-/opt/homebrew/bin/op}"
CURL="${CURL_BIN:-/usr/bin/curl}"
INTERVAL="${INTERVAL:-30}"
MAX_ITERS="${MAX_ITERS:-120}"

# Resolved from the manifest so the item ids are never duplicated here.
ref_for() {
  python3 - "$1" <<'PY'
import json, pathlib, sys, os
base = os.environ.get("ATELES_SECRETS_DIR", str(pathlib.Path.home()/"repos"/"ateles-private"))
m = json.loads((pathlib.Path(base)/"secrets"/"manifest.env-map.json").read_text())
want = sys.argv[1]
for blk in m.get("files", {}).values():
    for section in ("default", "production", "development"):
        ref = (blk.get(section) or {}).get(want)
        if ref:
            print(ref); raise SystemExit
raise SystemExit(f"{want} not in manifest")
PY
}

ATELES_REF=$(ref_for ATELES_AGENT_PAT) || { echo "ATELES_AGENT_PAT not in manifest"; exit 2; }
NEOTOMA_REF=$(ref_for NEOTOMA_AGENT_PAT) || { echo "NEOTOMA_AGENT_PAT not in manifest"; exit 2; }

# Classify a 1Password read: rotated | placeholder | unknown (never silently ok).
classify_op() {
  local out
  if ! out=$("$OP" read "$1" 2>/dev/null) || [ -z "$out" ]; then
    echo "unknown"; return
  fi
  case "$out" in
    PLACEHOLDER_*) echo "placeholder" ;;
    gh[po]_*|github_pat_*) echo "rotated" ;;
    *) echo "unknown" ;;
  esac
}

# Classify token liveness: live | dead | unknown. HTTP 000 means the probe
# failed, which is NOT evidence the token died.
classify_http() {
  local tok="$1" code
  [ -z "$tok" ] && { echo "unknown"; return; }
  code=$("$CURL" -s --max-time 15 -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $tok" https://api.github.com/user 2>/dev/null)
  case "$code" in
    200) echo "live" ;;
    401|403) echo "dead" ;;
    *) echo "unknown" ;;
  esac
}

# Prefixes of the tokens being retired, passed in rather than hardcoded so this
# is reusable for the next rotation. Each is matched against the transcript
# corpus to recover the full value, whose liveness we then poll.
OLD_ATELES_PREFIX="${OLD_ATELES_PREFIX:-}"
OLD_NEOTOMA_PREFIX="${OLD_NEOTOMA_PREFIX:-}"

if [ -z "$OLD_ATELES_PREFIX" ] || [ -z "$OLD_NEOTOMA_PREFIX" ]; then
  echo "Set OLD_ATELES_PREFIX and OLD_NEOTOMA_PREFIX to the first ~10 chars of the"
  echo "tokens being retired, e.g.:"
  echo "  OLD_ATELES_PREFIX=ghp_6Cfwyh OLD_NEOTOMA_PREFIX=ghp_Ixyw1T $0"
  exit 2
fi

old_ateles=$(grep -ohE "${OLD_ATELES_PREFIX}[A-Za-z0-9]{20,}" ~/.claude/projects/*/*.jsonl 2>/dev/null | sort -u | head -1)
old_neotoma=$(grep -ohE "${OLD_NEOTOMA_PREFIX}[A-Za-z0-9]{20,}" ~/.claude/projects/*/*.jsonl 2>/dev/null | sort -u | head -1)

prev=""
for _ in $(seq 1 "$MAX_ITERS"); do
  a=$(classify_op "$ATELES_REF")
  n=$(classify_op "$NEOTOMA_REF")
  la=$(classify_http "$old_ateles")
  ln=$(classify_http "$old_neotoma")

  cur="ateles_item=$a neotoma_item=$n old_ateles=$la old_neotoma=$ln"
  [ "$cur" != "$prev" ] && { echo "[$(date +%H:%M:%S)] $cur"; prev="$cur"; }

  # Exit ONLY on affirmative evidence from every probe. Any "unknown" keeps
  # waiting — that is the whole point.
  if [ "$a" = "rotated" ] && [ "$n" = "rotated" ] &&
     [ "$la" = "dead" ] && [ "$ln" = "dead" ]; then
    echo "VERIFIED: both PATs rotated in 1Password AND both old tokens rejected by GitHub"
    exit 0
  fi
  sleep "$INTERVAL"
done

echo "TIMED OUT after $((MAX_ITERS * INTERVAL))s — last state: ${prev:-none}"
echo "(timeout is not evidence of anything; re-run or check manually)"
exit 1
