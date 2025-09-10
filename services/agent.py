import os
import json
import sqlite3
import subprocess
import time
import schedule
from datetime import datetime
from flask_socketio import SocketIO
from openai import OpenAI
import typedstream
import csv
from pathlib import Path
from cryptography.fernet import Fernet
import httpx


# OpenAI API Key (env only)
OPENAI_API_KEY = os.getenv("OPENAI_KEY") or os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else OpenAI()

# macOS iMessage database path
CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")

# App support/state path (persist last_seen_date)
APP_SUPPORT_DIR = os.path.join(os.path.expanduser("~/Library/Application Support"), "imessage-ai")
STATE_FILE = os.path.join(APP_SUPPORT_DIR, "state.json")

# Flask SocketIO instance
socketio = None

# Track last seen message date (raw Apple epoch)
last_seen_date = 0

# Load AI settings from JSON
SETTINGS_FILE = "settings.json"

# Contacts no longer sourced from CSV; keep an empty map for display/logging.
contacts = {}

def normalize_phone(s: str) -> str:
    """Normalize a phone number to a consistent form. Removes common punctuation and 'tel:' prefix.
    Preserves leading '+' if present. Returns digits (and optional leading '+')."""
    if not s:
        return ""
    s = str(s).strip()
    if s.lower().startswith("tel:"):
        s = s[4:]
    # Keep leading '+' if present, strip all non-digits otherwise
    lead_plus = s.startswith('+')
    digits = ''.join(ch for ch in s if ch.isdigit())
    if not digits:
        return ""
    return ('+' if lead_plus else '') + digits


def normalize_handle(addr: str) -> str:
    """Normalize an iMessage handle id. If it looks like email, lower-case it; else treat as phone."""
    if not addr:
        return ""
    a = str(addr).strip()
    if '@' in a:
        return a.lower()
    return normalize_phone(a)


def _load_contacts_safe():
    # Deprecated: contacts.csv removed; return empty
    return {}

# Track replies and analytics
message_log = []

# Scheduled messages (phone, time, message)
scheduled_messages = []


# Default settings (minimal)
if os.path.exists(SETTINGS_FILE):
    with open(SETTINGS_FILE, "r") as file:
        ai_settings = json.load(file)
else:
    ai_settings = {}

# Encryption helpers for allowlist at rest
KEY_FILE = os.path.join(APP_SUPPORT_DIR, "secret.key")


def _get_or_create_key() -> bytes:
    _ensure_app_support_dir()
    try:
        if os.path.exists(KEY_FILE):
            with open(KEY_FILE, 'rb') as f:
                return f.read()
    except Exception as e:
        print(f"⚠️ Failed reading key: {e}")
    key = Fernet.generate_key()
    try:
        with open(KEY_FILE, 'wb') as f:
            f.write(key)
    except Exception as e:
        print(f"⚠️ Failed writing key file: {e}")
    return key


def _fernet() -> Fernet:
    return Fernet(_get_or_create_key())


def encrypt_list(items: list[str]) -> list[str]:
    f = _fernet()
    out = []
    for it in items or []:
        if not it:
            continue
        token = f.encrypt(it.encode('utf-8')).decode('utf-8')
        out.append(token)
    return out


def decrypt_list(tokens: list[str]) -> list[str]:
    f = _fernet()
    out = []
    for tok in tokens or []:
        if not tok:
            continue
        try:
            val = f.decrypt(tok.encode('utf-8')).decode('utf-8')
            out.append(val)
        except Exception:
            continue
    return out


# Ensure generalized agent settings exist
if "ai_trigger_tag" not in ai_settings:
    ai_settings["ai_trigger_tag"] = "@ai"
# OpenAI config (model + system prompt) with sensible defaults
if "openai_model" not in ai_settings:
    ai_settings["openai_model"] = "gpt-4o-mini"
if "system_prompt" not in ai_settings:
    ai_settings["system_prompt"] = "You are a concise, helpful assistant. Keep answers brief."
if "context_window" not in ai_settings:
    ai_settings["context_window"] = 25
if "enable_search" not in ai_settings:
    ai_settings["enable_search"] = False
if "google_cse_id" not in ai_settings:
    ai_settings["google_cse_id"] = ""
