#!/usr/bin/env bash
# Reproducible, provider-free release gate. Only disposable tests migrate or
# start fixture services; it never migrates the application DB or deploys.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

: "${DATABASE_URL:?DATABASE_URL must name the migrated restart-rehearsal database}"
: "${SCENEIT_TEST_DATABASE_URL:?SCENEIT_TEST_DATABASE_URL must name a separate disposable sceneit_test* database}"
if [[ "${SCENEIT_DISABLE_PROVIDER_NETWORK:-}" != "1" || -n "${TWELVE_LABS_API_KEY:-}" ]]; then
  echo "Release checks require disabled provider networking and no provider credential." >&2
  exit 1
fi

for tool in node pnpm uv python3 ffmpeg ffprobe pg_dump pg_restore; do
  command -v "$tool" >/dev/null || {
    echo "Required release tool is missing: $tool" >&2
    exit 1
  }
done

echo "node $(node --version); pnpm $(pnpm --version); $(uv --version)"
ffmpeg -version | sed -n '1p'
ffprobe -version | sed -n '1p'

# Both package managers must resolve only what the reviewed lockfiles contain.
pnpm install --frozen-lockfile
uv sync --locked

bash -n scripts/*.sh
bash scripts/contract-drift.sh
pnpm run typecheck
uv run --locked python -m compileall -q artifacts/api-server/sceneit artifacts/api-server/tests scripts
pnpm --filter @workspace/sceneit run test:behavior
# This named suite refuses DATABASE_URL, requires a distinct sceneit_test*
# database, and therefore cannot silently skip the claimed PostgreSQL gate.
uv run --locked python - <<'PY'
import os
from psycopg.conninfo import conninfo_to_dict

production = os.environ["DATABASE_URL"]
test = os.environ["SCENEIT_TEST_DATABASE_URL"]
name = conninfo_to_dict(test).get("dbname", "")
if test == production or not name.startswith("sceneit_test"):
    raise SystemExit(
        "SCENEIT_TEST_DATABASE_URL must differ from DATABASE_URL and name a sceneit_test* database"
    )
PY
(cd artifacts/api-server && uv run --locked python -m unittest discover \
  -s tests -p test_persistence_postgres.py -v)
pnpm --filter @workspace/api-server run test
pnpm --filter @workspace/sceneit run test:ui
# Vite's build-time config validates these values even without opening a port.
PORT=5173 BASE_PATH=/ pnpm run build

echo "Release checks passed with disposable PostgreSQL and controlled UI fixtures."