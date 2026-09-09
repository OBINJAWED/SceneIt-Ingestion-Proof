"""Disposable-PostgreSQL billing invariants; never contacts Stripe."""
import os
import threading
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.conninfo import conninfo_to_dict


TEST_URL = os.environ.get("SCENEIT_TEST_DATABASE_URL", "")
SAFE_TEST_DATABASE = (
    bool(TEST_URL)
    and TEST_URL != os.environ.get("DATABASE_URL")
    and conninfo_to_dict(TEST_URL).get("dbname", "").startswith("sceneit_test")
)
MIGRATIONS = Path(__file__).parents[1] / "sceneit" / "migrations"


def _sql(version):
    path = next(MIGRATIONS.glob(f"{version:03d}_*.sql"))
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().upper() not in {"BEGIN;", "COMMIT;"}
    )


@unittest.skipUnless(
    SAFE_TEST_DATABASE,
    "SCENEIT_TEST_DATABASE_URL must name a disposable sceneit_test* database",
)
class BillingPostgresTests(unittest.TestCase):
    def setUp(self):
        self.schema = f"sceneit_billing_test_{uuid.uuid4().hex}"
        with psycopg.connect(TEST_URL, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
            conn.execute(_sql(2))
            conn.execute(_sql(7))
            conn.execute(
                "INSERT INTO sceneit_auth_users(id,first_name) VALUES ('owner-1','Owner')"
            )
            conn.execute(
                "INSERT INTO sceneit_billing_accounts(owner_id,environment,customer_id,"
                "customer_attempt_state) VALUES ('owner-1','test','cus_1','created')"
            )

    def tearDown(self):
        with psycopg.connect(TEST_URL, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(self.schema)
            ))

    def _connect(self):
        conn = psycopg.connect(TEST_URL, autocommit=True)
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
        return conn

    @contextmanager
    def _billing_connection(self):
        with self._connect() as conn:
            conn.row_factory = dict_row
            yield conn

    def test_concurrent_different_keys_cannot_open_two_checkouts(self):
        barrier = threading.Barrier(2)
        results = []

        def insert():
            with self._connect() as conn:
                barrier.wait()
                try:
                    conn.execute(
                        "INSERT INTO sceneit_billing_checkouts"
                        "(owner_id,idempotency_key,plan,price_id,state) "
                        "VALUES ('owner-1',%s,'monthly','price_monthly','creating')",
                        (uuid.uuid4(),),
                    )
                    results.append("created")
                except psycopg.errors.UniqueViolation:
                    results.append("blocked")

        threads = [threading.Thread(target=insert) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(sorted(results), ["blocked", "created"])

    def test_invoice_reversal_tombstone_cannot_be_resurrected(self):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sceneit_paid_coverage"
                "(id,owner_id,subscription_id,starts_at,ends_at,reversed) "
                "VALUES ('in_1','owner-1','sub_1',now(),now()+interval '1 month',true)"
            )
            conn.execute(
                "INSERT INTO sceneit_paid_coverage"
                "(id,owner_id,subscription_id,starts_at,ends_at,reversed) "
                "VALUES ('in_1','owner-1','sub_1',now(),now()+interval '1 month',false) "
                "ON CONFLICT(id) DO UPDATE SET starts_at=EXCLUDED.starts_at,"
                "ends_at=EXCLUDED.ends_at"
            )
            row = conn.execute(
                "SELECT reversed FROM sceneit_paid_coverage WHERE id='in_1'"
            ).fetchone()
        self.assertTrue(row[0])

    def test_missed_refund_of_archived_price_revokes_existing_coverage(self):
        from sceneit import billing
        from sceneit.billing_config import BillingSettings
        from sceneit.billing_provider import Invoice
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)
        adapter = Mock()
        adapter.retrieve_invoice.return_value = Invoice(
            "in_old", "cus_1", "sub_old", "price_archived", 1000, "paid",
            start, end, False, True)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sceneit_paid_coverage(id,owner_id,subscription_id,starts_at,ends_at) "
                "VALUES('in_old','owner-1','sub_old',%s,%s)", (start, end))
        settings = BillingSettings(
            enabled=True, environment="test", prices={"monthly": "price_new"})
        with patch.object(billing, "connection", self._billing_connection):
            # Recovery intentionally uses invoice.paid, not a reversal event.
            billing._reconcile_event(
                "invoice.paid", {"data": {"object": {"id": "in_old"}}}, adapter, settings)
            billing._reconcile_event(
                "invoice.paid", {"data": {"object": {"id": "in_old"}}}, adapter, settings)
        adapter.verify_price.assert_not_called()
        adapter.retrieve_subscription.assert_not_called()
        with self._connect() as conn:
            self.assertTrue(conn.execute(
                "SELECT reversed FROM sceneit_paid_coverage WHERE id='in_old'").fetchone()[0])

    def test_cancellation_before_completion_cannot_strand_checkout(self):
        from sceneit import billing
        from sceneit.billing_config import BillingSettings
        from sceneit.billing_provider import HostedSession, Subscription
        settings = BillingSettings(
            enabled=True, environment="test", prices={"monthly": "price_monthly"})
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)
        canceled = Subscription(
            "sub_1", "cus_1", "price_monthly", "canceled", False, end, "owner-1", False)
        stale_active = Subscription(
            "sub_1", "cus_1", "price_monthly", "active", False, end, "owner-1", False)
        adapter = Mock()
        adapter.retrieve_checkout.return_value = (
            HostedSession("cs_1", None, None), "complete", "sub_1")
        adapter.retrieve_subscription.return_value = canceled
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sceneit_billing_checkouts(owner_id,idempotency_key,plan,"
                "price_id,state,provider_session_id) "
                "VALUES('owner-1',%s,'monthly','price_monthly','created','cs_1')",
                (uuid.uuid4(),))
        with patch.object(billing, "connection", self._billing_connection):
            billing._save_subscription(canceled, settings)
            billing._reconcile_event(
                "checkout.session.completed",
                {"data": {"object": {"id": "cs_1"}}}, adapter, settings)
            # A previously started reconciliation must not revive a terminal sub.
            billing._save_subscription(stale_active, settings)
            self.assertEqual(
                "expired", billing.complete_checkout("cs_1", "sub_1", adapter, settings))
        with self._connect() as conn:
            self.assertEqual("expired", conn.execute(
                "SELECT state FROM sceneit_billing_checkouts WHERE provider_session_id='cs_1'"
            ).fetchone()[0])
            self.assertEqual("canceled", conn.execute(
                "SELECT status FROM sceneit_billing_subscriptions WHERE subscription_id='sub_1'"
            ).fetchone()[0])

    def test_recovery_invoice_limit_preserves_real_account_continuation(self):
        from sceneit import billing_ops
        from sceneit.billing_config import BillingSettings
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sceneit_auth_users(id,first_name) VALUES('owner-2','Fixture')")
            conn.execute(
                "INSERT INTO sceneit_billing_accounts(owner_id,environment,customer_id,"
                "customer_attempt_state) VALUES('owner-2','test','cus_2','created')")
        provider = Mock()
        visited = []

        def invoices(customer, limit, cursor):
            visited.append(customer)
            return (["in_1", "in_2"] if customer == "cus_1" else ["in_3"]), None

        provider.list_paid_invoice_ids.side_effect = invoices
        with patch.object(billing_ops, "connection", self._billing_connection), \
                patch.object(billing_ops, "billing_settings", return_value=BillingSettings(
                    enabled=True, environment="test")), \
                patch.object(billing_ops, "StripeBillingProvider", return_value=provider), \
                patch.object(billing_ops, "_reconcile_event") as reconcile:
            first = billing_ops.recover(2, evidence="isolated fixture recovery")
            self.assertEqual(first["nextOwner"], "owner-1")
            self.assertEqual(first["unresolvedOwners"], ["owner-2"])
            second = billing_ops.recover(
                2, evidence="isolated fixture continuation", owner_after=first["nextOwner"])
            self.assertEqual(second["nextOwner"], "owner-2")
            self.assertEqual(visited, ["cus_1", "cus_2"])
            self.assertEqual(reconcile.call_count, 3)
            scoped = billing_ops.recover(
                2, evidence="isolated fixture target", customer_id="cus_1",
                owner_after="owner-2")
            self.assertIsNone(scoped["nextOwner"])
            self.assertEqual(scoped["customerOwner"], "owner-1")
            self.assertTrue(scoped["customerComplete"])