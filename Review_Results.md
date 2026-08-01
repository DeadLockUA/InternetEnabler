# InternetEnabler — Code Review

**Reviewer:** rigorous review pass (read-only)
**Date:** 2026-08-01
**Scope:** entire repository (`client/`, `server/`, `tests/`, installers, docs, agent definitions) at commit `44d104d`
**Verification performed:** full test suite executed — `66 passed in 6.46s` (pytest 9.1.1, Python 3.12.10, Windows 11). All findings below were verified by reading the actual code path; status is marked `CONFIRMED` (proven from source / test run) or `PLAUSIBLE` (believed but not fully verifiable in this environment).

---

## Overall verdict

**Solid, well-structured, honest code — but not ready to push.** The architecture is appropriately small for a household tool (no over-engineering, consistent with `AGENT.md`'s trust-based scope), tests are genuinely meaningful (66 passing, sensible subprocess mocking), and the comments explain *why*, not *what*. However, there are **two confirmed scheduler bugs** (duplicate reminders for every block, and re-firing + task-progress wipe after an agent restart) that directly affect the product's core daily behavior, plus a confirmed IPv6 gap in the firewall block. These should be fixed before release.

---

## Strengths (verified)

- **Modular separation is right-sized.** `agent.py` (tray + HTTP + scheduler), `firewall.py` (netsh/PowerShell), `server.py` (CLI) — no unnecessary abstraction for a two-machine LAN tool.
- **Excellent intent-explaining comments.** e.g. `firewall.py:25-31` (why PowerShell instead of parsing localized `netsh` output), `install.ps1:38-41` (why the logged-on user is queried rather than `$env:USERNAME`), `scheduler_tick`'s `>=` rationale.
- **Test suite is meaningful, not tautological.** 66 tests, all green; HTTP handlers tested against a real live `ThreadingHTTPServer` (`test_agent.py:225-251`); firewall mocked at the subprocess boundary; server CLI tested end-to-end via argv monkeypatching.
- **Good defensive touches:** `is_valid_time` shared validation, history retention pruning (`agent.py:115-116`), `timeout=5` on server requests, `ensure_rules` reconciles existing rules so config edits take effect on restart (`firewall.py:55-59`).
- **Appropriately trust-based security posture.** LAN-scoped inbound allow rule, shared-token auth checked on every endpoint before any work, no TLS demanded where `AGENT.md` explicitly disclaims hardening. No scope creep toward enterprise features.
- **Missed-block recovery** (`>=` comparison in `scheduler_tick`) is the right intent — see F1 for the flaw in how it interacts with restarts.

---

## Findings — ranked by severity

### F1. CONFIRMED — High — Scheduler re-blocks and wipes task progress after an agent restart
**Where:** `client/agent.py:44` (`_fired_today`), `agent.py:297-301` (`scheduler_tick` block trigger)
**What:** `_fired_today` is an **in-memory** dict. It is never persisted. The block trigger uses `current_time >= entry` (string comparison, fine for zero-padded `HH:MM`), so on the first tick after *any* process start, an entry whose time has already passed today fires — even if it already fired and was confirmed hours ago.
**Impact:** Any agent restart today (update, crash, reboot, `uninstall`/`install`) after a block already fired causes it to **re-block the internet and reset all task `done` flags** (`reset_tasks_done()`), silently discarding the son's confirmed progress. The comment at `agent.py:295-296` acknowledges the `>=` is for "PC was asleep" recovery, but the implementation cannot distinguish "missed while asleep" from "already fired, restarted" because the fired-state isn't durable.
**Fix:** Persist `_fired_today` (e.g., a `state.json` next to `schedule.json`, or a per-day timestamp in each schedule entry), or recover the "already fired today" state from `history.json` / the block rule's existing state on startup.
**Same root cause:** `_reminder_fired` (line 45) is also in-memory, so after restart, duplicate reminders are sent for the remainder of the day.

---

### F2. CONFIRMED — High — Duplicate reminder notification for every scheduled block
**Where:** `client/agent.py:283-293` (the `for occurrence in (entry_today, entry_today + timedelta(days=1))` loop)
**What:** The loop iterates today's and tomorrow's occurrence so a midnight-crossing reminder window fires. But both occurrences are exactly 24h apart, so subtracting the same `reminder_minutes` yields two instants with the **same wall-clock `strftime("%H:%M")`**. Because `_reminder_fired` keys on `(entry, occurrence.date())` — a different key for each — **both** match `current_time` and **both** fire.
**Impact:** For every schedule entry, at the reminder time the son gets **two identical notifications** ("Internet will be blocked at 08:00." twice). 100% reproducible — this isn't an edge case. The test at `test_agent.py:185-202` only asserts `any(...)`, so it passes while both notifications fire.
**Verification trace:** `current_time="07:45"`, `entry="08:00"`, `reminder_minutes=15` →
- occurrence today (08:00 D): reminder 07:45 D → `strftime` `"07:45"` matches → fires `("08:00", D)`
- occurrence tomorrow (08:00 D+1): reminder 07:45 D+1 → `strftime` `"07:45"` matches → fires `("08:00", D+1)`

**Fix:** Only consider tomorrow's occurrence **if** today's reminder time is already in the past (i.e., the upcoming reminder belongs to tomorrow's block), e.g. gate the second occurrence on `reminder_dt` of today's occurrence being earlier than `now`.

---

### F3. CONFIRMED — High — IPv6 outbound traffic is not blocked (protocol-level bypass)
**Where:** `client/firewall.py:41-47` (`_wan_ranges`), built from `ipaddress.ip_network("0.0.0.0/0")`
**What:** `_wan_ranges` only models IPv4 (`0.0.0.0/0`) and the block rule's `remoteip=` list contains only IPv4 CIDRs. Windows defaults commonly have IPv6 enabled; the created `dir=out` block rule does not match IPv6 traffic at all.
**Impact:** On any host with working IPv6, "block internet" leaves IPv6 reachable — the feature's primary purpose is partially defeated. (Consistent with the trust-based model, this isn't about a hostile teenager — it's about the block actually working as described.) Additionally, if `lan_subnet` were ever an IPv6 network, `_wan_ranges` would raise `TypeError` (version mismatch with `0.0.0.0/0`) with no handling.
**Fix:** Also generate IPv6 WAN exclusions (e.g., `::/0` minus `lan_subnet` if IPv6) and/or add a `dir=out` IPv6 block rule. Validate `lan_subnet` version at startup.

---

### F4. CONFIRMED — Medium — `on_enable` confirmation flow races with a concurrent block
**Where:** `client/agent.py:318-336` (`on_enable`)
**What:** `load_tasks()` is read **outside** `_lock`, then `ask_yes_no(n)` dialogs run for seconds-to-minutes without any lock held, then `mark_task_done`/`disable_block` are done under the lock. In that window, a server `/block` or a scheduled `scheduler_tick` can fire `reset_tasks_done()` under the lock.
**Impact:** The confirmation loop shows stale tasks; a block arriving mid-flow can re-block and reset completion right after the user confirmed everything — then `on_enable`'s final `disable_block()` unblocks again, leaving a permanently inconsistent state (internet on while `_fired_today` says blocked, tasks reset). Narrow window, but it's the product's main interaction flow.
**Fix:** Hold the lock for the duration, or snapshot tasks under the lock and re-verify the set hasn't changed before `disable_block()`; or have `on_enable` yield to the scheduler and re-check `is_blocked()` before the final unblock.

---

### F5. CONFIRMED — Medium — Inconsistent locking and non-atomic JSON persistence across threads
**Where:** `client/agent.py` — `save_schedule` called in POST `/schedule` (line 236) **without** `_lock`, while `scheduler_tick` reads the schedule every 20 s without a lock; `save_tasks` in POST `/tasks` *is* under `_lock`; `on_enable`/`refresh_loop` touch the same files from the tray threads.
**What:** `json.dump` to the destination file is not atomic (no temp-file + rename). If a concurrent write truncates a file mid-write, readers (`scheduler_tick`, GET handlers) hit `json.JSONDecodeError`; the scheduler catches it silently (`run_scheduler`'s bare `except`), while GET handlers would 500/close the connection.
**Impact:** Low probability in a single-child household, but the code is inconsistent: some paths serialize with `_lock`, others don't, with no documented invariant. Reader/writer corruption is silent.
**Fix:** Take `_lock` on **every** read and write of the JSON files (or use a small single-threaded persistence helper), and write atomically (write temp → `os.replace`).

---

### F6. CONFIRMED — Medium — HTTP handler and thread errors are effectively invisible
**Where:** `client/agent.py:256-257` (`log_message` → `pass`), no `try/except` in `do_GET`/`do_POST`, `run_http_server` runs in a daemon thread with no error logging.
**What:** The agent is launched via `pythonw` (no console) by the Scheduled Task. `log_message` silences all request logging; unhandled exceptions in handler threads (e.g., malformed `history.json` → `datetime.fromisoformat` `ValueError` at `agent.py:210`, memory errors in `_read_json_body`) write only to stderr, which nobody sees.
**Impact:** When the agent misbehaves, diagnosis is near-impossible; the server CLI shows a generic "Could not reach the client". The scheduler thread at least uses `traceback.print_exc()` — the HTTP path doesn't even do that.
**Fix:** Route exceptions through the same `traceback.print_exc()`, or better, a rotating log file in `BASE_DIR`.

---

### F7. CONFIRMED — Medium — `is_blocked()` collapses all errors to "not blocked"
**Where:** `client/firewall.py:120-122`
**What:** Any nonzero PowerShell exit or any output other than exactly `"True"` returns `False`.
**Impact:** If the rule is missing, permissions are broken, or PowerShell fails, the agent reports **internet OK** even though the block may still be enforced (or the rule may be gone entirely, leaving no protection). Both the tray indicator and the server `/status` endpoint present a false "green".
**Fix:** Distinguish "rule exists and disabled" from "couldn't determine state"; on error, surface a warning state rather than `False`.

---

### F8. CONFIRMED — Medium — Installer continues after `pip install` failure and doesn't verify the token was changed
**Where:** `client/install.ps1:36` (pip exit code never checked), `install.ps1:20-24` (config copied/editable but content never validated)
**What:** (a) If `pip install -r requirements.txt` fails, the script still registers the Scheduled Task and starts the agent, which then crashes on missing `pystray`/`Pillow` at startup. (b) The script prompts to edit `config.json` but never verifies `token` isn't `CHANGE_ME_SHARED_SECRET` or that `lan_subnet` is a valid CIDR — a misconfigured install is launched silently.
**Impact:** First-run "it just doesn't work" with no guidance; default-token configs get deployed.
**Fix:** Check `$LASTEXITCODE` after pip and abort; validate `token != "CHANGE_ME_SHARED_SECRET"` and try to parse `lan_subnet` (e.g., poke `[ipaddress]::…` or a tiny `python -c` check) before launching.

---

### F9. CONFIRMED — Low/Medium — `uninstall.ps1` kills any Python process whose command line contains `agent.py`
**Where:** `client/uninstall.ps1:18-20`
**What:** `Where-Object { $_.CommandLine -like "*agent.py*" }` matches **any** python process with `agent.py` anywhere in its path (another project's daemon, a second agent install, etc.), not just this agent's exact path.
**Fix:** Match the full expected command line (`*InternetEnabler*agent.py*` or compare against the resolved `$agentPath`), or stop only the Scheduled Task session.

---

### F10. CONFIRMED — Low — Server CLI: missing config-field validation; accepts nonsensical `--days`
**Where:** `server/server.py:28-33` (`load_config`), `server.py:69` (`--days`)
**What:** `load_config` returns whatever JSON is there; `request()` then raises an unhandled `KeyError` (traceback) if `client_host`/`token`/`client_port` are absent. `history --days 0` and negative/absurd values are passed straight to the client filter (negative actually moves `cutoff` into the future → everything filtered → silent empty output).
**Fix:** Validate required keys and `days >= 1` with a friendly `sys.exit` message.

---

### F11. PLAUSIBLE — Low — `netsh remoteip` list length for large LAN exclusions
**Where:** `client/firewall.py:46`, `firewall.py:60`
**What:** `0.0.0.0/0` minus a `10.0.0.0/8` LAN yields ~128 CIDR entries joined with commas. Windows has a command-line length limit (~8 KB); very large exclusion lists may fail `netsh advfirewall … add rule` with an obscure error. The unit tests only validate the CIDR math (`test_wan_ranges_*`), never the actual `netsh` invocation with a long list, so this is unverified end-to-end.
**Impact:** Likely fine for typical `192.168.x.0/24` (2 ranges) but unproven for large subnets.
**Fix:** Smoke-test `ensure_rules` against a `/8` config on a real Windows box, or chunk/harden against failure.

---

### F12. PLAUSIBLE — Low — tkinter dialogs driven from pystray's callback thread
**Where:** `client/agent.py:129-164`, `agent.py:313-337`
**What:** `on_enable`/`on_view_tasks`/`on_set_reminder` create a `tk.Tk()` and show `messagebox`/`simpledialog` from within a pystray `MenuItem` callback. On Windows, pystray's callback may run on its own GUI thread while the main `icon.run()` pumps the message loop; creating a Tk root from a non-main thread can raise `RuntimeError: main thread is not in main loop` or deadlock, depending on the exact threading arrangement.
**Impact:** The "Enable Internet" (the core feature) and dialogs could fail or hang on some Windows setups. Not verifiable without a GUI session; hence PLAUSIBLE.
**Fix:** Marshal dialog calls to the main thread (e.g., a queue pumped by a timer on the tray icon), or restructure to run dialogs before/after `icon.run()`.

---

### F13. CONFIRMED — Low — Unbounded HTTP request body read
**Where:** `client/agent.py:181-185` (`_read_json_body`)
**What:** Reads `Content-Length` bytes with no upper bound. A misbehaving LAN device (or bug) can force the client to buffer arbitrarily large bodies — a trivial memory exhaustion vector on the agent.
**Fix:** Cap at e.g. 64 KB and reject larger with 413.

---

### F14. CONFIRMED — Low — `set-schedule --clear` silently ignores any provided times
**Where:** `server/server.py:84`
**What:** `times = [] if args.clear else args.times` — `server.py set-schedule --clear 20:30 21:00` drops the times without warning, and the user sees "Schedule set: []" with no indication why.
**Fix:** Error if `--clear` is combined with positional times.

---

### F15. CONFIRMED — Low — Misc minor issues
- **Duplicate-rule edge (`firewall.py:121`):** if two firewall rules share the display name, `(Get-NetFirewallRule …).Enabled` returns an array (`"True False"`), so `out == "True"` fails → `is_blocked()` reports false even though a block rule is enabled. Consider `.Enabled -contains $true`.
- **Plaintext token over HTTP (`server.py:36-49`, `agent.py:170-185`):** acknowledged by the trust model (any LAN sniffer, or the client's own account, can read it — by design the son is local admin), flagged only as documentation-level note: `X-Auth-Token` over cleartext HTTP is trivially sniffable on the LAN.
- **`0.0.0.0` bind (`agent.py:262`):** HTTP server is reachable on every adapter (VPNs, secondary networks); the inbound firewall rule scopes to `lan_subnet`, mitigating, but the rule follows the active Windows profile — worth a README note.
- **`requirements.txt` loose pins (`client/requirements.txt`):** `pystray>=0.19`, `Pillow>=10.0` — future major releases could break the agent; consider upper bounds.
- **History column alignment (`server.py:113`):** tasks containing tabs/newlines misalign the `>>  event  task` column. Cosmetic.
- **`test_run_scheduler_survives_exception_from_tick` (`test_agent.py:205-220`):** uses `raise SystemExit` as a loop terminator inside the mocked tick — clever, but brittle; a `while`-counter approach would be clearer.

---

## Test-quality assessment

**Good:** auth rejection tests (wrong token / missing header), live HTTP round-trips, schedule/time validation, firewall CIDR math incl. "covers everything else exactly once", server CLI argv coverage, `scheduler_tick` fire-once behavior, persistence round-trips. Error paths are tested (`ensure_rules` raises, `is_blocked` false on error).

**Gaps found during review:**

| Gap | Why it matters |
|---|---|
| No test for **restart persistence** (F1) | The re-block/wipe bug ships undetected |
| No test asserting **exactly one** reminder per block (F2) — the existing midnight test asserts `any(...)` so it misses the duplicate | The double-notification bug ships undetected |
| No test for **IPv6** exclusion behavior or a non-IPv4 `lan_subnet` (F3) | The bypass ships undetected |
| No test for **locking/race** behavior in `on_enable` vs concurrent `reset_tasks_done` (F4) | Race ships undetected |
| No test for malformed/corrupt `history.json` / `schedule.json` (F6, F5) | Crash/500 path untested |
| No test for installer scripts (pip failure exit code, token validation) (F8) | Install-time robustness untested |

---

## Process / repository notes

- **No CI configuration** (no `.github/`): a GitHub-hosted repo with a 66-test suite and a `test-runner` agent has no automated gate. Consider a minimal `pytest` GitHub Action.
- **No LICENSE file** despite being a public GitHub repo.
- **`AGENT.md`/`agents/*` are well-written and self-consistent** (TDD-first, read-only reviewer, proportionate security). The `quality-progress-reviewer` spec's verification discipline matches what this review attempted.
- Minor: `conftest.py` mutates `sys.path` globally (`sys.path.insert(0, …)`) — acceptable for a small suite, but a `pyproject.toml`/`pythonpath` setting would be cleaner.

---

## Suggested priority order

1. F2 (duplicate reminders) — one-line-ish fix, high daily-visible impact
2. F1 (restart re-block wipes task progress) — durability fix, high impact
3. F3 (IPv6 bypass) — feature correctness
4. F4 + F5 (locking/races) — robustness of the core flow
5. F6/F7 (invisible errors, false-green status) — debuggability
6. F8/F9/F10 (installer/CLI robustness)
7. F11–F15 (minor/plausible)

---

*Reviewer note: all findings are verified against the code at the stated commit; F11 and F12 are explicitly marked PLAUSIBLE because they depend on Windows runtime behavior not exercisable here. The test suite result (66 passed) is reproduced independently.*