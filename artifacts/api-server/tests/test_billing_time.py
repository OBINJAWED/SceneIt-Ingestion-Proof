"""Provider-free checks for the process-local billing-time seam."""
from datetime import datetime, timezone
import unittest

from sceneit.billing_time import (
    BillingTimeError, billing_now, isolated_verification_time,
)


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, identity=None):
        self.identity = identity or {
            "database": "billing_clock_run001", "schema": "public",
            "has_coverage": True, "has_events": True,
        }
        self.wall_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def execute(self, query):
        if "current_database()" in query:
            return FakeResult(self.identity)
        if "clock_timestamp()" in query:
            return FakeResult({"now": self.wall_time})
        raise AssertionError(query)


class BillingTimeTests(unittest.TestCase):
    def test_defaults_to_fresh_database_wall_time(self):
        conn = FakeConnection()
        self.assertEqual(billing_now(conn), conn.wall_time)

    def test_override_is_scoped_and_requires_database_identity(self):
        conn = FakeConnection()
        target = datetime(2030, 1, 1, tzinfo=timezone.utc)
        environment = {
            "SCENEIT_STRIPE_TEST_CLOCK_APPROVED": "true",
            "SCENEIT_TEST_CLOCK_DATABASE_URL":
                "postgresql://localhost/billing_clock_run001",
            "DATABASE_URL": "postgresql://production/sceneit",
        }
        with isolated_verification_time(
            conn, target, "billing_clock_run001", environ=environment,
        ):
            self.assertEqual(billing_now(conn), target)
        self.assertEqual(billing_now(conn), conn.wall_time)

        conn.identity["database"] = "shared_database"
        with self.assertRaisesRegex(BillingTimeError, "identity"):
            with isolated_verification_time(
                conn, target, "billing_clock_run001", environ=environment,
            ):
                pass

    def test_no_normal_environment_time_override_exists(self):
        conn = FakeConnection()
        environment = {
            "SCENEIT_BILLING_NOW": "2099-01-01T00:00:00Z",
            "SCENEIT_TEST_CLOCK_DATABASE_URL":
                "postgresql://localhost/billing_clock_run001",
        }
        with self.assertRaisesRegex(BillingTimeError, "approval"):
            with isolated_verification_time(
                conn, datetime.now(timezone.utc), "billing_clock_run001",
                environ=environment,
            ):
                pass
        self.assertEqual(billing_now(conn), conn.wall_time)


if __name__ == "__main__":
    unittest.main()