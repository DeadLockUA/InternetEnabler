"""Windows Firewall control for InternetEnabler client.

Blocking is implemented as an outbound-block rule scoped to everything
EXCEPT the local LAN subnet, so the parent's server can still reach this
machine (and this machine can still answer) while internet access is cut.
"""

import ipaddress
import subprocess

BLOCK_RULE_NAME = "InternetEnabler-Block"
INBOUND_RULE_NAME = "InternetEnabler-Inbound"


def _run(args):
    result = subprocess.run(
        ["netsh"] + args,
        capture_output=True,
        text=True,
        shell=False,
    )
    return result.returncode, result.stdout, result.stderr


def _wan_ranges(lan_subnet):
    """Return the list of CIDR blocks covering 0.0.0.0/0 minus lan_subnet."""
    lan = ipaddress.ip_network(lan_subnet, strict=False)
    full = ipaddress.ip_network("0.0.0.0/0")
    remaining = full.address_exclude(lan)
    ranges = sorted(remaining, key=lambda n: n.network_address)
    return [str(n) for n in ranges]


def _rule_exists(name):
    code, out, _ = _run(["advfirewall", "firewall", "show", "rule", f'name="{name}"'])
    return code == 0 and "No rules match" not in out


def ensure_rules(lan_subnet, port):
    """Create the block and inbound-allow rules if they don't exist yet."""
    if not _rule_exists(BLOCK_RULE_NAME):
        remote_ip = ",".join(_wan_ranges(lan_subnet))
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


def enable_block():
    code, out, err = _run([
        "advfirewall", "firewall", "set", "rule",
        f'name="{BLOCK_RULE_NAME}"', "new", "enable=yes",
    ])
    if code != 0:
        raise RuntimeError(f"Failed to enable block: {err or out}")


def disable_block():
    code, out, err = _run([
        "advfirewall", "firewall", "set", "rule",
        f'name="{BLOCK_RULE_NAME}"', "new", "enable=no",
    ])
    if code != 0:
        raise RuntimeError(f"Failed to disable block: {err or out}")


def is_blocked():
    code, out, _ = _run(["advfirewall", "firewall", "show", "rule", f'name="{BLOCK_RULE_NAME}"'])
    if code != 0:
        return False
    for line in out.splitlines():
        if line.strip().startswith("Enabled:"):
            return "Yes" in line
    return False
