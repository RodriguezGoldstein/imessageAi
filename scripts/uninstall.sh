#!/usr/bin/env bash
set -euo pipefail

echo "== iMessage AI — Uninstall =="

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This uninstaller is intended for macOS only." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_DEST="${HOME}/Library/LaunchAgents/com.imessage.ai.plist"
APP_SUPPORT_DIR="${HOME}/Library/Application Support/imessage-ai"
LOG_FILE="${HOME}/Library/Logs/imessage-ai.log"

echo "Repository: $REPO_DIR"

# 1) Stop and remove launchd service
if [[ -f "$PLIST_DEST" ]]; then
  echo "Unloading launch agent..."
  launchctl unload "$PLIST_DEST" 2>/dev/null || true
  rm -f "$PLIST_DEST"
  echo "Launch agent removed: $PLIST_DEST"
else
  echo "Launch agent not found: $PLIST_DEST (skipping)"
fi

# 2) Remove app support (state + encryption key)
if [[ -d "$APP_SUPPORT_DIR" ]]; then
  read -r -p "Remove app support data (state + key) at '$APP_SUPPORT_DIR'? [y/N] " RESP || true
  if [[ "$RESP" =~ ^[Yy]$ ]]; then
    rm -rf "$APP_SUPPORT_DIR"
    echo "Removed $APP_SUPPORT_DIR"
  else
    echo "Kept $APP_SUPPORT_DIR"
  fi
fi

# 3) Remove logs
if [[ -f "$LOG_FILE" ]]; then
  read -r -p "Remove log file at '$LOG_FILE'? [y/N] " RESP || true
  if [[ "$RESP" =~ ^[Yy]$ ]]; then
    rm -f "$LOG_FILE"
    echo "Removed $LOG_FILE"
  else
    echo "Kept $LOG_FILE"
  fi
fi

# 4) Remove virtualenv
if [[ -d "$REPO_DIR/.venv" ]]; then
  read -r -p "Remove project virtualenv at '$REPO_DIR/.venv'? [y/N] " RESP || true
  if [[ "$RESP" =~ ^[Yy]$ ]]; then
    rm -rf "$REPO_DIR/.venv"
    echo "Removed .venv"
  else
    echo "Kept .venv"
  fi
fi

# 5) Remove helper scripts
if [[ -f "$REPO_DIR/scripts/run_local.sh" ]]; then
  read -r -p "Remove run helper at '$REPO_DIR/scripts/run_local.sh'? [y/N] " RESP || true
  if [[ "$RESP" =~ ^[Yy]$ ]]; then
    rm -f "$REPO_DIR/scripts/run_local.sh"
    echo "Removed scripts/run_local.sh"
  else
    echo "Kept scripts/run_local.sh"
  fi
fi

echo "Uninstall complete."

