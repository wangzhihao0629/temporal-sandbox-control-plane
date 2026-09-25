#!/usr/bin/env bash
# Checks what the built VM image's egress rules actually allow and refuse, on a
# real VM with real network. Needs `make image` first and the container daemon.
set -euo pipefail
cd "$(dirname "$0")/.."
container run --rm \
  --cap-add CAP_NET_ADMIN \
  --volume "$PWD/scripts:/probe:ro" \
  --entrypoint /bin/sh \
  "${SANDBOX_VM_IMAGE:-sandbox-vm:dev}" /probe/network-probe.sh
