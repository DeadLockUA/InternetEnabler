"""InternetEnabler client agent.

Runs in the background (tray icon), enforces a daily block schedule,
and listens for block/unblock/schedule/task commands from the parent's server.
"""

import json
import os
import threading
import time
import traceback
import tkinter as tk
from tkinter import messagebox, simpledialog
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from PIL import Image, ImageDraw
import pystray

import firewall
import web_ui

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SCHEDULE_PATH = os.path.join(BASE_DIR, "schedule.json")
TASKS_PATH = os.path.join(BASE_DIR, "tasks.json")
HISTORY_PATH = os.path.join(BASE_DIR, "history.json")
MESSAGES_PATH = os.path.join(BASE_DIR, "messages.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_PATH = os.path.join(BASE_DIR, "agent.log")

DEFAULT_REMINDER_MINUTES = 15
HISTORY_RETENTION_DAYS = 400
MAX_BODY_BYTES = 64 * 1024
MAX_MESSAGES = 100
MAX_MESSAGE_CHARS = 2000


def is_valid_time(value):
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return False
    try:
        datetime.strptime(value, "%H:%M")
        return True
    except ValueError:
        return False


# Reentrant: persistence helpers below take this lock, and some callers
# (e.g. on_enable) also hold it around a persistence call plus a firewall
# call, so a plain Lock would deadlock on the nested acquisition.
_lock = threading.RLock()
_icon_ref = {"icon": None}


def log_error(context, exc=False):
    """Append an error line (and traceback, if called from an except block) to
    agent.log. The agent runs via pythonw with no console, so this is the only
    place operators can see handler/thread failures."""
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} ERROR {context}\n")
        if exc:
            f.write(traceback.format_exc())


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, data):
    """Write via a temp file + atomic rename so a reader never observes a
    partially-written (truncated) file."""
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp_path, path)


def load_config():
    with _lock:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)


def save_config(config):
    with _lock:
        _write_json(CONFIG_PATH, config)


def load_schedule():
    with _lock:
        return _read_json(SCHEDULE_PATH, {"times": []}).get("times", [])


def save_schedule(times):
    with _lock:
        _write_json(SCHEDULE_PATH, {"times": times})


def load_tasks():
    with _lock:
        return _read_json(TASKS_PATH, {"tasks": []}).get("tasks", [])


def save_tasks(tasks):
    with _lock:
        _write_json(TASKS_PATH, {"tasks": tasks})


def reset_tasks_done():
    with _lock:
        tasks = _read_json(TASKS_PATH, {"tasks": []}).get("tasks", [])
        if not tasks:
            return
        for t in tasks:
            t["done"] = False
        _write_json(TASKS_PATH, {"tasks": tasks})


def mark_task_done(task_id):
    """Reload tasks fresh and mark a single one done, atomically."""
    with _lock:
        tasks = _read_json(TASKS_PATH, {"tasks": []}).get("tasks", [])
        for t in tasks:
            if t["id"] == task_id:
                t["done"] = True
        _write_json(TASKS_PATH, {"tasks": tasks})


def load_history():
    with _lock:
        return _read_json(HISTORY_PATH, {"entries": []}).get("entries", [])


def append_history(task_text, event):
    with _lock:
        entries = _read_json(HISTORY_PATH, {"entries": []}).get("entries", [])
        entries.append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "task": task_text,
            "event": event,  # "completed" or "skipped"
        })
        cutoff = datetime.now() - timedelta(days=HISTORY_RETENTION_DAYS)
        entries = [e for e in entries if datetime.fromisoformat(e["timestamp"]) >= cutoff]
        _write_json(HISTORY_PATH, {"entries": entries})


def load_messages():
    """Message inbox, newest first. Each entry: {"timestamp": iso, "text": str}."""
    with _lock:
        return _read_json(MESSAGES_PATH, {"messages": []}).get("messages", [])


def append_message(text):
    """Persist a message (newest first, capped at MAX_MESSAGES) and notify the
    tray icon if one is running. Returns the new entry."""
    entry = {"timestamp": datetime.now().isoformat(timespec="seconds"), "text": text}
    with _lock:
        messages = _read_json(MESSAGES_PATH, {"messages": []}).get("messages", [])
        messages.insert(0, entry)
        del messages[MAX_MESSAGES:]
        _write_json(MESSAGES_PATH, {"messages": messages})
    icon = _icon_ref["icon"]
    if icon is not None:
        try:
            icon.notify(f"Message: {text}", "InternetEnabler")
        except Exception:
            pass
    return entry


