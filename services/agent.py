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
        WHERE m.is_from_me = 0
          AND m.date > ?
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
    for (message_id, message_group, chat_guid, chat_type, is_from_me, address, date_raw, attributedBody, service) in rows:

        try:
            text = typedstream.unarchive_from_data(attributedBody).contents[0].value.value if attributedBody else ""
        except Exception:
            text = ""

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
    payload["allowed_users_encrypted"] = encrypt_list(allowed_plain)
    payload["allowed_users"] = []
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

    # Use Responses API (preferred over chat.completions)
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


def query_openai_stream(command: str, emit_ctx: dict | None = None) -> str:
    """Stream a response via the Responses API and emit deltas over Socket.IO.

    Returns the final concatenated text.
    """
    if not command:
        return ""
    model = (ai_settings.get("openai_model") or "gpt-4o-mini").strip()
    system_prompt = ai_settings.get("system_prompt") or "You are a concise, helpful assistant. Keep answers brief."

    ctx = dict(emit_ctx or {})
    text_parts: list[str] = []
    try:
        # Stream events from the Responses API
        with openai_client.responses.stream(
            model=model,
            input=command,
            instructions=system_prompt,
        ) as stream:
            for event in stream:
                try:
                    if getattr(event, "type", "") == "response.output_text.delta":
                        delta = getattr(event, "delta", None)
                        if delta:
                            text_parts.append(delta)
                            if socketio:
                                payload = {"delta": delta}
                                payload.update(ctx)
                                socketio.emit("ai_stream", payload)
                except Exception:
                    # Ignore malformed events but continue streaming
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
                    ai_reply = query_openai_stream(cmd, emit_ctx)
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
