"""InternetEnabler client agent.

Runs in the background (tray icon), enforces a daily block schedule,
and listens for block/unblock/schedule/task commands from the parent's server.
"""

import ctypes
from ctypes import wintypes
import functools
import json
import os
import queue
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import messagebox, simpledialog
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SCHEDULE_PATH = os.path.join(BASE_DIR, "schedule.json")
TASKS_PATH = os.path.join(BASE_DIR, "tasks.json")
HISTORY_PATH = os.path.join(BASE_DIR, "history.json")
MESSAGES_PATH = os.path.join(BASE_DIR, "messages.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_PATH = os.path.join(BASE_DIR, "agent.log")

# Logging is initialized BEFORE the third-party imports below on purpose.
# A missing dependency (pystray/Pillow) is the most common reason the agent
# silently exits with code 1 under pythonw.exe, and without these helpers
# running first that failure would never reach agent.log.
MAX_LOG_BYTES = 1024 * 1024


def _rotate_log_if_needed():
    """Keep agent.log bounded (rename once > 1 MB) so a long-running agent
    with frequent HTTP traffic or scheduler errors can't fill the disk."""
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > MAX_LOG_BYTES:
            rotated = f"{LOG_PATH}.1"
            if os.path.exists(rotated):
                os.remove(rotated)
            os.replace(LOG_PATH, rotated)
    except OSError:
        pass


def log(message):
    _rotate_log_if_needed()
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")


def log_error(context, exc=False):
    """Append an error line (and traceback, if called from an except block) to
    agent.log. The agent runs via pythonw with no console, so this is the only
    place operators can see handler/thread failures."""
    _rotate_log_if_needed()
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} ERROR {context}\n")
        if exc:
            f.write(traceback.format_exc())


try:
    from PIL import Image, ImageDraw
except Exception:
    log_error("FATAL failed to import PIL (Pillow) - is Pillow installed for this Python? See requirements.txt", exc=True)
    raise

try:
    import pystray
except Exception:
    log_error("FATAL failed to import pystray - is pystray installed for this Python? See requirements.txt", exc=True)
    raise

try:
    import firewall
except Exception:
    log_error("FATAL failed to import firewall - is firewall.py present next to agent.py?", exc=True)
    raise

try:
    import web_ui
except Exception:
    log_error("FATAL failed to import web_ui - is web_ui.py present next to agent.py?", exc=True)
    raise

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
# "blocked" caches the last known firewall state so the tray menu label and
# icon updates never spawn PowerShell themselves (C1/M4). Updated only by the
# 5 s refresh loop and the direct block/unblock paths.
_icon_ref = {"icon": None, "blocked": None}


def get_blocked_state():
    """Last known block state (True/False/None). Never calls out to
    PowerShell."""
    with _lock:
        return _icon_ref["blocked"]


def set_blocked_state(value):
    """Update the cached block state and repaint the tray icon/menu (best
    effort, exception-safe). Never called with _lock held by the caller of a
    code path that also acquires it. Takes _lock only to read the icon ref,
    then releases before touching pystray."""
    with _lock:
        _icon_ref["blocked"] = value
        icon = _icon_ref["icon"]
    if icon is not None:
        try:
            icon.icon = make_icon_image(value)
            icon.update_menu()
        except Exception:
            pass


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        # A corrupt/truncated file (crash mid-write before os.replace, external
        # edit) must not permanently kill the scheduler or take down an API -
        # fall back to the default instead (M5).
        log_error(f"ignoring unreadable/corrupt {path}, using defaults")
        return default


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


# --- Dialogs ---------------------------------------------------------------
#
# The tray menu callbacks (on_view_tasks / on_view_messages / on_enable /
# on_set_reminder) are invoked by pystray *synchronously inside the Win32
# message loop*: TrackPopupMenuEx returns the selected item, then the callback
# fires from the WM_NOTIFY handler, which runs inside the tray window's WndProc,
# which was entered from DispatchMessage. The popup menu is also still closing
# and handing the foreground back to the previously-active window at that
# moment.
#
# Showing ANY modal dialog synchronously from such a callback is unsafe, for
# two independent reasons:
#   1. Tkinter: messagebox.showinfo() starts a NESTED Tk event loop on the
#      very thread that is still executing the WndProc, so the dialog never
#      receives its input correctly - the OK button is unclickable and the X
#      button cannot close it.
#   2. Even a native MessageBoxW launched at that exact moment can be painted
#      WITHOUT ever becoming active (Windows foreground/hit-test state is still
#      mid-hand-off), so a native box can show the same dead OK/X symptoms.
#
# The robust fix for both: NEVER show a modal from the tray thread. Every
# dialog is submitted to a single dedicated worker thread and shown only after
# a short startup delay (enough for the menu teardown and foreground hand-off
# to finish). Native boxes additionally use MB_TOPMOST | MB_SETFOREGROUND so
# they reliably activate. The tray callback simply blocks for the result.