def load_state():
    """Persisted scheduler state: which schedule entries/reminders already
    fired today. Kept on disk (not just in memory) so an agent restart after
    a block already fired today doesn't refire it and wipe task progress."""
    with _lock:
        data = _read_json(STATE_PATH, {})
        return {
            "date": data.get("date"),
            "fired": data.get("fired", []),
            "reminders_fired": data.get("reminders_fired", []),
        }


def save_state(state):
    with _lock:
        _write_json(STATE_PATH, state)


def make_icon_image(blocked):
    if blocked is None:
        color = (230, 160, 20)  # amber: firewall state could not be determined
    else:
        color = (200, 40, 40) if blocked else (40, 170, 70)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, 60, 60), fill=color)
    return img


def _dialog_root():
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    return root


def ask_yes_no(question):
    root = _dialog_root()
    try:
        return messagebox.askyesno("InternetEnabler", question, parent=root)
    finally:
        root.destroy()


def show_info(title, message):
    root = _dialog_root()
    try:
        messagebox.showinfo(title, message, parent=root)
    finally:
        root.destroy()


def ask_reminder_minutes(current):
    root = _dialog_root()
    try:
        return simpledialog.askinteger(
            "InternetEnabler",
            "Remind me this many minutes before internet is blocked:",
            initialvalue=current,
            minvalue=0,
            maxvalue=180,
            parent=root,
        )
    finally:
        root.destroy()


class CommandHandler(BaseHTTPRequestHandler):
    config = None

    def _authorized(self):
        return self.headers.get("X-Auth-Token") == self.config["token"]

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def _send_html(self, code, html):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _client_ip(self):
        return self.client_address[0]

    def _auth_session(self):
        """Return the session id from the Cookie header if it is valid, else None."""
        cookie = self.headers.get("Cookie", "")
        sid = None
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith(web_ui.SESSION_COOKIE + "="):
                sid = part[len(web_ui.SESSION_COOKIE) + 1:]
                break
        return sid if web_ui.is_valid_session(sid) else None

    def _authed(self):
        """True iff the request has a valid token header OR a valid web session."""
        return self._authorized() or self._auth_session() is not None

    def _do_login(self):
        try:
            body = self._read_json_body()
            password = body.get("password", "")
        except (ValueError, KeyError):
            self._send_json(400, {"error": "invalid body"})
            return
        result = web_ui.check_login(self._client_ip(), password, self.config)
        if result["ok"]:
            sid = web_ui.create_session()
            self.send_response(200)
            self.send_header(
                "Set-Cookie",
                f"{web_ui.SESSION_COOKIE}={sid}; HttpOnly; SameSite=Strict; Path=/",
            )
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b'{"ok": true}')))
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
        else:
            self._send_json(401, result)

    def _do_logout(self):
        sid = self._auth_session()
        if sid is not None:
            web_ui.delete_session(sid)
        self.send_response(200)
        self.send_header(
            "Set-Cookie",
            f"{web_ui.SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
        )
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self):
        try:
            self._do_GET()
        except Exception:
            log_error(f"GET {self.path} failed", exc=True)
            self._send_json(500, {"error": "internal error"})

    def _do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._send_html(200, web_ui.load_login_html())
            return
        if path == "/panel":
            if self._auth_session() is None:
                self._send_redirect("/")
                return
            self._send_html(200, web_ui.load_panel_html())
            return
        if path.startswith("/api/"):
            if not self._authed():
                self._send_json(401, {"error": "unauthorized"})
                return
        elif not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return

        if path in ("/status", "/api/status"):
            with _lock:
                blocked = firewall.is_blocked()
            self._send_json(200, {"blocked": blocked})
        elif path in ("/schedule", "/api/schedule"):
            self._send_json(200, {"times": load_schedule()})
        elif path in ("/tasks", "/api/tasks"):
            self._send_json(200, {"tasks": load_tasks()})
        elif path in ("/history", "/api/history"):
            qs = parse_qs(parsed.query)
            try:
                days = int(qs.get("days", ["30"])[0])
            except ValueError:
                days = 30
            cutoff = datetime.now() - timedelta(days=days)
            entries = [
                e for e in load_history()
                if datetime.fromisoformat(e["timestamp"]) >= cutoff
            ]
            self._send_json(200, {"entries": entries})
        elif path == "/api/messages":
            self._send_json(200, {"messages": load_messages()})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        try:
            self._do_POST()
        except Exception:
            log_error(f"POST {self.path} failed", exc=True)
            self._send_json(500, {"error": "internal error"})

    def _do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "payload too large"})
            return
        if self.path == "/login":
            self._do_login()
            return
        if self.path == "/logout":
            self._do_logout()
            return
        if self.path.startswith("/api/"):
            if not self._authed():
                self._send_json(401, {"error": "unauthorized"})
                return
        elif not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        if self.path in ("/block", "/api/block"):
            with _lock:
                firewall.enable_block()
                reset_tasks_done()
            self._send_json(200, {"blocked": True})
        elif self.path == "/unblock":
            with _lock:
                firewall.disable_block()
            self._send_json(200, {"blocked": False})
        elif self.path in ("/schedule", "/api/schedule"):
            try:
                body = self._read_json_body()
                times = body.get("times", [])
                if not all(is_valid_time(t) for t in times):
                    self._send_json(400, {"error": "invalid time, expected HH:MM"})
                    return
                save_schedule(times)
                self._send_json(200, {"times": times})
            except (ValueError, KeyError):
                self._send_json(400, {"error": "invalid body"})
        elif self.path in ("/tasks", "/api/tasks"):
            try:
                body = self._read_json_body()
                texts = body.get("tasks", [])
                tasks = [
                    {"id": i + 1, "text": text, "done": False}
                    for i, text in enumerate(texts)
                ]
                save_tasks(tasks)
                self._send_json(200, {"tasks": tasks})
            except (ValueError, KeyError):
                self._send_json(400, {"error": "invalid body"})
        elif self.path == "/api/messages":
            try:
                body = self._read_json_body()
                text = str(body.get("text", "")).strip()
                if not text:
                    self._send_json(400, {"error": "message is empty"})
                    return
                if len(text) > MAX_MESSAGE_CHARS:
                    self._send_json(400, {"error": f"message too long (max {MAX_MESSAGE_CHARS} chars)"})
                    return
                append_message(text)
                self._send_json(200, {"messages": load_messages()})
            except (ValueError, KeyError):
                self._send_json(400, {"error": "invalid body"})
        else:
            self._send_json(404, {"error": "not found"})

    def log_message(self, format, *args):
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {self.address_string()} {format % args}\n")


