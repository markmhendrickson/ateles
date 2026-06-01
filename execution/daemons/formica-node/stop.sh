#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="${ROOT}/.run/daemon.pid"
if [[ ! -f "$PID_FILE" ]]; then
  echo "No pid file at ${PID_FILE} (daemon not running or started elsewhere)." >&2
  exit 1
fi
PID="$(tr -d ' \n' < "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID" || true
  echo "Sent SIGTERM to formica pid ${PID}."
else
  echo "Stale pid file (process ${PID} not running); removing." >&2
fi
rm -f "$PID_FILE"
