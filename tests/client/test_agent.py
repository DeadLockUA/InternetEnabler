import http.client
import json
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
    agent._fired_today.clear()
    agent._reminder_fired.clear()
    agent._icon_ref["icon"] = None
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

    assert any("00:10" in m for m in fake_icon.notified)


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
