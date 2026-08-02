# InternetEnabler

A single-software household tool: the son's PC runs a small agent that
disables its own internet on a schedule (or instantly on request) via a
Windows Firewall rule, and serves a password-protected web panel for the
whole family. Any device in the house — phone, tablet, laptop — can set
the schedule, assign tasks, or send him a message. Turning the internet
back on takes one click on his tray icon — a deliberate action, so
"I forgot" doesn't work as an excuse.

All communication is LAN-only. No server-side software is installed on
anyone else's computer anymore.

## How it works

- **agent** — runs on the son's computer. Auto-starts at login with
  administrator rights (via Scheduled Task, without a UAC prompt every
  time). Blocks outgoing internet traffic through Windows Firewall, but
  doesn't touch traffic within the local network — so the web panel stays
  reachable even when internet is blocked. Three parts:
  - **HTTP server + web panel** — a login-protected page (`/panel`) with
    tabs for Status, Schedule, Tasks, History and Messages, reachable from
    any browser on the LAN at `http://<son-pc-ip>:5987`. A family password
    (`web_password` in config.json) guards it; the raw `X-Auth-Token` API
    still works for automation.
  - **Tray icon** (red = blocked, green = OK) with:
    - **Enable Internet** — if there are pending tasks, asks "Was
      '<task>' complete?" one by one (Yes/No). Answering No stops
      immediately and internet stays blocked. Internet is only enabled
      once every task is confirmed done. Each block cycle resets tasks
      to not-done.
    - **View Tasks** — shows the current task list and completion status.
    - **View Messages** — inbox for messages sent from the web panel.
    - **Set Reminder Time...** — how many minutes before a scheduled
      block a tray notification should warn the son.
  - **Scheduler** — enforces daily block times even if no one touches the
    panel; fires missed blocks after sleep/restart.

## Setup (son's computer only)

Requires [Python 3](https://www.python.org/downloads/) installed.

```powershell
cd client
copy config.example.json config.json
notepad config.json   # fill in token, web_password and lan_subnet (e.g. 192.168.1.0/24)
```

Run `install.ps1` **from PowerShell as Administrator** — this will
create the firewall rules and the auto-start task:

```powershell
.\install.ps1
```

The script installs dependencies (`pystray`, `Pillow`) itself, sets up
auto-start, and launches the agent right away.

Alternatively, double-click `install.bat` — it re-launches itself
elevated (UAC prompt) and then runs `install.ps1` for you, so no manual
"Run as Administrator" step is needed.

It's best to fix the son's computer IP via a DHCP reservation on the
router so the web panel address doesn't change.

## Configuring `config.json`

`config.json` sits next to the agent scripts in the `client` folder. It
is a plain JSON file with the following settings:

| Field              | Example                  | Purpose                                                                  |
| ------------------ | ------------------------ | ------------------------------------------------------------------------ |
| `token`            | `CHANGE_ME_SHARED_SECRET`| Shared secret for API calls (`X-Auth-Token` header)                      |
| `web_password`     | `CHANGE_ME_FAMILY_PASSWORD` | Password for the web panel (`http://<son-pc-ip>:<port>`) login        |
| `port`             | `5987`                   | TCP port the web panel and API server listen on                          |
| `lan_subnet`       | `192.168.1.0/24`         | IPv4 CIDR of your local network                                          |
| `reminder_minutes` | `15`                     | Minutes before a scheduled block that the tray shows a warning           |

All fields are required. The file must be valid JSON — double-check quotes
and commas if the agent fails to start after editing.

**`token`** — any long random string (for example
`openssl rand -hex 32` on your own machine). It is sent in the
`X-Auth-Token` HTTP header in every API request from the web panel and
from any script that talks to the agent over the LAN. Anyone who knows it
can view and change the schedule and tasks.

**`web_password`** — the family password shown on the login page of the
web panel. If it is empty or missing, logins are rejected with
"web_password not configured on the client".

**`port`** — the TCP port of the agent's HTTP server. The web panel is
then reachable at `http://<son-pc-ip>:<port>`. If you change it after the
first install, restart the agent — the inbound firewall rule is reconciled
against the new port automatically.

**`lan_subnet`** — the IPv4 CIDR block of your local network, usually the
subnet of your router (e.g. `192.168.1.0/24`). It is used for two things:

- The outbound block rule allows traffic *to this subnet* even when
  internet is blocked, so the web panel keeps working from phones,
  tablets and laptops in the house.
- The inbound firewall rule only lets devices in this subnet reach the
  web panel at all.

If it is wrong, either the web panel becomes unreachable while internet
is blocked, or LAN devices are blocked from the panel. It must be IPv4 —
IPv6 is blocked outright while the block is active. Changing it after the
first install only takes effect after the agent restarts.

**`reminder_minutes`** — how many minutes before a scheduled block the
tray icon shows a warning notification (`0` disables the warning). This
can also be changed any time from the tray menu via **Set Reminder
Time...**.

After editing `config.json`, restart the agent (or just re-run
`install.ps1`) for the changes to take effect.

## Usage

Open `http://<son-pc-ip>:5987` from any browser on the LAN and log in
with the family password. Tabs:

- **Status** — current block state (BLOCKED / OK / UNKNOWN), Refresh,
  **Block internet now**.
- **Schedule** — set daily block times (one `HH:MM` per line).
- **Tasks** — replace the son's task list (full replace, resets
  completion) and see which are done.
- **History** — task completion log (completed/skipped) for the last N
  days, e.g. for a monthly review.
- **Messages** — send a message; it pops up as a tray notification on the
  son's PC and stays in his tray menu.

Scheduled blocks work even if no one is viewing the panel. The web panel
offers **block** only — unblocking happens on the son's side on purpose
(see tray icon above).

## Uninstalling

Run `uninstall.ps1` **from PowerShell as Administrator** (or double-click
`uninstall.bat`, which elevates itself automatically):

```powershell
cd client
.\uninstall.ps1
```

This stops the running agent, removes the `InternetEnablerAgent`
scheduled task, and deletes the `InternetEnabler-Block` /
`InternetEnabler-Inbound` firewall rules. It leaves `config.json`,
`schedule.json`, `tasks.json`, `history.json`, `messages.json` and
`state.json` in place — delete the `client` folder yourself if you want
those gone too.

## Important details

- `token` and `web_password` are the only protection against
  unauthorized commands over the local network. The token is sent as a
  plain `X-Auth-Token` HTTP header (no TLS), and the web panel transmits
  the password over plain HTTP too — anyone sniffing the local network
  could read them. Acceptable given the household trust model above, but
  don't reuse these credentials anywhere that matters.
- If the son's PC is on a different subnet/VLAN than your device,
  `lan_subnet` in `config.json` needs to be set correctly, otherwise the
  web panel won't be reachable after the block is enabled either.
- This is a household tool based on trust, not protection against a
  tech-savvy teenager: administrator access on their own computer still
  allows removing the firewall rules or stopping the agent via Task
  Manager.
- The agent's HTTP server listens on `0.0.0.0` (every network adapter,
  including VPNs), not just the LAN. The inbound firewall rule scopes
  incoming connections to `lan_subnet`, but that rule only applies to
  whichever Windows network profile is currently active - keep
  `lan_subnet` accurate if the machine has multiple networks.
- `lan_subnet` must be an IPv4 CIDR. The outbound block also includes a
  separate rule that blocks all IPv6 traffic outright (there's no IPv6
  LAN exclusion to configure), so IPv6-only devices on the LAN won't be
  reachable from the client while blocked.