"""Disposable-PostgreSQL billing invariants; never contacts Stripe."""
import os
import json
import subprocess
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
from psycopg.types.json import Jsonb
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
            conn.execute(_sql(10))
            conn.execute(_sql(11))
            conn.execute(_sql(13))
            conn.execute(_sql(14))
            commercial_usage = _sql(8)
            conn.execute(commercial_usage[
                commercial_usage.index("CREATE TABLE sceneit_usage_windows"):
                commercial_usage.index("CREATE TABLE sceneit_usage_reservations")
            ])
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

    def test_verified_irrelevant_event_has_redacted_durable_receipt(self):
        from sceneit import billing
        from sceneit.billing_config import BillingSettings
        event = {
            "id": "evt_redacted", "type": "customer.created",
            "livemode": False, "created": 1_700_000_000,
            "data": {"object": {
                "id": "cus_1", "customer": "cus_1",
                "email": "must-not-persist@example.test",
                "address": {"line1": "must not persist"},
            }},
        }
        with patch.object(
            billing, "billing_settings",
            return_value=BillingSettings(enabled=True, environment="test"),
        ), patch.object(billing, "connection", self._billing_connection):
            self.assertEqual("completed", billing.process_event(event, provider=Mock()))
            self.assertEqual("completed", billing.process_event(event, provider=Mock()))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state,attempts,delivery_attempts,payload::text,"
                "object_id,outcome "
                "FROM sceneit_billing_events WHERE event_id='evt_redacted'"
            ).fetchone()
        self.assertEqual("ignored", row[0])
        self.assertEqual(1, row[1])
        self.assertEqual(2, row[2])
        self.assertNotIn("must-not-persist", row[3])
        self.assertEqual("cus_1", row[4])
        self.assertEqual("ignored", row[5])

    def test_delivery_and_processing_attempts_are_independent(self):
        from sceneit import billing
        from sceneit.billing_config import BillingSettings
        from sceneit.billing_provider import BillingProviderError
        event = {
            "id": "evt_retry", "type": "invoice.paid",
            "livemode": False, "created": 1_700_000_000,
            "data": {"object": {"id": "in_retry"}},
        }
        provider = Mock()
        provider.retrieve_invoice.side_effect = BillingProviderError(
            "provider_unavailable"
        )
        with patch.object(
            billing, "billing_settings",
            return_value=BillingSettings(enabled=True, environment="test"),
        ), patch.object(billing, "connection", self._billing_connection):
            self.assertEqual(
                "pending", billing.process_event(event, provider=provider)
            )
            self.assertEqual(
                "pending", billing.process_event(event, provider=provider)
            )
            self.assertEqual(
                "pending", billing.process_event(
                    event, provider=provider, delivered=False
                )
            )
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state,attempts,delivery_attempts FROM "
                "sceneit_billing_events WHERE event_id='evt_retry'"
            ).fetchone()
        self.assertEqual(("pending", 3, 2), row)

    def test_firebase_identity_is_never_purchase_eligible(self):
        from sceneit import billing
        from sceneit.billing_config import BillingProblem
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sceneit_auth_users(id,first_name,provider) "
                "VALUES('firebase-owner','Fixture','firebase')"
            )
            conn.execute(
                "INSERT INTO sceneit_billing_accounts(owner_id,environment) "
                "VALUES('firebase-owner','test')"
            )
        with patch.object(billing, "connection", self._billing_connection):
            with self.assertRaises(BillingProblem) as caught:
                billing._require_purchase_eligibility(
                    "firebase-owner", authorized=True
                )
        self.assertEqual(
            "billing_purchase_ineligible", caught.exception.code
        )

    def test_operator_recovers_schedule_create_crash_without_retry(self):
        from sceneit import billing, billing_ops
        from sceneit.billing_provider import (
            BillingProviderError, ScheduleChange, Subscription,
        )
        from sceneit.billing_config import (
            BillingCatalog, BillingOffer, BillingSettings, BillingTier,
        )
        operation_id, preview_id, change_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        portal_id = uuid.uuid4()
        effective_at = datetime.now(timezone.utc).replace(microsecond=0) + \
            __import__("datetime").timedelta(days=30)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sceneit_billing_subscriptions(subscription_id,"
                "owner_id,customer_id,environment,price_id,status,current_period_end) "
                "VALUES('sub_crash','owner-1','cus_1','test','price_old','active',%s)",
                (effective_at,),
            )
            conn.execute(
                "INSERT INTO sceneit_billing_change_previews(preview_id,owner_id,"
                "idempotency_key,parameters_hash,subscription_id,source_price_id,"
                "target_price_id,target_tier_key,target_cadence,currency,kind,"
                "subtotal,tax,total,proration_at,effective_at,expires_at,state) VALUES"
                "(%s,'owner-1',%s,%s,'sub_crash','price_old','price_new','pro',"
                "'yearly','usd','scheduled',1000,0,1000,now(),%s,"
                "now()+interval '1 hour','confirmed')",
                (preview_id, uuid.uuid4(), "a" * 64, effective_at))
            conn.execute(
                "INSERT INTO sceneit_billing_operations(operation_id,owner_id,kind,"
                "idempotency_key,parameters_hash,state,created_at,updated_at) VALUES"
                "(%s,'owner-1','schedule',%s,%s,'creating',"
                "now()-interval '20 minutes',now()-interval '20 minutes'),"
                "(%s,'owner-1','portal',%s,%s,'uncertain',"
                "now()-interval '25 hours',now()-interval '25 hours')",
                (operation_id, uuid.uuid4(), "b" * 64,
                 portal_id, uuid.uuid4(), "c" * 64))
            conn.execute(
                "INSERT INTO sceneit_billing_changes(change_id,owner_id,preview_id,"
                "operation_id,subscription_id,kind,target_tier_key,target_cadence,"
                "currency,target_price_id,state,effective_at) VALUES(%s,'owner-1',%s,%s,"
                "'sub_crash','scheduled','pro','yearly','usd','price_new','confirming',%s)",
                (change_id, preview_id, operation_id, effective_at))
        provider = Mock()
        provider.find_schedule.return_value = "sub_sched_recovered"
        target_proven = {"value": False}
        withdrawal_proven = {"value": False}
        def schedule_state(
            _schedule, expected_price_id=None, expected_operation_id=None
        ):
            if expected_price_id and not target_proven["value"]:
                from sceneit.billing_provider import BillingProviderError
                raise BillingProviderError("schedule_target_unverified")
            return "not_started" if expected_price_id else "released"
        provider.retrieve_schedule.side_effect = schedule_state
        def verified_schedule(*_args, **_kwargs):
            if not target_proven["value"]:
                raise BillingProviderError("schedule_target_unverified")
            return ScheduleChange(
                "released" if withdrawal_proven["value"] else "active",
                Subscription(
                    "sub_crash", "cus_1", "price_old", "active", False,
                    effective_at, "owner-1", False, "usd", "si_crash",
                ),
            )
        provider.retrieve_schedule_change.side_effect = verified_schedule
        limits = {
            "imports": 10, "upload_attempts": 10, "analysis_seconds": 10,
            "searches": 10, "media_bytes": 10, "frames": 10,
            "storage_bytes": 10,
        }
        old_tier = BillingTier("old", "Old", 1, ("imports",), limits)
        pro_tier = BillingTier("pro", "Pro", 2, ("imports",), limits)
        old_offer = BillingOffer(
            "old", "monthly", "usd", "price_old", 1000,
            "exclusive", "txcd_10000000", False,
        )
        new_offer = BillingOffer(
            "pro", "yearly", "usd", "price_new", 2000,
            "exclusive", "txcd_10000000", False,
        )
        settings = BillingSettings(
            enabled=True, environment="test",
            catalog=BillingCatalog(
                {"old": old_tier, "pro": pro_tier},
                {
                    ("old", "monthly", "usd"): old_offer,
                    ("pro", "yearly", "usd"): new_offer,
                },
                {"price_old": old_offer, "price_new": new_offer},
            ),
        )
        with patch.object(billing_ops, "connection", self._billing_connection), \
                patch.object(
                    billing_ops, "billing_settings",
                    return_value=settings,
                ), patch.object(
                    billing_ops, "StripeBillingProvider",
                    return_value=provider,
                ):
            result = billing_ops.recover_operations(
                10, evidence="incident-123"
            )
            self.assertEqual(0, result["resolved"])
            self.assertIn(str(operation_id), result["unresolved"])
            target_proven["value"] = True
            result = billing_ops.recover_operations(
                10, evidence="incident-123"
            )
            target_proven["value"] = False
            with patch.object(
                billing, "connection", self._billing_connection
            ):
                with self.assertRaises(BillingProviderError):
                    billing._reconcile_event(
                        "subscription_schedule.updated",
                        {"data": {"object": {
                            "id": "sub_sched_recovered"
                        }}},
                        provider, settings,
                    )
                with self._connect() as conn:
                    self.assertEqual(
                        ("uncertain", "uncertain"),
                        conn.execute(
                            "SELECT c.state,o.state FROM "
                            "sceneit_billing_changes c JOIN "
                            "sceneit_billing_operations o "
                            "ON o.operation_id=c.operation_id "
                            "WHERE c.change_id=%s", (change_id,)
                        ).fetchone(),
                    )
                target_proven["value"] = True
                billing._reconcile_event(
                    "subscription_schedule.updated",
                    {"data": {"object": {
                        "id": "sub_sched_recovered"
                    }}},
                    provider, settings,
                )
            withdraw_id = uuid.uuid4()
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO sceneit_billing_operations(operation_id,owner_id,"
                    "kind,idempotency_key,parameters_hash,state,provider_object_id,"
                    "created_at,updated_at) VALUES(%s,'owner-1','withdraw',%s,%s,"
                    "'uncertain','sub_sched_recovered',now()-interval '20 minutes',"
                    "now()-interval '20 minutes')",
                    (withdraw_id, uuid.uuid4(), "d" * 64),
                )
            withdrawal_proven["value"] = True
            withdrawn = billing_ops.recover_operations(
                10, evidence="incident-123"
            )
            billing_ops.expire_uncertain_portal(
                portal_id, evidence="incident-123"
            )
        self.assertEqual(1, result["resolved"])
        self.assertEqual(1, withdrawn["resolved"])
        provider.find_schedule.assert_called_once()
        provider.schedule_change.assert_not_called()
        provider.create_portal.assert_not_called()
        with self._connect() as conn:
            states = conn.execute(
                "SELECT kind,state,provider_object_id FROM "
                "sceneit_billing_operations ORDER BY kind"
            ).fetchall()
        self.assertIn(
            ("schedule", "scheduled", "sub_sched_recovered"), states
        )
        self.assertIn(("portal", "expired", None), states)
        self.assertIn(
            ("withdraw", "completed", "sub_sched_recovered"), states
        )
        with self._connect() as conn:
            self.assertEqual(
                "withdrawn", conn.execute(
                    "SELECT state FROM sceneit_billing_changes "
                    "WHERE change_id=%s", (change_id,)
                ).fetchone()[0]
            )

    def test_payment_intent_success_resolves_via_invoice_payments_lookup(self):
        from sceneit import billing
        from sceneit.billing_config import BillingSettings
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sceneit_billing_payment_problems(invoice_id,owner_id,"
                "provider_created_at,code,state,next_action) VALUES"
                "('in_payment','owner-1',now(),'payment_failed','open',"
                "'manage_billing')"
            )
        provider = Mock()
        provider.invoice_id_for_payment_intent.return_value = "in_payment"
        with patch.object(billing, "connection", self._billing_connection):
            billing._reconcile_event(
                "payment_intent.succeeded",
                {"data": {"object": {"id": "pi_payment"}}},
                provider, BillingSettings(enabled=True, environment="test"),
            )
        provider.invoice_id_for_payment_intent.assert_called_once_with(
            "pi_payment"
        )
        with self._connect() as conn:
            self.assertEqual(
                "resolved", conn.execute(
                    "SELECT state FROM sceneit_billing_payment_problems "
                    "WHERE invoice_id='in_payment'"
                ).fetchone()[0]
            )

    def test_coverage_snapshot_shape_requires_all_entitlement_fields(self):
        with self._connect() as conn:
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO sceneit_paid_coverage(id,owner_id,subscription_id,"
                    "starts_at,ends_at,tier_key) VALUES('in_incomplete','owner-1',"
                    "'sub_1',now(),now()+interval '1 month','fixture')"
                )

    def test_upgrade_preview_confirm_and_paid_snapshot_are_durable(self):
        from sceneit import billing
        from sceneit.billing_config import (
            BillingCatalog, BillingOffer, BillingSettings, BillingTier,
        )
        from sceneit.billing_provider import (
            ChangePreview, Invoice, InvoiceLine, ScheduleChange, Subscription,
        )
        now = datetime.now(timezone.utc).replace(microsecond=0)
        end = now.replace(year=now.year + 1)
        limits = {
            "imports": 10, "upload_attempts": 10, "analysis_seconds": 10,
            "searches": 10, "media_bytes": 10, "frames": 10,
            "storage_bytes": 10,
        }
        base = BillingTier("base", "Base", 1, ("imports",), limits)
        pro = BillingTier(
            "pro", "Pro", 2, ("imports", "searches"),
            {key: value * 2 for key, value in limits.items()},
        )
        ultra = BillingTier(
            "ultra", "Ultra", 3, ("imports", "searches", "analysis"),
            {key: value * 3 for key, value in limits.items()},
        )
        base_offer = BillingOffer(
            "base", "yearly", "usd", "price_base", 1000,
            "exclusive", "txcd_10000000", True,
        )
        pro_offer = BillingOffer(
            "pro", "yearly", "usd", "price_pro", 2000,
            "exclusive", "txcd_10000000", True,
        )
        ultra_offer = BillingOffer(
            "ultra", "yearly", "usd", "price_ultra", 3000,
            "exclusive", "txcd_10000000", True,
        )
        catalog = BillingCatalog(
            {"base": base, "pro": pro, "ultra": ultra},
            {
                ("base", "yearly", "usd"): base_offer,
                ("pro", "yearly", "usd"): pro_offer,
                ("ultra", "yearly", "usd"): ultra_offer,
            },
            {
                "price_base": base_offer, "price_pro": pro_offer,
                "price_ultra": ultra_offer,
            },
        )
        settings = BillingSettings(
            enabled=True, environment="test", catalog=catalog,
            prices={"base:yearly:usd": "price_base",
                    "pro:yearly:usd": "price_pro",
                    "ultra:yearly:usd": "price_ultra"},
        )
        current = Subscription(
            "sub_1", "cus_1", "price_base", "active", False, end,
            "owner-1", False, "usd", "si_1",
        )
        still_old_pending = Subscription(
            "sub_1", "cus_1", "price_base", "active", False, end,
            "owner-1", False, "usd", "si_1",
        )
        provider = Mock()
        provider.retrieve_subscription.return_value = current
        provider.preview_change.side_effect = lambda _sub, _price, at, **_kw: ChangePreview(
            "upcoming_in_preview", "usd", 500, 50, 550, None,
            now + __import__("datetime").timedelta(minutes=20), at,
        )
        provider.confirm_upgrade.return_value = (still_old_pending, "in_upgrade")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sceneit_billing_subscriptions(subscription_id,owner_id,"
                "customer_id,environment,price_id,status,current_period_end,tier_key,"
                "cadence,currency) VALUES('sub_1','owner-1','cus_1','test',"
                "'price_base','active',%s,'base','yearly','usd')", (end,))
            conn.execute(
                "INSERT INTO sceneit_paid_coverage(id,owner_id,subscription_id,"
                "starts_at,ends_at,tier_key,tier_rank,capabilities_snapshot,"
                "limits_snapshot,cadence,currency,price_id) VALUES"
                "('in_base','owner-1','sub_1',%s,%s,'base',1,'[\"imports\"]',"
                "%s,'yearly','usd','price_base')",
                (now - __import__("datetime").timedelta(days=1), end,
                 Jsonb(limits)),
            )
        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings):
            scoped_now = now - __import__("datetime").timedelta(hours=3)
            with patch.object(
                billing, "billing_now", return_value=scoped_now
            ) as billing_clock:
                preview = billing.preview_change(
                    "owner-1", "pro", "yearly", "usd", uuid.uuid4(),
                    purchase_authorized=True, provider=provider,
                )
            billing_clock.assert_called_once()
            self.assertEqual(
                scoped_now, provider.preview_change.call_args_list[0].args[2]
            )
            confirmed = billing.confirm_change(
                "owner-1", preview["previewId"], uuid.uuid4(), provider=provider,
                purchase_authorized=True,
            )
            self.assertEqual("payment_pending", confirmed["state"])
            with self._billing_connection() as conn:
                pending = conn.execute(
                    "SELECT * FROM sceneit_billing_changes "
                    "WHERE change_id=%s", (confirmed["changeId"],)
                ).fetchone()
            self.assertEqual("in_upgrade", pending["pending_invoice_id"])
            invoice = Invoice(
                "in_upgrade", "cus_1", "sub_1", "price_pro", 550, "paid",
                now, end, False, False, "usd", 500, 50, 550, True, True, now,
                None, (
                    InvoiceLine("price_pro", 1000, True, now, end),
                    InvoiceLine("price_base", -500, True, now, end),
                ), str(pending["operation_id"]),
            )
            provider.retrieve_invoice.return_value = invoice
            provider.retrieve_subscription.return_value = Subscription(
                "sub_1", "cus_1", "price_pro", "active", False, end,
                "owner-1", False, "usd", "si_1",
            )
            provider.verify_price.return_value = 2000
            billing._reconcile_event(
                "invoice.paid",
                {"data": {"object": {"id": "in_upgrade"}}},
                provider, settings,
            )
        with self._connect() as conn:
            row = conn.execute(
                "SELECT c.coverage_kind,c.funds_coverage_id,c.tier_rank,"
                "b.state,b.funded_invoice_id FROM sceneit_paid_coverage c "
                "JOIN sceneit_billing_changes b ON b.funded_invoice_id=c.id "
                "WHERE c.id='in_upgrade'"
            ).fetchone()
        self.assertEqual(
            ("upgrade", "in_base", 2, "effective", "in_upgrade"), tuple(row)
        )
        # A second incremental upgrade must fund the immediate effective source,
        # not reach past it to an arbitrary period root.
        second_preview, second_operation, second_change = (
            uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        )
        with self._billing_connection() as conn:
            conn.execute(
                "INSERT INTO sceneit_billing_change_previews(preview_id,owner_id,"
                "idempotency_key,parameters_hash,subscription_id,source_price_id,"
                "target_price_id,kind,target_tier_key,target_cadence,currency,"
                "subtotal,tax,total,proration_at,effective_at,expires_at,state) "
                "VALUES(%s,'owner-1',%s,%s,'sub_1','price_pro','price_ultra',"
                "'upgrade','ultra','yearly','usd',500,50,550,%s,%s,%s,'confirmed')",
                (
                    second_preview, uuid.uuid4(), "c" * 64, now, now, end,
                ),
            )
            conn.execute(
                "INSERT INTO sceneit_billing_operations(operation_id,owner_id,kind,"
                "idempotency_key,parameters_hash,state) "
                "VALUES(%s,'owner-1','upgrade',%s,%s,'confirmed')",
                (second_operation, uuid.uuid4(), "d" * 64),
            )
            conn.execute(
                "INSERT INTO sceneit_billing_changes(change_id,owner_id,preview_id,"
                "operation_id,subscription_id,kind,target_tier_key,target_cadence,"
                "currency,target_price_id,state,pending_invoice_id) "
                "VALUES(%s,'owner-1',%s,%s,'sub_1','upgrade','ultra','yearly',"
                "'usd','price_ultra','payment_pending','in_ultra')",
                (second_change, second_preview, second_operation),
            )
            second_pending = conn.execute(
                "SELECT c.*,p.source_price_id FROM sceneit_billing_changes c "
                "JOIN sceneit_billing_change_previews p ON p.preview_id=c.preview_id "
                "WHERE c.change_id=%s", (second_change,),
            ).fetchone()
        ultra_invoice = Invoice(
            "in_ultra", "cus_1", "sub_1", "price_ultra", 550, "paid",
            now, end, False, False, "usd", 500, 50, 550, True, True, now,
            None, (), str(second_operation),
        )
        with patch.object(billing, "connection", self._billing_connection):
            billing._save_coverage(
                "owner-1", ultra_invoice, reversed=False, offer=ultra_offer,
                settings=settings, pending_change=second_pending,
            )
        with self._billing_connection() as conn:
            chain = conn.execute(
                "SELECT funds_coverage_id FROM sceneit_paid_coverage "
                "WHERE id='in_ultra'"
            ).fetchone()
            from sceneit.quota import effective_coverage
            selected = effective_coverage(conn, "owner-1", now)
        self.assertEqual("in_upgrade", chain["funds_coverage_id"])
        self.assertEqual("in_ultra", selected["coverage_id"])
        # Reversing the middle increment invalidates its dependent child. A
        # replay cannot silently rebind that child to the period root.
        with self._billing_connection() as conn:
            conn.execute(
                "UPDATE sceneit_paid_coverage SET reversed=true "
                "WHERE id='in_upgrade'"
            )
            from sceneit.quota import effective_coverage
            selected = effective_coverage(conn, "owner-1", now)
            second_pending = conn.execute(
                "SELECT c.*,p.source_price_id FROM sceneit_billing_changes c "
                "JOIN sceneit_billing_change_previews p ON p.preview_id=c.preview_id "
                "WHERE c.change_id=%s", (second_change,),
            ).fetchone()
        self.assertEqual("in_base", selected["coverage_id"])
        with patch.object(billing, "connection", self._billing_connection):
            billing._save_coverage(
                "owner-1", ultra_invoice, reversed=False, offer=ultra_offer,
                settings=settings, pending_change=second_pending,
            )
        with self._billing_connection() as conn:
            rebound = conn.execute(
                "SELECT funds_coverage_id FROM sceneit_paid_coverage "
                "WHERE id='in_ultra'"
            ).fetchone()
            selected = effective_coverage(conn, "owner-1", now)
        self.assertEqual("in_upgrade", rebound["funds_coverage_id"])
        self.assertEqual("in_base", selected["coverage_id"])
        renewal = Invoice(
            "in_ultra_renewal", "cus_1", "sub_1", "price_ultra", 3300, "paid",
            now, end, False, False, "usd", 3000, 300, 3300, True, False, now,
        )
        with patch.object(billing, "connection", self._billing_connection):
            billing._save_coverage(
                "owner-1", renewal, reversed=False, offer=ultra_offer,
                settings=settings,
            )
        with self._billing_connection() as conn:
            selected = effective_coverage(conn, "owner-1", now)
        self.assertEqual("in_ultra_renewal", selected["coverage_id"])
        expired_preview, expired_operation, expired_change = (
            uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        )
        with self._billing_connection() as conn:
            conn.execute(
                "INSERT INTO sceneit_billing_change_previews(preview_id,owner_id,"
                "idempotency_key,parameters_hash,subscription_id,source_price_id,"
                "target_price_id,kind,target_tier_key,target_cadence,currency,"
                "subtotal,tax,total,proration_at,effective_at,expires_at,state) "
                "VALUES(%s,'owner-1',%s,%s,'sub_1','price_ultra','price_ultra',"
                "'upgrade','ultra','yearly','usd',1,0,1,%s,%s,%s,'confirmed')",
                (expired_preview, uuid.uuid4(), "e" * 64, now, now, end),
            )
            conn.execute(
                "INSERT INTO sceneit_billing_operations(operation_id,owner_id,kind,"
                "idempotency_key,parameters_hash,state) "
                "VALUES(%s,'owner-1','upgrade',%s,%s,'confirmed')",
                (expired_operation, uuid.uuid4(), "f" * 64),
            )
            conn.execute(
                "INSERT INTO sceneit_billing_changes(change_id,owner_id,preview_id,"
                "operation_id,subscription_id,kind,target_tier_key,target_cadence,"
                "currency,target_price_id,state,pending_invoice_id) "
                "VALUES(%s,'owner-1',%s,%s,'sub_1','upgrade','ultra','yearly',"
                "'usd','price_ultra','payment_pending','in_expired_upgrade')",
                (expired_change, expired_preview, expired_operation),
            )
        void_invoice = Invoice(
            "in_expired_upgrade", "cus_1", "sub_1", "price_ultra", 0, "void",
            now, end, False, False, "usd", 1, 0, 1, True, True, now,
            None, (), str(expired_operation),
        )
        import httpx
        import stripe
        import time
        from sceneit.billing_provider import (
            StripeBillingProvider, _BoundedStripeHTTPClient,
        )
        sdk_requests = []

        async def raw_provider(request):
            sdk_requests.append(request.url.path)
            if request.url.path == "/v1/subscriptions/sub_1":
                return httpx.Response(200, json={
                    "id": "sub_1", "object": "subscription",
                    "customer": "cus_1", "status": "active",
                    "cancel_at_period_end": False, "livemode": False,
                    "metadata": {"sceneit_owner_id": "owner-1"},
                    "items": {"data": [{
                        "id": "si_1", "current_period_end": int(end.timestamp()),
                        "price": {
                            "id": "price_pro", "currency": "usd",
                        },
                    }]},
                })
            if request.url.path in (
                "/v1/invoices/in_expired_upgrade",
                "/v1/invoices/in_open_upgrade",
            ):
                is_open = request.url.path.endswith("in_open_upgrade")
                return httpx.Response(200, json={
                    "id": "in_open_upgrade" if is_open else "in_expired_upgrade",
                    "object": "invoice",
                    "customer": "cus_1", "subscription": "sub_1",
                    "status": "open" if is_open else "void",
                    "amount_paid": 0, "livemode": False,
                    "discounts": [], "subtotal": 1, "total": 1,
                    "currency": "usd", "created": int(now.timestamp()),
                    "total_taxes": [], "automatic_tax": {
                        "enabled": False, "status": "not_collecting",
                    },
                    "metadata": {
                        "sceneit_change_id": str(
                            open_operation if is_open else expired_operation
                        ),
                    },
                    "lines": {"has_more": False, "data": [{
                        "amount": 1, "quantity": 1,
                        "discount_amounts": [],
                        "period": {
                            "start": int(now.timestamp()),
                            "end": int(end.timestamp()),
                        },
                        "parent": {
                            "type": "subscription_item_details",
                            "subscription_item_details": {
                                "subscription": "sub_1", "proration": True,
                            },
                        },
                        "pricing": {"price_details": {
                            "price": "price_ultra",
                        }},
                    }]},
                })
            return httpx.Response(500, json={"error": {
                "message": "unexpected provider request",
            }})

        raw_adapter = object.__new__(StripeBillingProvider)
        raw_adapter._stripe = stripe
        raw_adapter._deadline = time.monotonic() + 2
        raw_adapter._client = stripe.StripeClient(
            "sk_test_fixture",
            http_client=_BoundedStripeHTTPClient(
                raw_adapter._deadline,
                transport=httpx.MockTransport(raw_provider),
            ),
            max_network_retries=0,
            base_addresses={"api": "https://stripe.mock"},
        ).v1
        with patch.object(
            billing, "connection", self._billing_connection
        ), patch.object(
            billing, "billing_settings", return_value=settings
        ):
            self.assertEqual(
                "completed",
                billing.process_event(
                    {
                        "id": "evt_pending_update_expired",
                        "type": "customer.subscription.pending_update_expired",
                        "livemode": False,
                        "created": int(now.timestamp()),
                        "data": {"object": {"id": "sub_1", "customer": "cus_1"}},
                    },
                    provider=raw_adapter,
                ),
            )
        self.assertNotIn("/v1/invoice_payments", " ".join(sdk_requests))
        with self._billing_connection() as conn:
            expired = conn.execute(
                "SELECT c.state,o.state operation_state,o.last_error_code,"
                "(SELECT reversed FROM sceneit_paid_coverage "
                "WHERE id='in_ultra_renewal') renewal_reversed "
                "FROM sceneit_billing_changes c JOIN sceneit_billing_operations o "
                "ON o.operation_id=c.operation_id WHERE c.change_id=%s",
                (expired_change,),
            ).fetchone()
        self.assertEqual(
            ("expired", "expired", "upgrade_invoice_expired", False),
            tuple(expired.values()),
        )
        open_preview, open_operation, open_change = (
            uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        )
        with self._billing_connection() as conn:
            conn.execute(
                "INSERT INTO sceneit_billing_change_previews(preview_id,owner_id,"
                "idempotency_key,parameters_hash,subscription_id,source_price_id,"
                "target_price_id,kind,target_tier_key,target_cadence,currency,"
                "subtotal,tax,total,proration_at,effective_at,expires_at,state) "
                "VALUES(%s,'owner-1',%s,%s,'sub_1','price_ultra','price_ultra',"
                "'upgrade','ultra','yearly','usd',1,0,1,%s,%s,%s,'confirmed')",
                (open_preview, uuid.uuid4(), "1" * 64, now, now, end),
            )
            conn.execute(
                "INSERT INTO sceneit_billing_operations(operation_id,owner_id,kind,"
                "idempotency_key,parameters_hash,state,updated_at) "
                "VALUES(%s,'owner-1','upgrade',%s,%s,'confirmed',"
                "now()-interval '11 minutes')",
                (open_operation, uuid.uuid4(), "2" * 64),
            )
            conn.execute(
                "INSERT INTO sceneit_billing_changes(change_id,owner_id,preview_id,"
                "operation_id,subscription_id,kind,target_tier_key,target_cadence,"
                "currency,target_price_id,state,pending_invoice_id,updated_at) "
                "VALUES(%s,'owner-1',%s,%s,'sub_1','upgrade','ultra','yearly',"
                "'usd','price_ultra','payment_pending','in_open_upgrade',"
                "now()-interval '11 minutes')",
                (open_change, open_preview, open_operation),
            )
        from sceneit import billing_ops
        with patch.object(
            billing_ops, "connection", self._billing_connection
        ), patch.object(
            billing_ops, "billing_settings", return_value=settings
        ), patch.object(
            billing_ops, "StripeBillingProvider", return_value=raw_adapter
        ), patch.object(billing_ops, "_audit"):
            recovered = billing_ops.recover_operations(10, evidence="fixture")
        self.assertIn(str(open_operation), recovered["unresolved"])
        with self._billing_connection() as conn:
            blocked = conn.execute(
                "SELECT c.state,o.state operation_state,"
                "(SELECT allowance_anchor FROM sceneit_billing_accounts "
                "WHERE owner_id='owner-1') allowance_anchor "
                "FROM sceneit_billing_changes c JOIN sceneit_billing_operations o "
                "ON o.operation_id=c.operation_id WHERE c.change_id=%s",
                (open_change,),
            ).fetchone()
        self.assertEqual("payment_pending", blocked["state"])
        self.assertEqual("confirmed", blocked["operation_state"])
        self.assertIsNotNone(blocked["allowance_anchor"])
        with self._billing_connection() as conn:
            conn.execute(
                "DELETE FROM sceneit_billing_changes WHERE change_id=%s",
                (open_change,),
            )
            conn.execute(
                "DELETE FROM sceneit_billing_operations WHERE operation_id=%s",
                (open_operation,),
            )
            conn.execute(
                "DELETE FROM sceneit_billing_change_previews WHERE preview_id=%s",
                (open_preview,),
            )
        from sceneit.server import create_app
        from sceneit.billing_provider import HostedSession
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sceneit_auth_users(id,first_name) "
                "VALUES('owner-2','Checkout')"
            )
        provider.create_customer.return_value = "cus_2"
        provider.list_subscriptions.return_value = []
        provider.create_checkout.return_value = HostedSession(
            "cs_route", "https://checkout.stripe.test/session",
            now + __import__("datetime").timedelta(minutes=20),
        )
        provider.create_portal.return_value = HostedSession(
            "bps_route", "https://billing.stripe.test/session", None,
        )
        effective = Subscription(
            "sub_1", "cus_1", "price_pro", "active", False, end,
            "owner-1", False, "usd", "si_1",
        )
        provider.retrieve_subscription.return_value = effective
        provider.schedule_change.side_effect = (
            lambda _sub, _price, _op, *, effective_at, on_created=None: (
                on_created("sub_sched_route") if on_created else None
            ) or "sub_sched_route"
        )
        provider.withdraw_schedule.return_value = "sub_sched_route"
        provider.retrieve_schedule_change.side_effect = (
            ScheduleChange("active", effective),
            ScheduleChange("released", effective),
        )
        session = {
            "id": "session", "user_id": "owner-1", "first_name": "Owner",
            "csrf_token": "csrf", "provider": "replit",
        }
        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings), \
                patch.object(billing, "StripeBillingProvider", return_value=provider), \
                patch("sceneit.billing_config.billing_settings", return_value=settings), \
                patch("sceneit.quota.usage_status", return_value=None), \
                patch("sceneit.auth._session_from_cookie", side_effect=lambda: session), \
                patch("sceneit.resources.admit_participant", return_value=None):
            app = create_app({
                "TESTING": True, "SESSION_SECRET": "s" * 48,
                "TRUSTED_HOSTS": ["sceneit.example"],
                "TRUST_PROXY_HOPS": 0, "DATABASE_CONFIGURED": True,
                "PILOT_ALLOWED_SUBJECTS": "owner-1,owner-2",
            })
            client = app.test_client()
            response = client.get(
                "/api/billing/status", base_url="https://sceneit.example"
            )
            emitted = [("GET", "/billing/status", response)]
            portal = client.post(
                "/api/billing/portal", base_url="https://sceneit.example",
                headers={"X-CSRF-Token": "csrf"},
                json={"action": "manage", "idempotencyKey": str(uuid.uuid4())},
            )
            emitted.append(("POST", "/billing/portal", portal))
            scheduled_preview = client.post(
                "/api/billing/change/preview",
                base_url="https://sceneit.example",
                headers={"X-CSRF-Token": "csrf"},
                json={
                    "tier": "base", "cadence": "yearly", "currency": "usd",
                    "idempotencyKey": str(uuid.uuid4()),
                },
            )
            emitted.append((
                "POST", "/billing/change/preview", scheduled_preview
            ))
            scheduled_confirm = client.post(
                "/api/billing/change/confirm",
                base_url="https://sceneit.example",
                headers={"X-CSRF-Token": "csrf"},
                json={
                    "previewId": scheduled_preview.get_json()["previewId"],
                    "idempotencyKey": str(uuid.uuid4()),
                },
            )
            emitted.append((
                "POST", "/billing/change/confirm", scheduled_confirm
            ))
            withdrawn = client.post(
                "/api/billing/change/withdraw",
                base_url="https://sceneit.example",
                headers={"X-CSRF-Token": "csrf"},
                json={
                    "changeId": scheduled_confirm.get_json()["changeId"],
                    "idempotencyKey": str(uuid.uuid4()),
                },
            )
            emitted.append((
                "POST", "/billing/change/withdraw", withdrawn
            ))
            session["user_id"] = "owner-2"
            checkout_key = str(uuid.uuid4())
            checkout = client.post(
                "/api/billing/checkout", base_url="https://sceneit.example",
                headers={"X-CSRF-Token": "csrf"},
                json={
                    "tier": "base", "cadence": "yearly", "currency": "usd",
                    "idempotencyKey": checkout_key,
                },
            )
            emitted.append(("POST", "/billing/checkout", checkout))
            owner_two_status = client.get(
                "/api/billing/status", base_url="https://sceneit.example"
            )
            emitted.append(("GET", "/billing/status", owner_two_status))
        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual("ultra", payload["effectiveTier"])
        self.assertEqual("yearly", payload["cadence"])
        owner_two_operations = owner_two_status.get_json()["operationStates"]
        self.assertEqual(
            [{
                "idempotencyKey": checkout_key,
                "kind": "checkout", "state": "created",
            }],
            owner_two_operations,
        )
        validator = Path(__file__).resolve().parents[3] / "scripts" / \
            "validate-openapi-response.mjs"
        for method, route, emitted_response in emitted:
            self.assertEqual(
                200, emitted_response.status_code,
                (route, emitted_response.get_json()),
            )
            validated = subprocess.run(
                ["node", str(validator), method, route, "200"],
                input=json.dumps(emitted_response.get_json()), text=True,
                capture_output=True, cwd=Path(__file__).resolve().parents[3],
                timeout=15,
            )
            self.assertEqual(
                0, validated.returncode,
                f"{route}: {validated.stderr}\n{emitted_response.get_json()}",
            )

    def test_missed_refund_of_archived_price_revokes_existing_coverage(self):
        from sceneit import billing
        from sceneit.billing_config import BillingSettings
        from sceneit.billing_provider import (
            ChangePreview, Invoice, InvoiceLine, Subscription,
        )
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)
        adapter = Mock()
        adapter.retrieve_invoice.return_value = Invoice(
            "in_old", "cus_1", "sub_old", "price_archived", 1000, "paid",
            start, end, False, True)
        adapter.invoice_id_for_reversal.side_effect = [None, "in_old"]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sceneit_paid_coverage(id,owner_id,subscription_id,starts_at,ends_at) "
                "VALUES('in_old','owner-1','sub_old',%s,%s)", (start, end))
        settings = BillingSettings(
            enabled=True, environment="test", prices={"monthly": "price_new"})
        with patch.object(billing, "connection", self._billing_connection):
            billing._reconcile_event(
                "refund.updated", {"data": {"object": {"id": "re_old"}}},
                adapter, settings,
            )
            with self._connect() as conn:
                self.assertFalse(conn.execute(
                    "SELECT reversed FROM sceneit_paid_coverage "
                    "WHERE id='in_old'").fetchone()[0])
            billing._reconcile_event(
                "refund.updated", {"data": {"object": {"id": "re_old"}}},
                adapter, settings,
            )
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

    def test_withdraw_api_same_key_exposes_only_confirmed_withdrawal(self):
        """The real HTTP route never presents an indeterminate replay as success."""
        from sceneit import billing
        from sceneit.billing_config import BillingSettings
        from sceneit.server import create_app

        settings = BillingSettings(enabled=True, environment="test")
        provider = Mock()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sceneit_billing_subscriptions(subscription_id,owner_id,"
                "customer_id,environment,price_id,status) VALUES"
                "('sub_withdraw','owner-1','cus_1','test','price_a','active')"
            )
        session = {
            "id": "session", "user_id": "owner-1", "first_name": "Owner",
            "csrf_token": "csrf", "provider": "replit",
        }
        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings), \
                patch.object(billing, "StripeBillingProvider", return_value=provider), \
                patch("sceneit.billing_config.billing_settings", return_value=settings), \
                patch("sceneit.auth._session_from_cookie", return_value=session):
            app = create_app({
                "TESTING": True, "SESSION_SECRET": "s" * 48,
                "TRUSTED_HOSTS": ["sceneit.example"], "TRUST_PROXY_HOPS": 0,
                "DATABASE_CONFIGURED": True,
                "PILOT_ALLOWED_SUBJECTS": "owner-1",
            })
            client = app.test_client()
            cases = (
                ("failed", "scheduled", 409, "withdrawal_failed"),
                ("creating", "scheduled", 409, "withdrawal_outcome_unknown"),
                ("uncertain", "uncertain", 409, "withdrawal_outcome_unknown"),
                ("withdrawn", "withdrawn", 200, None),
            )
            for operation_state, change_state, status, code in cases:
                with self.subTest(operation_state=operation_state):
                    preview_id, schedule_operation = uuid.uuid4(), uuid.uuid4()
                    change_id, withdrawal_operation = uuid.uuid4(), uuid.uuid4()
                    key = uuid.uuid4()
                    params_hash = billing._parameters_hash(
                        "withdraw", str(change_id)
                    )
                    with self._connect() as conn:
                        conn.execute(
                            "INSERT INTO sceneit_billing_change_previews("
                            "preview_id,owner_id,idempotency_key,parameters_hash,"
                            "subscription_id,source_price_id,target_price_id,"
                            "target_tier_key,target_cadence,currency,kind,subtotal,"
                            "tax,total,proration_at,expires_at,state) VALUES"
                            "(%s,'owner-1',%s,%s,'sub_withdraw','price_a','price_b',"
                            "'base','yearly','usd','scheduled',1,0,1,now(),"
                            "now()+interval '1 hour','confirmed')",
                            (preview_id, uuid.uuid4(), "a" * 64),
                        )
                        conn.execute(
                            "INSERT INTO sceneit_billing_operations(operation_id,"
                            "owner_id,kind,idempotency_key,parameters_hash,state,"
                            "provider_object_id) VALUES"
                            "(%s,'owner-1','schedule',%s,%s,'scheduled','sched_1'),"
                            "(%s,'owner-1','withdraw',%s,%s,%s,'sched_1')",
                            (
                                schedule_operation, uuid.uuid4(), "b" * 64,
                                withdrawal_operation, key, params_hash,
                                operation_state,
                            ),
                        )
                        conn.execute(
                            "INSERT INTO sceneit_billing_changes(change_id,owner_id,"
                            "preview_id,operation_id,subscription_id,"
                            "provider_schedule_id,kind,target_tier_key,"
                            "target_cadence,currency,target_price_id,state) VALUES"
                            "(%s,'owner-1',%s,%s,'sub_withdraw','sched_1',"
                            "'scheduled','base','yearly','usd','price_b',%s)",
                            (
                                change_id, preview_id, schedule_operation,
                                change_state,
                            ),
                        )
                    response = client.post(
                        "/api/billing/change/withdraw",
                        base_url="https://sceneit.example",
                        headers={"X-CSRF-Token": "csrf"},
                        json={
                            "changeId": str(change_id),
                            "idempotencyKey": str(key),
                        },
                    )
                    self.assertEqual(status, response.status_code)
                    if code:
                        self.assertEqual(code, response.get_json()["code"])
                    else:
                        self.assertEqual(
                            {
                                "changeId": str(change_id),
                                "state": "withdrawn",
                            },
                            response.get_json(),
                        )
                    with self._connect() as conn:
                        conn.execute(
                            "DELETE FROM sceneit_billing_changes "
                            "WHERE change_id=%s", (change_id,),
                        )
                        conn.execute(
                            "DELETE FROM sceneit_billing_operations "
                            "WHERE operation_id IN (%s,%s)",
                            (withdrawal_operation, schedule_operation),
                        )
                        conn.execute(
                            "DELETE FROM sceneit_billing_change_previews "
                            "WHERE preview_id=%s", (preview_id,),
                        )
        provider.withdraw_schedule.assert_not_called()

    def test_refund_before_paid_replay_and_recovery_terminalize_exact_upgrade(self):
        from sceneit import billing, billing_ops
        from sceneit.billing_config import (
            BillingCatalog, BillingOffer, BillingProblem, BillingSettings,
            BillingTier,
        )
        from sceneit.billing_provider import (
            ChangePreview, Invoice, InvoiceLine, Subscription,
        )

        now = datetime.now(timezone.utc).replace(microsecond=0)
        end = now + __import__("datetime").timedelta(days=365)
        limits = {
            "imports": 10, "upload_attempts": 10, "analysis_seconds": 10,
            "searches": 10, "media_bytes": 10, "frames": 10,
            "storage_bytes": 10,
        }
        base = BillingTier("base", "Base", 1, ("imports",), limits)
        pro = BillingTier(
            "pro", "Pro", 2, ("imports", "searches"),
            {key: value * 2 for key, value in limits.items()},
        )
        ultra = BillingTier(
            "ultra", "Ultra", 3, ("imports", "searches"),
            {key: value * 3 for key, value in limits.items()},
        )
        offers = [
            BillingOffer(tier.key, "yearly", "usd", f"price_{tier.key}",
                         tier.rank * 1000, "exclusive", "txcd_10000000", True)
            for tier in (base, pro, ultra)
        ]
        catalog = BillingCatalog(
            {tier.key: tier for tier in (base, pro, ultra)},
            {(offer.tier, "yearly", "usd"): offer for offer in offers},
            {offer.price_id: offer for offer in offers},
        )
        settings = BillingSettings(
            enabled=True, environment="test", catalog=catalog,
            prices={offer.tier: offer.price_id for offer in offers},
        )
        preview_id, operation_id, change_id = (
            uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        )
        anchor = now - __import__("datetime").timedelta(days=3)
        with self._connect() as conn:
            conn.execute(
                "UPDATE sceneit_billing_accounts SET allowance_anchor=%s "
                "WHERE owner_id='owner-1'", (anchor,),
            )
            conn.execute(
                "INSERT INTO sceneit_billing_subscriptions(subscription_id,"
                "owner_id,customer_id,environment,price_id,status,"
                "current_period_end,tier_key,cadence,currency) VALUES"
                "('sub_refund','owner-1','cus_1','test','price_pro','active',"
                "%s,'pro','yearly','usd')", (end,),
            )
            conn.execute(
                "INSERT INTO sceneit_paid_coverage(id,owner_id,subscription_id,"
                "starts_at,ends_at,tier_key,tier_rank,capabilities_snapshot,"
                "limits_snapshot,cadence,currency,price_id) VALUES"
                "('in_base_refund','owner-1','sub_refund',%s,%s,'base',1,"
                "'[\"imports\"]',%s,'yearly','usd','price_base')",
                (now - __import__("datetime").timedelta(days=1), end,
                 Jsonb(limits)),
            )
            conn.execute(
                "INSERT INTO sceneit_usage_windows(scope,starts_at,ends_at,"
                "metric,used,allowance) VALUES"
                "('owner:owner-1',%s,%s,'imports',4,10)",
                (anchor, anchor + __import__("datetime").timedelta(days=31)),
            )
            conn.execute(
                "INSERT INTO sceneit_billing_change_previews(preview_id,owner_id,"
                "idempotency_key,parameters_hash,subscription_id,source_price_id,"
                "target_price_id,target_tier_key,target_cadence,currency,kind,"
                "subtotal,tax,total,proration_at,expires_at,state) VALUES"
                "(%s,'owner-1',%s,%s,'sub_refund','price_base','price_pro',"
                "'pro','yearly','usd','upgrade',1000,100,1100,%s,%s,'confirmed')",
                (preview_id, uuid.uuid4(), "a" * 64, now, end),
            )
            conn.execute(
                "INSERT INTO sceneit_billing_operations(operation_id,owner_id,"
                "kind,idempotency_key,parameters_hash,state) VALUES"
                "(%s,'owner-1','upgrade',%s,%s,'confirmed')",
                (operation_id, uuid.uuid4(), "b" * 64),
            )
            conn.execute(
                "INSERT INTO sceneit_billing_changes(change_id,owner_id,preview_id,"
                "operation_id,subscription_id,kind,target_tier_key,target_cadence,"
                "currency,target_price_id,state,pending_invoice_id) VALUES"
                "(%s,'owner-1',%s,%s,'sub_refund','upgrade','pro','yearly','usd',"
                "'price_pro','payment_pending','in_refund_upgrade')",
                (change_id, preview_id, operation_id),
            )
        invoice = Invoice(
            "in_refund_upgrade", "cus_1", "sub_refund", "price_pro", 1100,
            "paid", now, end, False, True, "usd", 1000, 100, 1100,
            True, True, now, None, (
                InvoiceLine("price_pro", 2000, True, now, end),
                InvoiceLine("price_base", -1000, True, now, end),
            ), str(operation_id),
        )
        provider = Mock()
        provider.invoice_id_for_reversal.return_value = invoice.id
        provider.retrieve_invoice.return_value = invoice
        provider.retrieve_subscription.return_value = Subscription(
            "sub_refund", "cus_1", "price_pro", "active", False, end,
            "owner-1", False, "usd", "si_refund",
        )
        provider.verify_price.return_value = 2000
        with patch.object(billing, "connection", self._billing_connection):
            billing._reconcile_event(
                "charge.refunded",
                {"data": {"object": {"id": "ch_refund_upgrade"}}},
                provider, settings,
            )
        provider.invoice_id_for_reversal.assert_called_once_with(
            "charge.refunded", "ch_refund_upgrade"
        )
        replay_event = {
            "id": "evt_refund_upgrade_replay", "type": "invoice.paid",
            "livemode": False, "created": int(now.timestamp()),
            "data": {"object": {"id": invoice.id}},
        }
        with self._connect() as conn:
            conn.execute(
                "UPDATE sceneit_billing_changes SET state='payment_pending' "
                "WHERE change_id=%s", (change_id,),
            )
            conn.execute(
                "UPDATE sceneit_billing_operations SET state='confirmed' "
                "WHERE operation_id=%s", (operation_id,),
            )
            conn.execute(
                "INSERT INTO sceneit_billing_events(event_id,environment,"
                "event_type,state,payload) VALUES(%s,'test',%s,'pending',%s)",
                (replay_event["id"], replay_event["type"], Jsonb(replay_event)),
            )
        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings), \
                patch.object(billing, "StripeBillingProvider",
                             return_value=provider), \
                patch.object(billing_ops, "connection", self._billing_connection), \
                patch.object(billing_ops, "_audit"):
            self.assertEqual(
                ["completed"],
                billing_ops.replay(5, evidence="refund replay fixture"),
            )
        with self._connect() as conn:
            conn.execute(
                "UPDATE sceneit_billing_changes SET state='payment_pending',"
                "updated_at=now()-interval '20 minutes' WHERE change_id=%s",
                (change_id,),
            )
            conn.execute(
                "UPDATE sceneit_billing_operations SET state='confirmed',"
                "updated_at=now()-interval '20 minutes' WHERE operation_id=%s",
                (operation_id,),
            )
        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing_ops, "connection", self._billing_connection), \
                patch.object(billing_ops, "billing_settings",
                             return_value=settings), \
                patch.object(billing_ops, "StripeBillingProvider",
                             return_value=provider), \
                patch.object(billing_ops, "_audit"):
            recovered = billing_ops.recover_operations(
                5, evidence="refund recovery fixture"
            )
        self.assertEqual(1, recovered["resolved"])
        with self._connect() as conn:
            facts = conn.execute(
                "SELECT c.state,o.state,pc.reversed,a.allowance_anchor,"
                "u.used FROM sceneit_billing_changes c "
                "JOIN sceneit_billing_operations o ON o.operation_id=c.operation_id "
                "JOIN sceneit_paid_coverage pc ON pc.id='in_refund_upgrade' "
                "JOIN sceneit_billing_accounts a ON a.owner_id=c.owner_id "
                "JOIN sceneit_usage_windows u ON u.scope='owner:owner-1' "
                "AND u.metric='imports' WHERE c.change_id=%s", (change_id,),
            ).fetchone()
            base_row = conn.execute(
                "SELECT reversed FROM sceneit_paid_coverage "
                "WHERE id='in_base_refund'"
            ).fetchone()
        self.assertEqual(("failed", "failed", True, anchor, 4), facts)
        self.assertEqual((False,), base_row)

        # Stripe now reports Pro, but its increment was refunded. Charging only
        # Pro-to-Ultra must fail before any provider mutation.
        blocked_preview = uuid.uuid4()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sceneit_billing_change_previews(preview_id,owner_id,"
                "idempotency_key,parameters_hash,subscription_id,source_price_id,"
                "target_price_id,target_tier_key,target_cadence,currency,kind,"
                "subtotal,tax,total,proration_at,expires_at,state) VALUES"
                "(%s,'owner-1',%s,%s,'sub_refund','price_pro','price_ultra',"
                "'ultra','yearly','usd','upgrade',1000,100,1100,%s,%s,'open')",
                (blocked_preview, uuid.uuid4(), "c" * 64, now, end),
            )
        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings):
            with self.assertRaises(BillingProblem) as blocked:
                billing.confirm_change(
                    "owner-1", blocked_preview, uuid.uuid4(),
                    purchase_authorized=True, provider=provider,
                )
        self.assertEqual("upgrade_source_coverage_missing", blocked.exception.code)
        provider.confirm_upgrade.assert_not_called()

        # A scheduled full-period transition remains legitimate: it does not
        # sell another unfunded incremental entitlement.
        scheduled_preview = uuid.uuid4()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sceneit_billing_change_previews(preview_id,owner_id,"
                "idempotency_key,parameters_hash,subscription_id,source_price_id,"
                "target_price_id,target_tier_key,target_cadence,currency,kind,"
                "subtotal,tax,total,proration_at,effective_at,expires_at,state) "
                "VALUES(%s,'owner-1',%s,%s,'sub_refund','price_pro','price_base',"
                "'base','yearly','usd','scheduled',1000,100,1100,%s,%s,%s,'open')",
                (
                    scheduled_preview, uuid.uuid4(), "d" * 64, now, end,
                    end + __import__("datetime").timedelta(minutes=30),
                ),
            )
        provider.retrieve_subscription.return_value = Subscription(
            "sub_refund", "cus_1", "price_pro", "active", False, end,
            "owner-1", False, "usd", "si_refund",
        )
        provider.preview_change.return_value = ChangePreview(
            "upcoming_scheduled", "usd", 1000, 100, 1100, end,
            end + __import__("datetime").timedelta(minutes=30), now,
        )
        provider.schedule_change.return_value = "sched_after_refund"
        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings):
            scheduled = billing.confirm_change(
                "owner-1", scheduled_preview, uuid.uuid4(),
                purchase_authorized=True, provider=provider,
            )
        self.assertEqual("scheduled", scheduled["state"])
        self.assertEqual(1, provider.schedule_change.call_count)
        with self._connect() as conn:
            preserved = conn.execute(
                "SELECT a.allowance_anchor,u.used FROM sceneit_billing_accounts a "
                "JOIN sceneit_usage_windows u ON u.scope='owner:'||a.owner_id "
                "AND u.metric='imports' WHERE a.owner_id='owner-1'"
            ).fetchone()
        self.assertEqual((anchor, 4), preserved)