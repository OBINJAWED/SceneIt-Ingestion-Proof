"""Raw Stripe SDK and PostgreSQL checks for phase-aware withdrawal."""
import time
import threading
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import psycopg
import stripe
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.test_billing_schedule_recovery_postgres import (
    SAFE, TEST_URL, _migration,
)


@unittest.skipUnless(SAFE, "requires a disposable sceneit_test* PostgreSQL database")
class BillingPhaseAwareWithdrawalPostgresTests(unittest.TestCase):
    def setUp(self):
        self.schema = f"sceneit_phase_withdrawal_{uuid.uuid4().hex}"
        with psycopg.connect(TEST_URL, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
            for version in (1, 2, 7, 10, 11, 13, 14):
                conn.execute(_migration(version))

    def tearDown(self):
        with psycopg.connect(TEST_URL, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(self.schema)
            ))

    def _connect(self):
        conn = psycopg.connect(TEST_URL, autocommit=True, row_factory=dict_row)
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
        return conn

    @contextmanager
    def _billing_connection(self):
        with self._connect() as conn:
            yield conn

    def _fixture(
        self, *, effective_at, initial_price, after_price=None, recovery=False,
        provider_released=False, release_gate=None, persisted_price=None,
        recovery_released=True, release_failures=0,
        withdrawal_state="uncertain",
    ):
        from sceneit.billing_config import (
            BillingCatalog, BillingOffer, BillingSettings, BillingTier,
        )
        from sceneit.billing_provider import (
            StripeBillingProvider, _BoundedStripeHTTPClient,
        )
        effective_at = effective_at.replace(microsecond=0)
        limits = {name: 10 for name in (
            "imports", "upload_attempts", "analysis_seconds", "searches",
            "media_bytes", "frames", "storage_bytes",
        )}
        source_tier = BillingTier("source", "Source", 2, ("imports",), limits)
        target_tier = BillingTier("target", "Target", 1, ("imports",), limits)
        source = BillingOffer(
            "source", "monthly", "usd", "price_source", 2000,
            "exclusive", "txcd_10000000", True,
        )
        target = BillingOffer(
            "target", "yearly", "usd", "price_target", 1200,
            "exclusive", "txcd_10000000", True,
        )
        settings = BillingSettings(
            enabled=True, environment="test",
            catalog=BillingCatalog(
                {"source": source_tier, "target": target_tier},
                {
                    ("source", "monthly", "usd"): source,
                    ("target", "yearly", "usd"): target,
                },
                {"price_source": source, "price_target": target},
            ),
            prices={"monthly": "price_source", "yearly": "price_target"},
        )
        preview_id, schedule_operation, change_id = (
            uuid.uuid4(), uuid.uuid4(), uuid.uuid4(),
        )
        withdrawal_operation = uuid.uuid4() if recovery else None
        anchor = effective_at - timedelta(days=9)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sceneit_auth_users(id,first_name) "
                "VALUES('owner-1','Owner')"
            )
            conn.execute(
                "INSERT INTO sceneit_billing_accounts(owner_id,environment,"
                "customer_id,customer_attempt_state,allowance_anchor) "
                "VALUES('owner-1','test','cus_1','created',%s)", (anchor,)
            )
            conn.execute(
                "INSERT INTO sceneit_billing_subscriptions(subscription_id,owner_id,"
                "customer_id,environment,price_id,status,current_period_end,tier_key,"
                "cadence,currency) VALUES('sub_1','owner-1','cus_1','test',%s,"
                "'active',%s,'source','monthly','usd')",
                (persisted_price or initial_price, effective_at),
            )
            conn.execute(
                "INSERT INTO sceneit_paid_coverage(id,owner_id,subscription_id,"
                "starts_at,ends_at,tier_key,tier_rank,capabilities_snapshot,"
                "limits_snapshot,cadence,currency,price_id) VALUES"
                "('in_source','owner-1','sub_1',%s,%s,'source',2,'[\"imports\"]',"
                "%s,'monthly','usd','price_source')",
                (effective_at - timedelta(days=30), effective_at, Jsonb(limits)),
            )
            conn.execute(
                "INSERT INTO sceneit_import_usage(owner_id,imports_used,searches_used) "
                "VALUES('owner-1',5,4)"
            )
            conn.execute(
                "INSERT INTO sceneit_billing_change_previews(preview_id,owner_id,"
                "idempotency_key,parameters_hash,subscription_id,source_price_id,"
                "target_price_id,target_tier_key,target_cadence,currency,kind,"
                "subtotal,tax,total,proration_at,effective_at,expires_at,state) "
                "VALUES(%s,'owner-1',%s,%s,'sub_1','price_source','price_target',"
                "'target','yearly','usd','scheduled',1200,0,1200,now(),%s,"
                "now()+interval '1 day','confirmed')",
                (preview_id, uuid.uuid4(), "a" * 64, effective_at),
            )
            conn.execute(
                "INSERT INTO sceneit_billing_operations(operation_id,owner_id,kind,"
                "idempotency_key,parameters_hash,state,provider_object_id) VALUES"
                "(%s,'owner-1','schedule',%s,%s,'scheduled','sub_sched_1')",
                (schedule_operation, uuid.uuid4(), "b" * 64),
            )
            conn.execute(
                "INSERT INTO sceneit_billing_changes(change_id,owner_id,preview_id,"
                "operation_id,subscription_id,provider_schedule_id,kind,"
                "target_tier_key,target_cadence,currency,target_price_id,state,"
                "effective_at) VALUES(%s,'owner-1',%s,%s,'sub_1','sub_sched_1',"
                "'scheduled','target','yearly','usd','price_target',%s,%s)",
                (
                    change_id, preview_id, schedule_operation,
                    "uncertain" if recovery else "scheduled", effective_at,
                ),
            )
            if recovery:
                conn.execute(
                    "INSERT INTO sceneit_billing_operations(operation_id,owner_id,"
                    "kind,idempotency_key,parameters_hash,state,provider_object_id) "
                    "VALUES(%s,'owner-1','withdraw',%s,%s,%s,'sub_sched_1')",
                    (
                        withdrawal_operation, uuid.uuid4(), "c" * 64,
                        withdrawal_state,
                    ),
                )

        state = {
            "price": initial_price,
            "released": (
                (recovery and recovery_released) or provider_released
            ),
            "after_price": after_price or initial_price,
            "release_attempts": 0,
            "release_failures": release_failures,
            "release_keys": [],
            "anchor": anchor,
        }
        self._last_fixture_state = state
        requests = []

        def schedule():
            released = state["released"]
            return {
                "id": "sub_sched_1", "object": "subscription_schedule",
                "status": "released" if released else "active",
                "customer": "cus_1", "livemode": False,
                "subscription": None if released else "sub_1",
                "released_subscription": "sub_1" if released else None,
                "metadata": {"sceneit_change_id": str(schedule_operation)},
                "phases": [
                    {
                        "start_date": int((effective_at - timedelta(days=30)).timestamp()),
                        "end_date": int(effective_at.timestamp()),
                        "items": [{"price": "price_source", "quantity": 1}],
                    },
                    {
                        "start_date": int(effective_at.timestamp()),
                        "items": [{"price": "price_target", "quantity": 1}],
                    },
                ],
            }

        async def raw(request):
            requests.append((request.method, request.url.path))
            if request.method == "GET" and request.url.path == \
                    "/v1/subscription_schedules/sub_sched_1":
                return httpx.Response(200, json=schedule())
            if request.method == "GET" and request.url.path == "/v1/subscriptions/sub_1":
                price = state["price"]
                period_end = (
                    effective_at if price == "price_source"
                    else effective_at + timedelta(days=365)
                )
                return httpx.Response(200, json={
                    "id": "sub_1", "object": "subscription", "customer": "cus_1",
                    "status": "active", "cancel_at_period_end": False,
                    "livemode": False, "schedule": None if state["released"]
                    else "sub_sched_1",
                    "metadata": {"sceneit_owner_id": "owner-1"},
                    "items": {"data": [{
                        "id": "si_1", "current_period_end": int(period_end.timestamp()),
                        "price": {"id": price, "currency": "usd"},
                    }]},
                })
            if request.method == "POST" and request.url.path == \
                    "/v1/subscription_schedules/sub_sched_1/release":
                state["release_attempts"] += 1
                state["release_keys"].append(request.headers.get("Idempotency-Key"))
                if state["release_attempts"] <= state["release_failures"]:
                    raise httpx.ReadError(
                        "release transport outcome unknown", request=request
                    )
                state["released"] = True
                state["price"] = state["after_price"]
                if release_gate:
                    release_gate[0].set()
                    release_gate[1].wait(3)
                return httpx.Response(200, json=schedule())
            return httpx.Response(500, json={"error": {"message": "unexpected request"}})

        adapter = object.__new__(StripeBillingProvider)
        adapter._stripe = stripe
        adapter._deadline = time.monotonic() + 10
        adapter._client = stripe.StripeClient(
            "sk_test_fixture",
            http_client=_BoundedStripeHTTPClient(
                adapter._deadline, transport=httpx.MockTransport(raw),
            ),
            max_network_retries=0,
            base_addresses={"api": "https://stripe.mock"},
        ).v1
        return settings, adapter, requests, change_id, withdrawal_operation

    def _schedule_event(self, adapter, event_id):
        from sceneit import billing
        return billing.process_event({
            "id": event_id, "type": "subscription_schedule.released",
            "livemode": False, "created": int(time.time()),
            "data": {"object": {
                "id": "sub_sched_1", "customer": "cus_1",
            }},
        }, provider=adapter)

    def _states(self):
        with self._connect() as conn:
            return (
                conn.execute(
                    "SELECT kind,state,last_error_code FROM "
                    "sceneit_billing_operations ORDER BY created_at"
                ).fetchall(),
                conn.execute(
                    "SELECT state FROM sceneit_billing_changes"
                ).fetchone()["state"],
                conn.execute(
                    "SELECT count(*) count,bool_or(reversed) reversed "
                    "FROM sceneit_paid_coverage"
                ).fetchone(),
            )

    def test_withdraw_after_effective_deadline_target_is_not_released(self):
        from sceneit import billing
        from sceneit.billing_config import BillingProblem
        settings, adapter, requests, change_id, _ = self._fixture(
            effective_at=datetime.now(timezone.utc) - timedelta(days=1),
            initial_price="price_target",
        )
        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings):
            with self.assertRaises(BillingProblem) as caught:
                billing.withdraw_change(
                    "owner-1", change_id, uuid.uuid4(), provider=adapter,
                )
        self.assertEqual("change_not_withdrawable", caught.exception.code)
        self.assertFalse(any(path.endswith("/release") for _method, path in requests))
        operations, change_state, coverage = self._states()
        withdrawal = next(row for row in operations if row["kind"] == "withdraw")
        self.assertEqual(("withdraw", "failed", "change_not_withdrawable"),
                         tuple(withdrawal.values()))
        self.assertEqual("effective", change_state)
        self.assertEqual((1, False), (coverage["count"], coverage["reversed"]))

    def test_withdraw_release_race_target_is_never_marked_withdrawn(self):
        from sceneit import billing
        from sceneit.billing_config import BillingProblem
        settings, adapter, requests, change_id, _ = self._fixture(
            effective_at=datetime.now(timezone.utc) + timedelta(days=1),
            initial_price="price_source", after_price="price_target",
        )
        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings):
            with self.assertRaises(BillingProblem) as caught:
                billing.withdraw_change(
                    "owner-1", change_id, uuid.uuid4(), provider=adapter,
                )
        self.assertEqual("change_not_withdrawable", caught.exception.code)
        self.assertEqual(1, sum(path.endswith("/release") for _, path in requests))
        operations, change_state, coverage = self._states()
        withdrawal = next(row for row in operations if row["kind"] == "withdraw")
        self.assertEqual("failed", withdrawal["state"])
        self.assertEqual("effective", change_state)
        self.assertNotEqual("withdrawn", change_state)
        self.assertEqual(1, coverage["count"])

    def test_withdraw_predeadline_source_release_is_withdrawn(self):
        from sceneit import billing
        settings, adapter, requests, change_id, _ = self._fixture(
            effective_at=datetime.now(timezone.utc) + timedelta(days=1),
            initial_price="price_source", after_price="price_source",
        )
        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings):
            result = billing.withdraw_change(
                "owner-1", change_id, uuid.uuid4(), provider=adapter,
            )
        self.assertEqual("withdrawn", result["state"])
        self.assertEqual(1, sum(path.endswith("/release") for _, path in requests))
        operations, change_state, coverage = self._states()
        withdrawal = next(row for row in operations if row["kind"] == "withdraw")
        self.assertEqual("withdrawn", withdrawal["state"])
        self.assertEqual("withdrawn", change_state)
        self.assertEqual(1, coverage["count"])

    def test_uncertain_withdrawal_recovery_is_phase_aware(self):
        from sceneit import billing_ops
        for index, (provider_price, expected_change, expected_operation) in enumerate((
            ("price_target", "effective", "failed"),
            ("price_source", "withdrawn", "completed"),
        )):
            with self.subTest(provider_price=provider_price):
                if index:
                    with self._connect() as conn:
                        conn.execute(
                            "TRUNCATE sceneit_import_usage,"
                            "sceneit_auth_users CASCADE"
                        )
                settings, adapter, requests, _, operation_id = self._fixture(
                    effective_at=datetime.now(timezone.utc) + timedelta(days=1),
                    initial_price=provider_price, recovery=True,
                )
                with patch.object(
                    billing_ops, "connection", self._billing_connection
                ), patch.object(
                    billing_ops, "billing_settings", return_value=settings
                ), patch.object(
                    billing_ops, "StripeBillingProvider", return_value=adapter
                ), patch.object(billing_ops, "_audit"):
                    result = billing_ops.recover_operations(
                        10, evidence="phase-aware fixture",
                    )
                self.assertEqual(1, result["resolved"])
                self.assertEqual([], result["unresolved"])
                operations, change_state, coverage = self._states()
                withdrawal = next(
                    row for row in operations if row["kind"] == "withdraw"
                )
                self.assertEqual(expected_operation, withdrawal["state"])
                self.assertEqual(expected_change, change_state)
                self.assertEqual(1, coverage["count"])
                self.assertFalse(any(path.endswith("/release")
                                     for _method, path in requests))

    def test_released_webhook_with_current_target_marks_effective_without_access(self):
        from sceneit import billing
        effective_at = datetime.now(timezone.utc) - timedelta(hours=1)
        settings, adapter, requests, _, _ = self._fixture(
            effective_at=effective_at, initial_price="price_target",
            provider_released=True,
        )
        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings):
            self.assertEqual(
                "completed", self._schedule_event(adapter, "evt_released_target"),
            )
        operations, change_state, coverage = self._states()
        self.assertEqual("effective", change_state)
        self.assertEqual("completed", next(
            row for row in operations if row["kind"] == "schedule"
        )["state"])
        self.assertEqual((1, False), (coverage["count"], coverage["reversed"]))
        with self._connect() as conn:
            self.assertIsNone(conn.execute(
                "SELECT funded_invoice_id FROM sceneit_billing_changes"
            ).fetchone()["funded_invoice_id"])
        self.assertFalse(any(path.endswith("/release") for _, path in requests))

    def test_released_webhook_with_unchanged_source_period_marks_withdrawn(self):
        from sceneit import billing
        effective_at = datetime.now(timezone.utc) + timedelta(days=1)
        settings, adapter, requests, _, _ = self._fixture(
            effective_at=effective_at, initial_price="price_source",
            provider_released=True,
        )
        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings):
            self.assertEqual(
                "completed", self._schedule_event(adapter, "evt_released_source"),
            )
        operations, change_state, coverage = self._states()
        self.assertEqual("withdrawn", change_state)
        self.assertEqual("completed", next(
            row for row in operations if row["kind"] == "schedule"
        )["state"])
        self.assertEqual(1, coverage["count"])
        self.assertFalse(any(path.endswith("/release") for _, path in requests))

    def test_released_webhook_interleaves_with_target_release_race(self):
        from sceneit import billing
        from sceneit.billing_config import BillingProblem
        started, proceed = threading.Event(), threading.Event()
        settings, adapter, requests, change_id, _ = self._fixture(
            effective_at=datetime.now(timezone.utc) + timedelta(days=1),
            initial_price="price_source", after_price="price_target",
            release_gate=(started, proceed),
        )
        outcomes = []

        def withdraw():
            try:
                billing.withdraw_change(
                    "owner-1", change_id, uuid.uuid4(), provider=adapter,
                )
                outcomes.append("withdrawn")
            except BillingProblem as exc:
                outcomes.append(exc.code)

        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings):
            thread = threading.Thread(target=withdraw)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertEqual(
                "completed", self._schedule_event(adapter, "evt_release_race"),
            )
            proceed.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(["change_not_withdrawable"], outcomes)
        operations, change_state, coverage = self._states()
        self.assertEqual("effective", change_state)
        self.assertNotIn("withdrawn", [row["state"] for row in operations])
        self.assertEqual(1, coverage["count"])
        self.assertEqual(1, sum(path.endswith("/release") for _, path in requests))

    def test_released_webhook_replay_cannot_roll_back_effective_change(self):
        from sceneit import billing
        settings, adapter, requests, _, _ = self._fixture(
            effective_at=datetime.now(timezone.utc) - timedelta(hours=1),
            initial_price="price_target", provider_released=True,
        )
        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings):
            self.assertEqual(
                "completed", self._schedule_event(adapter, "evt_release_first"),
            )
            self.assertEqual(
                "completed", self._schedule_event(adapter, "evt_release_replay"),
            )
        operations, change_state, coverage = self._states()
        self.assertEqual("effective", change_state)
        self.assertEqual("completed", next(
            row for row in operations if row["kind"] == "schedule"
        )["state"])
        self.assertEqual(1, coverage["count"])
        self.assertFalse(any(path.endswith("/release") for _, path in requests))

    def _recover_scheduled_change(self, adapter, settings):
        from sceneit import billing_ops
        with self._connect() as conn:
            conn.execute(
                "UPDATE sceneit_billing_operations SET state='uncertain' "
                "WHERE kind='schedule'"
            )
            conn.execute(
                "UPDATE sceneit_billing_changes SET state='uncertain'"
            )
        with patch.object(
            billing_ops, "connection", self._billing_connection
        ), patch.object(
            billing_ops, "billing_settings", return_value=settings
        ), patch.object(
            billing_ops, "StripeBillingProvider", return_value=adapter
        ), patch.object(billing_ops, "_audit"):
            return billing_ops.recover_operations(
                10, evidence="released schedule fixture",
            )

    def _recover_operations(self, adapter, settings, evidence):
        from sceneit import billing_ops
        with patch.object(
            billing_ops, "connection", self._billing_connection
        ), patch.object(
            billing_ops, "billing_settings", return_value=settings
        ), patch.object(
            billing_ops, "StripeBillingProvider", return_value=adapter
        ), patch.object(billing_ops, "_audit"):
            return billing_ops.recover_operations(10, evidence=evidence)

    def test_unknown_withdrawal_replays_original_key_until_release_is_proven(self):
        from sceneit import billing
        from sceneit.billing_config import BillingProblem

        effective_at = datetime.now(timezone.utc) + timedelta(days=1)
        settings, adapter, requests, change_id, _ = self._fixture(
            effective_at=effective_at,
            initial_price="price_source",
            release_failures=2,
        )
        state = self._last_fixture_state
        customer_key = uuid.uuid4()
        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings):
            with self.assertRaises(BillingProblem) as initial:
                billing.withdraw_change(
                    "owner-1", change_id, customer_key, provider=adapter,
                )
        self.assertEqual("billing_outcome_unknown", initial.exception.code)
        self.assertFalse(state["released"])
        self.assertEqual(1, state["release_attempts"])
        with self._connect() as conn:
            withdrawal = conn.execute(
                "SELECT operation_id,state,idempotency_key FROM "
                "sceneit_billing_operations WHERE kind='withdraw'"
            ).fetchone()
            # Simulate the state written by the prior buggy recovery version.
            conn.execute(
                "UPDATE sceneit_billing_operations SET state='scheduled' "
                "WHERE operation_id=%s", (withdrawal["operation_id"],),
            )

        first = self._recover_operations(
            adapter, settings, "active source retry remains unknown"
        )
        self.assertEqual(0, first["resolved"], first)
        self.assertEqual(
            [str(withdrawal["operation_id"])], first["unresolved"]
        )
        self.assertFalse(state["released"])
        self.assertEqual(2, state["release_attempts"])
        with self._connect() as conn:
            pending = conn.execute(
                "SELECT o.state,c.state change_state,c.target_tier_key,"
                "c.target_cadence,c.target_price_id "
                "FROM sceneit_billing_operations o "
                "JOIN sceneit_billing_changes c "
                "ON c.provider_schedule_id=o.provider_object_id "
                "WHERE o.operation_id=%s", (withdrawal["operation_id"],),
            ).fetchone()
        self.assertEqual(
            ("uncertain", "uncertain", "target", "yearly", "price_target"),
            tuple(pending.values()),
        )

        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings):
            with self.assertRaises(BillingProblem) as replay_pending:
                billing.withdraw_change(
                    "owner-1", change_id, customer_key, provider=adapter,
                )
        self.assertEqual(
            "withdrawal_outcome_unknown", replay_pending.exception.code
        )
        self.assertEqual(2, state["release_attempts"])

        second = self._recover_operations(
            adapter, settings, "original withdrawal key succeeds"
        )
        self.assertEqual((1, []), (
            second["resolved"], second["unresolved"],
        ))
        self.assertTrue(state["released"])
        self.assertEqual(3, state["release_attempts"])
        with self._connect() as conn:
            terminal = conn.execute(
                "SELECT o.state,c.state change_state,"
                "(SELECT count(*) FROM sceneit_paid_coverage "
                "WHERE owner_id='owner-1') coverage_count,"
                "(SELECT bool_or(reversed) FROM sceneit_paid_coverage "
                "WHERE owner_id='owner-1') reversed,"
                "(SELECT imports_used FROM sceneit_import_usage "
                "WHERE owner_id='owner-1') imports_used,"
                "(SELECT searches_used FROM sceneit_import_usage "
                "WHERE owner_id='owner-1') searches_used,"
                "(SELECT allowance_anchor FROM sceneit_billing_accounts "
                "WHERE owner_id='owner-1') allowance_anchor "
                "FROM sceneit_billing_operations o "
                "JOIN sceneit_billing_changes c "
                "ON c.provider_schedule_id=o.provider_object_id "
                "WHERE o.operation_id=%s", (withdrawal["operation_id"],),
            ).fetchone()
        self.assertEqual(
            ("completed", "withdrawn", 1, False, 5, 4, state["anchor"]),
            tuple(terminal.values()),
        )
        expected_provider_key = str(withdrawal["operation_id"])
        self.assertEqual(
            [expected_provider_key, expected_provider_key, expected_provider_key],
            state["release_keys"],
        )
        self.assertEqual(customer_key, withdrawal["idempotency_key"])

        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings):
            replayed = billing.withdraw_change(
                "owner-1", change_id, customer_key, provider=adapter,
            )
        self.assertEqual(
            {"changeId": str(change_id), "state": "withdrawn"}, replayed
        )
        self.assertEqual(3, state["release_attempts"])

    def test_expired_provider_key_active_source_withdrawal_stays_uncertain(self):
        effective_at = datetime.now(timezone.utc) + timedelta(days=1)
        settings, adapter, requests, _change_id, withdrawal_operation = \
            self._fixture(
                effective_at=effective_at,
                initial_price="price_source",
                recovery=True,
                recovery_released=False,
                withdrawal_state="scheduled",
            )
        state = self._last_fixture_state
        with self._connect() as conn:
            conn.execute(
                "UPDATE sceneit_billing_operations SET "
                "created_at=now()-interval '25 hours',"
                "updated_at=now()-interval '25 hours' "
                "WHERE operation_id=%s", (withdrawal_operation,),
            )
        recovered = self._recover_operations(
            adapter, settings, "provider key retention elapsed"
        )
        self.assertEqual(0, recovered["resolved"], recovered)
        self.assertEqual(
            [str(withdrawal_operation)], recovered["unresolved"]
        )
        self.assertEqual(0, state["release_attempts"])
        self.assertFalse(state["released"])
        with self._connect() as conn:
            pending = conn.execute(
                "SELECT o.state,c.state change_state,c.target_tier_key,"
                "(SELECT count(*) FROM sceneit_paid_coverage "
                "WHERE owner_id='owner-1') coverage_count,"
                "(SELECT imports_used FROM sceneit_import_usage "
                "WHERE owner_id='owner-1') imports_used,"
                "(SELECT allowance_anchor FROM sceneit_billing_accounts "
                "WHERE owner_id='owner-1') allowance_anchor "
                "FROM sceneit_billing_operations o "
                "JOIN sceneit_billing_changes c "
                "ON c.provider_schedule_id=o.provider_object_id "
                "WHERE o.operation_id=%s", (withdrawal_operation,),
            ).fetchone()
        self.assertEqual(
            ("uncertain", "uncertain", "target", 1, 5, state["anchor"]),
            tuple(pending.values()),
        )
        self.assertFalse(any(
            method != "GET" for method, _path in requests
        ))

    def test_released_scheduled_operation_recovery_target_is_effective(self):
        effective_at = datetime.now(timezone.utc) - timedelta(hours=1)
        settings, adapter, requests, _, _ = self._fixture(
            effective_at=effective_at, initial_price="price_target",
            persisted_price="price_source", provider_released=True,
        )
        result = self._recover_scheduled_change(adapter, settings)
        self.assertEqual((1, []), (result["resolved"], result["unresolved"]))
        operations, change_state, coverage = self._states()
        self.assertEqual("completed", next(
            row for row in operations if row["kind"] == "schedule"
        )["state"])
        self.assertEqual("effective", change_state)
        with self._connect() as conn:
            subscription = conn.execute(
                "SELECT price_id,tier_key,cadence FROM "
                "sceneit_billing_subscriptions WHERE subscription_id='sub_1'"
            ).fetchone()
        self.assertEqual(
            ("price_target", "target", "yearly"), tuple(subscription.values()),
        )
        self.assertEqual(1, coverage["count"])
        self.assertFalse(any(method != "GET" for method, _path in requests))

    def test_released_scheduled_operation_recovery_source_is_withdrawn(self):
        settings, adapter, requests, _, _ = self._fixture(
            effective_at=datetime.now(timezone.utc) + timedelta(days=1),
            initial_price="price_source", provider_released=True,
        )
        result = self._recover_scheduled_change(adapter, settings)
        self.assertEqual((1, []), (result["resolved"], result["unresolved"]))
        operations, change_state, coverage = self._states()
        self.assertEqual("completed", next(
            row for row in operations if row["kind"] == "schedule"
        )["state"])
        self.assertEqual("withdrawn", change_state)
        self.assertEqual(1, coverage["count"])
        self.assertFalse(any(method != "GET" for method, _path in requests))

    def test_released_scheduled_operation_recovery_ambiguity_stays_blocked(self):
        settings, adapter, requests, _, _ = self._fixture(
            effective_at=datetime.now(timezone.utc) + timedelta(days=1),
            initial_price="price_ambiguous", persisted_price="price_source",
            provider_released=True,
        )
        result = self._recover_scheduled_change(adapter, settings)
        self.assertEqual(0, result["resolved"])
        self.assertEqual(1, len(result["unresolved"]))
        operations, change_state, coverage = self._states()
        self.assertEqual("uncertain", next(
            row for row in operations if row["kind"] == "schedule"
        )["state"])
        self.assertEqual("uncertain", change_state)
        self.assertEqual(1, coverage["count"])
        self.assertFalse(any(method != "GET" for method, _path in requests))