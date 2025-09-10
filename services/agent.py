import os
import json
import sqlite3
import subprocess
import time
import schedule
from datetime import datetime
from flask_socketio import SocketIO
from openai import OpenAI
from typing import List, Dict, Tuple, Optional, Any
import typedstream
import csv
from pathlib import Path
import PyPDF2
import base64
import mimetypes
from cryptography.fernet import Fernet
from tavily import TavilyClient


# OpenAI API Key (env only)
OPENAI_API_KEY = os.getenv("OPENAI_KEY") or os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else OpenAI()

# macOS iMessage database path
CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")

# App support/state path (persist last_seen_date)
APP_SUPPORT_DIR = os.path.join(os.path.expanduser("~/Library/Application Support"), "imessage-ai")
TMP_IMAGES_DIR = os.path.join(APP_SUPPORT_DIR, "tmp_images")
STATE_FILE = os.path.join(APP_SUPPORT_DIR, "state.json")

# Flask SocketIO instance
socketio = None

# Track last seen message date (raw Apple epoch)
last_seen_date = 0

# Load AI settings from JSON
SETTINGS_FILE = "settings.json"

# Contacts no longer sourced from CSV; keep an empty map for display/logging.
contacts = {}
# Cache of handle -> display name from Contacts
_contact_name_cache = {}

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


def encrypt_list(items: List[str]) -> List[str]:
    f = _fernet()
    out = []
    for it in items or []:
        if not it:
            continue
        token = f.encrypt(it.encode('utf-8')).decode('utf-8')
        out.append(token)
    return out


def decrypt_list(tokens: List[str]) -> List[str]:
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
if "search_cache_ttl" not in ai_settings:
    ai_settings["search_cache_ttl"] = 120

# In-memory cache for web search results: { (normalized_query, k): (ts, results) }
_search_cache = {}
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

    try:
        Path(TMP_IMAGES_DIR).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"⚠️ Could not create tmp images dir: {e}")


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
            "is_from_me": int(is_from_me or 0),
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


def _fetch_recent_messages_for_chat(chat_guid: str, limit: int = 10) -> List[dict]:
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


def _format_context_for_model(history: List[dict], requester: Optional[str], limit: int) -> str:
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


def _lookup_contact_name_via_contacts_app(handle: str) -> Optional[str]:
    """Best-effort lookup of a display name for a phone/email using Contacts via AppleScript.

    Caches results to avoid repeated AppleScript calls. Returns None if not found or on error.
    """
    if not handle:
        return None
    key = normalize_handle(handle)
    if key in _contact_name_cache:
        return _contact_name_cache.get(key)
    script = f'''
    on normalizePhone(s)
        set t to s as text
        set out to ""
        repeat with i from 1 to count of t
            set ch to character i of t
            if ch is in "+0123456789" then set out to out & ch
        end repeat
        return out
    end normalizePhone

    set target to "{key}"
    set targetDigits to normalizePhone(target)
    tell application "Contacts"
        set foundName to missing value
        repeat with p in people
            -- phones
            repeat with ph in (phones of p)
                try
                    set v to value of ph as text
                    set v2 to normalizePhone(v)
                    if v2 is not "" and v2 is equal to targetDigits then
                        set foundName to name of p as text
                        exit repeat
                    end if
                end try
            end repeat
            if foundName is not missing value then exit repeat
            -- emails
            repeat with em in (emails of p)
                try
                    set v to value of em as text
                    if v is equal to target then
                        set foundName to name of p as text
                        exit repeat
                    end if
                end try
            end repeat
            if foundName is not missing value then exit repeat
        end repeat
    end tell
    if foundName is missing value then
        return ""
    else
        return foundName
    end if
    '''
    try:
        res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=True)
        name = (res.stdout or "").strip()
        if name:
            _contact_name_cache[key] = name
            return name
    except Exception:
        pass
    _contact_name_cache[key] = None  # cache miss
    return None


