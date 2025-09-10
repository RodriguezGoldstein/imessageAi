REPO_DIR := $(CURDIR)
PYTHON := $(REPO_DIR)/.venv/bin/python
PIP := $(PYTHON) -m pip
PLIST_NAME := com.imessage.ai
PLIST_TMPL := packaging/$(PLIST_NAME).plist.tmpl
PLIST_DEST := $(HOME)/Library/LaunchAgents/$(PLIST_NAME).plist
LOG_DIR := $(HOME)/Library/Logs
LOG_FILE := $(LOG_DIR)/imessage-ai.log

.PHONY: setup run run-ui install-service uninstall-service logs doctor macos-setup

setup:
	python3 -m venv .venv
	$(PIP) install --upgrade pip setuptools wheel
	$(PIP) install -r requirements.txt

run:
	$(PYTHON) app.py

run-ui:
	BOT_DISABLE_MONITOR=1 $(PYTHON) app.py

install-service: $(PLIST_TMPL)
	@mkdir -p "$(HOME)/Library/LaunchAgents" "$(LOG_DIR)"
	@echo "Generating launchd plist at $(PLIST_DEST)"
	@sed \
	 -e 's#__REPO_DIR__#$(REPO_DIR)#g' \
	 -e 's#__PYTHON__#$(PYTHON)#g' \
	 -e 's#__OPENAI_API_KEY__#$(OPENAI_API_KEY)#g' \
	 -e 's#__BOT_DISABLE_MONITOR__#0#g' \
	 -e 's#__IMSG_AI_TOKEN__#$(IMSG_AI_TOKEN)#g' \
	 "$(PLIST_TMPL)" > "$(PLIST_DEST)"
	@echo "(Re)loading launch agent $(PLIST_DEST)"
	- launchctl unload "$(PLIST_DEST)" 2>/dev/null || true
	launchctl load -w "$(PLIST_DEST)"
	@echo "Loaded. Tail logs with: make logs"

uninstall-service:
	- launchctl unload "$(PLIST_DEST)" 2>/dev/null || true
	- rm -f "$(PLIST_DEST)"
	@echo "Uninstalled launch agent $(PLIST_DEST)"

logs:
	@echo "Tailing logs at $(LOG_FILE) (Ctrl-C to stop)"
	@touch "$(LOG_FILE)"
	tail -f "$(LOG_FILE)"

doctor:
	@echo "Checking Python..."
	@python3 -V || true
	@echo "Checking virtualenv..."
	@[ -x "$(PYTHON)" ] && echo "venv OK" || echo "venv missing; run: make setup"
	@echo "Checking Python packages..."
	@[ -x "$(PYTHON)" ] && $(PYTHON) - <<-'PY' || echo "(skipped)"
	import pkg_resources
	reqs = [
	    'flask==3.0.3',
	    'flask_socketio==5.3.6',
	    'openai==1.37.0',
	    'httpx==0.27.2',
	    'pytypedstream==0.1.0',
	    'schedule==1.2.1',
	]
	ok = True
	for r in reqs:
	    try:
	        pkg_resources.require([r])
	        print('[OK] ' + r)
	    except Exception as e:
	        ok = False
	        print('[MISSING] ' + r, e)
	print('Dependencies OK' if ok else 'Some dependencies missing; run: make setup')
	PY
	@echo "Checking Messages DB access..."
	@([ -x "$(PYTHON)" ] && $(PYTHON) - <<-'PY'
	from services.agent import check_db_reachability
	ok, err = check_db_reachability()
	print("DB OK" if ok else f"DB ERROR: {err}")
	PY
	) || echo "Could not run DB check; ensure venv is set up."
	@echo "If DB ERROR, grant Full Disk Access to your terminal/python and retry."
	@echo "Checking AppleScript / Automation permission..."
	@command -v osascript >/dev/null 2>&1 || { echo "osascript not found (macOS required)"; exit 0; }
	@osascript -e 'tell application "Messages" to get name of first service' >/dev/null 2>&1 \
	 && echo "Automation OK (Messages accessible)" \
	 || echo "Automation may be blocked. Open System Settings → Privacy & Security → Automation and allow your terminal/editor to control Messages."
	@echo "Env: OPENAI_API_KEY=$${OPENAI_API_KEY:+set} IMSG_AI_TOKEN=$${IMSG_AI_TOKEN:+set} IMSG_AI_SECRET=$${IMSG_AI_SECRET:+set}"

macos-setup:
	@bash scripts/macos-setup.sh
