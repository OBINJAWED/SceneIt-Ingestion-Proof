#!/usr/bin/env bash
# Production-style web restart against a disposable database. This verifies
# startup/readiness are side-effect free; controlled fixtures must separately
# prove that reopening saved results makes no provider call.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${DATABASE_URL:?DATABASE_URL must name a disposable PostgreSQL database}"
if [[ "${SCENEIT_DISABLE_PROVIDER_NETWORK:-}" != "1" ]]; then
  echo "Refusing restart rehearsal unless provider networking is disabled." >&2
  exit 1
fi
if [[ -n "${TWELVE_LABS_API_KEY:-}" ]]; then
  echo "Refusing restart rehearsal with a provider credential present." >&2
  exit 1
fi
command -v pg_dump >/dev/null
command -v curl >/dev/null

PORT="${SCENEIT_REHEARSAL_PORT:-18080}"
PID=""
cleanup() {
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
    kill -TERM "$PID"
    wait "$PID" || true
  fi
}
trap cleanup EXIT

fingerprint() {
  # Hash in the pipeline so private rows never reach logs or temporary files.
  pg_dump "$DATABASE_URL" --data-only --no-owner --no-acl \
    --table=sceneit_proofs --table=sceneit_searches \
    --table=sceneit_imports --table=sceneit_import_searches \
    --table=sceneit_import_usage --table=sceneit_import_app_usage \
    2>/dev/null | sed '/^\\restrict /d; /^\\unrestrict /d; /^--/d' | sha256sum | cut -d' ' -f1
}

restart_once() {
  (
    cd "$ROOT/artifacts/api-server"
    exec uv run --locked gunicorn --config sceneit/gunicorn.conf.py sceneit.server:app
  ) &
  PID=$!
  ready=0
  for _ in $(seq 1 40); do
    if curl --fail --silent --show-error \
      -H "Host: ${SCENEIT_REHEARSAL_HOST:-127.0.0.1}" \
      "http://127.0.0.1:$PORT/api/healthz" >/dev/null &&
       curl --fail --silent --show-error \
      -H "Host: ${SCENEIT_REHEARSAL_HOST:-127.0.0.1}" \
      "http://127.0.0.1:$PORT/api/readyz" >/dev/null; then
      ready=1
      break
    fi
    kill -0 "$PID" 2>/dev/null || {
      wait "$PID" || true
      echo "Gunicorn exited before readiness." >&2
      exit 1
    }
    sleep .25
  done
  [[ "$ready" == 1 ]] || {
    echo "Gunicorn did not become ready within ten seconds." >&2
    exit 1
  }
  kill -TERM "$PID"
  wait "$PID"
  PID=""
}

export PORT
before="$(fingerprint)"
restart_once
between="$(fingerprint)"
restart_once
after="$(fingerprint)"

if [[ "$before" != "$between" || "$before" != "$after" ]]; then
  echo "Persistent proof/import data changed during a web restart." >&2
  exit 1
fi
echo "Two graceful Gunicorn restarts preserved persistent application data."