_DIALOG_STARTUP_DELAY_SECONDS = 0.12   # let the tray menu teardown/foreground hand-off finish
_DIALOG_POLL_TIMEOUT_SECONDS = 0.25
_WIN32 = sys.platform == "win32"

_MB_OK = 0x00000000
_MB_YESNO = 0x00000004
_MB_ICONQUESTION = 0x00000020
_MB_ICONINFORMATION = 0x00000040
_MB_TOPMOST = 0x00040000
_MB_SETFOREGROUND = 0x00010000
_IDYES = 6

_NATIVE = 0      # native Win32 MessageBoxW
_TKINFO = 1      # tkinter messagebox.showinfo (non-Windows fallback)
_TKYESNO = 2     # tkinter messagebox.askyesno (non-Windows fallback)
_TKCUSTOM = 3    # arbitrary callable(root) on the dialog thread


def _native_message_box(title, message, flags):
    """Run a native Win32 modal message box and return the pressed button id.

    Windows only. Must be called on the dedicated dialog thread, after the
    short startup delay, i.e. NOT synchronously from inside a pystray menu
    callback: MessageBoxW launched while TrackPopupMenuEx is still unwinding
    (and the foreground is being handed back) can be drawn without ever
    becoming active, leaving its OK/X buttons unclickable.
    """
    # MB_TOPMOST | MB_SETFOREGROUND force the box to the front AND activate
    # it, so it reliably receives input even right after a foreground hand-off.
    return ctypes.windll.user32.MessageBoxW(
        0, message, title, flags | _MB_TOPMOST | _MB_SETFOREGROUND)


_PM_REMOVE = 0x0001


def _pump_pending_messages():
    """Drain this thread's Win32 message queue without blocking.

    Called from _run_dialog's wait loop, which runs on pystray's own
    message-loop thread: that thread's GetMessage loop cannot turn while
    we're blocked here, so without this pump the tray would stop reacting to
    the shell (e.g. WM_TASKBARCREATED after an Explorer restart) for as long
    as a dialog is open.
    """
    msg = wintypes.MSG()
    lpmsg = ctypes.byref(msg)
    while ctypes.windll.user32.PeekMessageW(lpmsg, None, 0, 0, _PM_REMOVE):
        ctypes.windll.user32.TranslateMessage(lpmsg)
        ctypes.windll.user32.DispatchMessageW(lpmsg)


def _tk_call(kind, call, root):
    """Run one dialog call on the dialog thread; returns the result.

    * native boxes (kind == _NATIVE): *call* is a zero-arg closure that runs
      MessageBoxW - no Tk involved, the Tk root is ignored.
    * Tk messagebox kinds: *call* is an (title, message) tuple.
    * custom (kind == _TKCUSTOM): *call* is callable(root).
    """
    if kind == _NATIVE:
        return call()
    if kind == _TKINFO:
        messagebox.showinfo(call[0], call[1], parent=root)
        return None
    if kind == _TKYESNO:
        return messagebox.askyesno(call[0], call[1], parent=root)
    return call(root)


# --- Dedicated dialog thread ----------------------------------------------
#
# Every dialog (native MessageBoxW, non-Windows Tk fallback, and the Tk input
# dialog) runs on ONE worker thread. Two reasons:
#   1. A modal dialog must NOT be opened synchronously inside a pystray menu
#      callback (which executes inside the tray window's WndProc within
#      DispatchMessage). Even a native MessageBoxW shown there can be painted
#      without being activated, so OK/X never respond. Deferring onto this
#      thread by ~0.12 s lets the menu teardown and foreground hand-off finish
#      first.
#   2. Tkinter requires all Tk widget work on a single thread.

_dialog_requests = queue.Queue()   # (result_queue, kind, call)
_dialog_thread = None
_dialog_thread_lock = threading.Lock()