if "search_max_results" not in ai_settings:
    ai_settings["search_max_results"] = 5
if "allowed_users_encrypted" in ai_settings and not ai_settings.get("allowed_users"):
    # Decrypt into memory for runtime use
    try:
        allowed_plain = decrypt_list(ai_settings.get("allowed_users_encrypted", []))
        ai_settings["allowed_users"] = sorted({normalize_handle(x) for x in allowed_plain if x})
    except Exception:
        ai_settings["allowed_users"] = []
elif "allowed_users" not in ai_settings:
    ai_settings["allowed_users"] = []
    # Persist initial encrypted state
    try:
        with open(SETTINGS_FILE, "w") as file:
            json.dump({
                **ai_settings,
                "allowed_users_encrypted": encrypt_list(ai_settings.get("allowed_users", [])),
                "allowed_users": []  # do not persist plaintext
            }, file, indent=4)
    except Exception:
        pass

"""Normalize existing allowlist on load (plaintext in memory only)."""
if isinstance(ai_settings.get("allowed_users"), list):
    ai_settings["allowed_users"] = sorted({normalize_handle(x) for x in ai_settings.get("allowed_users", []) if x})


def _ensure_app_support_dir():
    try:
        Path(APP_SUPPORT_DIR).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"⚠️ Could not create app support dir: {e}")


def _load_state():
    global last_seen_date
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                data = json.load(f)
                last_seen_date = int(data.get('last_seen_date', 0))
    except Exception as e:
        print(f"⚠️ Failed to load state: {e}")


def _save_state():
    try:
        _ensure_app_support_dir()
        with open(STATE_FILE, 'w') as f:
            json.dump({"last_seen_date": last_seen_date}, f)
    except Exception as e:
        print(f"⚠️ Failed to save state: {e}")


def _init_last_seen_if_needed():
    """If last_seen_date is 0, initialize to latest DB message date unless BOT_REPLAY_HISTORY=1."""
    global last_seen_date
    if last_seen_date:
        return
    if os.getenv("BOT_REPLAY_HISTORY", "0") == "1":
        return
    try:
        conn = sqlite3.connect(CHAT_DB)
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(MAX(date), 0) FROM message")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            last_seen_date = int(row[0])
            _save_state()
    except Exception as e:
        # If we cannot read DB (permissions), leave at 0; monitor will handle errors
        print(f"⚠️ Could not init last_seen_date from DB: {e}")


