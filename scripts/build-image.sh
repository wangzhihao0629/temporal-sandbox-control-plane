#!/usr/bin/env bash
# Build the VM image with Apple container's builder. Context is the repo root
# so the Dockerfile can COPY the sandbox package.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
container builder start >/dev/null 2>&1 || true
container build --tag sandbox-vm:dev --file images/vm/Dockerfile .
container image list | grep sandbox-vm
