#!/usr/bin/env bash
# Cold-start demo: wait for the reconciler to bring the pool to its floor, then
# run the smoke. Needs `make up` done and `make workers` running.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
echo "==> waiting for the reconciler to reach the pool floor"
for _ in $(seq 1 60); do
  idle=$(uv run python -m sandbox.cli.show 2>/dev/null | awk '$2=="idle" {n++} END {print n+0}')
  if [[ "$idle" -ge 2 ]]; then break; fi
  sleep 3
done
uv run python -m sandbox.cli.show
echo "==> smoke"
uv run python -m sandbox.cli.smoke
if ! grep -q '^RUNNER_URI=' .env; then
  echo "==> artifact (first run)"
  uv run python -m sandbox.runner.package
fi
echo "==> coding session (${SCENARIO:-multiply-with-bug})"
uv run python -m sandbox.cli.session --scenario "${SCENARIO:-multiply-with-bug}"