def escape_applescript(s: str) -> str:
    """Escape a string for safe inclusion inside an AppleScript string literal."""
    if s is None:
        return ""
    # Escape backslash first, then quotes; replace newlines and carriage returns
    return (
        str(s)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


### 📌 Function: Send iMessage (individual or group) ###
def send_message(message: str, phone_number: str = None, chat_guid: str = None):
    """Sends an iMessage. If chat_guid is provided, sends to that chat (group). Otherwise sends to buddy by phone."""
    if not message:
        return
    safe_message = escape_applescript(message)
    safe_phone = escape_applescript(phone_number) if phone_number else None
    safe_chat_guid = escape_applescript(chat_guid) if chat_guid else None
    if chat_guid:
        script = f'''
        tell application "Messages"
            set theChat to a reference to chat id "{safe_chat_guid}"
            send "{safe_message}" to theChat
        end tell
        '''
        target_desc = f"chat {safe_chat_guid}"
        emit_phone = phone_number or chat_guid
        emit_ctx = {"chat_type": "Group", "chat_guid": safe_chat_guid}
    else:
        if not phone_number:
            return
        script = f'''
        tell application "Messages"
            set targetService to 1st account whose service type = iMessage
            set targetBuddy to buddy "{safe_phone}" of targetService
            send "{safe_message}" to targetBuddy
        end tell
        '''
        target_desc = safe_phone
        emit_phone = phone_number
        emit_ctx = {"chat_type": "Individual", "chat_guid": None}

    try:
        subprocess.run(["osascript", "-e", script], check=True)
        print(f"✅ Sent message to {target_desc}: {message}")
    except subprocess.CalledProcessError as e:
        print(f"❌ Error sending message: {e}")

    # Log sent message
    log_message(emit_phone, message, "Sent")
    if socketio:
        payload = {"phone": emit_phone, "message": message}
        payload.update(emit_ctx)
        socketio.emit("message_sent", payload)


# Backwards-compat wrapper
def send_imessage(phone_number, message):
    return send_message(message=message, phone_number=normalize_phone(phone_number))


### 📌 Function: Fetch New iMessages (general) ###
def fetch_new_messages_all():
    """Fetch new received messages (individual chats) since the last seen date."""
    global last_seen_date
    try:
        conn = sqlite3.connect(CHAT_DB)
        cursor = conn.cursor()
    except sqlite3.OperationalError as e:
        msg = (
            "Cannot open Messages database. Grant Full Disk Access to your Python interpreter/terminal/editor "
            "in System Settings → Privacy & Security → Full Disk Access."
        )
        print(f"❌ DB open error: {e}")
        if socketio:
            socketio.emit("agent_error", {"type": "db_open", "error": str(e), "hint": msg})
        return []

    cursor.execute(
        """
        SELECT
            m.rowid as message_id,
            cmj.chat_id as message_group,
            c.guid as chat_guid,
            CASE p.participant_count
                WHEN 0 THEN "???"
                WHEN 1 THEN "Individual"
                ELSE "Group"
            END AS chat_type,
            m.is_from_me,
            h.id AS address,
            m.date,
            m.attributedBody,
            m.text as plain_text,
            m.service
        FROM message AS m
        LEFT JOIN handle AS h ON h.rowid = m.handle_id
        LEFT JOIN chat_message_join AS cmj ON cmj.message_id = m.rowid
        LEFT JOIN chat AS c ON c.rowid = cmj.chat_id
        LEFT JOIN (
            SELECT count(*) as participant_count, cmj.chat_id, cmj.message_id as mid
            FROM chat_handle_join as chj
            INNER JOIN chat_message_join as cmj on cmj.chat_id = chj.chat_id
            GROUP BY cmj.message_id, cmj.chat_id
        ) as p on p.mid = m.rowid
        WHERE m.date > ?
        ORDER BY m.date ASC
        LIMIT 200
        """,
        (last_seen_date,)
    )

    try:
        rows = cursor.fetchall()
    finally:
        conn.close()

    messages = []
    for (message_id, message_group, chat_guid, chat_type, is_from_me, address, date_raw, attributedBody, plain_text, service) in rows:

        text = None
        if attributedBody:
            try:
                text = typedstream.unarchive_from_data(attributedBody).contents[0].value.value
            except Exception:
                text = None
        if not text:
            text = plain_text or ""

        if date_raw and date_raw > last_seen_date:
            last_seen_date = date_raw
            _save_state()

        messages.append({
            "message_id": message_id,
            "address": address,
            "text": (text or "").strip(),
            "date_raw": date_raw,
            "service": service,
            "chat_guid": chat_guid,
            "chat_type": chat_type,
        })

    return messages


### 📌 Function: Log Messages for Analytics ###
def log_message(phone, message, direction):
    """Logs messages to track analytics."""
    message_log.append({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "phone": phone,
        "contact": contacts.get(phone, "Unknown"),
        "message": message,
        "direction": direction
    })
    if socketio:
        socketio.emit("update_analytics", message_log)


### Legacy monitors removed in favor of generalized @ai trigger monitor


### 📌 Function: Schedule Messages ###
def schedule_messages():
    """Schedules automated messages at specific times."""
    while True:
        schedule.run_pending()
        time.sleep(30)
        

def save_ai_settings():
    # Normalize and persist encrypted allowlist; do not write plaintext to disk
    allowed_plain = []
    if isinstance(ai_settings.get("allowed_users"), list):
        allowed_plain = sorted({normalize_handle(x) for x in ai_settings.get("allowed_users", []) if x})
        ai_settings["allowed_users"] = allowed_plain
    payload = {**ai_settings}
    # Keep both encrypted and plaintext to ensure persistence across sessions/sections
    payload["allowed_users_encrypted"] = encrypt_list(allowed_plain)
    payload["allowed_users"] = allowed_plain
    with open(SETTINGS_FILE, "w") as file:
        json.dump(payload, file, indent=4)


### 📌 Helper: Extract @ai command ###
def extract_trigger_command(text: str, tag: str = "@ai") -> str:
    """Extract and return the command text following the trigger tag, if present."""
    if not text or not tag:
        return ""
    idx = text.lower().find(tag.lower())
    if idx == -1:
        return ""
    return text[idx + len(tag):].strip(" :\n\t\r")


### 📌 Helper: Query OpenAI directly with a command ###
def query_openai_direct(command: str) -> str:
    """Query OpenAI via the Responses API using configurable model and system prompt."""
    if not command:
        return ""
    model = (ai_settings.get("openai_model") or "gpt-4o-mini").strip()
    system_prompt = ai_settings.get("system_prompt") or "You are a concise, helpful assistant. Keep answers brief."

    # Prefer Responses API if available; else fallback to chat.completions
    if hasattr(openai_client, "responses") and getattr(openai_client, "responses") is not None:
        resp = openai_client.responses.create(
            model=model,
            input=command,
            instructions=system_prompt,
        )

        # Prefer convenience attribute if available
        text = None
        try:
            text = getattr(resp, "output_text", None)
        except Exception:
            text = None
        if text:
            return text

        # Fallback: concatenate text parts from structured output
        try:
            parts = []
            for item in getattr(resp, "output", []) or []:
                for c in getattr(item, "content", []) or []:
                    t = getattr(c, "text", None)
                    if t:
                        parts.append(t)
            if parts:
                return "".join(parts)
        except Exception:
            pass

        # Last resort: stringify the response
        return str(resp)
    else:
        # Fallback: non-streaming chat completion
        try:
            resp = openai_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": command},
                ],
            )
            return resp.choices[0].message.content
        except Exception as e:
            return f"Error: {e}"


