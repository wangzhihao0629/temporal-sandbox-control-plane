#!/usr/bin/env bash
# Bring up the local stack: Apple container services, the Temporal dev server,
# DynamoDB Local and MinIO as containers, tables and buckets, and a .env file
# every host process and every launched VM reads its endpoints from.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL="$ROOT/.local"
mkdir -p "$LOCAL"
cd "$ROOT"

echo "==> container services"
container system start --enable-kernel-install >/dev/null 2>&1 || true

echo "==> temporal dev server"
if ! (exec 3<>/dev/tcp/127.0.0.1/7233) 2>/dev/null; then
  nohup temporal server start-dev \
    --ip 0.0.0.0 --port 7233 --ui-port 8233 \
    --db-filename "$LOCAL/temporal.db" --log-level warn \
    >"$LOCAL/temporal.log" 2>&1 &
  echo $! >"$LOCAL/temporal.pid"
  for _ in $(seq 1 30); do
    (exec 3<>/dev/tcp/127.0.0.1/7233) 2>/dev/null && break
    sleep 1
  done
fi
echo "    grpc 127.0.0.1:7233  ui http://localhost:8233"

ensure_container() {
  local name=$1
  shift
  if container ls --all --format json | python3 -c '
import json, sys
name = sys.argv[1]
items = json.load(sys.stdin)
ok = any(i["configuration"]["id"] == name and i["status"]["state"] == "running" for i in items)
sys.exit(0 if ok else 1)' "$name"; then
    return
  fi
  container delete --force "$name" >/dev/null 2>&1 || true
  container run --detach --name "$name" "$@" >/dev/null
}

echo "==> dynamodb local"
ensure_container sandbox-dynamodb amazon/dynamodb-local -jar DynamoDBLocal.jar -sharedDb -inMemory
echo "==> minio"
ensure_container sandbox-minio \
  --env MINIO_ROOT_USER=local --env MINIO_ROOT_PASSWORD=localsecret \
  quay.io/minio/minio server /data --console-address :9001

DDB_IP="$(python3 scripts/container_ip.py sandbox-dynamodb)"
MINIO_IP="$(python3 scripts/container_ip.py sandbox-minio)"
GATEWAY="$(python3 scripts/container_ip.py --gateway sandbox-dynamodb)"

cat >"$ROOT/.env" <<EOF
TEMPORAL_ADDRESS=127.0.0.1:7233
TEMPORAL_NAMESPACE=default
TEMPORAL_UI=http://localhost:8233
VM_TEMPORAL_ADDRESS=${GATEWAY}:7233
DYNAMODB_ENDPOINT=http://${DDB_IP}:8000
S3_ENDPOINT=http://${MINIO_IP}:9000
AWS_ACCESS_KEY_ID=local
AWS_SECRET_ACCESS_KEY=localsecret
AWS_DEFAULT_REGION=us-east-1
SANDBOX_VM_IMAGE=sandbox-vm:dev
SANDBOX_PROFILE=local
EOF
echo "==> wrote .env (dynamodb ${DDB_IP}, minio ${MINIO_IP}, vm gateway ${GATEWAY})"

echo "==> tables and buckets"
uv run python -m sandbox.bootstrap
echo "==> up. next: make image (once), make vm, make workers, make smoke"
