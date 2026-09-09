"""Bounded Postgres access; credentials are consumed only by the driver."""
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .config import settings

PROOF_ID = "resident-evil-proof"


class DatabaseResourceExhausted(RuntimeError):
    """Raised instead of waiting when all cross-process DB slots are occupied."""


def _claim_local_slot(directory, limit):
    """Claim capacity before opening a socket, shared by local web processes."""
    root = Path(directory)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for slot in range(limit):
        descriptor = os.open(
            root / f"slot-{slot}",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError:
            os.close(descriptor)
    raise DatabaseResourceExhausted("Database connection capacity is exhausted")


def _verify_role_connection_limit(conn, configured_limit, required):
    """Require the production role to provide the cross-instance hard ceiling."""
    if not required:
        return
    row = conn.execute(
        "SELECT rolconnlimit,rolsuper,rolcanlogin "
        "FROM pg_roles WHERE rolname=current_user"
    ).fetchone()
    role_limit = row["rolconnlimit"] if isinstance(row, dict) else row[0]
    superuser = row["rolsuper"] if isinstance(row, dict) else row[1]
    can_login = row["rolcanlogin"] if isinstance(row, dict) else row[2]
    if superuser or not can_login or role_limit < 1 or role_limit > configured_limit:
        raise DatabaseResourceExhausted(
            "Database role must be a non-superuser login with CONNECTION LIMIT "
            "at or below SCENEIT_DATABASE_PERMITS"
        )


def database_role_limit_status(conn):
    """Readiness helper; does not mutate the role or schema."""
    config = settings()
    row = conn.execute(
        "SELECT rolconnlimit,rolsuper,rolcanlogin "
        "FROM pg_roles WHERE rolname=current_user"
    ).fetchone()
    role_limit = row["rolconnlimit"] if isinstance(row, dict) else row[0]
    superuser = row["rolsuper"] if isinstance(row, dict) else row[1]
    can_login = row["rolcanlogin"] if isinstance(row, dict) else row[2]
    return {
        "required": config.require_db_role_connection_limit,
        "configured": role_limit,
        "superuser": superuser,
        "canLogin": can_login,
        "maximum": config.database_permits,
        "valid": (
            not config.require_db_role_connection_limit
            or (
                not superuser
                and can_login
                and 1 <= role_limit <= config.database_permits
            )
        ),
    }


@contextmanager
def connection():
    config = settings()
    local_slot = _claim_local_slot(
        config.db_slot_directory, config.database_permits
    )
    conn = None
    try:
        conn = psycopg.connect(
            config.database_url,
            row_factory=dict_row,
            connect_timeout=config.db_connect_timeout_seconds,
            application_name="sceneit",
        )
        _verify_role_connection_limit(
            conn, config.database_permits,
            config.require_db_role_connection_limit,
        )
        # set_config supports bound values; SET itself does not on all PG versions.
        conn.execute(
            "SELECT set_config('statement_timeout',%s,false)",
            (str(config.db_statement_timeout_ms),),
        )
        conn.execute(
            "SELECT set_config('lock_timeout',%s,false)",
            (str(config.db_lock_timeout_ms),),
        )
        conn.execute(
            "SELECT set_config('idle_in_transaction_session_timeout',%s,false)",
            (str(config.db_idle_transaction_timeout_ms),),
        )
        yield conn
        conn.commit()
    except Exception:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if conn is not None:
            conn.close()
        os.close(local_slot)


@contextmanager
def autocommit_connection(*, row_factory=None, application_name="sceneit-worker"):
    """Bounded raw session for advisory-lock workers and health checks."""
    config = settings()
    local_slot = _claim_local_slot(
        config.db_slot_directory, config.database_permits
    )
    conn = None
    try:
        conn = psycopg.connect(
            config.database_url,
            autocommit=True,
            row_factory=row_factory,
            connect_timeout=config.db_connect_timeout_seconds,
            application_name=application_name,
        )
        _verify_role_connection_limit(
            conn, config.database_permits,
            config.require_db_role_connection_limit,
        )
        conn.execute(
            "SELECT set_config('statement_timeout',%s,false)",
            (str(config.db_statement_timeout_ms),),
        )
        conn.execute(
            "SELECT set_config('lock_timeout',%s,false)",
            (str(config.db_lock_timeout_ms),),
        )
        yield conn
    finally:
        if conn is not None:
            conn.close()
        os.close(local_slot)


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
    with autocommit_connection() as conn:
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