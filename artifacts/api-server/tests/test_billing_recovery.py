"""Provider-free recovery cursor tests; no Stripe or database connections."""
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from sceneit import billing_ops
from sceneit.billing_provider import BillingProviderError


class _Result:
    def __init__(self, rows=None):
        self.rows = rows or []

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self, accounts, customer_filter=None):
        self.accounts = accounts
        self.customer_filter = customer_filter

    def execute(self, query, params=()):
        if query.startswith("UPDATE sceneit_billing_events"):
            return _Result()
        if "SELECT owner_id,customer_id,customer_attempt_state" in query:
            if "AND customer_id=%s" in query:
                return _Result([
                    account for account in self.accounts
                    if account["customer_id"] == params[1]
                ])
            after, limit = params[1], params[2]
            rows = [
                account for account in self.accounts
                if account["owner_id"] > after
            ][:limit]
            return _Result(rows)
        if "FROM sceneit_billing_checkouts c" in query:
            return _Result()
        if "WHERE environment=%s AND customer_id=%s" in query:
            rows = [
                {"customer_id": account["customer_id"]}
                for account in self.accounts
                if account["customer_id"] == params[1]
            ]
            return _Result(rows)
        if query.startswith("UPDATE sceneit_billing_accounts"):
            return _Result([{"owner_id": params[-1]}])
        raise AssertionError(query)


class _Provider:
    def __init__(self, invoices):
        self.invoices = invoices
        self.listed = []

    def find_customer(self, _owner):
        return None

    def list_paid_invoice_ids(self, customer_id, limit, starting_after=None):
        self.listed.append((customer_id, starting_after))
        invoices = self.invoices.get(customer_id, [])
        start = invoices.index(starting_after) + 1 if starting_after in invoices else 0
        page = invoices[start:start + limit]
        return page, page[-1] if page and start + limit < len(invoices) else None


