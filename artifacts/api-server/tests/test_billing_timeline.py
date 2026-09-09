"""Operator timeline pagination stays bounded and never sums delivery amounts."""
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
import io
import json
import unittest
from unittest.mock import patch

from sceneit import billing_ops


class BillingTimelineTests(unittest.TestCase):
    def test_cursor_requires_both_safe_fields_and_timezone(self):
        with self.assertRaises(ValueError):
            billing_ops.timeline(before_received_at=datetime.now(timezone.utc))
        with self.assertRaises(ValueError):
            billing_ops.timeline(
                before_received_at=datetime(2026, 1, 1), before_event_id="evt_test",
            )
        with self.assertRaises(ValueError):
            billing_ops.timeline(payment_key="not-a-payment@example.test")

    def test_cli_emits_timestamps_payment_grouping_and_next_cursor(self):
        instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [{
            "eventId": f"evt_{index}", "type": "invoice.paid",
            "state": "completed", "attempts": 1, "providerCreatedAt": instant,
            "receivedAt": instant, "processedAt": instant,
            "objectType": "invoice", "objectId": "in_fixture",
            "customerId": "cus_fixture", "invoiceId": "in_fixture",
            "subscriptionId": "sub_fixture", "paymentKey": "pi_fixture",
            "currency": "usd", "amount": 1000, "outcome": "completed",
            "errorCode": None,
        } for index in (2, 1)]
        output = io.StringIO()
        with patch.object(billing_ops, "timeline", return_value=rows) as timeline:
            with patch("sys.argv", [
                "billing_ops", "timeline", "--limit", "2",
                "--payment-key", "pi_fixture",
                "--before-received-at", instant.isoformat(),
                "--before-event-id", "evt_3",
            ]), redirect_stdout(output):
                billing_ops.main()
        document = json.loads(output.getvalue())
        self.assertEqual(1, document["pageDistinctPayments"])
        self.assertEqual("evt_1", document["nextCursor"]["eventId"])
        self.assertEqual(instant.isoformat(), document["events"][0]["receivedAt"])
        self.assertEqual("pi_fixture", timeline.call_args.kwargs["payment_key"])
        self.assertNotIn("total", document)

    def test_filters_and_cursor_are_bound_query_parameters(self):
        captured = []

        class Connection:
            def execute(self, query, params):
                captured.append((query, params))
                return self

            def fetchall(self):
                return []

        @contextmanager
        def connection():
            yield Connection()

        instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch.object(billing_ops, "connection", connection):
            self.assertEqual([], billing_ops.timeline(
                999, state="completed", event_type="invoice.paid",
                payment_key="pi_fixture", before_received_at=instant,
                before_event_id="evt_fixture",
            ))
        self.assertEqual(
            ("completed", "invoice.paid", "pi_fixture", instant, "evt_fixture", 100),
            captured[0][1],
        )
        self.assertNotIn("pi_fixture", captured[0][0])