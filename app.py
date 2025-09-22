from __future__ import annotations

import hashlib
import os
from functools import wraps
from typing import Any, Dict, List

from flask import (
    Blueprint,
    Flask,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_socketio import SocketIO

from services.agent import Agent, normalize_handle, normalize_phone
from services.config import load_config


# ---------------------------------------------------------------------------
# Application wiring
# ---------------------------------------------------------------------------
config = load_config()
agent_service = Agent(config)

app = Flask(__name__)
app.config["SECRET_KEY"] = config.session_secret or "dev-secret-change-me"

socketio = SocketIO(
    app,
    cors_allowed_origins=["http://127.0.0.1:5000", "http://localhost:5000"],
    async_mode="threading",
)
agent_service.attach_socketio(socketio)

AUTH_TOKEN = config.auth_token


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _extract_provided_token() -> str | None:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    if request.method in {"POST", "PUT", "PATCH"}:
        token = request.form.get("token")
        if token:
            return token
    return request.args.get("token")


def _token_digest(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _session_token_ok() -> bool:
    if not AUTH_TOKEN:
        return True
    expected = _token_digest(AUTH_TOKEN)
    return session.get("t_ok") is True and session.get("t_d") == expected


def require_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if AUTH_TOKEN:
            if _session_token_ok():
                g.token = None
                return fn(*args, **kwargs)
            provided = _extract_provided_token()
            if not provided or provided != AUTH_TOKEN:
                if request.accept_mimetypes.accept_html:
                    return redirect(url_for("ui.login", next=request.path))
                return ("Unauthorized", 401)
            session["t_ok"] = True
            session["t_d"] = _token_digest(AUTH_TOKEN)
            g.token = None
        else:
            g.token = None
        return fn(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Blueprints
# ---------------------------------------------------------------------------
ui_bp = Blueprint("ui", __name__)
api_bp = Blueprint("api", __name__)


@ui_bp.route("/")
@require_token
def dashboard():
    return render_template(
        "dashboard.html",
        contacts=agent_service.contacts,
        ai_settings=agent_service.get_ai_settings(),
    )


@ui_bp.route("/send_bulk", methods=["POST"])
@require_token
def send_bulk():
    message = request.form.get("message", "")
    phones_raw = request.form.get("phones", "")
    targets = [
        normalize_phone(p.strip())
        for p in phones_raw.replace("\n", ",").split(",")
        if p.strip()
    ]
    for phone in targets:
        if phone:
            agent_service.send_imessage(phone, message)
    return "Messages sent!", 200


@ui_bp.route("/schedule_message", methods=["POST"])
@require_token
def schedule_message():
    time_str = request.form.get("time", "")
    message = request.form.get("message", "")
    phones_raw = request.form.get("phones", "")
    targets = [
        normalize_phone(p.strip())
        for p in phones_raw.replace("\n", ",").split(",")
        if p.strip()
    ]
    for phone in targets:
        if not phone:
            continue
        try:
            agent_service.schedule_message(time_str, phone, message)
        except Exception as exc:
            return (f"Failed to schedule: {exc}", 400)
    return "Message Scheduled!", 200


@ui_bp.route("/update_ai_settings", methods=["POST"])
@require_token
def update_ai_settings():
    updates: Dict[str, str] = {}
    if "ai_trigger_tag" in request.form:
        tag = (request.form.get("ai_trigger_tag", "") or "").strip() or "@ai"
        updates["ai_trigger_tag"] = tag
    if "openai_model" in request.form:
        model = (request.form.get("openai_model", "") or "").strip()
        if model:
            updates["openai_model"] = model
    if "system_prompt" in request.form:
        prompt = (request.form.get("system_prompt", "") or "").strip()
        if prompt:
            updates["system_prompt"] = prompt
    if "context_window" in request.form:
        try:
            window = int((request.form.get("context_window", "") or "").strip())
            window = max(1, min(window, 100))
            updates["context_window"] = window
        except Exception:
            pass
    if "image_chunk_size" in request.form:
        try:
            chunk = int((request.form.get("image_chunk_size", "") or "").strip())
            chunk = max(1, min(chunk, 20))
            updates["image_chunk_size"] = chunk
        except Exception:
            pass
    if "enable_search" in request.form:
        updates["enable_search"] = True
    else:
        updates["enable_search"] = False
    if "search_max_results" in request.form:
        try:
            max_results = int((request.form.get("search_max_results", "") or "").strip())
            max_results = max(1, min(max_results, 10))
            updates["search_max_results"] = max_results
        except Exception:
            pass

    agent_service.update_ai_settings(updates)

    if "allowed_users" in request.form or "allowed_users_extra" in request.form:
        handles: List[str] = []
        handles.extend(request.form.getlist("allowed_users"))
        extra_raw = request.form.get("allowed_users_extra", "")
        if extra_raw:
            handles.extend([p.strip() for p in extra_raw.replace("\n", ",").split(",") if p.strip()])
        agent_service.set_allowed_users([normalize_handle(h) for h in handles if h])

    return "AI Settings Updated!", 200


@ui_bp.route("/settings")
@require_token
def settings_page():
    return render_template(
        "settings.html",
        contacts=agent_service.contacts,
        ai_settings=agent_service.get_ai_settings(),
    )


@ui_bp.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_TOKEN:
        return redirect(url_for("ui.dashboard"))
    if request.method == "POST":
        provided = request.form.get("token", "")
        if provided == AUTH_TOKEN:
            session["t_ok"] = True
            session["t_d"] = _token_digest(AUTH_TOKEN)
            nxt = request.args.get("next") or url_for("ui.dashboard")
            return redirect(nxt)
        return render_template("login.html", error="Invalid token")
    return render_template("login.html")


@ui_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("ui.login"))


@api_bp.get("/allowlist")
@require_token
def get_allowlist():
    settings = agent_service.get_ai_settings()
    return jsonify({
        "ai_trigger_tag": settings.get("ai_trigger_tag", "@ai"),
        "allowed_users": settings.get("allowed_users", []),
        "openai_model": settings.get("openai_model", "gpt-4o-mini"),
        "system_prompt": settings.get("system_prompt", "You are a concise, helpful assistant. Keep answers brief."),
        "context_window": settings.get("context_window", 25),
        "image_chunk_size": settings.get("image_chunk_size", 5),
        "enable_search": settings.get("enable_search", False),
        "search_max_results": settings.get("search_max_results", 5),
    })


@api_bp.post("/allowlist")
@require_token
def post_allowlist():
    data = request.get_json(silent=True) or {}
    if "ai_trigger_tag" in data:
        agent_service.update_ai_settings({"ai_trigger_tag": (data.get("ai_trigger_tag") or "@ai").strip() or "@ai"})
    if isinstance(data.get("allowed_users"), list):
        agent_service.set_allowed_users([normalize_handle(x) for x in data.get("allowed_users")])
    settings = agent_service.get_ai_settings()
    return jsonify({
        "status": "updated",
        "ai_trigger_tag": settings.get("ai_trigger_tag", "@ai"),
        "allowed_users": settings.get("allowed_users", []),
    })


@api_bp.post("/api/send")
@require_token
def api_send():
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    phone = data.get("phone")
    chat_guid = data.get("chat_guid")
    if not message:
        return jsonify({"error": "message is required"}), 400
    if bool(phone) == bool(chat_guid):
        return jsonify({"error": "provide exactly one of 'phone' or 'chat_guid'"}), 400
    try:
        if phone:
            phone_norm = normalize_phone(phone)
            if not phone_norm:
                return jsonify({"error": "invalid phone"}), 400
            agent_service.send_message(message=message, phone_number=phone_norm)
            return jsonify({"status": "sent", "to": phone_norm})
        agent_service.send_message(message=message, chat_guid=str(chat_guid))
        return jsonify({"status": "sent", "chat_guid": str(chat_guid)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@api_bp.get("/api/messages")
@require_token
def api_messages():
    try:
        limit = int(request.args.get("limit", 250))
    except Exception:
        limit = 250
    limit = max(1, min(limit, 500))
    messages = agent_service.get_message_log()
    if limit:
        messages = messages[-limit:]
    return jsonify({"messages": messages})


@api_bp.get("/api/schedule")
@require_token
def api_get_schedule():
    return jsonify({"scheduled": agent_service.get_scheduled_messages()})


@api_bp.post("/api/schedule")
@require_token
def api_add_schedule():
    data = request.get_json(silent=True) or {}
    time_str = (data.get("time") or "").strip()
    message = (data.get("message") or "").strip()
    phones_raw: Any = data.get("phones")
    if isinstance(phones_raw, str):
        phones = [phones_raw]
    elif isinstance(phones_raw, list):
        phones = phones_raw
    else:
        phones = []

    if not time_str or not message or not phones:
        return jsonify({"error": "time, message, and phones are required"}), 400

    scheduled_entries = []
    try:
        for phone in phones:
            entry = agent_service.schedule_message(time_str, phone, message)
            scheduled_entries.append(entry)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"scheduled": scheduled_entries}), 201


@api_bp.delete("/api/schedule/<entry_id>")
@require_token
def api_delete_schedule(entry_id: str):
    if not entry_id:
        return jsonify({"error": "entry_id required"}), 400
    removed = agent_service.remove_scheduled_message(entry_id)
    if not removed:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "deleted", "entry_id": entry_id})


