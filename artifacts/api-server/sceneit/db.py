"""Postgres access; credentials are consumed only by the driver."""
import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

PROOF_ID = "resident-evil-proof"


@contextmanager
def connection():
    with psycopg.connect(
        os.environ["DATABASE_URL"],
        row_factory=dict_row,
        connect_timeout=10,
    ) as conn:
        yield conn


def get_proof():
    with connection() as conn:
        return conn.execute(
            "SELECT * FROM sceneit_proofs WHERE id = %s", (PROOF_ID,)
        ).fetchone()


def update_proof(**changes):
    allowed = {
        "state", "message", "index_id", "asset_id", "indexed_asset_id",
        "provider_status", "provider_duration", "error_code",
    }
    if not changes or not set(changes).issubset(allowed):
        raise ValueError("Invalid proof update")
    # Column names are allowlisted; all values remain bound parameters.
    setters = ", ".join(f"{name} = %s" for name in changes)
    with connection() as conn:
        conn.execute(
            f"UPDATE sceneit_proofs SET {setters}, updated_at = now() WHERE id = %s",
            (*changes.values(), PROOF_ID),
        )


@contextmanager
def worker_lock():
    # A session lock is released automatically on process/connection loss.
    # There is no open transaction while an external provider request is running.
    with psycopg.connect(
        os.environ["DATABASE_URL"], autocommit=True, connect_timeout=10
    ) as conn:
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext('sceneit-one-video-worker'))"
        ).fetchone()[0]
        if not locked:
            raise RuntimeError("Another ingestion worker is already active")
        try:
            yield
        finally:
            conn.execute(
                "SELECT pg_advisory_unlock(hashtext('sceneit-one-video-worker'))"
            )