class BillingRecoveryTests(unittest.TestCase):
    def _run(
        self, accounts, provider, *, owner_after="", limit=20,
        customer_id=None, cursor=None, reconcile=None, clock=None,
    ):
        settings = SimpleNamespace(enabled=True, environment="test")

        @contextmanager
        def connected():
            yield _Connection(accounts)

        audits = []
        with (
            patch.object(billing_ops, "billing_settings", return_value=settings),
            patch.object(billing_ops, "StripeBillingProvider", return_value=provider),
            patch.object(billing_ops, "connection", connected),
            patch.object(
                billing_ops, "_reconcile_event",
                side_effect=reconcile or (lambda *_args: None),
            ),
            patch.object(
                billing_ops, "_audit",
                side_effect=lambda action, _evidence, affected:
                audits.append((action, affected)),
            ),
            patch.object(
                billing_ops.time, "monotonic",
                side_effect=clock or (lambda: 0),
            ),
        ):
            result = billing_ops.recover(
                limit, evidence="operator evidence", owner_after=owner_after,
                customer_id=customer_id, cursor=cursor,
            )
        return result, audits

    def test_one_account_cursor_covers_mixed_phase_pages_without_skips(self):
        accounts = [
            {"owner_id": "a", "customer_id": "cus_a",
             "customer_attempt_state": "created"},
            {"owner_id": "b", "customer_id": "cus_b",
             "customer_attempt_state": "created"},
            {"owner_id": "c", "customer_id": "cus_c",
             "customer_attempt_state": "created"},
            # A far-away uncertain account must not choose the invoice cursor.
            {"owner_id": "z", "customer_id": "cus_z",
             "customer_attempt_state": "uncertain"},
        ]
        provider = _Provider({
            "cus_a": ["in_a"], "cus_b": ["in_b"],
            "cus_c": ["in_c"], "cus_z": ["in_z"],
        })
        first, _ = self._run(accounts, provider, limit=2)
        second, _ = self._run(
            accounts, provider, limit=2, owner_after=first["nextOwner"]
        )
        self.assertEqual(first["nextOwner"], "b")
        self.assertEqual(second["nextOwner"], "z")
        self.assertEqual(
            [customer for customer, _cursor in provider.listed],
            ["cus_a", "cus_b", "cus_c", "cus_z"],
        )

    def test_failed_invoice_retains_owner_and_page_and_is_repeatable(self):
        accounts = [
            {"owner_id": "a", "customer_id": "cus_a",
             "customer_attempt_state": "created"},
            {"owner_id": "b", "customer_id": "cus_b",
             "customer_attempt_state": "created"},
        ]
        provider = _Provider({"cus_a": ["in_a"], "cus_b": ["in_b"]})

        def fail_current(_kind, event, *_args):
            if event["data"]["object"]["id"] == "in_a":
                raise BillingProviderError("temporary")

        result, audits = self._run(accounts, provider, reconcile=fail_current)
        self.assertIsNone(result["nextOwner"])
        self.assertIsNone(result["nextCursor"])
        self.assertEqual(result["nextCustomer"], "cus_a")
        self.assertEqual(result["retryInvoices"], ["in_a"])
        # Failure did not abort the batch, but traversal cannot skip owner a.
        self.assertEqual(provider.listed, [("cus_a", None), ("cus_b", None)])
        self.assertIn(("recover_invoice_failed", 0), audits)

        retry_provider = _Provider({"cus_a": ["in_a"]})
        retried, _ = self._run(
            accounts, retry_provider, customer_id=result["nextCustomer"],
            cursor=result["nextCursor"],
        )
        self.assertEqual(retried["retryInvoices"], [])
        self.assertEqual(retry_provider.listed, [("cus_a", None)])

    def test_deadline_before_next_invoice_retains_unprocessed_invoice(self):
        accounts = [
            {"owner_id": "a", "customer_id": "cus_a",
             "customer_attempt_state": "created"},
        ]
        provider = _Provider({"cus_a": ["in_1", "in_2"]})
        now = [0]

        def reconcile(*_args):
            now[0] = 61

        result, _ = self._run(
            accounts, provider, reconcile=reconcile, clock=lambda: now[0]
        )
        self.assertIsNone(result["nextOwner"])
        self.assertEqual(result["nextCustomer"], "cus_a")
        self.assertEqual(result["retryInvoices"], ["in_2"])
        self.assertIsNone(result["nextCursor"])

    def test_two_invoices_exhaust_limit_without_skipping_next_owner(self):
        accounts = [
            {"owner_id": "a", "customer_id": "cus_a", "customer_attempt_state": "created"},
            {"owner_id": "b", "customer_id": "cus_b", "customer_attempt_state": "created"},
        ]
        provider = _Provider({"cus_a": ["in_a1", "in_a2"], "cus_b": ["in_b"]})
        first, _ = self._run(accounts, provider, limit=2)
        self.assertEqual(first["nextOwner"], "a")
        self.assertEqual(first["unresolvedOwners"], ["b"])
        self.assertEqual(provider.listed, [("cus_a", None)])
        self.assertIsNone(first["nextCustomer"])
        second, _ = self._run(
            accounts, provider, limit=2, owner_after=first["nextOwner"])
        self.assertEqual(second["nextOwner"], "b")
        self.assertEqual(provider.listed, [("cus_a", None), ("cus_b", None)])

    def test_customer_continuation_never_advances_unrelated_owner_batch(self):
        accounts = [
            {"owner_id": "a", "customer_id": "cus_a", "customer_attempt_state": "created"},
            {"owner_id": "b", "customer_id": "cus_b", "customer_attempt_state": "created"},
            {"owner_id": "z", "customer_id": "cus_z", "customer_attempt_state": "created"},
        ]
        provider = _Provider({"cus_a": ["in_a1", "in_a2", "in_a3"]})
        first, _ = self._run(accounts, provider, limit=2, customer_id="cus_a")
        self.assertIsNone(first["nextOwner"])
        self.assertEqual(first["nextCustomer"], "cus_a")
        self.assertEqual(first["nextCursor"], "in_a2")
        self.assertFalse(first["customerComplete"])
        second, _ = self._run(
            accounts, provider, limit=2, customer_id=first["nextCustomer"],
            cursor=first["nextCursor"])
        self.assertIsNone(second["nextOwner"])
        self.assertIsNone(second["nextCustomer"])
        self.assertIsNone(second["nextCursor"])
        self.assertTrue(second["customerComplete"])
        self.assertEqual(second["customerOwner"], "a")
        self.assertEqual(provider.listed, [("cus_a", None), ("cus_a", "in_a2")])

    def test_global_customer_page_then_resume_reaches_next_account(self):
        accounts = [
            {"owner_id": "a", "customer_id": "cus_a", "customer_attempt_state": "created"},
            {"owner_id": "b", "customer_id": "cus_b", "customer_attempt_state": "created"},
        ]
        provider = _Provider({"cus_a": ["in_a1", "in_a2", "in_a3"], "cus_b": ["in_b"]})
        first, _ = self._run(accounts, provider, limit=2)
        self.assertIsNone(first["nextOwner"])
        self.assertEqual(first["nextCustomer"], "cus_a")
        page, _ = self._run(
            accounts, provider, limit=2, customer_id=first["nextCustomer"],
            cursor=first["nextCursor"])
        self.assertTrue(page["customerComplete"])
        final, _ = self._run(
            accounts, provider, limit=2, owner_after=page["customerOwner"])
        self.assertEqual(final["nextOwner"], "b")
        self.assertEqual(provider.listed, [
            ("cus_a", None), ("cus_a", "in_a2"), ("cus_b", None)])

    def test_targeted_listing_failure_keeps_input_cursor(self):
        accounts = [
            {"owner_id": "a", "customer_id": "cus_a", "customer_attempt_state": "created"},
            {"owner_id": "b", "customer_id": "cus_b", "customer_attempt_state": "created"},
        ]
        provider = _Provider({})
        with patch.object(provider, "list_paid_invoice_ids", side_effect=BillingProviderError("deadline")):
            result, _ = self._run(
                accounts, provider, customer_id="cus_a", cursor="in_previous")
        self.assertIsNone(result["nextOwner"])
        self.assertEqual(result["nextCursor"], "in_previous")
        self.assertEqual(result["nextCustomer"], "cus_a")
        self.assertFalse(result["customerComplete"])
        self.assertEqual(result["unresolvedOwners"], ["a"])


if __name__ == "__main__":
    unittest.main()