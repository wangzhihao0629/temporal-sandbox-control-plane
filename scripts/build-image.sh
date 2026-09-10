#!/usr/bin/env bash
# Build the VM image with Apple container's builder. Context is the repo root
# so the Dockerfile can COPY the sandbox package.
#
# The image installs from a lockfile, not from the resolver: `uv export` writes
# exactly what uv.lock pins, so the VM runs the versions the tests ran against
# and a rebuild weeks later is the same image. `--locked` fails rather than
# quietly re-resolving when pyproject.toml has moved ahead of uv.lock.
#
# `local` is dropped alongside `dev` because it exists for the host stack: it
# pulls moto's server and its whole web stack, 54 packages a VM never imports.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# `runner` holds what the runner shells out to inside a VM (pytest, ruff). It is
# a separate group so the export names it explicitly instead of dragging in the
# whole dev group.
uv export --format requirements-txt --no-dev --no-group local --group runner \
  --no-emit-project --locked -o images/vm/requirements.txt
container builder start >/dev/null 2>&1 || true
container build --tag sandbox-vm:dev --file images/vm/Dockerfile .
container image list | grep sandbox-vm
