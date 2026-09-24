#!/usr/bin/env bash
# Checks what the built VM image's sudoers rules actually allow and refuse.
# Needs `make image` first; needs the container daemon but not the rest of the stack.
set -euo pipefail
cd "$(dirname "$0")/.."
container run --rm \
  --volume "$PWD/scripts:/probe:ro" \
  --entrypoint /bin/sh \
  "${SANDBOX_VM_IMAGE:-sandbox-vm:dev}" /probe/sudoers-probe.sh
