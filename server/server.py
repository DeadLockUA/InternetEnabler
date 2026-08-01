"""InternetEnabler server CLI.

Controls the client agent running on the son's PC over the LAN.

Usage:
    python server.py status
    python server.py block
    python server.py unblock
    python server.py set-schedule 20:30 21:00
    python server.py set-schedule --clear
    python server.py set-tasks "Homework" "Clean room" "Walk dog"
    python server.py get-tasks
    python server.py history --days 30
"""

import argparse
import json
import os
import sys
from datetime import datetime
import urllib.request
import urllib.error

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


REQUIRED_CONFIG_FIELDS = ("client_host", "client_port", "token")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit(f"config.json not found. Copy config.example.json to config.json and edit it first.")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    missing = [field for field in REQUIRED_CONFIG_FIELDS if field not in config]
    if missing:
        sys.exit(f"config.json is missing required field(s): {', '.join(missing)}")
    return config


def request(config, method, path, body=None):
    url = f"http://{config['client_host']}:{config['client_port']}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Auth-Token", config["token"])
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit(f"Client returned an error ({e.code}): {e.read().decode('utf-8', 'ignore')}")
    except urllib.error.URLError as e:
        sys.exit(f"Could not reach the client at {config['client_host']}:{config['client_port']} ({e.reason})")


def main():
    parser = argparse.ArgumentParser(description="Control the InternetEnabler client.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show whether internet is currently blocked")
    sub.add_parser("block", help="Block internet now")
    sub.add_parser("unblock", help="Unblock internet now")

    sched = sub.add_parser("set-schedule", help="Set daily block times (HH:MM, 24h)")
    sched.add_argument("times", nargs="*", help="e.g. 20:30 21:00")
    sched.add_argument("--clear", action="store_true", help="Clear the schedule")

    tasks_parser = sub.add_parser("set-tasks", help="Set the son's task list")
    tasks_parser.add_argument("tasks", nargs="*", help='e.g. "Homework" "Clean room"')

    sub.add_parser("get-tasks", help="Show the son's task list and completion status")

    hist_parser = sub.add_parser("history", help="Show task completion history")
    hist_parser.add_argument("--days", type=int, default=30, help="How many days back (default 30)")

    args = parser.parse_args()

    if args.command == "set-schedule" and args.clear and args.times:
        sys.exit("--clear cannot be combined with explicit times")
    if args.command == "history" and args.days < 1:
        sys.exit(f"--days must be at least 1, got {args.days}")

    config = load_config()

    if args.command == "status":
        result = request(config, "GET", "/status")
        print("BLOCKED" if result["blocked"] else "OK (internet allowed)")
    elif args.command == "block":
        request(config, "POST", "/block")
        print("Internet blocked.")
    elif args.command == "unblock":
        request(config, "POST", "/unblock")
        print("Internet unblocked.")
    elif args.command == "set-schedule":
        times = [] if args.clear else args.times
        for t in times:
            if len(t) != 5 or t[2] != ":":
                sys.exit(f"Invalid time format: {t!r}, expected HH:MM")
            try:
                datetime.strptime(t, "%H:%M")
            except ValueError:
                sys.exit(f"Invalid time: {t!r}, expected a valid 24h HH:MM")
        result = request(config, "POST", "/schedule", {"times": times})
        print(f"Schedule set: {result['times']}")
    elif args.command == "set-tasks":
        result = request(config, "POST", "/tasks", {"tasks": args.tasks})
        print(f"Tasks set: {[t['text'] for t in result['tasks']]}")
    elif args.command == "get-tasks":
        result = request(config, "GET", "/tasks")
        tasks = result.get("tasks", [])
        if not tasks:
            print("No tasks assigned.")
        else:
            for t in tasks:
                mark = "x" if t.get("done") else " "
                print(f"[{mark}] {t['text']}")
    elif args.command == "history":
        result = request(config, "GET", f"/history?days={args.days}")
        entries = result.get("entries", [])
        if not entries:
            print("No history.")
        else:
            for e in entries:
                task = e["task"].replace("\t", " ").replace("\n", " ")
                print(f"{e['timestamp']}  {e['event']:>9}  {task}")


if __name__ == "__main__":
    main()
