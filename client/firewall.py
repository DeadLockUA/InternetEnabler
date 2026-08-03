"""Windows Firewall control for InternetEnabler client.

Blocking is implemented as an outbound-block rule scoped to everything
EXCEPT the local LAN subnet, so the parent's server can still reach this
machine (and this machine can still answer) while internet access is cut.
"""

import ipaddress
import subprocess
import sys

BLOCK_RULE_NAME = "InternetEnabler-Block"
BLOCK_RULE_NAME_V6 = "InternetEnabler-Block-IPv6"
INBOUND_RULE_NAME = "InternetEnabler-Inbound"

# The agent runs under pythonw.exe (no console). Spawning a console app like
# netsh/powershell without this flag makes Windows flash a new console window
# for every call; CREATE_NO_WINDOW keeps those subprocesses invisible.
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _run(args):
    result = subprocess.run(
        ["netsh"] + args,
        capture_output=True,
        text=True,
        shell=False,
        creationflags=CREATE_NO_WINDOW,
    )
    return result.returncode, result.stdout, result.stderr


def _run_ps(command):
    """Run a PowerShell expression and return (returncode, stripped stdout, stderr).

    Used instead of parsing netsh's human-readable output, which is localized
    (e.g. "Enabled: Yes" is not "Enabled: Yes" on a non-English Windows install)
    and would silently misreport rule state on such a machine.
    """
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        shell=False,
        creationflags=CREATE_NO_WINDOW,
    )
    return result.returncode, result.stdout.strip(), result.stderr


def _wan_ranges(lan_subnet):
    """Return the list of IPv4 CIDR blocks covering 0.0.0.0/0 minus lan_subnet."""
    lan = ipaddress.ip_network(lan_subnet, strict=False)
    if lan.version != 4:
        raise ValueError(f"lan_subnet must be an IPv4 network, got {lan_subnet!r}")
    full = ipaddress.ip_network("0.0.0.0/0")
    remaining = full.address_exclude(lan)
    ranges = sorted(remaining, key=lambda n: n.network_address)
    return [str(n) for n in ranges]


def _rule_exists(name):
    code, out, _ = _run_ps(f"[bool](Get-NetFirewallRule -DisplayName '{name}' -ErrorAction SilentlyContinue)")
    return code == 0 and out == "True"


def ensure_rules(lan_subnet, port):
    """Create the block and inbound-allow rules if missing, or reconcile their
    remoteip/port against the current config if they already exist (so editing
    lan_subnet/port in config.json after first install takes effect on restart
    instead of being silently ignored forever).

    lan_subnet is IPv4-only (validated by _wan_ranges), so there's no LAN
    exclusion to compute for IPv6 - a separate rule blocks all outbound IPv6
    unconditionally. Without this, "block internet" left IPv6 traffic
    completely unaffected on any host with IPv6 enabled."""
    remote_ip = ",".join(_wan_ranges(lan_subnet))
    if not _rule_exists(BLOCK_RULE_NAME):
        code, out, err = _run([
            "advfirewall", "firewall", "add", "rule",
            f'name="{BLOCK_RULE_NAME}"',
            "dir=out",
            "action=block",
            "enable=no",
            f"remoteip={remote_ip}",
        ])
        if code != 0:
            raise RuntimeError(f"Failed to create block rule: {err or out}")
    else:
        code, out, err = _run([
            "advfirewall", "firewall", "set", "rule",
            f'name="{BLOCK_RULE_NAME}"', "new", f"remoteip={remote_ip}",
        ])
        if code != 0:
            raise RuntimeError(f"Failed to update block rule: {err or out}")

    if not _rule_exists(BLOCK_RULE_NAME_V6):
        code, out, err = _run([
            "advfirewall", "firewall", "add", "rule",
            f'name="{BLOCK_RULE_NAME_V6}"',
            "dir=out",
            "action=block",
            "enable=no",
            "remoteip=::/1,8000::/1",
        ])
        if code != 0:
            raise RuntimeError(f"Failed to create IPv6 block rule: {err or out}")
    else:
        code, out, err = _run([
            "advfirewall", "firewall", "set", "rule",
            f'name="{BLOCK_RULE_NAME_V6}"', "new", "remoteip=::/1,8000::/1",
        ])
        if code != 0:
            raise RuntimeError(f"Failed to update IPv6 block rule: {err or out}")

    if not _rule_exists(INBOUND_RULE_NAME):
        code, out, err = _run([
            "advfirewall", "firewall", "add", "rule",
            f'name="{INBOUND_RULE_NAME}"',
            "dir=in",
            "action=allow",
            "protocol=TCP",
            f"localport={port}",
            f"remoteip={lan_subnet}",
        ])
        if code != 0:
            raise RuntimeError(f"Failed to create inbound rule: {err or out}")
    else:
        code, out, err = _run([
            "advfirewall", "firewall", "set", "rule",
            f'name="{INBOUND_RULE_NAME}"', "new",
            f"localport={port}", f"remoteip={lan_subnet}",
        ])
        if code != 0:
            raise RuntimeError(f"Failed to update inbound rule: {err or out}")


def _set_rule_enabled(name, enabled, action_desc):
    code, out, err = _run([
        "advfirewall", "firewall", "set", "rule",
        f'name="{name}"', "new", f"enable={'yes' if enabled else 'no'}",
    ])
    if code != 0:
        raise RuntimeError(f"Failed to {action_desc} ({name}): {err or out}")


def enable_block():
    _set_rule_enabled(BLOCK_RULE_NAME, True, "enable block")
    _set_rule_enabled(BLOCK_RULE_NAME_V6, True, "enable IPv6 block")


def disable_block():
    _set_rule_enabled(BLOCK_RULE_NAME, False, "disable block")
    _set_rule_enabled(BLOCK_RULE_NAME_V6, False, "disable IPv6 block")


def is_blocked():
    """True/False when the rule state is known, None when it couldn't be
    determined (PowerShell error, or an unexpected/ambiguous value such as a
    duplicate-named rule returning an array). None must NOT be treated as
    "not blocked" - that would report a false "internet OK" to the parent."""
    code, out, _ = _run_ps(
        f"@(Get-NetFirewallRule -DisplayName '{BLOCK_RULE_NAME}' -ErrorAction SilentlyContinue).Enabled -contains $true"
    )
    if code != 0 or out not in ("True", "False"):
        return None
    return out == "True"