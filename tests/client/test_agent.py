import http.client
import json
import os
import threading
import time
from datetime import datetime, timedelta

import pytest

import agent


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(agent, "SCHEDULE_PATH", str(tmp_path / "schedule.json"))
    monkeypatch.setattr(agent, "TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(agent, "HISTORY_PATH", str(tmp_path / "history.json"))
    monkeypatch.setattr(agent, "MESSAGES_PATH", str(tmp_path / "messages.json"))
    monkeypatch.setattr(agent, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(agent, "LOG_PATH", str(tmp_path / "agent.log"))
    agent._icon_ref["icon"] = None
    agent._icon_ref["blocked"] = None
    yield


# -- config/schedule/tasks/history persistence -------------------------------

def test_save_and_load_config():
    agent.save_config({"token": "abc", "port": 5987})
    assert agent.load_config() == {"token": "abc", "port": 5987}


def test_load_schedule_missing_file_returns_empty_list():
    assert agent.load_schedule() == []


def test_save_and_load_schedule():
    agent.save_schedule(["20:30", "21:00"])
    assert agent.load_schedule() == ["20:30", "21:00"]


def test_load_tasks_missing_file_returns_empty_list():
    assert agent.load_tasks() == []


def test_save_and_load_tasks():
    tasks = [{"id": 1, "text": "Homework", "done": False}]
    agent.save_tasks(tasks)
    assert agent.load_tasks() == tasks


def test_reset_tasks_done_clears_flags():
    agent.save_tasks([
        {"id": 1, "text": "A", "done": True},
        {"id": 2, "text": "B", "done": True},
    ])
    agent.reset_tasks_done()
    tasks = agent.load_tasks()
    assert all(t["done"] is False for t in tasks)


def test_reset_tasks_done_noop_when_no_tasks_file():
    agent.reset_tasks_done()  # should not raise
    assert agent.load_tasks() == []


def test_mark_task_done_marks_only_matching_id():
    agent.save_tasks([
        {"id": 1, "text": "A", "done": False},
        {"id": 2, "text": "B", "done": False},
    ])
    agent.mark_task_done(2)
    tasks = agent.load_tasks()
    assert tasks[0]["done"] is False
    assert tasks[1]["done"] is True


def test_load_history_missing_file_returns_empty_list():
    assert agent.load_history() == []


def test_append_history_appends_entries():
    agent.append_history("Homework", "completed")
    agent.append_history("Clean room", "skipped")
    entries = agent.load_history()
    assert len(entries) == 2
    assert entries[0]["task"] == "Homework"
    assert entries[0]["event"] == "completed"
    assert entries[1]["event"] == "skipped"
    assert "timestamp" in entries[0]


def test_append_history_prunes_entries_older_than_retention():
    old = (datetime.now() - timedelta(days=agent.HISTORY_RETENTION_DAYS + 10)).isoformat(timespec="seconds")
    with open(agent.HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump({"entries": [{"timestamp": old, "task": "Old", "event": "completed"}]}, f)

    agent.append_history("New", "completed")

    entries = agent.load_history()
    assert [e["task"] for e in entries] == ["New"]


# -- messages -------------------------------------------------------------

def test_load_messages_missing_file_returns_empty_list():
    assert agent.load_messages() == []


def test_append_message_inserts_newest_first_and_notifies(monkeypatch):
    notified = []
    monkeypatch.setattr(agent, "_icon_ref", {"icon": type("Fake", (), {"notify": lambda self, m, t: notified.append(m)})()})
    agent.append_message("Dinner ready")
    agent.append_message("Clean up")
    messages = agent.load_messages()
    assert [m["text"] for m in messages] == ["Clean up", "Dinner ready"]
    assert "timestamp" in messages[0]
    assert len(notified) == 2


def test_append_message_caps_at_max_messages():
    for i in range(agent.MAX_MESSAGES + 10):
        agent.append_message(f"msg {i}")
    messages = agent.load_messages()
    assert len(messages) == agent.MAX_MESSAGES
    assert messages[0]["text"] == f"msg {agent.MAX_MESSAGES + 9}"


def test_append_message_noop_notify_when_no_icon():
    agent._icon_ref["icon"] = None
    agent.append_message("hello")  # should not raise
    assert agent.load_messages()[0]["text"] == "hello"


# -- is_valid_time ------------------------------------------------------

def test_is_valid_time_accepts_zero_padded_24h():
    assert agent.is_valid_time("20:30") is True
    assert agent.is_valid_time("00:00") is True
    assert agent.is_valid_time("23:59") is True


def test_is_valid_time_rejects_bad_input():
    assert agent.is_valid_time("25:99") is False
    assert agent.is_valid_time("8:30") is False
    assert agent.is_valid_time("abcde") is False
    assert agent.is_valid_time(123) is False
    assert agent.is_valid_time(None) is False


# -- on_enable ------------------------------------------------------------

def test_on_enable_confirms_all_tasks_then_unblocks(monkeypatch):
    agent.save_tasks([
        {"id": 1, "text": "Homework", "done": False},
        {"id": 2, "text": "Clean room", "done": False},
    ])
    monkeypatch.setattr(agent.firewall, "is_blocked", lambda: True)
    disabled = []
    monkeypatch.setattr(agent.firewall, "disable_block", lambda: disabled.append(1))
    monkeypatch.setattr(agent, "ask_yes_no", lambda question: True)

    agent.on_enable(None, None)

    assert disabled == [1]
    assert all(t["done"] for t in agent.load_tasks())
    assert [e["event"] for e in agent.load_history()] == ["completed", "completed"]


def test_on_enable_stops_on_no_and_keeps_blocked(monkeypatch):
    agent.save_tasks([{"id": 1, "text": "Homework", "done": False}])
    monkeypatch.setattr(agent.firewall, "is_blocked", lambda: True)
    disabled = []
    monkeypatch.setattr(agent.firewall, "disable_block", lambda: disabled.append(1))
    monkeypatch.setattr(agent, "ask_yes_no", lambda question: False)
    shown = []
    monkeypatch.setattr(agent, "show_info", lambda title, message: shown.append(message))

    agent.on_enable(None, None)

    assert disabled == []
    assert agent.load_tasks()[0]["done"] is False
    assert agent.load_history()[0]["event"] == "skipped"
    assert shown


def test_on_enable_noop_when_already_unblocked(monkeypatch):
    monkeypatch.setattr(agent.firewall, "is_blocked", lambda: False)
    called = []
    monkeypatch.setattr(agent, "ask_yes_no", lambda question: called.append(1))

    agent.on_enable(None, None)

    assert called == []


def test_on_enable_proceeds_when_block_state_unknown(monkeypatch):
    # firewall.is_blocked() returning None means "couldn't determine state" (F7) -
    # on_enable must not treat that as "already unblocked" and silently skip.
    monkeypatch.setattr(agent.firewall, "is_blocked", lambda: None)
    monkeypatch.setattr(agent, "ask_yes_no", lambda question: True)
    agent.save_tasks([{"id": 1, "text": "Homework", "done": False}])
    disabled = []
    monkeypatch.setattr(agent.firewall, "disable_block", lambda: disabled.append(1))

    agent.on_enable(None, None)

    assert disabled == [1]


def test_on_enable_aborts_if_tasks_reset_during_confirmation(monkeypatch):
    # F4: a scheduled block firing mid-dialog resets all task "done" flags via
    # reset_tasks_done(). on_enable must notice this at the end and refuse to
    # unblock, instead of blindly disabling the block it just raced with.
    agent.save_tasks([
        {"id": 1, "text": "Already done earlier today", "done": True},
        {"id": 2, "text": "Homework", "done": False},
    ])
    monkeypatch.setattr(agent.firewall, "is_blocked", lambda: True)
    disabled = []
    monkeypatch.setattr(agent.firewall, "disable_block", lambda: disabled.append(1))

    def fake_ask(question):
        agent.reset_tasks_done()  # simulates a concurrent scheduler_tick firing
        return True

    monkeypatch.setattr(agent, "ask_yes_no", fake_ask)
    shown = []
    monkeypatch.setattr(agent, "show_info", lambda title, message: shown.append(message))

    agent.on_enable(None, None)

    assert disabled == []
    assert shown


def test_on_enable_aborts_if_task_list_replaced_with_empty_during_confirmation(monkeypatch):
    # M11: if the parent replaces the task list with an empty one mid-flow,
    # load_tasks() returning [] must NOT satisfy the final guard (that would
    # unblock without any task confirmation). Empty/unconfirmed = stay blocked.
    agent.save_tasks([{"id": 1, "text": "Homework", "done": False}])
    monkeypatch.setattr(agent.firewall, "is_blocked", lambda: True)
    disabled = []
    monkeypatch.setattr(agent.firewall, "disable_block", lambda: disabled.append(1))

    def fake_ask(question):
        agent.save_tasks([])  # parent replaced the list with an empty one
        return True

    monkeypatch.setattr(agent, "ask_yes_no", fake_ask)
    shown = []
    monkeypatch.setattr(agent, "show_info", lambda title, message: shown.append(message))

    agent.on_enable(None, None)

    assert disabled == []
    assert shown


# -- block-state cache (C1/M2/M4) ----------------------------------------

def test_blocked_state_cache_defaults_to_none():
    assert agent.get_blocked_state() is None


def test_set_blocked_state_updates_cache_even_without_icon(monkeypatch):
    agent._icon_ref["icon"] = None
    monkeypatch.setattr(agent, "make_icon_image", lambda value: value)
    agent.set_blocked_state(True)
    assert agent.get_blocked_state() is True


def test_set_blocked_state_repaints_icon_best_effort(monkeypatch):
    calls = []

    class FakeIcon:
        def __init__(self):
            self.icon = None
        def update_menu(self):
            calls.append("menu")

    fake = FakeIcon()
    agent._icon_ref["icon"] = fake
    monkeypatch.setattr(agent, "make_icon_image", lambda value: ("img", value))

    agent.set_blocked_state(False)

    assert fake.icon == ("img", False)
    assert calls == ["menu"]
    assert agent.get_blocked_state() is False


def test_set_blocked_state_tolerates_icon_exception(monkeypatch):
    class BoomIcon:
        @property
        def icon(self):
            return None

        @icon.setter
        def icon(self, value):
            raise RuntimeError("boom")

    agent._icon_ref["icon"] = BoomIcon()
    monkeypatch.setattr(agent, "make_icon_image", lambda value: value)
    agent.set_blocked_state(True)  # must not raise
    assert agent.get_blocked_state() is True


# -- scheduler_tick ---------------------------------------------------------

def test_scheduler_tick_ignores_invalid_schedule_entries():
    agent.save_schedule(["25:99"])
    agent.save_config({"token": "t", "port": 5987})
    agent.scheduler_tick(datetime(2026, 1, 1, 12, 0))  # should not raise


def test_scheduler_tick_fires_missed_schedule_and_only_once(monkeypatch):
    agent.save_schedule(["08:00"])
    agent.save_config({"token": "t", "port": 5987})
    enabled = []
    monkeypatch.setattr(agent.firewall, "enable_block", lambda: enabled.append(1))

    agent.scheduler_tick(datetime(2026, 1, 1, 8, 5))  # PC was asleep at 08:00, woke up at 08:05
    assert enabled == [1]

    agent.scheduler_tick(datetime(2026, 1, 1, 8, 6))  # must not refire same day
    assert enabled == [1]


def test_scheduler_tick_reminder_crosses_midnight(monkeypatch):
    agent.save_schedule(["00:10"])
    agent.save_config({"token": "t", "port": 5987, "reminder_minutes": 15})
    monkeypatch.setattr(agent.firewall, "enable_block", lambda: None)

    class FakeIcon:
        def __init__(self):
            self.notified = []

        def notify(self, message, title):
            self.notified.append(message)

    fake_icon = FakeIcon()
    agent._icon_ref["icon"] = fake_icon

    agent.scheduler_tick(datetime(2026, 1, 1, 23, 55))  # evening before the 00:10 block

    # F2: exactly one notification, not two (was firing for both today's and
    # tomorrow's occurrence since they share the same HH:MM wall-clock string).
    matching = [m for m in fake_icon.notified if "00:10" in m]
    assert len(matching) == 1


def test_scheduler_tick_reminder_fires_exactly_once_normal_case(monkeypatch):
    agent.save_schedule(["08:00"])
    agent.save_config({"token": "t", "port": 5987, "reminder_minutes": 15})
    monkeypatch.setattr(agent.firewall, "enable_block", lambda: None)

    class FakeIcon:
        def __init__(self):
            self.notified = []

        def notify(self, message, title):
            self.notified.append(message)

    fake_icon = FakeIcon()
    agent._icon_ref["icon"] = fake_icon

    agent.scheduler_tick(datetime(2026, 1, 1, 7, 45))  # 15 min before 08:00

    assert len(fake_icon.notified) == 1


def test_scheduler_tick_state_survives_simulated_restart(monkeypatch):
    # F1: a restarted process has no in-memory history, only whatever was
    # persisted. Reload state fresh from disk (simulating "process restarted")
    # and make sure the block does not refire / wipe task progress again.
    agent.save_schedule(["08:00"])
    agent.save_config({"token": "t", "port": 5987})
    agent.save_tasks([{"id": 1, "text": "Homework", "done": False}])
    enabled = []
    monkeypatch.setattr(agent.firewall, "enable_block", lambda: enabled.append(1))

    agent.scheduler_tick(datetime(2026, 1, 1, 8, 5))
    assert enabled == [1]

    state = agent.load_state()
    assert "08:00" in state["fired"]

    agent.mark_task_done(1)  # son confirmed his task after the block fired

    # "restart": nothing in memory carries over, scheduler_tick only ever
    # reads/writes state.json, so this reproduces a fresh process.
    agent.scheduler_tick(datetime(2026, 1, 1, 9, 0))

    assert enabled == [1]  # must not refire
    assert agent.load_tasks()[0]["done"] is True  # must not wipe progress


def test_scheduler_tick_state_resets_on_new_day(monkeypatch):
    agent.save_schedule(["08:00"])
    agent.save_config({"token": "t", "port": 5987})
    enabled = []
    monkeypatch.setattr(agent.firewall, "enable_block", lambda: enabled.append(1))

    agent.scheduler_tick(datetime(2026, 1, 1, 8, 5))
    agent.scheduler_tick(datetime(2026, 1, 2, 8, 5))

    assert enabled == [1, 1]


def test_run_scheduler_survives_exception_from_tick(monkeypatch):
    calls = []

    def fake_tick(now=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        raise SystemExit  # stop the infinite loop once we've proven it kept going

    monkeypatch.setattr(agent, "scheduler_tick", fake_tick)
    monkeypatch.setattr(agent.time, "sleep", lambda seconds: None)

    with pytest.raises(SystemExit):
        agent.run_scheduler()

    assert len(calls) == 2


# -- HTTP command handler ------------------------------------------------

class LiveServer:
    def __init__(self, config):
        agent.CommandHandler.config = config
        self.config = config
        self.httpd = agent.ThreadingHTTPServer(("127.0.0.1", 0), agent.CommandHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def request(self, method, path, token="secret", body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if token is not None:
            headers["X-Auth-Token"] = token
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, payload

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(agent.firewall, "is_blocked", lambda: False)
    monkeypatch.setattr(agent.firewall, "enable_block", lambda: None)
    monkeypatch.setattr(agent.firewall, "disable_block", lambda: None)
    srv = LiveServer({"token": "secret", "port": 0})
    yield srv
    srv.stop()


def test_status_returns_null_when_block_state_unknown(server, monkeypatch):
    monkeypatch.setattr(agent.firewall, "is_blocked", lambda: None)
    status, payload = server.request("GET", "/status")
    assert status == 200
    assert payload == {"blocked": None}


def test_post_rejects_oversized_body(server):
    status, payload = server.request(
        "POST", "/tasks", body={"tasks": ["x" * (agent.MAX_BODY_BYTES + 1)]}
    )
    assert status == 413


def _raw_post(server, path, raw_headers, body=b""):
    """POST with arbitrary raw headers (bypasses the json helper so invalid
    Content-Length values can be sent literally)."""
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.putrequest("POST", path)
    conn.putheader("X-Auth-Token", "secret")
    for k, v in raw_headers:
        conn.putheader(k, v)
    conn.endheaders()
    if body:
        conn.send(body)
    resp = conn.getresponse()
    payload = json.loads(resp.read().decode("utf-8"))
    conn.close()
    return resp.status, payload


def test_post_rejects_non_numeric_content_length(server):
    # M7: a non-numeric Content-Length must be a clean 400, not a logged 500.
    status, payload = _raw_post(server, "/tasks", [("Content-Length", "abc")])
    assert status == 400
    assert payload == {"error": "invalid Content-Length"}


def test_post_rejects_negative_content_length(server):
    # M7: read(-1) can read until EOF (unbounded memory) - must be rejected.
    status, payload = _raw_post(server, "/tasks", [("Content-Length", "-1")])
    assert status == 400
    assert payload == {"error": "invalid Content-Length"}


def test_post_accepts_valid_content_length(server):
    body = b'{"tasks": []}'
    status, payload = _raw_post(
        server, "/tasks", [("Content-Type", "application/json"),
                           ("Content-Length", str(len(body)))],
        body=body,
    )
    assert status == 200
    assert payload == {"tasks": []}


def test_get_history_recovers_from_corrupt_file(server):
    # M5: a corrupt/truncated JSON file must not permanently take down the
    # scheduler or 500 every API call - fall back to defaults and log a warning.
    with open(agent.HISTORY_PATH, "w", encoding="utf-8") as f:
        f.write("{not valid json")

    status, payload = server.request("GET", "/history")

    assert status == 200
    assert payload == {"entries": []}
    with open(agent.LOG_PATH, "r", encoding="utf-8") as f:
        log_contents = f.read()
    assert "history" in log_contents.lower()


def test_status_requires_auth(server):
    status, payload = server.request("GET", "/status", token="wrong")
    assert status == 401
    assert payload == {"error": "unauthorized"}


def test_status_requires_auth_header_present(server):
    status, payload = server.request("GET", "/status", token=None)
    assert status == 401


def test_status_ok(server):
    status, payload = server.request("GET", "/status")
    assert status == 200
    assert payload == {"blocked": False}


def test_unknown_get_path_404(server):
    status, payload = server.request("GET", "/nope")
    assert status == 404


def test_unknown_post_path_404(server):
    status, payload = server.request("POST", "/nope")
    assert status == 404


def test_block_and_unblock(server):
    status, payload = server.request("POST", "/block")
    assert status == 200
    assert payload == {"blocked": True}

    status, payload = server.request("POST", "/unblock")
    assert status == 200
    assert payload == {"blocked": False}


def test_block_resets_task_done_flags(server, monkeypatch):
    calls = []
    monkeypatch.setattr(agent, "reset_tasks_done", lambda: calls.append(1))
    server.request("POST", "/block")
    assert calls == [1]


def test_schedule_round_trip(server):
    status, payload = server.request("POST", "/schedule", body={"times": ["20:30", "21:00"]})
    assert status == 200
    assert payload == {"times": ["20:30", "21:00"]}

    status, payload = server.request("GET", "/schedule")
    assert status == 200
    assert payload == {"times": ["20:30", "21:00"]}


def test_schedule_rejects_invalid_time(server):
    status, payload = server.request("POST", "/schedule", body={"times": ["25:99"]})
    assert status == 400
    assert agent.load_schedule() == []


def test_tasks_round_trip(server):
    status, payload = server.request("POST", "/tasks", body={"tasks": ["Homework", "Clean room"]})
    assert status == 200
    assert payload["tasks"] == [
        {"id": 1, "text": "Homework", "done": False},
        {"id": 2, "text": "Clean room", "done": False},
    ]

    status, payload = server.request("GET", "/tasks")
    assert status == 200
    assert payload["tasks"][0]["text"] == "Homework"


def test_history_filters_by_days(server, monkeypatch):
    from datetime import datetime, timedelta

    old = (datetime.now() - timedelta(days=40)).isoformat(timespec="seconds")
    recent = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    with open(agent.HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump({"entries": [
            {"timestamp": old, "task": "Old", "event": "completed"},
            {"timestamp": recent, "task": "Recent", "event": "completed"},
        ]}, f)

    status, payload = server.request("GET", "/history?days=30")
    assert status == 200
    tasks = [e["task"] for e in payload["entries"]]
    assert tasks == ["Recent"]


def test_history_default_days(server):
    status, payload = server.request("GET", "/history")
    assert status == 200
    assert payload == {"entries": []}


def test_history_days_clamped_to_range(server, monkeypatch):
    # M8: negative/absurd days must be clamped, not produce empty results or
    # an OverflowError->500 from timedelta.
    old = (datetime.now() - timedelta(days=350)).isoformat(timespec="seconds")
    recent = (datetime.now() - timedelta(hours=12)).isoformat(timespec="seconds")
    with open(agent.HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump({"entries": [
            {"timestamp": old, "task": "Old", "event": "completed"},
            {"timestamp": recent, "task": "Recent", "event": "completed"},
        ]}, f)

    # days=-30 clamps to 1 -> only within the last day
    status, payload = server.request("GET", "/history?days=-30")
    assert status == 200
    assert [e["task"] for e in payload["entries"]] == ["Recent"]

    # absurd days clamp to HISTORY_RETENTION_DAYS instead of OverflowError
    status, payload = server.request("GET", f"/history?days={10**15}")
    assert status == 200
    tasks = [e["task"] for e in payload["entries"]]
    assert tasks == ["Old", "Recent"]


# -- web panel (login / sessions / api) ----------------------------------

def test_login_sets_session_cookie(server):
    server.config["web_password"] = "family"
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    body = json.dumps({"password": "family"}).encode("utf-8")
    conn.request("POST", "/login", body=body, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    payload = json.loads(resp.read().decode("utf-8"))
    cookies = resp.getheader("Set-Cookie", "")
    conn.close()
    assert resp.status == 200
    assert payload == {"ok": True}
    assert "ie_session=" in cookies


def test_login_wrong_password_rejected(server):
    server.config["web_password"] = "family"
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    body = json.dumps({"password": "wrong"}).encode("utf-8")
    conn.request("POST", "/login", body=body, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    payload = json.loads(resp.read().decode("utf-8"))
    conn.close()
    assert resp.status == 401
    assert payload["ok"] is False


def test_login_not_configured(server):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    body = json.dumps({"password": "x"}).encode("utf-8")
    conn.request("POST", "/login", body=body, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    payload = json.loads(resp.read().decode("utf-8"))
    conn.close()
    assert resp.status == 401
    assert "not configured" in payload["error"].lower()


def test_root_serves_login_page(server):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.request("GET", "/")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    assert resp.status == 200
    assert "<html" in body.lower()
    assert "password" in body.lower()


def test_panel_requires_session(server):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.request("GET", "/panel")
    resp = conn.getresponse()
    conn.close()
    assert resp.status == 302
    assert resp.getheader("Location") == "/"


def test_panel_served_with_valid_session(server):
    server.config["web_password"] = "family"
    connto = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    body = json.dumps({"password": "family"}).encode("utf-8")
    connto.request("POST", "/login", body=body, headers={"Content-Type": "application/json"})
    resp = connto.getresponse()
    cookie = resp.getheader("Set-Cookie", "").split(";")[0]
    resp.read()
    connto.close()

    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.request("GET", "/panel", headers={"Cookie": cookie})
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    assert resp.status == 200
    assert "<html" in body.lower()


def test_api_requires_auth_without_session(server):
    status, payload = server.request("GET", "/api/status", token=None)
    assert status == 401


def test_api_works_with_session(server):
    server.config["web_password"] = "family"
    connto = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    body = json.dumps({"password": "family"}).encode("utf-8")
    connto.request("POST", "/login", body=body, headers={"Content-Type": "application/json"})
    resp = connto.getresponse()
    cookie = resp.getheader("Set-Cookie", "").split(";")[0]
    resp.read()
    connto.close()

    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.request("GET", "/api/status", headers={"Cookie": cookie})
    resp = conn.getresponse()
    payload = json.loads(resp.read().decode("utf-8"))
    conn.close()
    assert resp.status == 200
    assert payload == {"blocked": False}


def test_api_block_with_session_resets_tasks(server, monkeypatch):
    server.config["web_password"] = "family"
    connto = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    body = json.dumps({"password": "family"}).encode("utf-8")
    connto.request("POST", "/login", body=body, headers={"Content-Type": "application/json"})
    resp = connto.getresponse()
    cookie = resp.getheader("Set-Cookie", "").split(";")[0]
    resp.read()
    connto.close()
    calls = []
    monkeypatch.setattr(agent, "reset_tasks_done", lambda: calls.append(1))

    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.request("POST", "/api/block", headers={"Cookie": cookie})
    resp = conn.getresponse()
    payload = json.loads(resp.read().decode("utf-8"))
    conn.close()
    assert resp.status == 200
    assert payload == {"blocked": True}
    assert calls == [1]


def test_messages_round_trip(server):
    status, payload = server.request("POST", "/api/messages", body={"text": "Dinner ready"})
    assert status == 200
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["text"] == "Dinner ready"

    status, payload = server.request("GET", "/api/messages")
    assert status == 200
    assert payload["messages"][0]["text"] == "Dinner ready"


def test_messages_reject_empty_or_too_long(server):
    status, payload = server.request("POST", "/api/messages", body={"text": "   "})
    assert status == 400
    status, payload = server.request(
        "POST", "/api/messages", body={"text": "x" * (agent.MAX_MESSAGE_CHARS + 1)}
    )
    assert status == 400


def test_logout_invalidates_session(server):
    server.config["web_password"] = "family"
    connto = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    body = json.dumps({"password": "family"}).encode("utf-8")
    connto.request("POST", "/login", body=body, headers={"Content-Type": "application/json"})
    resp = connto.getresponse()
    cookie = resp.getheader("Set-Cookie", "").split(";")[0]
    resp.read()
    connto.close()

    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.request("POST", "/logout", headers={"Cookie": cookie})
    resp = conn.getresponse()
    resp.read()
    conn.close()

    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.request("GET", "/api/status", headers={"Cookie": cookie})
    resp = conn.getresponse()
    conn.close()
    assert resp.status == 401


# -- startup logging -------------------------------------------------------

def test_log_writes_timestamped_line():
    agent.log("hello world")
    with open(agent.LOG_PATH, "r", encoding="utf-8") as f:
        contents = f.read()
    assert "hello world" in contents
    assert contents.split()[0].count("-") == 2  # ISO date prefix e.g. 2026-08-03


def test_log_rotates_when_over_limit():
    # M10: agent.log must not grow without bound.
    with open(agent.LOG_PATH, "w", encoding="utf-8") as f:
        f.write("x" * (agent.MAX_LOG_BYTES + 1))
    agent.log("after rotation")
    assert os.path.exists(agent.LOG_PATH + ".1")
    with open(agent.LOG_PATH, "r", encoding="utf-8") as f:
        contents = f.read()
    assert "after rotation" in contents


def test_main_crashes_logs_traceback_and_exits_nonzero(monkeypatch):
    # The whole point of the main() wrapper: a crash during startup (bad
    # config, firewall failure, ...) must land in agent.log as a traceback
    # instead of dying silently with exit code 1 under pythonw.
    def boom():
        raise RuntimeError("config bomb")

    monkeypatch.setattr(agent, "_main", boom)

    with pytest.raises(SystemExit) as excinfo:
        agent.main()

    assert excinfo.value.code == 1
    with open(agent.LOG_PATH, "r", encoding="utf-8") as f:
        contents = f.read()
    assert "main crashed" in contents
    assert "config bomb" in contents
    assert "Traceback" in contents


def test_main_logs_startup_milestones(monkeypatch):
    # A healthy startup should record the milestones so operators can see how
    # far the agent got. run_tray normally blocks forever, so stop the fake
    # startup by raising SystemExit(0) from it - SystemExit propagates through
    # main()'s except Exception handler unchanged (graceful termination, no
    # error logging, exit code 0).
    agent.save_config({
        "token": "t",
        "web_password": "p",
        "lan_subnet": "192.168.1.0/24",
        "port": 5999,
    })
    monkeypatch.setattr(agent.firewall, "ensure_rules", lambda lan_subnet, port: None)

    def fake_tray(config):
        raise SystemExit(0)

    monkeypatch.setattr(agent, "run_tray", fake_tray)
    # Don't actually bind a socket or start threads in the unit test.
    monkeypatch.setattr(agent, "ThreadingHTTPServer", lambda *a, **k: type("S", (), {"server_address": (None, 5999)})())
    monkeypatch.setattr(agent.threading.Thread, "start", lambda self: None)

    with pytest.raises(SystemExit) as excinfo:
        agent.main()
    assert excinfo.value.code == 0

    with open(agent.LOG_PATH, "r", encoding="utf-8") as f:
        contents = f.read()
    assert "Starting InternetEnabler agent" in contents
    assert "config.json loaded" in contents
    assert "firewall rules ensured" in contents
    assert "HTTP server listening" in contents
    assert "scheduler thread started" in contents


def test_http_server_bind_failure_logged(monkeypatch):
    # Binding the HTTP server is done synchronously in _main() so a port
    # conflict is reported clearly instead of killing a background thread.
    agent.save_config({
        "token": "t",
        "web_password": "p",
        "lan_subnet": "192.168.1.0/24",
        "port": 5999,
    })
    monkeypatch.setattr(agent.firewall, "ensure_rules", lambda lan_subnet, port: None)

    def fake_bind(*args, **kwargs):
        raise OSError("address already in use")

    monkeypatch.setattr(agent, "ThreadingHTTPServer", fake_bind)

    with pytest.raises(SystemExit) as excinfo:
        agent.main()

    assert excinfo.value.code == 1
    with open(agent.LOG_PATH, "r", encoding="utf-8") as f:
        contents = f.read()
    assert "cannot bind HTTP server" in contents
    assert "address already in use" in contents


def test_main_rejects_out_of_range_port(monkeypatch):
    # M9: an out-of-range port must fail with the clear "invalid port" message
    # instead of an OverflowError traceback from ThreadingHTTPServer.
    agent.save_config({
        "token": "t",
        "web_password": "p",
        "lan_subnet": "192.168.1.0/24",
        "port": 99999,
    })

    with pytest.raises(SystemExit) as excinfo:
        agent.main()

    assert excinfo.value.code == 1
    with open(agent.LOG_PATH, "r", encoding="utf-8") as f:
        contents = f.read()
    assert "invalid port" in contents
