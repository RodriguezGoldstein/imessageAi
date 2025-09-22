# iMessage @ai Agent + Dashboard

An on-device iMessage agent for macOS that:

- Monitors all incoming iMessages via the Messages database.
- Responds only when an allowed user includes an `@ai` trigger in their message.
- Replies in the same context (direct or group) using AppleScript.
- Provides a real-time web dashboard with message stream and a settings page.

This project is focused on privacy and explicit control: the agent never auto-responds unless you or an allowed user explicitly uses the `@ai` tag.

## Features

- @ai trigger: e.g., `@ai summarize the plan in 3 bullets`.
- Allowlist of phone numbers that can use the trigger.
- Group chat support: replies go back to the same group thread by chat GUID.
- Dashboard: real-time feed of messages and AI replies via WebSockets.
- Streaming responses in the web UI while the model thinks.
- Settings page: manage trigger tag, allowlist, and OpenAI model/prompt from your browser.
- Bulk send and scheduled messages to contacts from `contacts.csv`.

## Architecture

- Backend: Flask + Flask-SocketIO (threading mode) for real-time updates.
- Agent core: `services/agent.py` polls `~/Library/Messages/chat.db` for new messages and sends replies with AppleScript.
- UI: `templates/dashboard.html` for live feed; `templates/settings.html` for trigger + allowlist management.
- Data:
- `~/Library/Application Support/imessage-ai/config.json` – stores `ai_trigger_tag`, `openai_model`, `system_prompt`, `context_window`, and the allowlist (encrypted at rest).

## Requirements

- macOS with the built-in Messages app and iMessage enabled.
- Python 3.10+ recommended.
- Permissions:
  - Full Disk Access for the process running the app (to read `~/Library/Messages/chat.db`).
  - Automation permission to allow AppleScript to control Messages (System Settings → Privacy & Security → Automation).

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt  # pinned versions
```

Or via Make:

```bash
make setup
```

Guided installer:

```bash
bash scripts/install.sh
```
Walks you through dependency install, permissions, secrets, and either runs locally or installs a launchd service.

## Configure

1) OpenAI API key (required to answer @ai prompts)

```bash
export OPENAI_KEY=sk-...
# or
export OPENAI_API_KEY=sk-...
```

2) Local auth token (recommended)

Set a local bearer token to protect the dashboard and write endpoints:

```bash
export IMSG_AI_TOKEN=choose-a-strong-token
```

UI login: visit `http://127.0.0.1:5000/login` and enter the token. After login, the browser stays authenticated via a session cookie. For API calls, send `Authorization: Bearer <token>`.

3) Session secret (recommended)

Set a secret for signing the session cookie:

```bash
export IMSG_AI_SECRET=$(python - <<'PY'
import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())
PY
)
```

4) Settings (auto-created if missing)

Runtime settings now persist under `~/Library/Application Support/imessage-ai/config.json`.
The legacy `settings.json` file is migrated automatically on first launch. You can
manage everything from the Settings page or via the API.

## Run

UI-only (no DB polling; useful for quick checks):

```bash
BOT_DISABLE_MONITOR=1 python app.py
```

Full agent (polling + replies):

```bash
python app.py
# Visit http://127.0.0.1:5000/ (you will be redirected to /login if a token is set)
```

Using Make:

```bash
make run         # full agent
make run-ui      # UI only (no DB polling)
```

Using the generated helper after installer:

```bash
scripts/run_local.sh
```

Stop a background (nohup) run:

```bash
bash scripts/stop_background.sh
```

From an allowed number, send in any thread:

```
@ai translate “hello” to Spanish
```

The agent replies in the same direct chat or group.

## HTTP API

- GET `/allowlist` → `{ ai_trigger_tag, allowed_users, openai_model, system_prompt }`
- POST `/allowlist` (JSON) → update `ai_trigger_tag` and `allowed_users`
- POST `/update_ai_settings` (form) → update `ai_trigger_tag`, `openai_model`, `system_prompt`, `allowed_users`
- GET `/healthz` → `{ ok, db_ok, db_error, last_seen_date }`
- GET `/api/messages` → `{ messages: [...] }`
- GET `/api/schedule` → `{ scheduled: [...] }`
- POST `/api/schedule` → schedule one or more numbers `{ time, message, phones: [] }`
- DELETE `/api/schedule/<id>` → cancel a scheduled message
- GET `/api/settings` → full AI settings payload
- PATCH `/api/settings` → update AI settings via JSON

Programmatic send:

- POST `/api/send` (JSON)
  - Body: `{ "phone": "+15555555555", "message": "Hello" }` OR `{ "chat_guid": "iMessage;+15555555555;12345", "message": "Hello group" }`
  - Auth: `Authorization: Bearer $IMSG_AI_TOKEN`

Example (authorized):

```bash
curl -X POST http://127.0.0.1:5000/allowlist \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $IMSG_AI_TOKEN" \
  -d '{"ai_trigger_tag":"@ai","allowed_users":["+15555555555"]}'
```

Send a message to a phone:

```bash
curl -X POST http://127.0.0.1:5000/api/send \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $IMSG_AI_TOKEN" \
  -d '{"phone":"+15555555555","message":"Hello from API"}'
```

Send a message to a specific chat (group) by GUID:

```bash
curl -X POST http://127.0.0.1:5000/api/send \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $IMSG_AI_TOKEN" \
  -d '{"chat_guid":"iMessage;+15555555555;12345","message":"Hello group"}'
```