def _get_chat_participants(chat_guid: str) -> List[dict]:
    """Return a list of participants for a chat with optional contact names.

    Each item: {"handle": str, "name": str or None}
    """
    if not chat_guid:
        return []
    try:
        conn = sqlite3.connect(CHAT_DB)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT h.id
            FROM handle AS h
            INNER JOIN chat_handle_join AS chj ON chj.handle_id = h.rowid
            INNER JOIN chat AS c ON c.rowid = chj.chat_id
            WHERE c.guid = ?
            """,
            (chat_guid,),
        )
        rows = cur.fetchall()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    out = []
    seen = set()
    for (hid,) in rows or []:
        h = normalize_handle(hid)
        if not h or h in seen:
            continue
        seen.add(h)
        name = _lookup_contact_name_via_contacts_app(h)
        out.append({"handle": h, "name": name})
    return out


def _parse_name_mentions(cmd: str) -> List[str]:
    """Extract @name mentions (excluding '@ai') from a command string."""
    if not cmd:
        return []
    toks = []
    parts = cmd.split()
    for p in parts:
        if p.startswith("@") and len(p) > 1:
            tag = p[1:].strip(".,:;!?)").lower()
            if tag and tag != "ai":
                toks.append(tag)
    return toks


def _resolve_mention_to_participant(chat_guid: str, mention: str) -> Optional[dict]:
    """Best-effort match of a mention like 'jon' to a chat participant by display name.

    Returns {"handle": str, "name": str or None} or None.
    """
    mention_l = (mention or "").strip().lower()
    if not mention_l:
        return None
    participants = _get_chat_participants(chat_guid)
    # Exact and prefix/substring matching on normalized name
    cand = None
    for p in participants:
        nm = (p.get("name") or "").strip()
        if not nm:
            continue
        nl = nm.lower()
        if nl == mention_l or nl.startswith(mention_l) or mention_l in nl:
            cand = p
            break
    return cand


def _resolve_mentions_in_chat(chat_guid: str, mentions: List[str]) -> Tuple[List[dict], Dict, List[str]]:
    """Resolve a list of mentions to participants and fetch their latest messages.

    Returns (resolved, ambiguous, missing)
      - resolved: list of {mention, handle, name, latest_text}
      - ambiguous: {mention: [candidates...]}, where candidate = {handle, name}
      - missing: list of mentions that did not match anyone
    """
    resolved = []
    ambiguous = {}
    missing = []
    if not (chat_guid and mentions):
        return resolved, ambiguous, missing

    participants = _get_chat_participants(chat_guid)
    # Build name->participant mapping for substring matches
    for m in mentions:
        m_l = (m or "").strip().lower()
        if not m_l:
            continue
        cands = []
        for p in participants:
            nm = (p.get("name") or "").strip()
            nl = nm.lower() if nm else ""
            if nl and (nl == m_l or nl.startswith(m_l) or m_l in nl):
                cands.append({"handle": p.get("handle"), "name": nm})
        if not cands:
            missing.append(m)
            continue
        if len(cands) > 1:
            ambiguous[m] = cands
            continue
        # Exactly one candidate
        chosen = cands[0]
        # Find latest message text from this handle
        latest_text = None
        try:
            hist = _fetch_recent_messages_for_chat(chat_guid, limit=50)
            for msg in reversed(hist or []):
                if normalize_handle(msg.get("address")) == chosen.get("handle") and not msg.get("is_from_me"):
                    latest_text = (msg.get("text") or "").strip()
                    if latest_text:
                        break
        except Exception:
            latest_text = None
        resolved.append({
            "mention": m,
            "handle": chosen.get("handle"),
            "name": chosen.get("name"),
            "latest_text": latest_text,
        })
    return resolved, ambiguous, missing


def tavily_search(query: str, k: int = 5) -> dict:
    """Search the web using Tavily API and return a structured payload.

    Returns a dict with keys:
      - answer: short synthesized answer (string or empty)
      - results: list of {title, url, content, score}

    Requires env TAVILY_API_KEY (or TAVILY_KEY).
    """
    query = (query or "").strip()
    if not query:
        return {"answer": "", "results": []}
    api_key = os.getenv("TAVILY_API_KEY") or os.getenv("TAVILY_KEY")
    if not api_key:
        raise RuntimeError("Tavily search not configured (set TAVILY_API_KEY)")
    try:
        client = TavilyClient(api_key=api_key)
        num = max(1, min(int(k or 5), 10))
        data = client.search(
            query=query,
            max_results=num,
            search_depth="basic",
            include_answer="advanced",
            include_images=False,
            include_favicon=False,
            country="united states",
        )
        answer = data.get("answer") or ""
        out_results = []
        for item in (data.get("results") or [])[:num]:
            out_results.append({
                "title": item.get("title"),
                "url": item.get("url"),
                "content": item.get("content") or "",
                "score": item.get("score"),
            })
        return {"answer": answer, "results": out_results}
    except Exception as e:
        raise RuntimeError(f"Tavily search error: {e}")


def _norm_query(q: str) -> str:
    return " ".join((q or "").strip().split()).lower()


def web_search_cached(query: str, k: int) -> dict:
    ttl = 0
    try:
        ttl = int(ai_settings.get("search_cache_ttl", 120))
    except Exception:
        ttl = 120
    key = (_norm_query(query), int(k or 5))
    now = time.time()
    if key in _search_cache:
        ts, res = _search_cache[key]
        if now - ts <= max(1, ttl):
            return res
    data = tavily_search(query, k)
    _search_cache[key] = (now, data)
    return data


def _maybe_force_search_query(cmd: str) -> Optional[str]:
    """Heuristics: if the command clearly asks for browsing/news/stocks, return a search query."""
    if not cmd:
        return None
    c = cmd.strip()
    lc = c.lower()
    prefixes = [
        "search:", "search ",
        "news:", "news ",
        "latest ",
        "headlines",
        "latest stock price",
        "stock price",
    ]
    for p in prefixes:
        if lc.startswith(p):
            # strip leading directive words
            if p.endswith(":") or p.endswith(" "):
                return c[len(p):].strip() or c
            return c
    # If the command contains strong browse cues
    cues = ["today", "breaking", "headline", "latest", "stock price", "market today"]
    if any(w in lc for w in cues):
        return c
    return None


def _is_image_row(uti: Optional[str], mime: Optional[str], filename: Optional[str]) -> bool:
    u = (uti or "").lower()
    m = (mime or "").lower()
    f = (filename or "").lower()
    if m.startswith("image/"):
        return True
    if any(x in u for x in ("public.jpeg", "public.png", "public.tiff", "heic", "public.heic")):
        return True
    if any(f.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".heic", ".tif", ".tiff", ".webp")):
        return True
    return False


def _is_pdf_row(uti: Optional[str], mime: Optional[str], filename: Optional[str]) -> bool:
    u = (uti or "").lower()
    m = (mime or "").lower()
    f = (filename or "").lower()
    if m == "application/pdf" or "pdf" in u:
        return True
    if f.endswith(".pdf"):
        return True
    return False


def _find_recent_image_attachments(chat_guid: str, before_date: Optional[int] = None, max_images: int = 3) -> List[str]:
    """Return up to max_images absolute file paths for recent image attachments in a chat."""
    if not chat_guid:
        return []
    try:
        conn = sqlite3.connect(CHAT_DB)
        cur = conn.cursor()
        if before_date is None:
            cur.execute(
                """
                SELECT a.filename, a.mime_type, a.uti, m.date
                FROM message AS m
                INNER JOIN chat_message_join AS cmj ON cmj.message_id = m.rowid
                INNER JOIN chat AS c ON c.rowid = cmj.chat_id
                INNER JOIN message_attachment_join AS maj ON maj.message_id = m.rowid
                INNER JOIN attachment AS a ON a.rowid = maj.attachment_id
                WHERE c.guid = ?
                ORDER BY m.date DESC
                LIMIT 50
                """,
                (chat_guid,),
            )
        else:
            cur.execute(
                """
                SELECT a.filename, a.mime_type, a.uti, m.date
                FROM message AS m
                INNER JOIN chat_message_join AS cmj ON cmj.message_id = m.rowid
                INNER JOIN chat AS c ON c.rowid = cmj.chat_id
                INNER JOIN message_attachment_join AS maj ON maj.message_id = m.rowid
                INNER JOIN attachment AS a ON a.rowid = maj.attachment_id
                WHERE c.guid = ? AND m.date <= ?
                ORDER BY m.date DESC
                LIMIT 50
                """,
                (chat_guid, int(before_date)),
            )
        rows = cur.fetchall()
    except Exception:
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass

    paths = []
    for (filename, mime_type, uti, _date) in rows or []:
        if _is_image_row(uti, mime_type, filename) and filename:
            try:
                # Expand tilde if present
                fp = os.path.expanduser(filename)
                if os.path.exists(fp):
                    paths.append(fp)
                if len(paths) >= max_images:
                    break
            except Exception:
                continue
    return paths


def _find_recent_pdf_attachments(chat_guid: str, before_date: Optional[int] = None, max_docs: int = 3) -> List[str]:
    if not chat_guid:
        return []
    try:
        conn = sqlite3.connect(CHAT_DB)
        cur = conn.cursor()
        if before_date is None:
            cur.execute(
                """
                SELECT a.filename, a.mime_type, a.uti, m.date
                FROM message AS m
                INNER JOIN chat_message_join AS cmj ON cmj.message_id = m.rowid
                INNER JOIN chat AS c ON c.rowid = cmj.chat_id
                INNER JOIN message_attachment_join AS maj ON maj.message_id = m.rowid
                INNER JOIN attachment AS a ON a.rowid = maj.attachment_id
                WHERE c.guid = ?
                ORDER BY m.date DESC
                LIMIT 50
                """,
                (chat_guid,),
            )
        else:
            cur.execute(
                """
                SELECT a.filename, a.mime_type, a.uti, m.date
                FROM message AS m
                INNER JOIN chat_message_join AS cmj ON cmj.message_id = m.rowid
                INNER JOIN chat AS c ON c.rowid = cmj.chat_id
                INNER JOIN message_attachment_join AS maj ON maj.message_id = m.rowid
                INNER JOIN attachment AS a ON a.rowid = maj.attachment_id
                WHERE c.guid = ? AND m.date <= ?
                ORDER BY m.date DESC
                LIMIT 50
                """,
                (chat_guid, int(before_date)),
            )
        rows = cur.fetchall()
    except Exception:
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass

    paths = []
    for (filename, mime_type, uti, _date) in rows or []:
        if _is_pdf_row(uti, mime_type, filename) and filename:
            try:
                fp = os.path.expanduser(filename)
                if os.path.exists(fp):
                    paths.append(fp)
                if len(paths) >= max_docs:
                    break
            except Exception:
                continue
    return paths


def _encode_image_as_data_url(path: str) -> Optional[str]:
    """Return a data URL for an image. Converts unsupported formats (e.g., HEIC/TIFF) to JPEG via 'sips'."""
    try:
        allowed = {"image/png", "image/jpeg", "image/webp", "image/gif"}
        mime, _ = mimetypes.guess_type(path)
        if mime not in allowed:
            # Try converting to JPEG using macOS 'sips'
            try:
                _ensure_app_support_dir()
                base = os.path.splitext(os.path.basename(path))[0]
                out_path = os.path.join(TMP_IMAGES_DIR, base + ".jpg")
                subprocess.run(["sips", "-s", "format", "jpeg", path, "--out", out_path], check=True)
                path = out_path
                mime = "image/jpeg"
            except Exception as ce:
                print(f"⚠️ Failed to convert image {path} to JPEG: {ce}")
        # Read and encode
        with open(path, 'rb') as f:
            data = f.read()
        # If still unknown, default to jpeg
        if not mime:
            mime = 'image/jpeg'
        if mime not in allowed:
            print(f"⚠️ Skipping unsupported image mime {mime} for {path}")
            return None
        b64 = base64.b64encode(data).decode('ascii')
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        print(f"⚠️ Failed to read image {path}: {e}")
        return None


def describe_images_with_openai(image_paths: List[str], instruction: Optional[str] = None) -> str:
    """Call the OpenAI API with one or more images and return a concise description."""
    if not image_paths:
        return ""
    prompt = (instruction or "Describe the image(s) clearly and concisely.").strip()
    content = [{"type": "text", "text": prompt}]
    attached = 0
    for p in image_paths:
        url = _encode_image_as_data_url(p)
        if url:
            content.append({"type": "image_url", "image_url": {"url": url}})
            attached += 1
    if attached == 0:
        return "I couldn't read any shared images (unsupported format). Please resend as JPEG/PNG/WebP or say 'convert and describe' and I'll try again."
    try:
        # Prefer chat.completions for multimodal messages
        resp = openai_client.chat.completions.create(
            model=(ai_settings.get("openai_model") or "gpt-4o-mini").strip(),
            messages=[
                {"role": "system", "content": ai_settings.get("system_prompt") or "You are a concise, helpful assistant. Keep answers brief."},
                {"role": "user", "content": content},
            ],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        return f"Error describing image: {e}"


def extract_text_from_pdf(path: str, max_chars: int = 30000) -> str:
    try:
        reader = PyPDF2.PdfReader(path)
        texts = []
        for page in reader.pages:
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            if t:
                texts.append(t.strip())
            if sum(len(x) for x in texts) > max_chars:
                break
        raw = "\n\n".join(texts)
        if len(raw) > max_chars:
            raw = raw[:max_chars]
        return raw.strip()
    except Exception as e:
        return ""


def summarize_text_with_openai(text: str, instruction: Optional[str] = None) -> str:
    if not (text or "").strip():
        return ""
    ask = (instruction or "Give a concise summary (5-7 bullets or 1 short paragraph). Focus on the main points, claims, and any important numbers.").strip()
    try:
        resp = openai_client.chat.completions.create(
            model=(ai_settings.get("openai_model") or "gpt-4o-mini").strip(),
            messages=[
                {"role": "system", "content": ai_settings.get("system_prompt") or "You are a concise, helpful assistant. Keep answers brief."},
                {"role": "user", "content": ask + "\n\n=== Document ===\n" + text},
            ],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        return f"Error summarizing document: {e}"


def _upload_file_to_openai(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            fo = openai_client.files.create(file=f, purpose="assistants")
        return getattr(fo, "id", None)
    except Exception as e:
        print(f"⚠️ Failed to upload file to OpenAI: {e}")
        return None


def summarize_pdfs_with_openai_file_refs(paths: List[str], instruction: Optional[str] = None) -> str:
    """Upload one or more PDFs to OpenAI and request a concise summary using the Responses API.

    Falls back to local text extraction if the Responses API is unavailable or upload fails.
    """
    files = []
    for p in paths or []:
        if not p:
            continue
        fid = _upload_file_to_openai(p)
        if fid:
            files.append(fid)
    prompt = (instruction or "Give a concise summary (5-7 bullets or 1 short paragraph). Focus on the main points, claims, and any important numbers.").strip()

    # Use Responses API if available and we have file ids
    if files and hasattr(openai_client, "responses") and getattr(openai_client, "responses") is not None:
        try:
            input_msg = [{
                "role": "user",
                "content": ([{"type": "input_text", "text": prompt}] + [{"type": "input_file", "file_id": fid} for fid in files])
            }]
            resp = openai_client.responses.create(
                model=(ai_settings.get("openai_model") or "gpt-4o-mini").strip(),
                input=input_msg,
                instructions=ai_settings.get("system_prompt") or "You are a concise, helpful assistant. Keep answers brief.",
            )
            try:
                text = getattr(resp, "output_text", None)
                if text:
                    return text
            except Exception:
                pass
            # Fallback: concatenate parts
            parts = []
            try:
                for item in getattr(resp, "output", []) or []:
                    for c in getattr(item, "content", []) or []:
                        t = getattr(c, "text", None)
                        if t:
                            parts.append(t)
            except Exception:
                pass
            if parts:
                return "".join(parts)
            return str(resp)
        except Exception as e:
            print(f"⚠️ Responses API failed for PDF summary: {e}")

    # Fallback: local text extraction + text summarization
    try:
        texts = []
        for p in paths or []:
            t = extract_text_from_pdf(p)
            if not t:
                continue
            texts.append(t)
        if not texts:
            return "I couldn't read the PDF(s). They may be scanned/image-only or protected."
        merged = "\n\n".join(texts)
        return summarize_text_with_openai(merged, instruction=prompt)
    except Exception as e:
        return f"Error summarizing document: {e}"


def _infer_requested_image_count(text: str, default: int = 1) -> int:
    """Infer how many images the user asked to consider. Defaults to 1 (latest).

    Recognizes simple numeric mentions (e.g., "last 3"), and keywords like
    "both", "couple" (2), "few"/"several"/"more pictures" (3), and "all" (up to 5).
    Caps between 1 and 5.
    """
    try:
        t = (text or "").lower()
        if not t:
            return max(1, min(default, 5))
        # Keywords
        if "all" in t:
            return 5
        if any(k in t for k in ("both", "couple")):
            return 2
        if any(k in t for k in ("few", "several", "more picture", "more images", "more pics", "recent pictures", "recent images")):
            return 3
        # Word numbers
        words_map = {
            "two": 2, "three": 3, "four": 4, "five": 5,
            "2": 2, "3": 3, "4": 4, "5": 5,
        }
        for w, n in words_map.items():
            if w in t:
                return max(1, min(n, 5))
        # Digits anywhere
        import re
        m = re.search(r"(\d+)", t)
        if m:
            try:
                n = int(m.group(1))
                return max(1, min(n, 5))
            except Exception:
                pass
        return max(1, min(default, 5))
    except Exception:
        return max(1, min(default, 5))


def _is_search_configured() -> bool:
    """Return True if web search is enabled and Tavily API key is present."""
    if not bool(ai_settings.get("enable_search")):
        return False
    api_key = os.getenv("TAVILY_API_KEY") or os.getenv("TAVILY_KEY")
    return bool(api_key)


def query_openai_stream(command: str, emit_ctx: Optional[dict] = None, *, chat_guid: Optional[str] = None, requester: Optional[str] = None, extra_context: Optional[str] = None) -> str:
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

    # Optionally prefetch web results via heuristic (deterministic, plus tools remain available)
    pre_results_text = ""
    enable_search = bool(ai_settings.get("enable_search"))
    search_configured = _is_search_configured()
    try:
        print(f"🧠 query_openai_stream: enable_search={enable_search} configured={search_configured}")
    except Exception:
        pass
    forced_q = _maybe_force_search_query(command) if search_configured else None
    if forced_q:
        try:
            k = int(ai_settings.get("search_max_results", 5))
            data = web_search_cached(forced_q, k)
            lines = ["Web results (heuristic):"]
            ans = (data.get("answer") or "").strip()
            if ans:
                lines.append(f"Answer: {ans}")
            for r in (data.get("results") or []):
                if not r:
                    continue
                title = (r.get("title") or "").strip()
                url = (r.get("url") or "").strip()
                content = (r.get("content") or "").strip()
                score = r.get("score")
                score_s = f" ({score:.2f})" if isinstance(score, (int, float)) else ""
                if title or url:
                    lines.append(f"- {title}{score_s} — {url}")
                if content:
                    lines.append(f"  {content}")
            pre_results_text = "\n".join(lines)
        except Exception as e:
            # If search is enabled but misconfigured, avoid nudging the model into tools.
            pre_results_text = f"Web search unavailable: {e}"

    # Fast-path: if it's clearly a news/search query and search is configured, return headlines directly
    if forced_q:
        try:
            print(f"🧠 fast-path web headlines for query='{forced_q}'")
        except Exception:
            pass
        try:
            k = int(ai_settings.get("search_max_results", 5))
        except Exception:
            k = 5
        try:
            data = web_search_cached(forced_q, k)
            if data:
                lines = []
                ans = (data.get("answer") or "").strip()
                if ans:
                    lines.append(ans)
                    # Add a blank line before the list for readability
                    lines.append("")
                for i, r in enumerate((data.get("results") or []), 1):
                    title = (r.get("title") or "").strip()
                    url = (r.get("url") or "").strip()
                    score = r.get("score")
                    score_s = f" ({score:.2f})" if isinstance(score, (int, float)) else ""
                    if title and url:
                        lines.append(f"{i}. {title}{score_s} — {url}")
                # Join with new lines to produce a clean layout
                out_text = "\n".join([ln for ln in lines if ln is not None])
                if out_text.strip():
                    return out_text
        except Exception as e:
            # Fall back to model if search unexpectedly fails here
            pass

    # Build combined input with context + optional web results + user request
    parts = [context_text.strip()]
    if extra_context:
        parts.append(extra_context.strip())
    if pre_results_text:
        parts.append(pre_results_text)
    parts.append("User request: " + command.strip())
    input_text = "\n\n".join(p for p in parts if p).strip()

    ctx = dict(emit_ctx or {})
    text_parts = []
    # Prefer Responses API streaming if available
    if hasattr(openai_client, "responses") and getattr(openai_client, "responses") is not None:
        try:
            tools = None
            # Only expose tools when fully configured to avoid the model waiting on tool calls
            if search_configured:
                # Responses API tool shape (not Chat Completions): top-level name/parameters
                tools = [{
                    "type": "function",
                    "name": "web_search",
                    "description": "Search the web for up-to-date information using Tavily",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "The search query"},
                            "k": {"type": "integer", "minimum": 1, "maximum": 10, "default": int(ai_settings.get("search_max_results", 5))},
                        },
                        "required": ["query"],
                    },
                }]

            with openai_client.responses.stream(
                model=model,
                input=input_text,
                instructions=system_prompt,
                tools=tools or None,
            ) as stream:
                tool_buffers = {}
                for event in stream:
                    try:
                        et = getattr(event, "type", "")
                        # Debug trace for tricky tool flows (only when clearly a tool event)
                        if et.startswith("response.tool_call"):
                            try:
                                name = getattr(event, "name", None)
                                if name:
                                    print(f"🛠 Tool event: {et} name={name}")
                            except Exception:
                                pass
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
                                            output = tavily_search(targs.get("query", ""), int(targs.get("k") or ai_settings.get("search_max_results", 5)))
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
                # If the stream yielded no text, fallback to a non-streaming call
                if not (final_text or "").strip():
                    try:
                        fallback = query_openai_direct(command)
                        if fallback:
                            final_text = fallback
                    except Exception:
                        pass

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
        if not (final_text or "").strip():
            try:
                resp = openai_client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": input_text},
                    ],
                )
                final_text = resp.choices[0].message.content
            except Exception:
                pass
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
            date_raw = item.get("date_raw")
            is_from_me = int(item.get("is_from_me") or 0)

            if not phone_number or not message:
                continue

            print(f"📩 New message from {phone_number}: {message}")
            log_message(phone_number, message, "Received")

            # Emit raw message update
            if socketio:
                socketio.emit("new_message", {"phone": phone_number, "message": message, "chat_type": chat_type, "chat_guid": chat_guid})

            # Only react to inbound messages (not our own sends)
            if is_from_me:
                continue

            # Check trigger and allowlist
            cmd = extract_trigger_command(message, ai_settings.get("ai_trigger_tag", "@ai"))
            if cmd:
                print(f"🔎 Trigger detected. Sender={phone_number} Cmd='{cmd}' chat_type={chat_type} chat_guid={chat_guid}")
            allowed = phone_number in ai_settings.get("allowed_users", [])
            if not allowed and cmd:
                print(f"⛔ Ignoring trigger: sender not in allowlist. Allowed={ai_settings.get('allowed_users', [])}")
            if allowed and cmd:
                try:
                    emit_ctx = {"phone": phone_number, "chat_type": chat_type, "chat_guid": chat_guid}
                    # If the request seems to be about an image or a PDF, try specialized handlers
                    wants_image_desc = any(w in cmd.lower() for w in ["describe", "image", "picture", "photo", "pic", "what is this", "what's this"]) or cmd.strip().lower() in ("describe", "describe this", "describe the picture", "describe the image")
                    wants_pdf_summary = ("pdf" in cmd.lower() or any(w in cmd.lower() for w in ["document", "article", "paper", "report"])) and any(w in cmd.lower() for w in ["summary", "summarize", "tl;dr", "quick summary", "short summary", "overview", "explain"])
                    ai_reply = None
                    if chat_guid and wants_image_desc:
                        max_imgs = _infer_requested_image_count(cmd, default=1)
                        img_paths = _find_recent_image_attachments(chat_guid, before_date=date_raw, max_images=max_imgs)
                        if img_paths:
                            print(f"🖼 Describing {len(img_paths)} image(s) for chat {chat_guid}")
                            ai_reply = describe_images_with_openai(img_paths, instruction=cmd)
                    elif chat_guid and wants_pdf_summary:
                        max_docs = _infer_requested_image_count(cmd, default=1)
                        pdf_paths = _find_recent_pdf_attachments(chat_guid, before_date=date_raw, max_docs=max_docs)
                        if pdf_paths:
                            print(f"📄 Summarizing {len(pdf_paths)} PDF(s) for chat {chat_guid}")
                            if max_docs == 1 or len(pdf_paths) == 1:
                                only_path = pdf_paths[0]
                                only_name = os.path.basename(only_path)
                                s = summarize_pdfs_with_openai_file_refs([only_path], instruction=cmd)
                                ai_reply = f"{only_name}:\n{s}" if s else s
                            else:
                                parts = []
                                for idx, p in enumerate(pdf_paths, 1):
                                    s = summarize_pdfs_with_openai_file_refs([p], instruction=cmd)
                                    if s:
                                        name = os.path.basename(p)
                                        parts.append(f"{idx}. {name}: {s}")
                                ai_reply = "\n\n".join(parts)
                    if not ai_reply:
                        # Name mentions support: resolve @name to a participant and pull their latest message
                        extra_ctx = None
                        mentions = _parse_name_mentions(cmd)
                        if chat_guid and mentions:
                            resolved, ambiguous, missing = _resolve_mentions_in_chat(chat_guid, mentions)
                            # If any ambiguous, ask for clarification and skip model call
                            if ambiguous:
                                lines = ["I found multiple matches:"]
                                for m, cands in ambiguous.items():
                                    lines.append(f"For @{m}:")
                                    for i, c in enumerate(cands, 1):
                                        disp = c.get("name") or c.get("handle")
                                        lines.append(f"  {i}. {disp} ({c.get('handle')})")
                                lines.append("")
                                lines.append("Please reply like: '@ai choose 2 for @jon' or '@ai choose 1 for @mary'.")
                                ai_reply = "\n".join(lines)
                            elif resolved:
                                # Build extra context blocks for all resolved mentions
                                blocks = []
                                for r in resolved:
                                    disp = r.get("name") or r.get("handle")
                                    if r.get("latest_text"):
                                        blocks.append(f"Target from @{r.get('mention')} ({disp}):\n{r.get('latest_text')}")
                                extra_ctx = "\n\n".join(blocks) if blocks else None

                        if not ai_reply:
                            ai_reply = query_openai_stream(cmd, emit_ctx, chat_guid=chat_guid, requester=phone_number, extra_context=extra_ctx)
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
