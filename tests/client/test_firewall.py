import ipaddress
import subprocess
import sys

import pytest

import firewall


def test_wan_ranges_excludes_lan_subnet():
    ranges = firewall._wan_ranges("192.168.1.0/24")
    lan = ipaddress.ip_network("192.168.1.0/24")
    for cidr in ranges:
        net = ipaddress.ip_network(cidr)
        assert not net.overlaps(lan)


def test_wan_ranges_cover_everything_else():
    ranges = [ipaddress.ip_network(c) for c in firewall._wan_ranges("10.0.0.0/8")]
    # spot-check addresses outside the LAN are covered by exactly one returned network
    for probe in ["1.1.1.1", "192.168.1.1", "255.255.255.254"]:
        addr = ipaddress.ip_address(probe)
        matches = [n for n in ranges if addr in n]
        assert len(matches) == 1, probe


def test_wan_ranges_lan_untouched(monkeypatch):
    ranges = [ipaddress.ip_network(c) for c in firewall._wan_ranges("10.0.0.0/8")]
    lan_addr = ipaddress.ip_address("10.5.5.5")
    assert not any(lan_addr in n for n in ranges)


def test_wan_ranges_rejects_ipv6_lan_subnet():
    with pytest.raises(ValueError):
        firewall._wan_ranges("2001:db8::/32")


def _fake_run(returncode, stdout, stderr=""):
    def run(args):
        return returncode, stdout, stderr
    return run


def _fake_run_ps(returncode, stdout, stderr=""):
    def run(command):
        return returncode, stdout, stderr
    return run


def test_rule_exists_true(monkeypatch):
    monkeypatch.setattr(firewall, "_run_ps", _fake_run_ps(0, "True"))
    assert firewall._rule_exists("X") is True


def test_rule_exists_false_when_no_match(monkeypatch):
    monkeypatch.setattr(firewall, "_run_ps", _fake_run_ps(0, "False"))
    assert firewall._rule_exists("X") is False


def test_rule_exists_false_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(firewall, "_run_ps", _fake_run_ps(1, "", "error"))
    assert firewall._rule_exists("X") is False


def test_ensure_rules_reconciles_when_rules_present(monkeypatch):
    calls = []
    monkeypatch.setattr(firewall, "_rule_exists", lambda name: True)

    def run(args):
        calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(firewall, "_run", run)
    firewall.ensure_rules("192.168.1.0/24", 5987)

    assert len(calls) == 3
    block_call, block_v6_call, inbound_call = calls
    assert "set" in block_call
    assert f'name="{firewall.BLOCK_RULE_NAME}"' in block_call
    assert "set" in block_v6_call
    assert f'name="{firewall.BLOCK_RULE_NAME_V6}"' in block_v6_call
    assert "set" in inbound_call
    assert f'name="{firewall.INBOUND_RULE_NAME}"' in inbound_call
    assert "localport=5987" in inbound_call


def test_ensure_rules_creates_missing_rules(monkeypatch):
    monkeypatch.setattr(firewall, "_rule_exists", lambda name: False)
    calls = []

    def run(args):
        calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(firewall, "_run", run)
    firewall.ensure_rules("192.168.1.0/24", 5987)

    assert len(calls) == 3
    block_call, block_v6_call, inbound_call = calls
    assert f'name="{firewall.BLOCK_RULE_NAME}"' in block_call
    assert "dir=out" in block_call
    assert "action=block" in block_call
    assert f'name="{firewall.BLOCK_RULE_NAME_V6}"' in block_v6_call
    assert "dir=out" in block_v6_call
    assert "action=block" in block_v6_call
    assert "remoteip=::/1,8000::/1" in block_v6_call
    assert f'name="{firewall.INBOUND_RULE_NAME}"' in inbound_call
    assert "dir=in" in inbound_call
    assert "localport=5987" in inbound_call


def test_ensure_rules_raises_on_failure(monkeypatch):
    monkeypatch.setattr(firewall, "_rule_exists", lambda name: False)
    monkeypatch.setattr(firewall, "_run", _fake_run(1, "", "boom"))
    with pytest.raises(RuntimeError):
        firewall.ensure_rules("192.168.1.0/24", 5987)