def _fetch_recent_messages_for_chat(chat_guid: str, limit: int = 10) -> list[dict]:
    """Return last N messages for a chat GUID (most recent first in DB, returned oldest→newest)."""
    if not chat_guid:
        return []
    try:
        conn = sqlite3.connect(CHAT_DB)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT m.is_from_me, h.id AS address, m.attributedBody, m.text, m.date
            FROM message AS m
            LEFT JOIN handle AS h ON h.rowid = m.handle_id
            INNER JOIN chat_message_join AS cmj ON cmj.message_id = m.rowid
            INNER JOIN chat AS c ON c.rowid = cmj.chat_id
            WHERE c.guid = ?
            ORDER BY m.date DESC
            LIMIT ?
            """,
            (chat_guid, int(limit)),
        )
        rows = cur.fetchall()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    out = []
    for (is_from_me, address, attributedBody, plain_text, date_raw) in reversed(rows or []):
        try:
            body = typedstream.unarchive_from_data(attributedBody).contents[0].value.value if attributedBody else (plain_text or "")
        except Exception:
            body = plain_text or ""
        out.append({
            "is_from_me": int(is_from_me or 0),
            "address": normalize_handle(address),
            "text": (body or "").strip(),
            "date_raw": date_raw,
        })
    return out


def _format_context_for_model(history: list[dict], requester: str | None, limit: int) -> str:
    """Format recent messages as readable context for the model."""
    lines = [f"Conversation history (latest {int(limit)} messages):"]
    for item in history or []:
        if not item.get("text"):
            continue
        speaker = "Me" if item.get("is_from_me") else (item.get("address") or "Participant")
        lines.append(f"- {speaker}: {item.get('text')}")
    if requester:
        lines.append("")
        lines.append(f"Requester: {requester}")
    lines.append("")
    lines.append("Respond to the latest user message. Keep it concise.")
    return "\n".join(lines)


def google_search(query: str, k: int = 5) -> list[dict]:
    """Search Google Programmable Search (CSE) and return a list of results.

    Requires env GOOGLE_API_KEY and settings ai_settings["google_cse_id"].
    """
    query = (query or "").strip()
    if not query:
        return []
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_CSE_API_KEY")
    cse_id = ai_settings.get("google_cse_id") or ""
    if not api_key or not cse_id:
        raise RuntimeError("Google Search not configured (set GOOGLE_API_KEY and google_cse_id in Settings)")
    params = {"key": api_key, "cx": cse_id, "q": query, "num": max(1, min(int(k or 5), 10))}
    try:
        r = httpx.get("https://www.googleapis.com/customsearch/v1", params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        out = []
        for item in (data.get("items") or [])[: params["num"]]:
            out.append({
                "title": item.get("title"),
                "url": item.get("link"),
                "snippet": item.get("snippet") or "",
            })
        return out
    except Exception as e:
        raise RuntimeError(f"Google Search error: {e}")


def query_openai_stream(command: str, emit_ctx: dict | None = None, *, chat_guid: str | None = None, requester: str | None = None) -> str:
    """Stream a response via the Responses API and emit deltas over Socket.IO.

    Returns the final concatenated text.
    """
    if not command:
        return ""
    model = (ai_settings.get("openai_model") or "gpt-4o-mini").strip()
    system_prompt = ai_settings.get("system_prompt") or "You are a concise, helpful assistant. Keep answers brief."
    # Determine context window size
    try:
        window = int(ai_settings.get("context_window", 25))
    except Exception:
        window = 25
    if window < 1:
        window = 1
    if window > 100:
        window = 100

    context_text = _format_context_for_model(_fetch_recent_messages_for_chat(chat_guid, limit=window) if chat_guid else [], requester, window)

    # Build combined input with context + user request
    input_text = (context_text.strip() + "\n\nUser request: " + command.strip()).strip()

    ctx = dict(emit_ctx or {})
    text_parts: list[str] = []
    # Prefer Responses API streaming if available
    if hasattr(openai_client, "responses") and getattr(openai_client, "responses") is not None:
        try:
            tools = None
            enable_search = bool(ai_settings.get("enable_search"))
            if enable_search:
                tools = [{
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "description": "Search the web for up-to-date information using Google Programmable Search",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "The search query"},
                                "k": {"type": "integer", "minimum": 1, "maximum": 10, "default": int(ai_settings.get("search_max_results", 5))},
                            },
                            "required": ["query"],
                        },
                    },
                }]

            with openai_client.responses.stream(
                model=model,
                input=input_text,
                instructions=system_prompt,
                tools=tools or None,
            ) as stream:
                tool_buffers: dict[str, dict] = {}
                for event in stream:
                    try:
                        et = getattr(event, "type", "")
                        if et == "response.output_text.delta":
                            delta = getattr(event, "delta", None)
                            if delta:
                                text_parts.append(delta)
                                if socketio:
                                    payload = {"delta": delta}
                                    payload.update(ctx)
                                    socketio.emit("ai_stream", payload)
                        elif et.startswith("response.tool_call"):
                            # Accumulate tool call arguments by id
                            tid = getattr(event, "id", None) or getattr(event, "tool_call_id", None)
                            name = getattr(event, "name", None)
                            delta = getattr(event, "delta", None) or getattr(event, "arguments_delta", None) or getattr(event, "arguments", None)
                            if tid:
                                buf = tool_buffers.setdefault(tid, {"name": name, "args": ""})
                                if name:
                                    buf["name"] = name
                                if isinstance(delta, str):
                                    buf["args"] += delta
                                elif isinstance(delta, dict):
                                    try:
                                        buf["args"] += json.dumps(delta)
                                    except Exception:
                                        pass
                                # If tool call is complete, execute
                                if et.endswith(".completed") or et.endswith(".done"):
                                    tname = buf.get("name") or name
                                    args_s = buf.get("args") or ""
                                    try:
                                        targs = json.loads(args_s) if args_s else {}
                                    except Exception:
                                        targs = {"query": args_s}
                                    output = None
                                    if tname == "web_search":
                                        try:
                                            output = google_search(targs.get("query", ""), int(targs.get("k") or ai_settings.get("search_max_results", 5)))
                                        except Exception as e:
                                            output = {"error": str(e)}
                                    if output is not None:
                                        try:
                                            stream.submit_tool_outputs([
                                                {"tool_call_id": tid, "output": json.dumps(output)}
                                            ])
                                        except Exception as e:
                                            if socketio:
                                                socketio.emit("ai_stream", {**ctx, "error": f"tool submit error: {e}"})
                                    tool_buffers.pop(tid, None)
                    except Exception:
                        continue

                # Try to obtain final text from helper or fallback to collected parts
                final_text = None
                try:
                    final_text = getattr(stream, "get_final_text", lambda: None)()
                except Exception:
                    final_text = None
                if not final_text:
                    final_text = "".join(text_parts)

                if socketio:
                    done_payload = {"done": True, "text": final_text}
                    done_payload.update(ctx)
                    socketio.emit("ai_stream", done_payload)
                return final_text
        except Exception as e:
            if socketio:
                err_payload = {"error": str(e)}
                err_payload.update(ctx)
                socketio.emit("ai_stream", err_payload)
            return f"Error: {e}"

    # Fallback: stream via chat.completions if Responses API is unavailable
    try:
        stream = openai_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input_text},
            ],
            stream=True,
        )
        for chunk in stream:
            try:
                delta = None
                choices = getattr(chunk, "choices", None)
                if choices:
                    delta = getattr(choices[0].delta, "content", None)
                if delta:
                    text_parts.append(delta)
                    if socketio:
                        payload = {"delta": delta}
                        payload.update(ctx)
                        socketio.emit("ai_stream", payload)
            except Exception:
                continue
        final_text = "".join(text_parts)
        if not final_text:
            # As a final fallback, request non-streaming
            resp = openai_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": input_text},
                ],
            )
            final_text = resp.choices[0].message.content
        if socketio:
            done_payload = {"done": True, "text": final_text}
            done_payload.update(ctx)
            socketio.emit("ai_stream", done_payload)
        return final_text
    except Exception as e:
        if socketio:
            err_payload = {"error": str(e)}
            err_payload.update(ctx)
            socketio.emit("ai_stream", err_payload)
        return f"Error: {e}"


### 📌 Function: Monitor DB with @ai Trigger ###
def monitor_db_polling_general():
    """Polls for new messages and reacts only to @ai triggers from allowed users."""
    print("🔍 General monitor: polling chat.db for new messages every 5 seconds...")
    print(f"🔧 Trigger tag: {ai_settings.get('ai_trigger_tag', '@ai')}")
    print(f"🔧 Allowed users: {ai_settings.get('allowed_users', [])}")

    # Initialize last_seen_date from persisted state/DB
    _ensure_app_support_dir()
    _load_state()
    _init_last_seen_if_needed()

    while True:
        new_items = fetch_new_messages_all()

        for item in new_items:
            phone_number_raw = item.get("address")
            phone_number = normalize_handle(phone_number_raw)
            message = item.get("text", "")
            chat_guid = item.get("chat_guid")
            chat_type = item.get("chat_type")

            if not phone_number or not message:
                continue

            print(f"📩 New message from {phone_number}: {message}")
            log_message(phone_number, message, "Received")

            # Emit raw message update
            if socketio:
                socketio.emit("new_message", {"phone": phone_number, "message": message, "chat_type": chat_type, "chat_guid": chat_guid})

            # Check trigger and allowlist
            cmd = extract_trigger_command(message, ai_settings.get("ai_trigger_tag", "@ai"))
            if cmd:
                print(f"🔎 Trigger detected. Sender={phone_number} Cmd='{cmd}'")
            allowed = phone_number in ai_settings.get("allowed_users", [])
            if not allowed and cmd:
                print(f"⛔ Ignoring trigger: sender not in allowlist. Allowed={ai_settings.get('allowed_users', [])}")
            if allowed and cmd:
                try:
                    emit_ctx = {"phone": phone_number, "chat_type": chat_type, "chat_guid": chat_guid}
                    ai_reply = query_openai_stream(cmd, emit_ctx, chat_guid=chat_guid, requester=phone_number)
                except Exception as e:
                    ai_reply = f"Error: {e}"

                ai_reply = (ai_reply or "").replace('"', '\\"')
                if ai_reply:
                    # Reply into the same context (group or individual)
                    if chat_type == "Group" and chat_guid:
                        send_message(message=ai_reply, chat_guid=chat_guid, phone_number=phone_number)
                    else:
                        send_imessage(phone_number, ai_reply)
                    if socketio:
                        socketio.emit("new_message", {
                            "phone": phone_number,
                            "message": f"@ai {cmd}",
                            "response": ai_reply,
                            "chat_type": chat_type,
                            "chat_guid": chat_guid
                        })

        time.sleep(5)


def check_db_reachability():
    """Return (ok, error_message)."""
    try:
        conn = sqlite3.connect(CHAT_DB)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        conn.close()
        return True, None
    except Exception as e:
        return False, str(e)
