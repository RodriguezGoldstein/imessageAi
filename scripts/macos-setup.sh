#!/usr/bin/env bash
set -euo pipefail

echo "Opening System Settings for Full Disk Access..."
open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" || true

echo "Opening System Settings for Automation permissions..."
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation" || true

echo "Triggering a harmless Automation prompt (System Events dialog)..."
osascript -e 'tell application "System Events" to display dialog "imessage-ai setup check" giving up after 1' || true

echo "Done. In System Settings → Privacy & Security:"
echo " - Add your terminal/editor/python under Full Disk Access."
echo " - Under Automation, allow your tool to control Messages."

