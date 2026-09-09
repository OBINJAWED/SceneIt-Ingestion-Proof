#!/usr/bin/env bash
# Regenerate the API clients and compare the generated trees with their exact
# pre-generation contents. This deliberately does not compare with git HEAD:
# intentional, uncommitted contract work is a valid baseline for this check.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
GENERATED=(
  "lib/api-client-react/src/generated"
  "lib/api-zod/src/generated"
)

restore() {
  for tree in "${GENERATED[@]}"; do
    rm -rf "$ROOT/$tree"
    if [[ -d "$TMP/before/$tree" ]]; then
      mkdir -p "$(dirname "$ROOT/$tree")"
      cp -a "$TMP/before/$tree" "$ROOT/$tree"
    fi
  done
  rm -rf "$TMP"
}
trap restore EXIT

for tree in "${GENERATED[@]}"; do
  if [[ -d "$ROOT/$tree" ]]; then
    mkdir -p "$TMP/before/$(dirname "$tree")"
    cp -a "$ROOT/$tree" "$TMP/before/$tree"
  fi
done

cd "$ROOT"
pnpm --filter @workspace/api-spec run codegen

drift=0
for tree in "${GENERATED[@]}"; do
  if ! diff -ruN "$TMP/before/$tree" "$ROOT/$tree"; then
    drift=1
  fi
done
if (( drift )); then
  echo "Generated API sources drifted. Run the API codegen command and retain its output." >&2
  exit 1
fi
echo "Generated API sources match a clean regeneration."