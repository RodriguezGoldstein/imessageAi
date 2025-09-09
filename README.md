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
- Settings page: manage trigger tag and allowlist from your browser.
- Bulk send and scheduled messages to contacts from `contacts.csv`.

## Architecture

- Backend: Flask + Flask-SocketIO (`eventlet`) for real-time updates.
- Agent core: `services/agent.py` polls `~/Library/Messages/chat.db` for new messages and sends replies with AppleScript.
- UI: `templates/dashboard.html` for live feed; `templates/settings.html` for trigger + allowlist management.
- Data:
  - `contacts.csv` (name, phone) – your address book for bulk/scheduled sends and allowlist convenience.
  - `settings.json` – stores `ai_trigger_tag` and `allowed_users`.

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
pip install -r requirements.txt
```

## Configure

1) OpenAI API key (required to answer @ai prompts)

```bash
export OPENAI_KEY=sk-...
# or
export OPENAI_API_KEY=sk-...
```

2) Contacts file (optional but recommended)

Edit `contacts.csv`:

```
name,phone
Alice,+11234567890
Bob,+10987654321
```

3) Settings (auto-created if missing)

`settings.json`

```json
{
  "ai_trigger_tag": "@ai",
  "allowed_users": ["+11234567890"]
}
```

You can manage these from the Settings page or via the API.

## Run

UI-only (no DB polling; useful for quick checks):

```bash
BOT_DISABLE_MONITOR=1 python app.py
```

Full agent (polling + replies):

```bash
python app.py
# Visit http://127.0.0.1:5000/
```

From an allowed number, send in any thread:

```
@ai translate “hello” to Spanish
```

The agent replies in the same direct chat or group.

## HTTP API

- GET `/allowlist` → `{ ai_trigger_tag, allowed_users, contacts }`
- POST `/allowlist` (JSON) → update `ai_trigger_tag` and `allowed_users`

Example:

```bash
curl -X POST http://127.0.0.1:5000/allowlist \
  -H 'Content-Type: application/json' \
  -d '{"ai_trigger_tag":"@ai","allowed_users":["+15555555555"]}'
```

## Dashboard

- Real-time list of received messages and bot replies (with [Group]/[Direct] indicator; group shows `chat_guid`).
- Bulk send to checked contacts.
- Schedule a message for a time of day.
- Link to Settings page for trigger and allowlist.

## Notes and Limitations

- macOS-only (uses the Messages database and AppleScript).
- Polls the database every 5 seconds; heavy traffic could benefit from tuning.
- Attachments are ignored; only text is processed.
- The Messages DB format can change with macOS updates; SQL may need adjustments.
- Ensure Full Disk Access; otherwise reads from `chat.db` will fail.
- Ensure Automation permission; otherwise AppleScript won’t send messages.

## Privacy & Security

- Only messages containing your trigger from allowed numbers are sent to OpenAI.
- The agent does not auto-respond outside explicit `@ai` commands.
- Logs are local and ephemeral (in-memory for the session’s dashboard feed).

## Project Layout

```
app.py                 # Flask app + Socket.IO setup
services/agent.py      # Agent: DB polling, AppleScript sending, @ai processing
templates/
  dashboard.html       # Live feed and controls
  settings.html        # Trigger + allowlist management
contacts.csv           # Your contacts for UI and allowlist convenience
settings.json          # Persisted agent settings
```

## Development Tips

- To iterate on UI without touching macOS permissions, use `BOT_DISABLE_MONITOR=1`.
- If the agent isn’t replying, check:
  - The sender is on `allowed_users`.
  - `OPENAI_KEY`/`OPENAI_API_KEY` is set.
  - App has Full Disk Access and Automation permissions.
  - Console logs for AppleScript errors.

---

MIT license (see LICENSE.txt). Contributions welcome!
