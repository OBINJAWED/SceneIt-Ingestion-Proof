"""Explicit, additive development migrations. Never called by web/worker/build."""
import argparse
import os
from pathlib import Path

import psycopg


def migrate():
    if os.environ.get("REPLIT_DEPLOYMENT"):
        raise SystemExit("Development migrations cannot run in a deployment.")
    root = Path(__file__).resolve().parents[1]
    # Credentials are consumed by psycopg only; never output the connection.
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(hashtext('sceneit-development-migrations'))")
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sceneit_schema_migrations "
                "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
            for migration in sorted((root / "artifacts/api-server/sceneit/migrations").glob("*.sql")):
                if conn.execute("SELECT 1 FROM sceneit_schema_migrations WHERE name=%s",
                                (migration.name,)).fetchone():
                    continue
                # Migration files are trusted checked-in SQL, never request input.
                with conn.transaction():
                    sql = migration.read_text()
                    # Keep transaction ownership here, including legacy SQL wrappers.
                    statements = "\n".join(line for line in sql.splitlines()
                                           if line.strip().upper() not in ("BEGIN;", "COMMIT;"))
                    conn.execute(statements)
                    conn.execute("INSERT INTO sceneit_schema_migrations(name) VALUES (%s)",
                                 (migration.name,))
                print(f"Applied development migration: {migration.name}")
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtext('sceneit-development-migrations'))")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development", action="store_true", required=True)
    parser.parse_args()
    migrate()