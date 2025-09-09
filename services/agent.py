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


# OpenAI API Key (env only)
OPENAI_API_KEY = os.getenv("OPENAI_KEY") or os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else OpenAI()

# macOS iMessage database path
CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")

# Flask SocketIO instance
socketio = None

# Track last seen message date (raw Apple epoch)
last_seen_date = 0

# Load AI settings from JSON
SETTINGS_FILE = "settings.json"

# Load contacts from CSV
CONTACTS_FILE = "contacts.csv"

contacts = {}
with open(CONTACTS_FILE, mode='r') as file:
    csv_reader = csv.DictReader(file)
    for row in csv_reader:
        contacts[row['phone']] = row['name']

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

# Ensure generalized agent settings exist
if "ai_trigger_tag" not in ai_settings:
    ai_settings["ai_trigger_tag"] = "@ai"
if "allowed_users" not in ai_settings:
    try:
        ai_settings["allowed_users"] = list(contacts.keys())
    except Exception:
        ai_settings["allowed_users"] = []
    with open(SETTINGS_FILE, "w") as file:
        json.dump(ai_settings, file, indent=4)


### 📌 Function: Send iMessage (individual or group) ###
def send_message(message: str, phone_number: str = None, chat_guid: str = None):
    """Sends an iMessage. If chat_guid is provided, sends to that chat (group). Otherwise sends to buddy by phone."""
    if not message:
        return
    if chat_guid:
        script = f'''
        tell application "Messages"
            set theChat to a reference to chat id "{chat_guid}"
            send "{message}" to theChat
        end tell
        '''
        target_desc = f"chat {chat_guid}"
        emit_phone = phone_number or chat_guid
        emit_ctx = {"chat_type": "Group", "chat_guid": chat_guid}
    else:
        if not phone_number:
            return
        script = f'''
        tell application "Messages"
            set targetService to 1st account whose service type = iMessage
            set targetBuddy to buddy "{phone_number}" of targetService
            send "{message}" to targetBuddy
        end tell
        '''
        target_desc = phone_number
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
    return send_message(message=message, phone_number=phone_number)


### 📌 Function: Fetch New iMessages (general) ###
def fetch_new_messages_all():
    """Fetch new received messages (individual chats) since the last seen date."""
    global last_seen_date
    conn = sqlite3.connect(CHAT_DB)
    cursor = conn.cursor()

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

    rows = cursor.fetchall()
    conn.close()

    messages = []
    for (message_id, message_group, chat_guid, chat_type, is_from_me, address, date_raw, attributedBody, service) in rows:

        try:
            text = typedstream.unarchive_from_data(attributedBody).contents[0].value.value if attributedBody else ""
        except Exception:
            text = ""

        if date_raw and date_raw > last_seen_date:
            last_seen_date = date_raw

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
    socketio.emit("update_analytics", message_log)


### Legacy monitors removed in favor of generalized @ai trigger monitor


### 📌 Function: Schedule Messages ###
def schedule_messages():
    """Schedules automated messages at specific times."""
    while True:
        schedule.run_pending()
        time.sleep(30)
        

def save_ai_settings():
    with open(SETTINGS_FILE, "w") as file:
        json.dump(ai_settings, file, indent=4)


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
    """Simple passthrough to OpenAI for @ai commands."""
    if not command:
        return ""
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a concise, helpful assistant. Keep answers brief."},
            {"role": "user", "content": command},
        ]
    )
    return response.choices[0].message.content


### 📌 Function: Monitor DB with @ai Trigger ###
def monitor_db_polling_general():
    """Polls for new messages and reacts only to @ai triggers from allowed users."""
    print("🔍 General monitor: polling chat.db for new messages every 5 seconds...")

    while True:
        new_items = fetch_new_messages_all()

        for item in new_items:
            phone_number = item.get("address")
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
            if phone_number in ai_settings.get("allowed_users", []) and cmd:
                try:
                    ai_reply = query_openai_direct(cmd)
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
