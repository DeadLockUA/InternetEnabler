import web_ui


def test_create_session_returns_unique_tokens_and_validates():
    sid1 = web_ui.create_session(now=1000.0)
    sid2 = web_ui.create_session(now=1000.0)
    assert sid1 != sid2
    assert web_ui.is_valid_session(sid1, now=1000.0) is True


def test_session_expires_after_ttl_of_inactivity():
    sid = web_ui.create_session(now=1000.0)
    # one valid use slides the expiry forward; a full TTL of inactivity expires it
    assert web_ui.is_valid_session(sid, now=1000.0) is True
    assert web_ui.is_valid_session(sid, now=1000.0 + web_ui.SESSION_TTL_SECONDS + 1) is False
    assert sid not in web_ui._sessions


def test_session_sliding_renewal():
    sid = web_ui.create_session(now=1000.0)
    web_ui.is_valid_session(sid, now=1000.0 + web_ui.SESSION_TTL_SECONDS - 1)
    # a valid use pushes the expiry forward, so it survives a full TTL more
    assert web_ui.is_valid_session(sid, now=1000.0 + web_ui.SESSION_TTL_SECONDS * 2 - 1) is True


def test_invalid_or_missing_session_rejected():
    assert web_ui.is_valid_session(None, now=0) is False
    assert web_ui.is_valid_session("", now=0) is False
    assert web_ui.is_valid_session("does-not-exist", now=0) is False


def test_delete_session_removes_it():
    sid = web_ui.create_session(now=0)
    web_ui.delete_session(sid)
    assert web_ui.is_valid_session(sid, now=0) is False


def test_check_login_success_clears_failures():
    config = {"web_password": "family"}
    web_ui._failures["1.2.3.4"] = {"count": 2, "window_start": 0}
    result = web_ui.check_login("1.2.3.4", "family", config, now=0)
    assert result == {"ok": True}
    assert "1.2.3.4" not in web_ui._failures


def test_check_login_wrong_password_counts_failure():
    config = {"web_password": "family"}
    result = web_ui.check_login("1.2.3.4", "wrong", config, now=0)
    assert result["ok"] is False
    assert "attempts left" in result["error"]
    assert web_ui._failures["1.2.3.4"]["count"] == 1


def test_check_login_locks_out_after_five_failures():
    config = {"web_password": "family"}
    ip = "1.2.3.4"
    for _ in range(5):
        result = web_ui.check_login(ip, "wrong", config, now=0)
    assert result["ok"] is False
    assert result["retry_after"] is not None
    # even the correct password is rejected while locked out
    result = web_ui.check_login(ip, "family", config, now=0)
    assert result["ok"] is False
    assert "too many" in result["error"].lower()


def test_check_login_failure_window_resets():
    config = {"web_password": "family"}
    ip = "1.2.3.4"
    for _ in range(5):
        web_ui.check_login(ip, "wrong", config, now=0)
    result = web_ui.check_login(ip, "family", config, now=web_ui.FAILURE_WINDOW_SECONDS)
    assert result["ok"] is True


def test_check_login_window_start_does_not_slide_on_failures():
    # M3: window_start must stay fixed at the FIRST failure. Sliding it on
    # every attempt would let a sustained attacker extend the lockout forever.
    config = {"web_password": "family"}
    ip = "10.0.0.5"
    first = 100
    for _ in range(5):
        web_ui.check_login(ip, "wrong", config, now=first)
    assert web_ui._failures[ip]["window_start"] == first
    assert web_ui._failures[ip]["count"] >= web_ui.LOCKOUT_THRESHOLD

    # Lockout persists for the whole window measured from the FIRST failure,
    # even with later attempts in between (before the window elapses).
    result = web_ui.check_login(ip, "family", config, now=first + 100)
    assert result["ok"] is False
    assert "too many" in result["error"].lower()
    assert web_ui._failures[ip]["window_start"] == first

    # Once the full window from the first failure has elapsed, it resets.
    result = web_ui.check_login(
        ip, "family", config, now=first + web_ui.FAILURE_WINDOW_SECONDS
    )
    assert result["ok"] is True
    assert ip not in web_ui._failures


def test_create_session_prunes_expired_entries():
    # M6: creating a new session must sweep expired sessions so random LAN
    # cookie submissions can't grow _sessions without bound.
    before = set(web_ui._sessions)
    stale = web_ui.create_session(now=1000.0)
    web_ui._sessions[stale] = {"expires": 500.0}  # force-expire it
    stale2 = web_ui.create_session(now=1000.0)
    web_ui._sessions[stale2] = {"expires": 500.0}

    fresh = web_ui.create_session(now=2000.0)

    assert stale not in web_ui._sessions
    assert stale2 not in web_ui._sessions
    assert fresh in web_ui._sessions
    # Only the pre-existing live sessions + the fresh one remain.
    assert set(web_ui._sessions) == before | {fresh}


def test_check_login_returns_error_when_not_configured():
    result = web_ui.check_login("1.2.3.4", "anything", {"token": "t"}, now=0)
    assert result["ok"] is False
    assert "not configured" in result["error"].lower()
    assert "web_password" in result["error"]


def test_html_pages_load():
    login = web_ui.load_login_html()
    panel = web_ui.load_panel_html()
    assert "<html" in login.lower()
    assert "<html" in panel.lower()
    assert "password" in login.lower()