@api_bp.get("/api/settings")
@require_token
def api_get_settings():
    return jsonify(agent_service.get_ai_settings())


@api_bp.patch("/api/settings")
@require_token
def api_patch_settings():
    data = request.get_json(silent=True) or {}
    updates: Dict[str, Any] = {}

    for key in (
        "ai_trigger_tag",
        "openai_model",
        "system_prompt",
        "context_window",
        "image_chunk_size",
        "enable_search",
        "search_max_results",
    ):
        if key not in data:
            continue
        value = data.get(key)
        if key in {"context_window", "image_chunk_size", "search_max_results"}:
            try:
                value = int(value)
            except Exception:
                continue
            if key == "context_window":
                value = max(1, min(value, 100))
            if key == "image_chunk_size":
                value = max(1, min(value, 20))
            if key == "search_max_results":
                value = max(1, min(value, 10))
        if key == "enable_search":
            value = bool(value)
        if isinstance(value, str):
            value = value.strip()
        updates[key] = value

    allowed_users = data.get("allowed_users")
    if allowed_users is not None:
        if not isinstance(allowed_users, list):
            return jsonify({"error": "allowed_users must be a list"}), 400
        agent_service.set_allowed_users([normalize_handle(x) for x in allowed_users if x])

    if updates:
        agent_service.update_ai_settings(updates)

    return jsonify(agent_service.get_ai_settings())


@api_bp.get("/healthz")
def healthz():
    ok, db_error = agent_service.check_db_reachability()
    return jsonify({
        "ok": ok,
        "db_ok": ok,
        "db_error": db_error,
        "last_seen_date": agent_service.last_seen_date,
    }), (200 if ok else 503)


app.register_blueprint(ui_bp)
app.register_blueprint(api_bp)


@socketio.on("connect")
def on_connect(auth):
    if not AUTH_TOKEN:
        return True
    if _session_token_ok():
        return True
    provided = None
    if isinstance(auth, dict):
        provided = auth.get("token")
    if not provided:
        provided = request.args.get("token")
    if provided == AUTH_TOKEN:
        session["t_ok"] = True
        session["t_d"] = _token_digest(AUTH_TOKEN)
        return True
    return False


if __name__ == "__main__":
    disable_monitor = os.getenv("BOT_DISABLE_MONITOR", "0") == "1"
    if not disable_monitor:
        agent_service.start_background_tasks(monitor_db=True, run_scheduler=True)

    socketio.run(
        app,
        host="127.0.0.1",
        port=5000,
        debug=True,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )
