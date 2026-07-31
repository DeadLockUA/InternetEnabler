# InternetEnabler

A simple tool for household chores: internet on the son's computer gets
disabled on a schedule (or manually from the parent's computer) via a
Windows Firewall rule. Turning it back on takes one click on the tray
icon — a deliberate action, so "I forgot" doesn't work as an excuse.

Both computers must be on the same local network. The parent's computer
can reach the son's computer, so all communication flows
"server → client": the server sends commands to the client.

## How it works

- **client** — runs on the son's computer. Auto-starts at login with
  administrator rights (via Scheduled Task, without a UAC prompt every
  time). Blocks outgoing internet traffic through Windows Firewall, but
  doesn't touch traffic within the local network — so the server can still
  reach it even when internet is blocked. Tray icon (red = blocked,
  green = OK) with an "Enable Internet" button.
- **server** — a simple CLI on the parent's computer that sends commands
  to the client over HTTP (protected by a shared secret token).

## Setup

### 1. Client (son's computer)

Requires [Python 3](https://www.python.org/downloads/) installed.

```powershell
cd client
copy config.example.json config.json
notepad config.json   # fill in token and lan_subnet (e.g. 192.168.1.0/24)
```

Run `install.ps1` **from PowerShell as Administrator** — this will
create the firewall rules and the auto-start task:

```powershell
.\install.ps1
```

The script installs dependencies (`pystray`, `Pillow`) itself, sets up
auto-start, and launches the agent right away.

### 2. Server (your computer)

```powershell
cd server
copy config.example.json config.json
notepad config.json   # same token, client_host = son's computer IP, client_port = 5987
```

It's best to fix the son's computer IP via a DHCP reservation on the
router so it doesn't change.

## Usage

```powershell
python server.py status
python server.py block
python server.py unblock
python server.py set-schedule 20:30 21:00
python server.py set-schedule --clear
```

`set-schedule` sets the time(s) at which the client will enable the
block itself — this works even if your computer is turned off.

## Important details

- The token (`token` in config.json) must match on the client and
  server — it's the only protection against unauthorized commands over
  the local network.
- If the client and server are on different subnets/VLANs, `lan_subnet`
  in `client/config.json` needs to be set correctly, otherwise the
  server won't be able to reach the client after it's blocked either.
- This is a household tool based on trust, not protection against a
  tech-savvy teenager: administrator access on their own computer still
  allows removing the firewall rules or stopping the agent via Task
  Manager.
