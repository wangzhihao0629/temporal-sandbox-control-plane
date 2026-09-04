#!/usr/bin/env python3
"""Print a VM container's IPv4 address, or its bridge gateway with --gateway.

A debugging helper for the `sbx-*` containers: when a VM cannot reach Temporal
or the object store, the first question is which addresses it actually has.
Reads `container ls --all --format json` and pulls `ipv4Address` / `ipv4Gateway`
off the container's first network.

Usage: scripts/container_ip.py [--gateway] <container-name>
"""

import json
import subprocess
import sys


def main() -> None:
    args = sys.argv[1:]
    want_gateway = "--gateway" in args
    names = [a for a in args if not a.startswith("--")]
    if not names:
        sys.exit("usage: container_ip.py [--gateway] <container-name>")
    raw = subprocess.run(
        ["container", "ls", "--all", "--format", "json"], capture_output=True, text=True, check=True
    ).stdout
    for item in json.loads(raw or "[]"):
        if item.get("configuration", {}).get("id") != names[0]:
            continue
        networks = (item.get("status") or {}).get("networks") or []
        if not networks:
            sys.exit(f"{names[0]} has no network yet")
        first = networks[0]
        print(first["ipv4Gateway"] if want_gateway else first["ipv4Address"].split("/")[0])
        return
    sys.exit(f"no container named {names[0]}")


if __name__ == "__main__":
    main()
