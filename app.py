from flask import Flask, render_template, request, jsonify, g, redirect, url_for, session
from flask_socketio import SocketIO
import threading
import schedule
from services import agent
import os
from functools import wraps
import hashlib

app = Flask(__name__)
# Secret for session cookies (set IMSG_AI_SECRET in env for persistence)
app.config['SECRET_KEY'] = os.getenv('IMSG_AI_SECRET') or 'dev-secret-change-me'
# Use threading mode to avoid eventlet/gevent complexity
socketio = SocketIO(
    app,
    cors_allowed_origins=["http://127.0.0.1:5000", "http://localhost:5000"],
    async_mode="threading",
)
agent.socketio = socketio  # Assign WebSocket instance to agent


# Simple bearer token for local auth
IMSG_AI_TOKEN = os.getenv("IMSG_AI_TOKEN")


def _extract_provided_token():
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if request.method in ("POST", "PUT", "PATCH"):
        tok = request.form.get("token")
        if tok:
            return tok
    return request.args.get("token")


def _token_digest(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _session_token_ok() -> bool:
    if not IMSG_AI_TOKEN:
        return True
    expected = _token_digest(IMSG_AI_TOKEN)
    return session.get("t_ok") is True and session.get("t_d") == expected


def require_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        configured = IMSG_AI_TOKEN
        if configured:
            # Allow if valid session
            if _session_token_ok():
                g.token = None
                return fn(*args, **kwargs)
            # Else check request-provided token
            provided = _extract_provided_token()
            if not provided or provided != configured:
                # If browser, redirect to login; for API, return 401
                if request.accept_mimetypes.accept_html:
                    return redirect(url_for('login', next=request.path))
                return ("Unauthorized", 401)
            # Valid bearer -> set session for convenience
            session['t_ok'] = True
            session['t_d'] = _token_digest(configured)
            g.token = None
        else:
            g.token = None
        return fn(*args, **kwargs)
    return wrapper


@app.route("/")
@require_token
def dashboard():
    return render_template("dashboard.html", contacts=agent.contacts, ai_settings=agent.ai_settings)


@app.route("/send_bulk", methods=["POST"])
@require_token
def send_bulk():
    message = request.form["message"]
    phones_raw = request.form.get("phones", "")
    targets = [agent.normalize_phone(p.strip()) for p in phones_raw.replace("\n", ",").split(",") if p.strip()]
    for phone in targets:
        if phone:
            agent.send_imessage(phone, message)

    return "Messages sent!", 200


@app.route("/schedule_message", methods=["POST"])
@require_token
def schedule_message():
    time = request.form["time"]
    message = request.form["message"]
    phones_raw = request.form.get("phones", "")
    targets = [agent.normalize_phone(p.strip()) for p in phones_raw.replace("\n", ",").split(",") if p.strip()]

    for phone in targets:
        if not phone:
            continue
        agent.scheduled_messages.append({"time": time, "phone": phone, "message": message})
        schedule.every().day.at(time).do(agent.send_imessage, phone, message)

    return "Message Scheduled!", 200


@app.route("/update_ai_settings", methods=["POST"])
@require_token
def update_ai_settings():
    # Update only fields provided by the form to avoid overwriting others with empty values
    # Legacy settings removed (persona, auto-reply, predefined responses, memory)

    # Generalized agent settings
    if "ai_trigger_tag" in request.form:
        agent.ai_settings["ai_trigger_tag"] = request.form.get("ai_trigger_tag", agent.ai_settings.get("ai_trigger_tag", "@ai")).strip() or "@ai"
    if "allowed_users" in request.form or "allowed_users_extra" in request.form:
        allowed = set(agent.normalize_handle(x) for x in request.form.getlist("allowed_users"))
        extra_raw = request.form.get("allowed_users_extra", "")
        for token in [t.strip() for t in extra_raw.replace("\n", ",").split(",") if t.strip()]:
            allowed.add(agent.normalize_handle(token))
        agent.ai_settings["allowed_users"] = sorted(list(allowed))
    # OpenAI settings
    if "openai_model" in request.form:
        m = (request.form.get("openai_model", "") or "").strip()
        if m:
            agent.ai_settings["openai_model"] = m
    if "system_prompt" in request.form:
        sp = (request.form.get("system_prompt", "") or "").strip()
        if sp:
            agent.ai_settings["system_prompt"] = sp
    if "context_window" in request.form:
        cw_raw = (request.form.get("context_window", "") or "").strip()
        try:
            cw = int(cw_raw)
            if cw < 1:
                cw = 1
            if cw > 100:
                cw = 100
            agent.ai_settings["context_window"] = cw
        except Exception:
            pass
    # Vision settings
    if "image_chunk_size" in request.form:
        ics_raw = (request.form.get("image_chunk_size", "") or "").strip()
        try:
            ics = int(ics_raw)
            if ics < 1:
                ics = 1
            if ics > 20:
                ics = 20
            agent.ai_settings["image_chunk_size"] = ics
        except Exception:
            pass
    # Search settings (Tavily)
    if "enable_search" in request.form:
        agent.ai_settings["enable_search"] = True
    else:
        # Checkbox not sent means off
        agent.ai_settings["enable_search"] = False
    if "search_max_results" in request.form:
        smr_raw = (request.form.get("search_max_results", "") or "").strip()
        try:
            smr = int(smr_raw)
            if smr < 1:
                smr = 1
            if smr > 10:
                smr = 10
            agent.ai_settings["search_max_results"] = smr
        except Exception:
            pass
    agent.save_ai_settings()
    return "AI Settings Updated!", 200


@app.get("/allowlist")
@require_token
def get_allowlist():
    return jsonify({
        "ai_trigger_tag": agent.ai_settings.get("ai_trigger_tag", "@ai"),
        "allowed_users": agent.ai_settings.get("allowed_users", []),
        "openai_model": agent.ai_settings.get("openai_model", "gpt-4o-mini"),
        "system_prompt": agent.ai_settings.get("system_prompt", "You are a concise, helpful assistant. Keep answers brief."),
        "context_window": agent.ai_settings.get("context_window", 25),
        "image_chunk_size": agent.ai_settings.get("image_chunk_size", 5),
        "enable_search": agent.ai_settings.get("enable_search", False),
        "search_max_results": agent.ai_settings.get("search_max_results", 5),
    })


@app.post("/allowlist")
@require_token
def post_allowlist():
    data = request.get_json(silent=True) or {}
    if "ai_trigger_tag" in data:
        agent.ai_settings["ai_trigger_tag"] = (data.get("ai_trigger_tag") or "@ai").strip() or "@ai"
    if "allowed_users" in data and isinstance(data.get("allowed_users"), list):
        agent.ai_settings["allowed_users"] = [agent.normalize_handle(x) for x in data.get("allowed_users")]
    agent.save_ai_settings()
    return jsonify({
        "status": "updated",
        "ai_trigger_tag": agent.ai_settings.get("ai_trigger_tag", "@ai"),
        "allowed_users": agent.ai_settings.get("allowed_users", []),
    })


@app.post("/api/send")
@require_token
def api_send():
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    phone = data.get("phone")
    chat_guid = data.get("chat_guid")
    if not message:
        return jsonify({"error": "message is required"}), 400
    if bool(phone) == bool(chat_guid):  # either phone or chat_guid, but not both
        return jsonify({"error": "provide exactly one of 'phone' or 'chat_guid'"}), 400
    try:
        if phone:
            phone_norm = agent.normalize_phone(phone)
            if not phone_norm:
                return jsonify({"error": "invalid phone"}), 400
            agent.send_message(message=message, phone_number=phone_norm)
            return jsonify({"status": "sent", "to": phone_norm})
        else:
            agent.send_message(message=message, chat_guid=str(chat_guid))
            return jsonify({"status": "sent", "chat_guid": str(chat_guid)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/settings")
@require_token
def settings_page():
    return render_template("settings.html", contacts=agent.contacts, ai_settings=agent.ai_settings)


# Removed legacy endpoints: /reset_conversations and /toggle_auto_reply


@app.get("/healthz")
def healthz():
    ok, db_error = agent.check_db_reachability()
    return jsonify({
        "ok": ok,
        "db_ok": ok,
        "db_error": db_error,
        "last_seen_date": agent.last_seen_date,
    }), (200 if ok else 503)


@socketio.on("connect")
def on_connect(auth):
    # If a token is set, require it for WebSocket connections
    configured = IMSG_AI_TOKEN
    if not configured:
        return True
    # Accept if session already authenticated
    if _session_token_ok():
        return True
    provided = None
    if isinstance(auth, dict):
        provided = auth.get("token")
    if not provided:
        provided = request.args.get("token")
    if provided == configured:
        # Set session for the socket request context as well
        session['t_ok'] = True
        session['t_d'] = _token_digest(configured)
        return True
    return False  # Reject connection


@app.route('/login', methods=['GET', 'POST'])
def login():
    if not IMSG_AI_TOKEN:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        provided = request.form.get('token', '')
        if provided == IMSG_AI_TOKEN:
            session['t_ok'] = True
            session['t_d'] = _token_digest(IMSG_AI_TOKEN)
            nxt = request.args.get('next') or url_for('dashboard')
            return redirect(nxt)
        return render_template('login.html', error='Invalid token')
    return render_template('login.html')


@app.post('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


if __name__ == "__main__":
    # Optionally skip background threads (useful in restricted environments/tests)
    disable_monitor = os.getenv("BOT_DISABLE_MONITOR", "0") == "1"
    if not disable_monitor:
        thread_schedule = threading.Thread(target=agent.schedule_messages, daemon=True)
        thread_schedule.start()

        thread_monitor = threading.Thread(target=agent.monitor_db_polling_general, daemon=True)
        thread_monitor.start()

    # Werkzeug dev server safety gate (Flask 2.3+/3.x):
    # When running under nohup/production-like contexts, Flask/Flask-SocketIO
    # raises unless explicitly allowed. This app uses the built-in server for
    # local use, so allow it here.
    socketio.run(
        app,
        host="127.0.0.1",
        port=5000,
        debug=True,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )
