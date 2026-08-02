# API Contract — Client (web panel + HTTP API)

Frozen interface. Agents code against THIS, not against each other.
Do not change signatures without updating this file + all consumers.

The agent on the son's PC serves both the web panel and a JSON API from a
single HTTP server (`0.0.0.0:{port}`, default 5987). No server-side
component exists anymore — any browser on the LAN is the only UI needed.

## Authentication

Two interchangeable methods:

1. **`X-Auth-Token` header** — the shared secret `token` from
   `client/config.json` (used by automation/tests and the legacy raw API paths).
2. **Web session cookie** — obtained via `POST /login` (validates the family
   password `web_password`), stored in an `ie_session` cookie
   (`HttpOnly; SameSite=Strict; Path=/`), 30-minute sliding expiry,
   5 failed logins per IP lock the IP out for 5 minutes. `POST /logout`
   invalidates the session.

Both are checked for every API request — either one alone grants access.

## Pages (HTML, no JS build step)

| Path | Description |
|------|-------------|
| `GET /` | Login page. Redirect target after a successful `POST /login`. |
| `GET /panel` | Main web panel. Redirects (302) to `/` if no valid session. |

## JSON API

API routes are prefixed `/api/` and additionally mirror the legacy raw
paths (`/status`, `/schedule`, `/tasks`, `/history`, `/block`, `/unblock`)
which accept only the `X-Auth-Token` header. Everything below uses either
auth method.

| Method | Path | Body | Response |
|--------|------|------|----------|
| POST | `/login` | `{"password": str}` | 200 `{"ok": true}` + `Set-Cookie`; 401 `{"ok": false, "error": str, "retry_after": int?}` |
| POST | `/logout` | — | 200 `{}` + cookie cleared |
| GET | `/api/status` | — | `{"blocked": bool \| null}` (null = firewall state unknown) |
| POST | `/api/block` | — | `{"blocked": true}` (also resets task completion) |
| GET | `/api/schedule` | — | `{"times": ["HH:MM", ...]}` |
| POST | `/api/schedule` | `{"times": ["HH:MM", ...]}` | `{"times": [...]}` (400 on invalid time) |
| GET | `/api/tasks` | — | `{"tasks": [{"id": int, "text": str, "done": bool}]}` |
| POST | `/api/tasks` | `{"tasks": [str, ...]}` | `{"tasks": [...]}` (full replace, resets completion) |
| GET | `/api/history?days=N` | — | `{"entries": [{"timestamp": iso, "task": str, "event": "completed"\|"skipped"}]}` |
| GET | `/api/messages` | — | `{"messages": [{"timestamp": iso, "text": str}]}` (newest first, max 100) |
| POST | `/api/messages` | `{"text": str}` | `{"messages": [...]}` (400 if empty or > 2000 chars) |

## Notes

- `GET /panel` and all `/api/` routes return 401 when unauthenticated.
- `POST /unblock` exists only on the raw path (token-only) **and is not
  offered by the web panel** — unblocking stays a deliberate action on the
  son's tray icon after every task is confirmed.
- Messages arrive on the son's PC as a tray notification and are listed in
  the tray menu ("View Messages").
- Body size is capped at 64 KiB (413 on exceed); messages are capped at
  100 entries and 2000 chars each.