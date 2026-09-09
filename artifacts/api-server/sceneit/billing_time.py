"""Billing wall-time seam with a process-local, test-only verification scope.

Production has no environment-controlled time value: the default always reads
PostgreSQL wall time.  Test Clock verification must explicitly enter the scope
with an already-open connection to an approved disposable database.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import os
import re


class BillingTimeError(RuntimeError):
    pass


_verification_time = ContextVar("sceneit_billing_verification_time", default=None)


def billing_now(conn):
    """Return process-scoped verification time or fresh PostgreSQL wall time."""
    overridden = _verification_time.get()
    if overridden is not None:
        return overridden
    return conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]


def validate_isolated_database(conn, database_namespace, *, environ=None):
    """Validate the exact disposable database before any verification write."""
    env = os.environ if environ is None else environ
    if env.get("SCENEIT_STRIPE_TEST_CLOCK_APPROVED") != "true":
        raise BillingTimeError("explicit_test_clock_approval_required")
    test_url = env.get("SCENEIT_TEST_CLOCK_DATABASE_URL", "")
    production_url = env.get("DATABASE_URL", "")
    if (
        not isinstance(database_namespace, str)
        or not re.fullmatch(r"billing_clock_[a-z0-9_]{4,48}", database_namespace)
        or not test_url
        or database_namespace not in test_url
        or test_url == production_url
    ):
        raise BillingTimeError("isolated_test_database_required")
    identity = conn.execute(
        "SELECT current_database() AS database,current_schema() AS schema,"
        "to_regclass('sceneit_paid_coverage') IS NOT NULL AS has_coverage,"
        "to_regclass('sceneit_billing_events') IS NOT NULL AS has_events"
    ).fetchone()
    if (
        database_namespace not in (identity["database"], identity["schema"])
        or not identity["has_coverage"] or not identity["has_events"]
    ):
        raise BillingTimeError("test_database_identity_rejected")
    return identity


@contextmanager
def isolated_verification_time(conn, value, database_namespace, *, environ=None):
    """Temporarily bind Test Clock time after proving database isolation.

    This cannot be activated from normal application configuration and does not
    modify system/PostgreSQL time. ContextVar scoping prevents leakage to other
    requests, threads, or tasks.
    """
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise BillingTimeError("timezone_aware_verification_time_required")
    value = value.astimezone(timezone.utc)
    validate_isolated_database(
        conn, database_namespace, environ=environ,
    )
    token = _verification_time.set(value)
    try:
        yield value
    finally:
        _verification_time.reset(token)