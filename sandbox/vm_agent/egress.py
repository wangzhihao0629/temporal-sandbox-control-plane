"""Apply the network policy to the job user, with nftables, at boot.

What: `render` turns a `NetworkPolicy` plus what it resolved (host IPs, the
VM's nameservers, the object store's address) into an nftables ruleset that
refuses every outbound connection from the `agent` uid except DNS to the VM's
nameservers, the object store, and the allowed hosts and ports. `main` runs as
root from the image's entrypoint, before it drops privileges, and exits
non-zero if the rules cannot be applied — the VM then never boots, rather than
booting open.
Why: rules keyed on the socket's uid leave the worker, which runs as
`sandbox-agent`, untouched, and cover everything a job starts, whatever
language it is written in. Refusing with `reject` rather than `drop` makes a
blocked connection fail at once instead of hanging for a timeout.
Allowed hosts are matched by the addresses they resolved to at boot, and those
same addresses are pinned in /etc/hosts, so a job that resolves the name again
gets an address the rules allow even after the host's DNS has moved on. Known
gaps, both fine for a local demo and both listed in the policy's docstring: a
pinned address that the host retires stops working until the VM is replaced;
and a job can still make DNS queries for any name through the allowed resolver,
which is a slow but real exfiltration channel.
Production: an egress proxy in front of the VM does the hostname allowlist.
"""

import ipaddress
import os
import pwd
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from sandbox.contract.network_policy import NetworkPolicy

TABLE = "sandbox_egress"
HOSTS_FILE = Path("/etc/hosts")
_HOSTS_BEGIN = "# BEGIN sandbox egress: allowed hosts, pinned at boot"
_HOSTS_END = "# END sandbox egress"


def resolve(hosts: tuple[str, ...]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for host in hosts:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        out[host] = sorted({info[4][0] for info in infos})
        if not out[host]:
            raise RuntimeError(f"{host} resolved to no addresses")
    return out


def nameservers(resolv_conf: Path = Path("/etc/resolv.conf")) -> list[str]:
    servers = []
    for line in resolv_conf.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            servers.append(parts[1])
    return servers


def endpoint(url: str) -> tuple[str, int] | None:
    parsed = urlparse(url)
    if not parsed.hostname:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return socket.gethostbyname(parsed.hostname), port


def _split(addresses: list[str]) -> tuple[list[str], list[str]]:
    v4 = [a for a in addresses if ipaddress.ip_address(a).version == 4]
    v6 = [a for a in addresses if ipaddress.ip_address(a).version == 6]
    return v4, v6


def _set(items) -> str:
    return "{ " + ", ".join(str(i) for i in items) + " }"


def render(
    policy: NetworkPolicy,
    uid: int,
    resolved: dict[str, list[str]],
    dns: list[str],
    object_store: tuple[str, int] | None,
) -> str:
    rules = [
        f"meta skuid != {uid} accept",
        'oifname "lo" accept',
        "ct state established,related accept",
    ]
    dns4, dns6 = _split(dns)
    for family, addrs in (("ip", dns4), ("ip6", dns6)):
        if addrs:
            rules.append(f"{family} daddr {_set(addrs)} udp dport 53 accept")
            rules.append(f"{family} daddr {_set(addrs)} tcp dport 53 accept")
    if object_store:
        host, port = object_store
        family = "ip" if ipaddress.ip_address(host).version == 4 else "ip6"
        rules.append(f"{family} daddr {host} tcp dport {port} accept")
    if policy.allow_ports:
        ports = _set(policy.allow_ports)
        v4, v6 = _split(sorted({a for addrs in resolved.values() for a in addrs}))
        for family, addrs in (("ip", v4), ("ip6", v6)):
            if addrs:
                rules.append(f"{family} daddr {_set(addrs)} tcp dport {ports} accept")
    rules.append("counter reject")
    body = "\n".join(f"    {r}" for r in rules)
    return (
        f"table inet {TABLE}\n"
        f"delete table inet {TABLE}\n"
        f"table inet {TABLE} {{\n"
        f"  chain output {{\n"
        f"    type filter hook output priority 0; policy accept;\n"
        f"{body}\n"
        f"  }}\n"
        f"}}\n"
    )


def pin_hosts(text: str, resolved: dict[str, list[str]]) -> str:
    """`text` (an /etc/hosts) with the allowed hosts pinned to the addresses the
    rules allow, replacing any earlier pinned block.

    The rules match addresses, but a job resolves the name again when it connects,
    and a host like github.com hands out a different address minutes later. Pinned
    here, the job resolves to exactly what the rules allow for the VM's lifetime.
    """
    kept, inside = [], False
    for line in text.splitlines():
        if line == _HOSTS_BEGIN:
            inside = True
        elif line == _HOSTS_END:
            inside = False
        elif not inside:
            kept.append(line)
    block = [_HOSTS_BEGIN]
    for host, addresses in sorted(resolved.items()):
        block += [f"{address} {host}" for address in addresses]
    block.append(_HOSTS_END)
    return "\n".join(kept + block) + "\n"


def main(env=None) -> int:
    env = os.environ if env is None else env
    policy = NetworkPolicy.from_env(env)
    user = env.get("SANDBOX_RUN_AS_USER", "agent")
    uid = pwd.getpwnam(user).pw_uid
    store = env.get("S3_ENDPOINT", "")
    resolved = resolve(policy.allow_hosts)
    ruleset = render(
        policy,
        uid,
        resolved,
        nameservers(),
        endpoint(store) if store else None,
    )
    proc = subprocess.run(["nft", "-f", "-"], input=ruleset, text=True, capture_output=True)
    if proc.returncode != 0:
        print(f"egress: nft refused the ruleset: {proc.stderr.strip()}", file=sys.stderr)
        print(ruleset, file=sys.stderr)
        return 1
    HOSTS_FILE.write_text(pin_hosts(HOSTS_FILE.read_text(), resolved))
    print(
        f"egress: {user} (uid {uid}) may reach {', '.join(policy.allow_hosts) or 'no hosts'}"
        f" on {', '.join(map(str, policy.allow_ports))}, plus DNS and the object store",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
