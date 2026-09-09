#!/usr/bin/env bash
# One-command isolated rehearsal. Never reads or connects to the application DB.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
for tool in initdb pg_ctl createdb; do command -v "$tool" >/dev/null; done
WORK="$(mktemp -d /tmp/sceneit-gate-XXXXXX)"
cleanup() {
  pg_ctl -D "$WORK/data" -m immediate -w stop >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
initdb -D "$WORK/data" -U sceneit_fixture -A trust --no-locale -E UTF8 >/dev/null
# Unix socket only: this disposable trust-authenticated cluster is never public.
pg_ctl -D "$WORK/data" -l "$WORK/postgres.log" \
  -o "-h '' -k $WORK -p 55459" -w start >/dev/null
createdb -U sceneit_fixture -h "$WORK" -p 55459 sceneit_test_runtime
createdb -U sceneit_fixture -h "$WORK" -p 55459 sceneit_test_transactions
unset TWELVE_LABS_API_KEY
export DATABASE_URL="postgresql://sceneit_fixture@/sceneit_test_runtime?host=$WORK&port=55459"
export SCENEIT_TEST_DATABASE_URL="postgresql://sceneit_fixture@/sceneit_test_transactions?host=$WORK&port=55459"
export SCENEIT_DISABLE_PROVIDER_NETWORK=1
export SCENEIT_DB_SLOT_DIRECTORY="$WORK/connection-slots"
export SESSION_SECRET=fixture-only-session-secret-with-at-least-32-characters
export PILOT_ALLOWED_SUBJECTS=fixture-pilot
export TRUSTED_HOSTS=localhost,127.0.0.1
export TRUST_PROXY_HOPS=0
export REPL_ID=fixture-app
export PRIVATE_OBJECT_DIR=/fixture-bucket/private
SCENEIT_REHEARSAL_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
export SCENEIT_REHEARSAL_PORT
if command -v chromium >/dev/null; then
  # Nix's wrapper supplies system libraries absent from upstream browser zips.
  SCENEIT_TEST_CHROMIUM_EXECUTABLE="$(command -v chromium)"
  export SCENEIT_TEST_CHROMIUM_EXECUTABLE
fi
(
  cd artifacts/api-server
  uv run --locked python -m sceneit.migrate upgrade --operator-approved
  uv run --locked python -m sceneit.migrate upgrade --operator-approved
  uv run --locked python -m sceneit.migrate status
  uv run --locked python ../../scripts/seed-rehearsal.py
)
bash scripts/restart-rehearsal.sh
bash scripts/release-check.sh