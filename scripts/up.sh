#!/usr/bin/env bash
# Bring up the local stack: Apple container services, the Temporal dev server,
# a host moto server standing in for DynamoDB and S3, tables and buckets, and
# a .env file every host process and every launched VM reads its endpoints
# from. moto replaces DynamoDB Local and MinIO containers: on a managed
# laptop, host processes are confined to loopback, so the `container` bridge
# network is unreachable from here even though a VM can reach a host listener
# over that same bridge.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL="$ROOT/.local"
MOTO_PORT="${SANDBOX_MOTO_PORT:-5050}"
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

echo "==> moto (dynamodb + s3)"
if curl -fsS -m 2 "http://127.0.0.1:${MOTO_PORT}/moto-api/" >/dev/null 2>&1; then
  : # already up and it is actually moto
elif (exec 3<>/dev/tcp/127.0.0.1/"${MOTO_PORT}") 2>/dev/null; then
  echo "==> ERROR: port ${MOTO_PORT} is held by something that is not moto; set SANDBOX_MOTO_PORT to another port" >&2
  exit 1
else
  [[ -x "$ROOT/.venv/bin/moto_server" ]] || uv sync
  nohup "$ROOT/.venv/bin/moto_server" -H 0.0.0.0 -p "$MOTO_PORT" >"$LOCAL/moto.log" 2>&1 &
  echo $! >"$LOCAL/moto.pid"
  # Falling through this loop without an answer used to leave `up` reporting
  # success and every later step failing on a connection refused.
  moto_ready=""
  for _ in $(seq 1 30); do
    if curl -fsS -m 2 "http://127.0.0.1:${MOTO_PORT}/moto-api/" >/dev/null 2>&1; then
      moto_ready=1
      break
    fi
    sleep 1
  done
  if [[ -z "$moto_ready" ]]; then
    echo "==> ERROR: moto did not answer on 127.0.0.1:${MOTO_PORT} within 30s" >&2
    echo "    see $LOCAL/moto.log" >&2
    exit 1
  fi
fi
echo "    dynamodb+s3 127.0.0.1:${MOTO_PORT}"

GATEWAY="${SANDBOX_VM_GATEWAY:-192.168.64.1}"

echo "==> verifying a VM can reach the host moto server via the gateway"
# `container run` interleaves its own VM-boot progress lines (newline-terminated,
# not just \r-updated) ahead of the command's own output, so the HTTP response
# is not necessarily line 1 of the capture: check the whole output for "HTTP/"
# rather than just its first line.
VM_CHECK="$(container run --rm alpine:3.20 wget -q -S -O /dev/null -T 5 "http://${GATEWAY}:${MOTO_PORT}/" 2>&1)" || true
if [[ "$VM_CHECK" != *"HTTP/"* ]]; then
  echo "==> ERROR: a VM could not reach the host moto server at ${GATEWAY}:${MOTO_PORT}" >&2
  echo "    got: $(tail -1 <<<"$VM_CHECK")" >&2
  exit 1
fi

cat >"$ROOT/.env" <<EOF
TEMPORAL_ADDRESS=127.0.0.1:7233
TEMPORAL_NAMESPACE=default
TEMPORAL_UI=http://localhost:8233
VM_TEMPORAL_ADDRESS=${GATEWAY}:7233
DYNAMODB_ENDPOINT=http://127.0.0.1:${MOTO_PORT}
S3_ENDPOINT=http://127.0.0.1:${MOTO_PORT}
VM_DYNAMODB_ENDPOINT=http://${GATEWAY}:${MOTO_PORT}
VM_S3_ENDPOINT=http://${GATEWAY}:${MOTO_PORT}
AWS_ACCESS_KEY_ID=local
AWS_SECRET_ACCESS_KEY=localsecret
AWS_DEFAULT_REGION=us-east-1
SANDBOX_VM_IMAGE=sandbox-vm:dev
SANDBOX_PROFILE=local
# SANDBOX_RECONCILE_DISABLED=1
# SANDBOX_RECONCILE_INTERVAL_SECONDS=15
EOF
echo "==> wrote .env (moto 127.0.0.1:${MOTO_PORT}, vm gateway ${GATEWAY})"

echo "==> tables and buckets"
uv run python -m sandbox.bootstrap
echo "==> up. next: make image (once), make workers, make smoke  (the reconciler launches VMs; make vm is a manual override)"
