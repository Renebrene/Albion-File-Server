#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$BASE_DIR/../.." && pwd)"
PYTHON="${PYTHON:-$ROOT/venv/bin/python}"
BUN="${BUN:-bun}"
PORT="${ALBION_E2E_PORT:-18081}"
DATA_DIR="$(mktemp -d /tmp/albion-e2e.XXXXXX)"
SERVER_PID=""

cleanup() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$DATA_DIR"
}
trap cleanup EXIT

if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi
if ! command -v "$BUN" >/dev/null 2>&1; then
  echo "bun not found; set BUN to the bun executable" >&2
  exit 1
fi

cd "$BASE_DIR"
if [ ! -d node_modules ]; then
  "$BUN" install
fi

"$PYTHON" start_instance.py "$PORT" "$DATA_DIR" >"$DATA_DIR/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 50); do
  if curl -fsS --max-time 1 "http://127.0.0.1:$PORT/login" >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done
if ! curl -fsS --max-time 1 "http://127.0.0.1:$PORT/login" >/dev/null 2>&1; then
  echo "E2E server did not start; see $DATA_DIR/server.log" >&2
  exit 1
fi

export ALBION_E2E_BASE="http://127.0.0.1:$PORT"
echo "== public =="
"$BUN" public.spec.mjs
echo "== mobile =="
"$BUN" mobile.spec.mjs
echo "== admin =="
"$BUN" admin.spec.mjs
