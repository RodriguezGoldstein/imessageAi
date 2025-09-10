#!/usr/bin/env bash
set -euo pipefail

echo "== iMessage AI — Interactive Installer =="

# 1) Platform checks
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This app only runs on macOS. Exiting." >&2
  exit 1
fi

command -v python3 >/dev/null 2>&1 || { echo "python3 not found. Install Xcode CLT or Python 3." >&2; exit 1; }

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

echo "Repo: $REPO_DIR"

# 2) Create venv and install requirements
if [[ ! -x .venv/bin/python ]]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
fi
echo "Installing dependencies (pinned)..."
.venv/bin/pip install -r requirements.txt

# 3) Collect configuration
read -r -p "Enter your OPENAI_API_KEY (required): " OPENAI_API_KEY
if [[ -z "${OPENAI_API_KEY}" ]]; then
  echo "OPENAI_API_KEY is required. Exiting." >&2
  exit 1
fi

read -r -p "Enter a local auth token (IMSG_AI_TOKEN) [leave blank to auto-generate]: " IMSG_AI_TOKEN || true
if [[ -z "${IMSG_AI_TOKEN:-}" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    IMSG_AI_TOKEN="$(openssl rand -hex 16)"
  else
    IMSG_AI_TOKEN="$(.venv/bin/python - <<'PY'
import os, binascii
print(binascii.hexlify(os.urandom(16)).decode())
PY
)"
  fi
  echo "Generated token: ${IMSG_AI_TOKEN}"
fi

read -r -p "Enter session secret (IMSG_AI_SECRET) [leave blank to auto-generate]: " IMSG_AI_SECRET || true
if [[ -z "${IMSG_AI_SECRET:-}" ]]; then
  IMSG_AI_SECRET="$(.venv/bin/python - <<'PY'
import os,base64
print(base64.urlsafe_b64encode(os.urandom(32)).decode())
PY
)"
  echo "Generated secret."
fi

echo "\nConfiguration summary:"
echo "- OPENAI_API_KEY: set"
echo "- IMSG_AI_TOKEN: ${IMSG_AI_TOKEN}"
echo "- IMSG_AI_SECRET: set"

# 4) Offer macOS permission helper
read -r -p "Open macOS permission panes now (Full Disk Access + Automation)? [Y/n] " RESP || true
RESP=${RESP:-Y}
if [[ "$RESP" =~ ^[Yy]$ ]]; then
  bash scripts/macos-setup.sh || true
fi

# 5) Doctor checks
if command -v make >/dev/null 2>&1; then
  echo "Running 'make doctor'..."
  OPENAI_API_KEY="$OPENAI_API_KEY" IMSG_AI_TOKEN="$IMSG_AI_TOKEN" IMSG_AI_SECRET="$IMSG_AI_SECRET" make doctor || true
else
  echo "'make' not found; skipping doctor checks. Install Xcode CLT for 'make'."
fi

# 6) Create a local run helper
RUN_HELPER="scripts/run_local.sh"
cat > "$RUN_HELPER" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export OPENAI_API_KEY='${OPENAI_API_KEY}'
export IMSG_AI_TOKEN='${IMSG_AI_TOKEN}'
export IMSG_AI_SECRET='${IMSG_AI_SECRET}'
exec .venv/bin/python app.py
EOF
chmod +x "$RUN_HELPER"
echo "Created $RUN_HELPER — use this to run locally with your config."

# 7) Ask to run or install service / background
echo "\nHow would you like to proceed?"
echo "  [1] Run now (foreground)"
echo "  [2] Install as a login service (launchd)"
echo "  [3] Run in background (nohup)"
echo "  [4] Exit"
read -r -p "Select 1/2/3/4: " CH || true
CH=${CH:-4}

case "$CH" in
  1)
    echo "Starting app. Visit http://127.0.0.1:5000/ (login at /login)."
    exec "$RUN_HELPER"
    ;;
  2)
    if ! command -v make >/dev/null 2>&1; then
      echo "'make' is required to install the service. Install Xcode CLT and rerun."
      exit 1
    fi
    echo "Installing launchd service..."
    OPENAI_API_KEY="$OPENAI_API_KEY" IMSG_AI_TOKEN="$IMSG_AI_TOKEN" IMSG_AI_SECRET="$IMSG_AI_SECRET" make install-service
    echo "Service installed. Tail logs with: make logs"
    ;;
  3)
    LOG_DIR="${HOME}/Library/Logs"
    LOG_FILE="${LOG_DIR}/imessage-ai.log"
    mkdir -p "$LOG_DIR"
    echo "Starting in background with nohup. Logs: $LOG_FILE"
    nohup "$RUN_HELPER" >> "$LOG_FILE" 2>&1 &
    BG_PID=$!
    disown || true
    echo "Background PID: $BG_PID"
    echo "Tail logs: tail -f '$LOG_FILE'"
    ;;
  *)
    echo "Setup complete. To run later: $RUN_HELPER"
    ;;
esac
