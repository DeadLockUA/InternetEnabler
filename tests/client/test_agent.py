import http.client
import json
import threading
import time

import pytest

import agent


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(agent, "SCHEDULE_PATH", str(tmp_path / "schedule.json"))
    monkeypatch.setattr(agent, "TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(agent, "HISTORY_PATH", str(tmp_path / "history.json"))
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
