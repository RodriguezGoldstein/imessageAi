#!/usr/bin/env bash
set -euo pipefail

echo "== iMessage AI — Run in Background (nohup) =="

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This app only runs on macOS. Exiting." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

# Prefer the helper created by scripts/install.sh so env vars are exported.
RUN_HELPER="scripts/run_local.sh"
if [[ ! -x "$RUN_HELPER" ]]; then
  echo "Warning: $RUN_HELPER not found or not executable." >&2
  echo "Falling back to running .venv/bin/python app.py (make sure env vars are set)." >&2
  RUN_HELPER=".venv/bin/python app.py"
fi

LOG_DIR="${HOME}/Library/Logs"
LOG_FILE="${LOG_DIR}/imessage-ai.log"
mkdir -p "$LOG_DIR"

echo "Starting in background with nohup. Logs: $LOG_FILE"
nohup bash -lc "$RUN_HELPER" >> "$LOG_FILE" 2>&1 &
BG_PID=$!
disown || true

echo "Background PID: $BG_PID"
echo "Tail logs: tail -f '$LOG_FILE'"
echo "Stop: scripts/stop_background.sh"

