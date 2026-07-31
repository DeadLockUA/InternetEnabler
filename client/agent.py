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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SCHEDULE_PATH = os.path.join(BASE_DIR, "schedule.json")
TASKS_PATH = os.path.join(BASE_DIR, "tasks.json")
HISTORY_PATH = os.path.join(BASE_DIR, "history.json")

DEFAULT_REMINDER_MINUTES = 15
HISTORY_RETENTION_DAYS = 400


def is_valid_time(value):
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return False
    try:
        datetime.strptime(value, "%H:%M")
        return True
    except ValueError:
        return False


_lock = threading.Lock()
_fired_today = {}  # {"HH:MM": date} -> last date this schedule entry fired the block
_reminder_fired = set()  # {("HH:MM", date)} -> reminder already fired for this occurrence
_icon_ref = {"icon": None}


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)


def load_schedule():
    if not os.path.exists(SCHEDULE_PATH):
        return []
    with open(SCHEDULE_PATH, "r", encoding="utf-8") as f:
        return json.load(f).get("times", [])


def save_schedule(times):
    with open(SCHEDULE_PATH, "w", encoding="utf-8") as f:
        json.dump({"times": times}, f, indent=4)


def load_tasks():
    if not os.path.exists(TASKS_PATH):
        return []
    with open(TASKS_PATH, "r", encoding="utf-8") as f:
        return json.load(f).get("tasks", [])


def save_tasks(tasks):
    with open(TASKS_PATH, "w", encoding="utf-8") as f:
        json.dump({"tasks": tasks}, f, indent=4)


def reset_tasks_done():
    tasks = load_tasks()
    if not tasks:
        return
    for t in tasks:
        t["done"] = False
    save_tasks(tasks)


def mark_task_done(task_id):
    """Reload tasks fresh and mark a single one done, under the caller's lock."""
    tasks = load_tasks()
    for t in tasks:
        if t["id"] == task_id:
            t["done"] = True
    save_tasks(tasks)


def load_history():
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        return json.load(f).get("entries", [])


def append_history(task_text, event):
    entries = load_history()
    entries.append({
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "task": task_text,
        "event": event,  # "completed" or "skipped"
    })
    cutoff = datetime.now() - timedelta(days=HISTORY_RETENTION_DAYS)
    entries = [e for e in entries if datetime.fromisoformat(e["timestamp"]) >= cutoff]
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump({"entries": entries}, f, indent=4)


def make_icon_image(blocked):
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

    def do_GET(self):
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/status":
            with _lock:
                blocked = firewall.is_blocked()
            self._send_json(200, {"blocked": blocked})
        elif path == "/schedule":
            self._send_json(200, {"times": load_schedule()})
        elif path == "/tasks":
            self._send_json(200, {"tasks": load_tasks()})
        elif path == "/history":
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
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        if self.path == "/block":
            with _lock:
                firewall.enable_block()
                reset_tasks_done()
            self._send_json(200, {"blocked": True})
        elif self.path == "/unblock":
            with _lock:
                firewall.disable_block()
            self._send_json(200, {"blocked": False})
        elif self.path == "/schedule":
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
        elif self.path == "/tasks":
            try:
                body = self._read_json_body()
                texts = body.get("tasks", [])
                tasks = [
                    {"id": i + 1, "text": text, "done": False}
                    for i, text in enumerate(texts)
                ]
                with _lock:
                    save_tasks(tasks)
                self._send_json(200, {"tasks": tasks})
            except (ValueError, KeyError):
                self._send_json(400, {"error": "invalid body"})
        else:
            self._send_json(404, {"error": "not found"})

    def log_message(self, format, *args):
        pass  # keep console quiet


def run_http_server(config):
    CommandHandler.config = config
    server = ThreadingHTTPServer(("0.0.0.0", config["port"]), CommandHandler)
    server.serve_forever()


def scheduler_tick(now=None):
    """Run one scheduler check. Split out from run_scheduler so it's unit-testable
    and so a single bad tick can't permanently kill the scheduler thread."""
    if now is None:
        now = datetime.now()
    current_time = now.strftime("%H:%M")
    today = now.date()
    config = load_config()
    reminder_minutes = config.get("reminder_minutes", DEFAULT_REMINDER_MINUTES)

    for entry in load_schedule():
        if not is_valid_time(entry):
            continue
        entry_today = datetime.combine(today, datetime.strptime(entry, "%H:%M").time())

        # Check both today's and tomorrow's occurrence so a reminder window that
        # crosses midnight (e.g. a 00:10 block reminded 15 minutes earlier) still fires.
        for occurrence in (entry_today, entry_today + timedelta(days=1)):
            reminder_dt = occurrence - timedelta(minutes=reminder_minutes)
            fire_key = (entry, occurrence.date())
            if reminder_dt.strftime("%H:%M") == current_time and fire_key not in _reminder_fired:
                icon = _icon_ref["icon"]
                if icon is not None:
                    try:
                        icon.notify(f"Internet will be blocked at {entry}.", "InternetEnabler")
                    except Exception:
                        pass
                _reminder_fired.add(fire_key)

        # >= (not ==) so a block time missed while the PC was off/asleep still
        # fires as soon as the agent is running again, instead of being skipped for the day.
        if current_time >= entry and _fired_today.get(entry) != today:
            with _lock:
                firewall.enable_block()
                reset_tasks_done()
            _fired_today[entry] = today


def run_scheduler():
    while True:
        try:
            scheduler_tick()
        except Exception:
            traceback.print_exc()
        time.sleep(20)


def on_enable(icon, item):
    """Enable-Internet tray action: gate on confirming every pending task.

    Module-level (not a run_tray closure) so it's directly unit-testable.
    """
    with _lock:
        already_unblocked = not firewall.is_blocked()
    if already_unblocked:
        return

    tasks = load_tasks()
    pending = [t for t in tasks if not t.get("done")]
    for t in pending:
        answered_yes = ask_yes_no(f"Was '{t['text']}' complete?")
        if not answered_yes:
            append_history(t["text"], "skipped")
            show_info("InternetEnabler", "Finish your tasks first.")
            return
        with _lock:
            mark_task_done(t["id"])
        append_history(t["text"], "completed")

    with _lock:
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
        return "Internet: BLOCKED" if blocked else "Internet: OK"

    menu = pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.MenuItem("Enable Internet", on_enable, default=True),
        pystray.MenuItem("View Tasks", on_view_tasks),
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
