"""Disposable-PostgreSQL concurrency checks for the notification outbox."""
import os
import threading
import unittest
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from sceneit import billing_notifications
from sceneit.billing_notifications import InvoiceNotificationFact
from sceneit.billing_smtp import NotificationSettings


TEST_URL = os.environ.get("SCENEIT_TEST_DATABASE_URL", "")
SAFE_TEST_DATABASE = (
    bool(TEST_URL)
    and TEST_URL != os.environ.get("DATABASE_URL")
    and conninfo_to_dict(TEST_URL).get("dbname", "").startswith("sceneit_test")
)
MIGRATION = (
    Path(__file__).parents[1]
    / "sceneit" / "migrations" / "012_billing_notifications.sql"
).read_text(encoding="utf-8")


@unittest.skipUnless(
    SAFE_TEST_DATABASE,
    "SCENEIT_TEST_DATABASE_URL must name a disposable sceneit_test* database",
)
class BillingNotificationPostgresTests(unittest.TestCase):
    def setUp(self):
        self.schema = f"sceneit_notification_test_{uuid.uuid4().hex}"
        with psycopg.connect(TEST_URL, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(
                sql.Identifier(self.schema)
            ))
            conn.execute(sql.SQL("SET search_path TO {}").format(
                sql.Identifier(self.schema)
            ))
            conn.execute(MIGRATION)
            # The core migration owns this table; this narrow compatible shape
            # isolates notification scanning from unrelated billing fixtures.
            conn.execute(
                "CREATE TABLE sceneit_billing_events ("
                "event_id text PRIMARY KEY,state text NOT NULL,attempts integer NOT NULL,"
                "last_error_code text,updated_at timestamptz NOT NULL)"
            )
        self.settings = NotificationSettings(
            enabled=True, scheduler_enabled=True, dunning_enabled=True,
            alerts_enabled=True,
            message_domain="mail.example.test", schedule_hours=(0, 24),
            max_attempts=3, lease_seconds=60, timeout_seconds=50,
            operator_recipient="ops@example.test",
            alert_cooldown_seconds=3600,
            pending_age_seconds=300, stalled_age_seconds=300,
        )

    def tearDown(self):
        with psycopg.connect(TEST_URL, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(self.schema)
            ))

    @contextmanager
    def _connection(self):
        with psycopg.connect(TEST_URL, row_factory=dict_row) as conn:
            conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(
                sql.Identifier(self.schema)
            ))
            yield conn

    def _raw(self):
        conn = psycopg.connect(TEST_URL, autocommit=True, row_factory=dict_row)
        conn.execute(sql.SQL("SET search_path TO {}").format(
            sql.Identifier(self.schema)
        ))
        return conn

    def test_concurrent_due_claims_do_not_duplicate_campaign_steps(self):
        now = datetime(2025, 1, 1, tzinfo=timezone.utc)
        with self._raw() as conn:
            for number in (1, 2):
                conn.execute(
                    "INSERT INTO sceneit_billing_notification_campaigns"
                    "(environment,invoice_id,problem_id,state,next_attempt_at,"
                    "last_checked_at,created_at) VALUES "
                    "('test',%s,%s,'active',%s,%s,%s)",
                    (f"in_{number}", f"problem_{number}", now, now, now),
                )
        barrier = threading.Barrier(2)

        class Resolver:
            def resolve_invoice_notification(self, invoice_id):
                barrier.wait(timeout=5)
                return InvoiceNotificationFact(
                    invoice_id=invoice_id, recipient="billing@example.test",
                    state="past_due", tier="fixture_tier", currency="usd",
                    amount_due=100, attempts=1,
                )

        results = []

        def enqueue():
            results.append(billing_notifications.enqueue_due_dunning(
                Resolver(), now=now, limit=1
            ))

        with patch.object(
            billing_notifications, "notification_settings",
            return_value=self.settings,
        ), patch.object(
            billing_notifications, "connection", self._connection,
        ):
            threads = [threading.Thread(target=enqueue) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([1, 1], sorted(results))
        with self._raw() as conn:
            deliveries = conn.execute(
                "SELECT source_id,source_sequence,message_id FROM "
                "sceneit_billing_notification_deliveries"
            ).fetchall()
        self.assertEqual(2, len(deliveries))
        self.assertEqual(2, len({row["source_id"] for row in deliveries}))
        self.assertEqual(2, len({row["message_id"] for row in deliveries}))
        self.assertTrue(all(row["source_sequence"] == 0 for row in deliveries))

    def test_expired_delivery_lease_becomes_ambiguous_and_stops_campaign(self):
        now = datetime(2025, 1, 2, tzinfo=timezone.utc)
        with self._raw() as conn:
            campaign = conn.execute(
                "INSERT INTO sceneit_billing_notification_campaigns"
                "(environment,invoice_id,problem_id,state,next_attempt_at,"
                "last_checked_at) VALUES "
                "('test','in_lease','problem_lease','active',%s,%s) RETURNING id",
                (now, now),
            ).fetchone()
            conn.execute(
                "INSERT INTO sceneit_billing_notification_deliveries"
                "(source_type,source_id,source_sequence,message_kind,state,"
                "message_id,next_attempt_at,lease_token,lease_expires_at) VALUES "
                "('dunning',%s,0,'dunning','leased',"
                "'<lease@mail.example.test>',%s,%s,%s)",
                (campaign["id"], now, uuid.uuid4(), now - timedelta(seconds=1)),
            )
        with patch.object(
            billing_notifications, "connection", self._connection,
        ):
            claimed = billing_notifications._claim_delivery(self.settings, now)
        self.assertIsNone(claimed)
        with self._raw() as conn:
            campaign_state = conn.execute(
                "SELECT state FROM sceneit_billing_notification_campaigns"
            ).fetchone()["state"]
            delivery_state = conn.execute(
                "SELECT state FROM sceneit_billing_notification_deliveries"
            ).fetchone()["state"]
        self.assertEqual("needs_review", campaign_state)
        self.assertEqual("ambiguous", delivery_state)

    def test_incident_cooldown_and_resolution_notice_are_deduplicated(self):
        now = datetime(2025, 1, 3, tzinfo=timezone.utc)
        with self._raw() as conn:
            conn.execute(
                "INSERT INTO sceneit_billing_events"
                "(event_id,state,attempts,last_error_code,updated_at) "
                "VALUES ('evt_safe','rejected',2,'invalid_shape',%s)", (now,),
            )
        with patch.object(
            billing_notifications, "notification_settings",
            return_value=self.settings,
        ), patch.object(
            billing_notifications, "connection", self._connection,
        ):
            self.assertEqual(1, billing_notifications.scan_webhook_incidents(now=now))
            self.assertEqual(0, billing_notifications.scan_webhook_incidents(
                now=now + timedelta(minutes=5)
            ))
            with self._raw() as conn:
                conn.execute(
                    "UPDATE sceneit_billing_events SET state='completed',updated_at=%s "
                    "WHERE event_id='evt_safe'", (now + timedelta(minutes=6),),
                )
            self.assertEqual(1, billing_notifications.scan_webhook_incidents(
                now=now + timedelta(minutes=6)
            ))
        with self._raw() as conn:
            kinds = [
                row["message_kind"] for row in conn.execute(
                    "SELECT message_kind FROM "
                    "sceneit_billing_notification_deliveries ORDER BY created_at,id"
                ).fetchall()
            ]
            state = conn.execute(
                "SELECT state FROM sceneit_billing_notification_incidents"
            ).fetchone()["state"]
        self.assertEqual(["incident_open", "incident_resolved"], kinds)
        self.assertEqual("resolved", state)

    def _incident_delivery(self, conn, suffix, now):
        incident = conn.execute(
            "INSERT INTO sceneit_billing_notification_incidents"
            "(incident_key,event_id,incident_type,state,reason,attempts,"
            "first_seen_at,last_seen_at) VALUES "
            "(%s,%s,'rejected','active','fixture_error',1,%s,%s) RETURNING id",
            (f"rejected:evt_{suffix}", f"evt_{suffix}", now, now),
        ).fetchone()
        conn.execute(
            "INSERT INTO sceneit_billing_notification_deliveries"
            "(source_type,source_id,source_sequence,message_kind,state,"
            "message_id,next_attempt_at) VALUES "
            "('incident',%s,1,'incident_open','queued',%s,%s)",
            (incident["id"], f"<{suffix}@mail.example.test>", now),
        )

    def test_slow_batch_gets_a_fresh_lease_for_each_claim(self):
        now = datetime(2025, 1, 4, tzinfo=timezone.utc)
        with self._raw() as conn:
            self._incident_delivery(conn, "slow-1", now)
            self._incident_delivery(conn, "slow-2", now)
        clock_value = [now]

        class SlowMailer:
            def send(self, *_args, **_kwargs):
                clock_value[0] += timedelta(seconds=40)

        with patch.object(
            billing_notifications, "notification_settings",
            return_value=self.settings,
        ), patch.object(
            billing_notifications, "connection", self._connection,
        ):
            result = billing_notifications.dispatch(
                object(), mailer=SlowMailer(), clock=lambda: clock_value[0],
                limit=2,
            )
        self.assertEqual(2, result["accepted"])
        self.assertEqual(0, result["ambiguous"])

    def test_concurrent_expiry_marks_inflight_delivery_ambiguous(self):
        now = datetime(2025, 1, 5, tzinfo=timezone.utc)
        with self._raw() as conn:
            self._incident_delivery(conn, "concurrent", now)
        settings = replace(self.settings, lease_seconds=1, timeout_seconds=1)
        clock_value = [now]
        entered = threading.Event()
        release = threading.Event()
        result = []

        class BlockingMailer:
            def send(self, *_args, **_kwargs):
                entered.set()
                release.wait(5)

        def worker():
            result.append(billing_notifications.dispatch(
                object(), mailer=BlockingMailer(),
                clock=lambda: clock_value[0], limit=1,
            ))

        with patch.object(
            billing_notifications, "notification_settings",
            return_value=settings,
        ), patch.object(
            billing_notifications, "connection", self._connection,
        ):
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(entered.wait(5))
            clock_value[0] = now + timedelta(seconds=2)
            self.assertIsNone(
                billing_notifications._claim_delivery(settings, clock_value[0])
            )
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(1, result[0]["ambiguous"])
        self.assertEqual(0, result[0]["accepted"])
        with self._raw() as conn:
            state = conn.execute(
                "SELECT state FROM sceneit_billing_notification_deliveries"
            ).fetchone()["state"]
        self.assertEqual("ambiguous", state)


if __name__ == "__main__":
    unittest.main()