def run_http_server(config):
    try:
        CommandHandler.config = config
        server = ThreadingHTTPServer(("0.0.0.0", config["port"]), CommandHandler)
        server.serve_forever()
    except Exception:
        log_error("run_http_server crashed", exc=True)
        raise


def scheduler_tick(now=None):
    """Run one scheduler check. Split out from run_scheduler so it's unit-testable
    and so a single bad tick can't permanently kill the scheduler thread."""
    if now is None:
        now = datetime.now()
    current_time = now.strftime("%H:%M")
    today = now.date()
    config = load_config()
    reminder_minutes = config.get("reminder_minutes", DEFAULT_REMINDER_MINUTES)

    state = load_state()
    if state["date"] != today.isoformat():
        state = {"date": today.isoformat(), "fired": [], "reminders_fired": []}
        state_changed = True
    else:
        state_changed = False

    for entry in load_schedule():
        if not is_valid_time(entry):
            continue
        entry_today = datetime.combine(today, datetime.strptime(entry, "%H:%M").time())

        # The next occurrence of this entry from "now": today's if it hasn't
        # passed yet, otherwise tomorrow's. Using a single occurrence (instead
        # of checking both) avoids firing the reminder twice - today's and
        # tomorrow's occurrence are exactly 24h apart and would otherwise
        # produce the same HH:MM wall-clock reminder time.
        next_occurrence = entry_today if entry_today >= now else entry_today + timedelta(days=1)
        reminder_dt = next_occurrence - timedelta(minutes=reminder_minutes)
        fire_key = f"{entry}|{next_occurrence.date().isoformat()}"

        if (
            reminder_dt.date() == today
            and reminder_dt.strftime("%H:%M") == current_time
            and fire_key not in state["reminders_fired"]
        ):
            icon = _icon_ref["icon"]
            if icon is not None:
                try:
                    icon.notify(f"Internet will be blocked at {entry}.", "InternetEnabler")
                except Exception:
                    pass
            state["reminders_fired"].append(fire_key)
            state_changed = True

        # >= (not ==) so a block time missed while the PC was off/asleep still
        # fires as soon as the agent is running again, instead of being skipped
        # for the day. "fired" is persisted to state.json (not just kept in
        # memory) so an agent restart after a block already fired today does
        # not refire it and wipe the son's confirmed task progress.
        if current_time >= entry and entry not in state["fired"]:
            with _lock:
                firewall.enable_block()
                reset_tasks_done()
            state["fired"].append(entry)
            state_changed = True

    if state_changed:
        save_state(state)


