"""InternetEnabler client agent.

Runs in the background (tray icon), enforces a daily block schedule,
and listens for block/unblock/schedule commands from the parent's server.
"""

import json
import os
import sys
import threading
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw
import pystray

import firewall

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SCHEDULE_PATH = os.path.join(BASE_DIR, "schedule.json")

_lock = threading.Lock()
_fired_today = {}  # {"HH:MM": date} -> last date this schedule entry fired


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_schedule():
    if not os.path.exists(SCHEDULE_PATH):
        return []
    with open(SCHEDULE_PATH, "r", encoding="utf-8") as f:
        return json.load(f).get("times", [])


def save_schedule(times):
    with open(SCHEDULE_PATH, "w", encoding="utf-8") as f:
        json.dump({"times": times}, f, indent=4)


def make_icon_image(blocked):
    color = (200, 40, 40) if blocked else (40, 170, 70)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, 60, 60), fill=color)
    return img


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
        if self.path == "/status":
            with _lock:
                blocked = firewall.is_blocked()
            self._send_json(200, {"blocked": blocked})
        elif self.path == "/schedule":
            self._send_json(200, {"times": load_schedule()})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        if self.path == "/block":
            with _lock:
                firewall.enable_block()
            self._send_json(200, {"blocked": True})
        elif self.path == "/unblock":
            with _lock:
                firewall.disable_block()
            self._send_json(200, {"blocked": False})
        elif self.path == "/schedule":
            try:
                body = self._read_json_body()
                times = body.get("times", [])
                save_schedule(times)
                self._send_json(200, {"times": times})
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


def run_scheduler():
    while True:
        now = datetime.now()
        current_time = now.strftime("%H:%M")
        today = date.today()
        for entry in load_schedule():
            if entry == current_time and _fired_today.get(entry) != today:
                with _lock:
                    firewall.enable_block()
                _fired_today[entry] = today
        time.sleep(20)


def run_tray(config):
    def on_enable(icon, item):
        with _lock:
            firewall.disable_block()
        icon.icon = make_icon_image(False)

    def status_text(item):
        with _lock:
            blocked = firewall.is_blocked()
        return "Internet: BLOCKED" if blocked else "Internet: OK"

    menu = pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.MenuItem("Enable Internet", on_enable, default=True),
    )

    with _lock:
        initial_blocked = firewall.is_blocked()
    icon = pystray.Icon("InternetEnabler", make_icon_image(initial_blocked), "InternetEnabler", menu)

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
