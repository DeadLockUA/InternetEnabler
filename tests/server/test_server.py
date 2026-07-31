import json
import sys

import pytest
import urllib.error

import server


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "token": "secret",
        "client_host": "192.168.1.50",
        "client_port": 5987,
    }))
    monkeypatch.setattr(server, "CONFIG_PATH", str(path))
    return path


# -- load_config ------------------------------------------------------------

def test_load_config_missing_file_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CONFIG_PATH", str(tmp_path / "missing.json"))
    with pytest.raises(SystemExit):
        server.load_config()


def test_load_config_reads_json(config_file):
    config = server.load_config()
    assert config["token"] == "secret"
    assert config["client_port"] == 5987


# -- request() ----------------------------------------------------------

class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_request_builds_url_and_headers(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=5):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["token"] = req.get_header("X-auth-token")
        return FakeResponse({"ok": True})

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    config = {"client_host": "1.2.3.4", "client_port": 9999, "token": "tok"}
    result = server.request(config, "GET", "/status")

    assert captured["url"] == "http://1.2.3.4:9999/status"
    assert captured["method"] == "GET"
    assert captured["token"] == "tok"
    assert result == {"ok": True}


def test_request_sends_json_body(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=5):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["content_type"] = req.get_header("Content-type")
        return FakeResponse({"times": ["20:30"]})

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    config = {"client_host": "1.2.3.4", "client_port": 9999, "token": "tok"}
    server.request(config, "POST", "/schedule", {"times": ["20:30"]})

    assert captured["body"] == {"times": ["20:30"]}
    assert captured["content_type"] == "application/json"


def test_request_http_error_exits(monkeypatch):
    def fake_urlopen(req, timeout=5):
        raise urllib.error.HTTPError(
            req.full_url, 401, "unauthorized", hdrs=None, fp=__import__("io").BytesIO(b'{"error":"unauthorized"}')
        )

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    config = {"client_host": "1.2.3.4", "client_port": 9999, "token": "bad"}
    with pytest.raises(SystemExit):
        server.request(config, "GET", "/status")


def test_request_url_error_exits(monkeypatch):
    def fake_urlopen(req, timeout=5):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    config = {"client_host": "1.2.3.4", "client_port": 9999, "token": "tok"}
    with pytest.raises(SystemExit):
        server.request(config, "GET", "/status")


# -- CLI (main) -----------------------------------------------------------

def run_cli(monkeypatch, config_file, argv, responses):
    """responses: dict mapping (method, path) -> payload to return from server.request"""
    calls = []

    def fake_request(config, method, path, body=None):
        calls.append((method, path, body))
        key = (method, path.split("?")[0])
        return responses.get(key, {})

    monkeypatch.setattr(server, "request", fake_request)
    monkeypatch.setattr(sys, "argv", ["server.py"] + argv)
    server.main()
    return calls


def test_cli_status_ok(monkeypatch, config_file, capsys):
    run_cli(monkeypatch, config_file, ["status"], {("GET", "/status"): {"blocked": False}})
    assert "OK (internet allowed)" in capsys.readouterr().out


def test_cli_status_blocked(monkeypatch, config_file, capsys):
    run_cli(monkeypatch, config_file, ["status"], {("GET", "/status"): {"blocked": True}})
    assert "BLOCKED" in capsys.readouterr().out


def test_cli_block(monkeypatch, config_file, capsys):
    calls = run_cli(monkeypatch, config_file, ["block"], {})
    assert calls == [("POST", "/block", None)]
    assert "Internet blocked." in capsys.readouterr().out


def test_cli_unblock(monkeypatch, config_file, capsys):
    calls = run_cli(monkeypatch, config_file, ["unblock"], {})
    assert calls == [("POST", "/unblock", None)]


def test_cli_set_schedule_valid(monkeypatch, config_file, capsys):
    calls = run_cli(
        monkeypatch, config_file, ["set-schedule", "20:30", "21:00"],
        {("POST", "/schedule"): {"times": ["20:30", "21:00"]}},
    )
    assert calls == [("POST", "/schedule", {"times": ["20:30", "21:00"]})]


def test_cli_set_schedule_invalid_format_exits(monkeypatch, config_file):
    monkeypatch.setattr(sys, "argv", ["server.py", "set-schedule", "8:30"])
    with pytest.raises(SystemExit):
        server.main()


def test_cli_set_schedule_clear(monkeypatch, config_file):
    calls = run_cli(
        monkeypatch, config_file, ["set-schedule", "--clear"],
        {("POST", "/schedule"): {"times": []}},
    )
    assert calls == [("POST", "/schedule", {"times": []})]


def test_cli_set_tasks(monkeypatch, config_file, capsys):
    calls = run_cli(
        monkeypatch, config_file, ["set-tasks", "Homework", "Clean room"],
        {("POST", "/tasks"): {"tasks": [
            {"id": 1, "text": "Homework", "done": False},
            {"id": 2, "text": "Clean room", "done": False},
        ]}},
    )
    assert calls == [("POST", "/tasks", {"tasks": ["Homework", "Clean room"]})]
    assert "Homework" in capsys.readouterr().out


def test_cli_get_tasks_empty(monkeypatch, config_file, capsys):
    run_cli(monkeypatch, config_file, ["get-tasks"], {("GET", "/tasks"): {"tasks": []}})
    assert "No tasks assigned." in capsys.readouterr().out


def test_cli_get_tasks_lists_done_state(monkeypatch, config_file, capsys):
    run_cli(monkeypatch, config_file, ["get-tasks"], {("GET", "/tasks"): {"tasks": [
        {"id": 1, "text": "Homework", "done": True},
        {"id": 2, "text": "Clean room", "done": False},
    ]}})
    out = capsys.readouterr().out
    assert "[x] Homework" in out
    assert "[ ] Clean room" in out


def test_cli_history_empty(monkeypatch, config_file, capsys):
    run_cli(monkeypatch, config_file, ["history"], {("GET", "/history"): {"entries": []}})
    assert "No history." in capsys.readouterr().out


def test_cli_history_default_days(monkeypatch, config_file):
    calls = run_cli(monkeypatch, config_file, ["history"], {("GET", "/history"): {"entries": []}})
    assert calls == [("GET", "/history?days=30", None)]


def test_cli_history_custom_days(monkeypatch, config_file):
    calls = run_cli(monkeypatch, config_file, ["history", "--days", "7"], {("GET", "/history"): {"entries": []}})
    assert calls == [("GET", "/history?days=7", None)]
