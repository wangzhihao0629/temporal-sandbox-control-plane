#!/usr/bin/env bash
# Tear the local stack down: VMs, infra containers, the Temporal dev server, .env.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> deleting sandbox VMs"
container ls --all --format json 2>/dev/null | python3 -c '
import json, subprocess, sys
for item in json.load(sys.stdin):
    name = item["configuration"]["id"]
    if name.startswith("sbx-"):
        subprocess.run(["container", "delete", "--force", name], check=False)
        print("    deleted", name)'

echo "==> deleting infra containers"
for name in sandbox-dynamodb sandbox-minio; do
  container delete --force "$name" >/dev/null 2>&1 && echo "    deleted $name"
done

echo "==> stopping temporal dev server"
if [[ -f .local/temporal.pid ]]; then
  kill "$(cat .local/temporal.pid)" 2>/dev/null && echo "    stopped"
  rm -f .local/temporal.pid
fi
rm -f .env
echo "==> down"
