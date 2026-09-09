#!/bin/bash
set -euo pipefail
pnpm install --frozen-lockfile
uv sync --locked
# Development setup only. Flask owns its schema; never push the unused Drizzle
# scaffold, run DDL on application startup, or migrate production from this hook.
python3 scripts/migrate-development.py --development
