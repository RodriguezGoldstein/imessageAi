#!/usr/bin/env bash
set -euo pipefail

echo "== iMessage AI — Stop Background Instance =="

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script is intended for macOS only." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT=5000

if ! command -v lsof >/dev/null 2>&1; then
  echo "lsof not found. Install it (e.g., via Xcode CLT) and retry." >&2
  exit 1
fi

echo "Looking for processes listening on TCP port $PORT..."
PIDS=$(lsof -n -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null || true)

if [[ -z "$PIDS" ]]; then
  echo "No listeners on port $PORT found. Nothing to stop."
  exit 0
fi

STOPPED_ANY=0
for PID in $PIDS; do
  # Verify the process belongs to this repo by checking its CWD
  CWD=$(lsof -p "$PID" 2>/dev/null | awk '/cwd/ {for (i=9;i<=NF;i++) printf $i" "; print ""}' | sed 's/ *$//')
  CMD=$(ps -o command= -p "$PID" 2>/dev/null || true)
  if [[ "$CWD" == "$REPO_DIR"* ]] || [[ "$CMD" == *"$REPO_DIR/app.py"* ]] || [[ "$CMD" == *"python app.py"* ]]; then
    echo "Stopping PID $PID (cwd=$CWD)"
    kill "$PID" 2>/dev/null || true
    # Wait up to ~5s
    for i in {1..10}; do
      if ps -p "$PID" >/dev/null 2>&1; then
        sleep 0.5
      else
        break
      fi
    done
    if ps -p "$PID" >/dev/null 2>&1; then
      echo "PID $PID did not exit; sending SIGKILL"
      kill -9 "$PID" 2>/dev/null || true
    fi
    STOPPED_ANY=1
  else
    echo "Skipping PID $PID (not from this repo). CMD='$CMD' CWD='$CWD'"
  fi
done

if [[ "$STOPPED_ANY" -eq 1 ]]; then
  echo "Background instance(s) stopped."
else
  echo "No matching background instance to stop."
fi

