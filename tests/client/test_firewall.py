import ipaddress

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


def _fake_run(returncode, stdout, stderr=""):
    def run(args):
        return returncode, stdout, stderr
    return run


def test_rule_exists_true(monkeypatch):
    monkeypatch.setattr(firewall, "_run", _fake_run(0, "Rule Name: X\nEnabled: Yes"))
    assert firewall._rule_exists("X") is True


def test_rule_exists_false_when_no_match(monkeypatch):
    monkeypatch.setattr(firewall, "_run", _fake_run(0, "No rules match the specified criteria."))
    assert firewall._rule_exists("X") is False


def test_rule_exists_false_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(firewall, "_run", _fake_run(1, "", "error"))
    assert firewall._rule_exists("X") is False


def test_ensure_rules_skips_creation_when_rules_present(monkeypatch):
    calls = []
    monkeypatch.setattr(firewall, "_rule_exists", lambda name: True)

    def run(args):
        calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(firewall, "_run", run)
    firewall.ensure_rules("192.168.1.0/24", 5987)
    assert calls == []


def test_ensure_rules_creates_missing_rules(monkeypatch):
    monkeypatch.setattr(firewall, "_rule_exists", lambda name: False)
    calls = []

    def run(args):
        calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(firewall, "_run", run)
    firewall.ensure_rules("192.168.1.0/24", 5987)

    assert len(calls) == 2
    block_call, inbound_call = calls
    assert f'name="{firewall.BLOCK_RULE_NAME}"' in block_call
    assert "dir=out" in block_call
    assert "action=block" in block_call
    assert f'name="{firewall.INBOUND_RULE_NAME}"' in inbound_call
    assert "dir=in" in inbound_call
    assert "localport=5987" in inbound_call


def test_ensure_rules_raises_on_failure(monkeypatch):
    monkeypatch.setattr(firewall, "_rule_exists", lambda name: False)
    monkeypatch.setattr(firewall, "_run", _fake_run(1, "", "boom"))
    with pytest.raises(RuntimeError):
        firewall.ensure_rules("192.168.1.0/24", 5987)


def test_enable_block_success(monkeypatch):
    monkeypatch.setattr(firewall, "_run", _fake_run(0, "", ""))
    firewall.enable_block()  # should not raise


def test_enable_block_failure(monkeypatch):
    monkeypatch.setattr(firewall, "_run", _fake_run(1, "", "denied"))
    with pytest.raises(RuntimeError):
        firewall.enable_block()


def test_disable_block_failure(monkeypatch):
    monkeypatch.setattr(firewall, "_run", _fake_run(1, "", "denied"))
    with pytest.raises(RuntimeError):
        firewall.disable_block()


def test_is_blocked_true(monkeypatch):
    monkeypatch.setattr(firewall, "_run", _fake_run(0, "Rule Name: X\nEnabled: Yes\n"))
    assert firewall.is_blocked() is True


def test_is_blocked_false_when_disabled(monkeypatch):
    monkeypatch.setattr(firewall, "_run", _fake_run(0, "Rule Name: X\nEnabled: No\n"))
    assert firewall.is_blocked() is False


def test_is_blocked_false_on_error(monkeypatch):
    monkeypatch.setattr(firewall, "_run", _fake_run(1, "", "no such rule"))
    assert firewall.is_blocked() is False
