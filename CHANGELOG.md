# Changelog

All notable changes to this project will be documented in this file.

## v0.3.0 — 2025-09-10

Highlights:
- Switched to OpenAI Responses API with streaming in the web UI.
- Configurable OpenAI model and system prompt via Settings and settings.json.
- Improved installer and Makefile reliability (pip via python -m, heredoc fix).

Added:
- Streaming responses over Socket.IO (`ai_stream` events) visible on the dashboard.
- Settings UI fields for `openai_model` and `system_prompt`.
- API: GET `/allowlist` now returns `openai_model` and `system_prompt`.

Changed:
- `query_openai_direct` now uses `client.responses.create` (was `chat.completions`).
- Monitor replies still sent as a single iMessage after streaming completes.
- README updated with streaming docs and model selection guidance.

Fixed:
- Pin `httpx==0.27.2` to resolve OpenAI client `proxies` arg error.
- Pin `pytypedstream==0.1.0` (previous pin to 1.0.2 didn’t exist on PyPI).
- Use `python -m pip` to avoid stale venv shebang issues.
- Makefile heredoc indentation to fix “missing separator”.

Upgrade Notes:
- After pulling, run `make setup` to ensure pins are applied.
- You can change the model/prompt in Settings → OpenAI Settings without restart.

## v0.2.0 — 2025-09-10

Highlights:
- Secure auth with bearer token and session login at `/login`.
- New endpoints: `/api/send` (phone or `chat_guid`) and `/healthz`.
- Allowlist encrypted at rest; normalized phone/email handling.
- Persist `last_seen_date` under Application Support; opt‑in history replay.
- Pinned dependencies; removed `eventlet`; Socket.IO uses threading mode.
- Makefile tooling, installer/uninstaller scripts, macOS permission helper, and launchd plist.
- UI improvements: bulk paste numbers, logout button, login page; better error surfacing.
- AppleScript escaping and clearer DB permission errors.
- Remove `contacts.csv` (no longer required).

Added:
- Bearer token auth (`IMSG_AI_TOKEN`) with session cookie login page.
- `/api/send` endpoint for direct or group messages via `chat_guid`.
- `/healthz` endpoint with DB reachability and `last_seen_date`.
- `Makefile` targets: `setup`, `run`, `run-ui`, `install-service`, `uninstall-service`, `logs`, `doctor`, `macos-setup`.
- Scripts: `scripts/install.sh`, `scripts/uninstall.sh`, `scripts/macos-setup.sh`, `scripts/stop_background.sh`.
- Launch agent template: `packaging/com.imessage.ai.plist.tmpl`.

Changed:
- Flask-SocketIO now runs in `threading` mode; CORS restricted to localhost.
- UI pages updated for bulk phone input; added logout controls.
- AppleScript message/handle escaping.
- Error events surfaced via WebSocket to dashboard.

Security/Privacy:
- Allowlist stored encrypted at rest in `settings.json` (`allowed_users_encrypted`).
- Local auth protects pages and write endpoints; session cookie signed with `IMSG_AI_SECRET`.
- Only `@ai` messages from allowed users are sent to OpenAI.

Persistence:
- `last_seen_date` stored at `~/Library/Application Support/imessage-ai/state.json`.
- Set `BOT_REPLAY_HISTORY=1` to process prior history on first run.

Breaking:
- `contacts.csv` removed; UI now accepts pasted numbers for bulk/scheduled sends.

Upgrade Notes:
- Set env vars: `OPENAI_API_KEY`, `IMSG_AI_TOKEN`, `IMSG_AI_SECRET`.
- Run `make setup` then `make doctor` to verify environment and permissions.
- For background run at login: `make install-service` (provide envs inline).
