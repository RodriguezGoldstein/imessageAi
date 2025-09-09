from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import threading
import schedule
from services import agent
import os

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")  # Allow WebSocket connections from all origins
agent.socketio = socketio  # Assign WebSocket instance to agent


@app.route("/")
def dashboard():
    return render_template("dashboard.html", contacts=agent.contacts, ai_settings=agent.ai_settings)


@app.route("/send_bulk", methods=["POST"])
def send_bulk():
    message = request.form["message"]
    selected_contacts = request.form.getlist("contacts")

    for phone in selected_contacts:
        agent.send_imessage(phone, message)

    return "Messages sent!", 200


@app.route("/schedule_message", methods=["POST"])
def schedule_message():
    time = request.form["time"]
    message = request.form["message"]
    selected_contacts = request.form.getlist("contacts")

    for phone in selected_contacts:
        agent.scheduled_messages.append({"time": time, "phone": phone, "message": message})
        schedule.every().day.at(time).do(agent.send_imessage, phone, message)

    return "Message Scheduled!", 200


@app.route("/update_ai_settings", methods=["POST"])
def update_ai_settings():
    # Update only fields provided by the form to avoid overwriting others with empty values
    # Legacy settings removed (persona, auto-reply, predefined responses, memory)

    # Generalized agent settings
    if "ai_trigger_tag" in request.form:
        agent.ai_settings["ai_trigger_tag"] = request.form.get("ai_trigger_tag", agent.ai_settings.get("ai_trigger_tag", "@ai")).strip() or "@ai"
    if "allowed_users" in request.form or "allowed_users_extra" in request.form:
        allowed = set(request.form.getlist("allowed_users"))
        extra_raw = request.form.get("allowed_users_extra", "")
        for token in [t.strip() for t in extra_raw.replace("\n", ",").split(",") if t.strip()]:
            allowed.add(token)
        agent.ai_settings["allowed_users"] = sorted(list(allowed))
    agent.save_ai_settings()
    return "AI Settings Updated!", 200


@app.get("/allowlist")
def get_allowlist():
    return jsonify({
        "ai_trigger_tag": agent.ai_settings.get("ai_trigger_tag", "@ai"),
        "allowed_users": agent.ai_settings.get("allowed_users", []),
        "contacts": agent.contacts,
    })


@app.post("/allowlist")
def post_allowlist():
    data = request.get_json(silent=True) or {}
    if "ai_trigger_tag" in data:
        agent.ai_settings["ai_trigger_tag"] = (data.get("ai_trigger_tag") or "@ai").strip() or "@ai"
    if "allowed_users" in data and isinstance(data.get("allowed_users"), list):
        agent.ai_settings["allowed_users"] = [str(x) for x in data.get("allowed_users")]
    agent.save_ai_settings()
    return jsonify({
        "status": "updated",
        "ai_trigger_tag": agent.ai_settings.get("ai_trigger_tag", "@ai"),
        "allowed_users": agent.ai_settings.get("allowed_users", []),
    })


@app.get("/settings")
def settings_page():
    return render_template("settings.html", contacts=agent.contacts, ai_settings=agent.ai_settings)


# Removed legacy endpoints: /reset_conversations and /toggle_auto_reply



if __name__ == "__main__":
    # Optionally skip background threads (useful in restricted environments/tests)
    disable_monitor = os.getenv("BOT_DISABLE_MONITOR", "0") == "1"
    if not disable_monitor:
        thread_schedule = threading.Thread(target=agent.schedule_messages, daemon=True)
        thread_schedule.start()

        thread_monitor = threading.Thread(target=agent.monitor_db_polling_general, daemon=True)
        thread_monitor.start()

    socketio.run(app, debug=True, use_reloader=False)