def run_scheduler():
    while True:
        try:
            scheduler_tick()
        except Exception:
            log_error("scheduler_tick failed", exc=True)
        time.sleep(20)


def on_enable(icon, item):
    """Enable-Internet tray action: gate on confirming every pending task.

    Module-level (not a run_tray closure) so it's directly unit-testable.
    """
    with _lock:
        blocked = firewall.is_blocked()
    if blocked is False:
        return  # only skip when we positively know it's already unblocked

    tasks = load_tasks()
    pending = [t for t in tasks if not t.get("done")]
    for t in pending:
        answered_yes = ask_yes_no(f"Was '{t['text']}' complete?")
        if not answered_yes:
            append_history(t["text"], "skipped")
            show_info("InternetEnabler", "Finish your tasks first.")
            return
        mark_task_done(t["id"])
        append_history(t["text"], "completed")

    with _lock:
        # Re-verify nothing changed while the (potentially long-running)
        # confirmation dialogs were up - a scheduled block firing mid-flow
        # would reset_tasks_done() concurrently. If that happened, refuse to
        # unblock rather than leaving the internet on with unconfirmed tasks.
        if any(not t.get("done") for t in load_tasks()):
            show_info("InternetEnabler", "Tasks changed while confirming - please try again.")
            return
        firewall.disable_block()
    if icon is not None:
        icon.icon = make_icon_image(False)


def on_view_tasks(icon, item):
    tasks = load_tasks()
    if not tasks:
        show_info("Your Tasks", "No tasks assigned.")
        return
    lines = [f"[{'x' if t.get('done') else ' '}] {t['text']}" for t in tasks]
    show_info("Your Tasks", "\n".join(lines))


def on_view_messages(icon, item):
    messages = load_messages()
    if not messages:
        show_info("Messages", "No messages.")
        return
    lines = [f"{m['timestamp']}: {m['text']}" for m in messages[:20]]
    show_info("Messages", "\n".join(lines))


def on_set_reminder(icon, item):
    config = load_config()
    current = config.get("reminder_minutes", DEFAULT_REMINDER_MINUTES)
    minutes = ask_reminder_minutes(current)
    if minutes is None:
        return
    config["reminder_minutes"] = minutes
    save_config(config)


def run_tray(config):
    def status_text(item):
        with _lock:
            blocked = firewall.is_blocked()
        if blocked is None:
            return "Internet: UNKNOWN"
        return "Internet: BLOCKED" if blocked else "Internet: OK"

    menu = pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.MenuItem("Enable Internet", on_enable, default=True),
        pystray.MenuItem("View Tasks", on_view_tasks),
        pystray.MenuItem("View Messages", on_view_messages),
        pystray.MenuItem("Set Reminder Time...", on_set_reminder),
    )

    with _lock:
        initial_blocked = firewall.is_blocked()
    icon = pystray.Icon("InternetEnabler", make_icon_image(initial_blocked), "InternetEnabler", menu)
    _icon_ref["icon"] = icon

    def refresh_loop():
        while True:
            with _lock:
                blocked = firewall.is_blocked()
            icon.icon = make_icon_image(blocked)
            icon.update_menu()
            time.sleep(5)

    threading.Thread(target=refresh_loop, daemon=True).start()
    icon.run()


def main():
    config = load_config()
    firewall.ensure_rules(config["lan_subnet"], config["port"])

    threading.Thread(target=run_http_server, args=(config,), daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()

    run_tray(config)


if __name__ == "__main__":
    main()