def _dialog_worker():
    """The single thread that owns the Tk root and runs every dialog.

    The Tk root is created lazily (only once a Tk-kind request actually
    arrives), so a broken Tcl/Tk install does not block the native
    MessageBoxW path (_NATIVE), which needs no Tk at all. Both root creation
    and each dialog call are scoped per-request: a single failure is reported
    back to that caller and logged, but this loop - and thus the thread -
    never exits, so a bad request can't permanently brick every future tray
    action the way a dead worker thread would.
    """
    root = None
    while True:
        result_q, kind, call = _dialog_requests.get()
        try:
            time.sleep(_DIALOG_STARTUP_DELAY_SECONDS)
            if kind != _NATIVE and root is None:
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
            result_q.put(_tk_call(kind, call, root))
        except Exception as exc:
            log_error("dialog failed on dialog thread", exc=True)
            result_q.put(exc)


def _ensure_dialog_thread():
    global _dialog_thread
    if _dialog_thread is not None and _dialog_thread.is_alive():
        return _dialog_thread
    with _dialog_thread_lock:
        if _dialog_thread is None or not _dialog_thread.is_alive():
            thread = threading.Thread(
                target=_dialog_worker, name="ie-dialog", daemon=True)
            thread.start()
            _dialog_thread = thread
        return _dialog_thread


def _run_dialog(kind, call):
    """Submit *call* to the dialog thread and block for its result.

    The caller (e.g. a pystray menu callback on the main thread) blocks here;
    the modal itself opens on the dialog thread AFTER the startup delay, so it
    is never shown inside the tray's DispatchMessage. While waiting, pumps
    any pending Win32 messages for this thread (WM_TASKBARCREATED, repaints):
    this call runs on the same thread as pystray's own GetMessage loop, and
    that loop cannot run again until this call returns, so without this the
    tray icon would stop responding to the shell for as long as the dialog is
    open. Polls so a dead dialog thread is reported instead of hanging
    forever, and restarts it (via _ensure_dialog_thread) rather than staying
    stuck for the remainder of the process.
    """
    thread = _ensure_dialog_thread()
    result_q = queue.Queue()
    _dialog_requests.put((result_q, kind, call))
    while True:
        try:
            result = result_q.get(timeout=_DIALOG_POLL_TIMEOUT_SECONDS)
            if isinstance(result, BaseException):
                raise result
            return result
        except queue.Empty:
            if _WIN32:
                _pump_pending_messages()
            if not thread.is_alive():
                raise RuntimeError("dialog thread is not running")


def ask_yes_no(question):
    if _WIN32:
        result = _run_dialog(
            _NATIVE,
            lambda: _native_message_box(
                "InternetEnabler",
                question,
                _MB_ICONQUESTION | _MB_YESNO,
            ),
        )
        return result == _IDYES
    return _run_dialog(_TKYESNO, ("InternetEnabler", question))


def show_info(title, message):
    if _WIN32:
        _run_dialog(
            _NATIVE,
            lambda: _native_message_box(
                title, message, _MB_ICONINFORMATION | _MB_OK),
        )
        return
    _run_dialog(_TKINFO, (title, message))


def ask_reminder_minutes(current):
    return _run_dialog(
        _TKCUSTOM,
        lambda root: simpledialog.askinteger(
            "InternetEnabler",
            "Remind me this many minutes before internet is blocked:",
            initialvalue=current,
            minvalue=0,
            maxvalue=180,
            parent=root,
        ),
    )


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

    def _content_length(self):
        """Content-Length parsed defensively; None for non-numeric or negative
        values. A negative value must never reach rfile.read() - read(-1) on
        some implementations reads until EOF (unbounded memory)."""
        raw = self.headers.get("Content-Length", "0")
        try:
            length = int(raw)
        except (TypeError, ValueError):
            return None
        if length < 0:
            return None
        return length

    def _read_json_body(self):
        length = self._content_length()
        if length is None or length > MAX_BODY_BYTES:
            raise ValueError("invalid Content-Length")
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
            days = max(1, min(days, HISTORY_RETENTION_DAYS))
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
        length = self._content_length()
        if length is None:
            self._send_json(400, {"error": "invalid Content-Length"})
            return
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
            set_blocked_state(True)
            self._send_json(200, {"blocked": True})
        elif self.path == "/unblock":
            with _lock:
                firewall.disable_block()
            set_blocked_state(False)
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
        _rotate_log_if_needed()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {self.address_string()} {format % args}\n")