## Desktop UI (Svelte + NodeGUI)

A native desktop shell lives under `desktop/` and mirrors the browser dashboard
using Svelte rendered through NodeGUI. It spawns the Python backend locally and
consumes the REST/Socket.IO APIs exposed by `app.py`.

```bash
cd desktop
npm install
npm run dev  # launches the desktop app and spawns python app.py
```

Before launching, ensure you have configured the backend, exported the required
environment variables (`OPENAI_API_KEY`, `IMSG_AI_TOKEN`, `IMSG_AI_SECRET`), and
run `make setup` so `.venv` exists. Build artifacts land in `desktop/dist/`; package
them with `@nodegui/qode` or your preferred bundler to produce a `.app` bundle.

## Dashboard

- Real-time list of received messages and bot replies (with [Group]/[Direct] indicator; group shows `chat_guid`).
- Streaming AI output in the UI as partial tokens, finalized into a single reply.
- Bulk send: paste phone numbers (comma/newline separated) and a message.
- Schedule a message for a time of day to pasted numbers.
- Link to Settings page for trigger and allowlist.

### Streaming behavior

- The web UI shows partial output live over a Socket.IO event `ai_stream`.
- The final response is sent once to iMessage (not token-by-token) to avoid spamming chats.

## Model selection

- Default in this repo: `gpt-4o-mini` for low-latency, low-cost replies.
- You can set the model and system prompt in Settings or by editing `~/Library/Application Support/imessage-ai/config.json`.
- If you prefer higher quality, set `openai_model` to `gpt-4o`.

## Context window

- The agent includes recent conversation context when answering `@ai` requests.
- Configure the number of recent messages with `context_window` (default 25, range 1–100) in Settings or `~/Library/Application Support/imessage-ai/config.json`.

## Service Setup (launchd)

The app can run at login under launchd.

1) Ensure the virtualenv is installed:

```bash
make setup
```

2) Install the launch agent (uses your current repo path and venv):

```bash
OPENAI_API_KEY=sk-... IMSG_AI_TOKEN=choose-a-strong-token IMSG_AI_SECRET=... make install-service
```

3) Logs:

```bash
make logs
```

4) Uninstall:

```bash
make uninstall-service
bash scripts/uninstall.sh     # guided full uninstall (agent, state, logs, venv)
```

If DB errors appear, grant Full Disk Access to your terminal/python and allow Automation for controlling Messages.

## Notes and Limitations

- macOS-only (uses the Messages database and AppleScript).
- Polls the database every 5 seconds; heavy traffic could benefit from tuning.
- Attachments are ignored; only text is processed.
- The Messages DB format can change with macOS updates; SQL may need adjustments.
- Ensure Full Disk Access; otherwise reads from `chat.db` will fail.
- Ensure Automation permission; otherwise AppleScript won’t send messages.
- Persists `last_seen_date` under `~/Library/Application Support/imessage-ai/state.json` to avoid replaying history; set `BOT_REPLAY_HISTORY=1` to process existing messages.
- Phone normalization: The app normalizes phones by stripping punctuation and a leading `tel:` prefix, preserving a leading `+` if present (e.g., `tel:+1 (555) 123-4567` → `+15551234567`). Email handles are lower‑cased.
- Allowlist privacy: Phone numbers/emails in the allowlist are encrypted at rest in `config.json`. A key is stored under `~/Library/Application Support/imessage-ai/secret.key`.

## macOS Permissions Helper

Run:

```bash
make macos-setup
```

It opens the relevant System Settings panes and triggers a harmless prompt to help you grant permissions.

## Privacy & Security

- Only messages containing your trigger from allowed numbers are sent to OpenAI.
- The agent does not auto-respond outside explicit `@ai` commands.
- Logs are local and ephemeral (in-memory for the session’s dashboard feed).
- Local auth protects all pages and write endpoints; login at `/login` uses a session cookie signed with `IMSG_AI_SECRET`.
- WebSocket connections are token/session protected and Socket.IO CORS is restricted to `localhost`.

## Make Targets

```bash
make setup             # create venv and install pinned deps
make run               # run full agent
make run-ui            # run UI only (no DB polling)
make install-service   # install launchd agent (use env OPENAI_API_KEY, IMSG_AI_TOKEN, IMSG_AI_SECRET)
make uninstall-service # remove launchd agent
make logs              # tail service logs
make doctor            # check env, dependencies, DB access, Automation
make macos-setup       # open permissions panes and prompt
```

## Project Layout

```
app.py                 # Flask app + Socket.IO setup
services/agent.py      # Agent: DB polling, AppleScript sending, @ai processing
services/config.py     # Centralized runtime configuration/state helpers
templates/
  dashboard.html       # Live feed and controls
  settings.html        # Trigger + allowlist management
~/Library/Application Support/imessage-ai/config.json  # Persisted agent settings
desktop/               # Optional Svelte + NodeGUI desktop frontend
```

## Development Tips

- To iterate on UI without touching macOS permissions, use `BOT_DISABLE_MONITOR=1`.
- If the agent isn’t replying, check:
  - The sender is on `allowed_users`.
  - `OPENAI_KEY`/`OPENAI_API_KEY` is set.
  - App has Full Disk Access and Automation permissions.
  - Console logs for AppleScript errors.
  - `make doctor` for quick environment and permissions checks.

---

MIT license (see LICENSE.txt). Contributions welcome!