def test_ensure_rules_rejects_ipv6_lan_subnet(monkeypatch):
    monkeypatch.setattr(firewall, "_rule_exists", lambda name: False)
    monkeypatch.setattr(firewall, "_run", _fake_run(0, "", ""))
    with pytest.raises(ValueError):
        firewall.ensure_rules("2001:db8::/32", 5987)


def test_enable_block_success(monkeypatch):
    monkeypatch.setattr(firewall, "_run", _fake_run(0, "", ""))
    firewall.enable_block()  # should not raise


def test_enable_block_toggles_both_v4_and_v6_rules(monkeypatch):
    calls = []

    def run(args):
        calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(firewall, "_run", run)
    firewall.enable_block()

    assert len(calls) == 2
    assert f'name="{firewall.BLOCK_RULE_NAME}"' in calls[0]
    assert f'name="{firewall.BLOCK_RULE_NAME_V6}"' in calls[1]
    assert all("enable=yes" in c for c in calls)


def test_disable_block_toggles_both_v4_and_v6_rules(monkeypatch):
    calls = []

    def run(args):
        calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(firewall, "_run", run)
    firewall.disable_block()

    assert len(calls) == 2
    assert all("enable=no" in c for c in calls)


def test_enable_block_failure(monkeypatch):
    monkeypatch.setattr(firewall, "_run", _fake_run(1, "", "denied"))
    with pytest.raises(RuntimeError):
        firewall.enable_block()


def test_disable_block_failure(monkeypatch):
    monkeypatch.setattr(firewall, "_run", _fake_run(1, "", "denied"))
    with pytest.raises(RuntimeError):
        firewall.disable_block()


def test_is_blocked_true(monkeypatch):
    monkeypatch.setattr(firewall, "_run_ps", _fake_run_ps(0, "True"))
    assert firewall.is_blocked() is True


def test_is_blocked_false_when_disabled(monkeypatch):
    monkeypatch.setattr(firewall, "_run_ps", _fake_run_ps(0, "False"))
    assert firewall.is_blocked() is False


def test_is_blocked_none_on_nonzero_exit(monkeypatch):
    # F7: a PowerShell failure means "couldn't determine state", not "not
    # blocked" - collapsing it to False would report a false "internet OK".
    monkeypatch.setattr(firewall, "_run_ps", _fake_run_ps(1, "", "no such rule"))
    assert firewall.is_blocked() is None


def test_is_blocked_none_on_unexpected_output(monkeypatch):
    monkeypatch.setattr(firewall, "_run_ps", _fake_run_ps(0, "True False"))
    assert firewall.is_blocked() is None


# -- C1: subprocesses must never flash a console window --------------------

def test_run_uses_create_no_window_flag(monkeypatch, tmp_path):
    seen = {}

    def spy_run(args, **kwargs):
        seen.update(kwargs)
        raise SystemExit  # stop before actually spawning a process

    monkeypatch.setattr(subprocess, "run", spy_run)
    monkeypatch.setattr(sys, "platform", "win32")
    try:
        firewall._run(["show", "rule"])
    except SystemExit:
        pass
    assert seen.get("creationflags") == firewall.CREATE_NO_WINDOW


def test_run_ps_uses_create_no_window_flag(monkeypatch):
    seen = {}

    def spy_run(args, **kwargs):
        seen.update(kwargs)
        raise SystemExit  # stop before actually spawning a process

    monkeypatch.setattr(subprocess, "run", spy_run)
    monkeypatch.setattr(sys, "platform", "win32")
    try:
        firewall._run_ps("Write-Output 1")
    except SystemExit:
        pass
    assert seen.get("creationflags") == firewall.CREATE_NO_WINDOW


def test_create_no_window_flag_is_zero_on_non_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    import importlib
    importlib.reload(firewall)
    try:
        assert firewall.CREATE_NO_WINDOW == 0
    finally:
        monkeypatch.setattr(sys, "platform", "win32")
        importlib.reload(firewall)