def run_http_server(config, server=None):
    """Serve HTTP until the process exits. When called from _main the server
    is already bound (so a bind error is reported synchronously at startup);
    when called standalone it creates its own server first."""
    try:
        CommandHandler.config = config
        if server is None:
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
            set_blocked_state(True)
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


def _safe_tray_action(func):
    """Log a tray menu callback's failure instead of losing it.

    pystray catches whatever a menu callback raises and reports it to a
    stdlib `logging` logger that this project never configures a handler
    for, so under pythonw.exe (no console) an uncaught exception here - e.g.
    _run_dialog surfacing a dead dialog thread - vanishes with no trace in
    agent.log. This puts it there.
    """
    @functools.wraps(func)
    def wrapper(icon, item):
        try:
            func(icon, item)
        except Exception:
            log_error(f"tray action '{func.__name__}' failed", exc=True)
    return wrapper


@_safe_tray_action
def on_enable(icon, item):
    """Enable-Internet tray action: gate on confirming every pending task.

    Module-level (not a run_tray closure) so it's directly unit-testable.
    If a confirmation dialog itself fails (as opposed to the son answering
    "no"), ask_yes_no now raises instead of silently returning False, so
    this aborts without writing a bogus "skipped" history entry for a task
    he was never actually asked about.
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
        # would reset_tasks_done() concurrently, or the parent could replace
        # the whole task list (including with an empty one). In either case
        # refuse to unblock rather than leaving the internet on with
        # unconfirmed tasks.
        current = load_tasks()
        if [t["id"] for t in current] != [t["id"] for t in tasks] or any(not t.get("done") for t in current):
            show_info("InternetEnabler", "Tasks changed while confirming - please try again.")
            return
        firewall.disable_block()
    set_blocked_state(False)


@_safe_tray_action
def on_view_tasks(icon, item):
    tasks = load_tasks()
    if not tasks:
        show_info("Your Tasks", "No tasks assigned.")
        return
    lines = [f"[{'x' if t.get('done') else ' '}] {t['text']}" for t in tasks]
    show_info("Your Tasks", "\n".join(lines))


@_safe_tray_action
def on_view_messages(icon, item):
    messages = load_messages()
    if not messages:
        show_info("Messages", "No messages.")
        return
    lines = [f"{m['timestamp']}: {m['text']}" for m in messages[:20]]
    show_info("Messages", "\n".join(lines))


@_safe_tray_action
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
        # Reads the cached state only - pystray may re-render the menu on
        # hover/open/periodically, so this callback must never spawn
        # PowerShell (C1/M4).
        blocked = get_blocked_state()
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
    _icon_ref["blocked"] = initial_blocked
    icon = pystray.Icon("InternetEnabler", make_icon_image(initial_blocked), "InternetEnabler", menu)
    _icon_ref["icon"] = icon

    def refresh_loop():
        while True:
            try:
                # Sleep first: the startup path already queried the real
                # state once, so avoid firing another PowerShell immediately.
                time.sleep(5)
                with _lock:
                    blocked = firewall.is_blocked()
                set_blocked_state(blocked)
            except Exception:
                # Any subprocess/firewall failure must NOT permanently kill
                # this thread - the tray icon would freeze forever with no
                # log entry (M2).
                log_error("tray refresh failed", exc=True)

    threading.Thread(target=refresh_loop, daemon=True).start()
    icon.run()


def main():
    try:
        _main()
    except SystemExit:
        raise
    except Exception:
        log_error("main crashed", exc=True)
        sys.exit(1)


def _main():
    log(f"Starting InternetEnabler agent (python {sys.version.split()[0]}, {sys.executable})")
    config = load_config()
    log(f"config.json loaded (lan_subnet={config['lan_subnet']}, port={config['port']})")

    port = config["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        log_error(f"invalid port {port!r} in config.json (expected an integer 1-65535)")
        sys.exit(1)

    firewall.ensure_rules(config["lan_subnet"], port)
    log("firewall rules ensured")

    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), CommandHandler)
    except OSError:
        log_error(
            f"cannot bind HTTP server on port {port} "
            "(address in use, or no permission for that port?)",
            exc=True,
        )
        raise
    log(f"HTTP server listening on port {server.server_address[1]}")

    threading.Thread(target=run_http_server, args=(config, server), daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    log("scheduler thread started")

    run_tray(config)
    log("agent terminated")


if __name__ == "__main__":
    main()
