"""Session management and login guard for the client's web panel.

The panel is served by the agent's HTTP server and is used from any device
on the LAN (phone, tablet, parent's laptop). Plain HTTP + a single family
password (household trust model - same caveat as the X-Auth-Token API).
"""

import os
import secrets
import threading
import time

SESSION_TTL_SECONDS = 30 * 60        # 30 minutes, sliding
FAILURE_WINDOW_SECONDS = 5 * 60      # lockout window
LOCKOUT_THRESHOLD = 5                # failed attempts before lockout
SESSION_COOKIE = "ie_session"

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

_lock = threading.Lock()
_sessions = {}   # sid -> {"expires": unix_ts}
_failures = {}   # ip -> {"count": int, "window_start": unix_ts}


def create_session(now=None):
    """Create a new session and return its id (renewed on each valid use)."""
    if now is None:
        now = time.time()
    sid = secrets.token_hex(16)
    with _lock:
        _sessions[sid] = {"expires": now + SESSION_TTL_SECONDS}
    return sid


def is_valid_session(sid, now=None):
    """True iff sid is a live session. Valid use slides the expiry forward."""
    if not isinstance(sid, str) or not sid:
        return False
    if now is None:
        now = time.time()
    with _lock:
        entry = _sessions.get(sid)
        if entry is None:
            return False
        if now > entry["expires"]:
            del _sessions[sid]
            return False
        entry["expires"] = now + SESSION_TTL_SECONDS
        return True


def delete_session(sid):
    with _lock:
        _sessions.pop(sid, None)


def check_login(ip, password, config, now=None):
    """Validate a web-panel login attempt from `ip`.

    Returns {"ok": True} on success, otherwise {"ok": False, "error": str}
    plus "retry_after" (seconds) when the ip is locked out.
    """
    if now is None:
        now = time.time()

    with _lock:
        failure = _failures.get(ip)
        if failure and now - failure["window_start"] >= FAILURE_WINDOW_SECONDS:
            del _failures[ip]
            failure = None
        if failure and failure["count"] >= LOCKOUT_THRESHOLD:
            retry_after = int(failure["window_start"] + FAILURE_WINDOW_SECONDS - now)
            if retry_after < 0:
                retry_after = 0
            return {
                "ok": False,
                "retry_after": retry_after,
                "error": f"Too many failed attempts. Try again in {retry_after} seconds.",
            }

        stored = config.get("web_password")
        if not stored:
            return {"ok": False, "error": "web_password not configured on the client"}

        if password == stored:
            _failures.pop(ip, None)
            return {"ok": True}

        count = (failure["count"] if failure else 0) + 1
        _failures[ip] = {"count": count, "window_start": now}
        if count >= LOCKOUT_THRESHOLD:
            retry_after = int(FAILURE_WINDOW_SECONDS)
            return {
                "ok": False,
                "retry_after": retry_after,
                "error": f"Too many failed attempts. Try again in {retry_after} seconds.",
            }
        remaining = LOCKOUT_THRESHOLD - count
        return {"ok": False, "error": f"Wrong password. {remaining} attempts left."}


def load_login_html():
    with open(os.path.join(WEB_DIR, "login.html"), "r", encoding="utf-8") as f:
        return f.read()


def load_panel_html():
    with open(os.path.join(WEB_DIR, "panel.html"), "r", encoding="utf-8") as f:
        return f.read()