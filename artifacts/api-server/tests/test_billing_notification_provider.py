"""SDK-intercepted notification resolver and bounded CLI checks."""
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import stripe

from sceneit.billing_config import (
    BillingCatalog, BillingOffer, BillingSettings, BillingTier,
)
from sceneit.billing_notification_provider import (
    StripeInvoiceNotificationResolver, resolve_invoice_notification,
)
from sceneit.billing_provider import StripeBillingProvider, _BoundedStripeHTTPClient
from sceneit import billing_notification_ops


def _settings():
    tier = BillingTier("fixture", "Fixture", 1, ("imports",), {"imports": 1})
    offer = BillingOffer(
        "fixture", "monthly", "usd", "price_fixture", 1200,
        "exclusive", "txcd_10000000", False,
    )
    return BillingSettings(
        enabled=True, environment="test",
        catalog=BillingCatalog(
            {"fixture": tier},
            {("fixture", "monthly", "usd"): offer},
            {"price_fixture": offer},
        ),
    )


def _invoice(**changes):
    result = {
        "id": "in_fixture", "object": "invoice", "livemode": False,
        "customer": "cus_fixture", "customer_email": "billing@example.test",
        "subscription": "sub_fixture", "status": "open",
        "collection_method": "charge_automatically", "auto_advance": True,
        "currency": "usd", "amount_remaining": 1200, "amount_paid": 0,
        "attempt_count": 2,
        "lines": {"object": "list", "has_more": False, "data": [{
            "id": "il_fixture", "object": "line_item", "quantity": 1,
            "subscription": "sub_fixture",
            "price": {"id": "price_fixture", "object": "price"},
        }]},
    }
    result.update(changes)
    return result


def _customer(**changes):
    result = {
        "id": "cus_fixture", "object": "customer", "livemode": False,
        "email": "billing@example.test", "deleted": False,
        "metadata": {"sceneit_owner_id": "owner-fixture"},
    }
    result.update(changes)
    return result


def _subscription(**changes):
    result = {
        "id": "sub_fixture", "object": "subscription", "livemode": False,
        "customer": "cus_fixture", "status": "past_due",
        "metadata": {"sceneit_owner_id": "owner-fixture"},
        "items": {"object": "list", "data": [{
            "id": "si_fixture", "object": "subscription_item", "quantity": 1,
            "price": {
                "id": "price_fixture", "object": "price", "currency": "usd",
            },
        }]},
    }
    result.update(changes)
    return result


class NotificationResolverTests(unittest.TestCase):
    def _adapter(self, invoice=None, customer=None, subscription=None):
        adapter = Mock()
        adapter._settings = _settings()
        adapter._client = SimpleNamespace(
            invoices=SimpleNamespace(retrieve=Mock(return_value=invoice or _invoice())),
            invoice_payments=SimpleNamespace(list=Mock(return_value={
                "has_more": False,
                "data": [{
                    "id": "inpay_fixture", "invoice": "in_fixture",
                    "livemode": False, "currency": "usd",
                    "amount_requested": 1200, "is_default": True,
                    "status": "open", "payment": {
                        "type": "payment_intent",
                        "payment_intent": "pi_fixture",
                    },
                }],
            })),
            payment_intents=SimpleNamespace(retrieve=Mock(return_value={
                "id": "pi_fixture", "status": "requires_payment_method",
                "customer": "cus_fixture", "currency": "usd",
                "last_payment_error": {"decline_code": "card_declined"},
            })),
            customers=SimpleNamespace(
                retrieve=Mock(return_value=customer or _customer())
            ),
            subscriptions=SimpleNamespace(
                retrieve=Mock(return_value=subscription or _subscription())
            ),
        )
        adapter._call.side_effect = lambda function, *args: function(*args)
        adapter._payment_reversed.return_value = False
        return adapter

    def test_resolver_uses_current_stripe_customer_not_auth_identity(self):
        adapter = self._adapter()
        fact = resolve_invoice_notification(adapter, "in_fixture")
        self.assertEqual("billing@example.test", fact.recipient)
        self.assertEqual("past_due", fact.state)
        self.assertTrue(fact.eligible)
        adapter._client.customers.retrieve.assert_called_once_with("cus_fixture")

    def test_owner_email_environment_and_price_mismatches_fail_closed(self):
        cases = (
            {"customer": _customer(metadata={"sceneit_owner_id": "other"})},
            {"customer": _customer(email="new@example.test")},
            {"invoice": _invoice(livemode=True)},
            {"subscription": _subscription(items={"data": [{
                "quantity": 1,
                "price": {"id": "price_unknown", "currency": "usd"},
            }]})},
        )
        for values in cases:
            with self.subTest(values=tuple(values)):
                with self.assertRaises(ValueError):
                    resolve_invoice_notification(
                        self._adapter(**values), "in_fixture"
                    )

    def test_paid_void_and_refunded_current_facts_are_suppressed(self):
        paid = self._adapter(invoice=_invoice(
            status="paid", auto_advance=False, amount_remaining=0,
            amount_paid=1200,
        ))
        paid._payment_reversed.return_value = True
        fact = resolve_invoice_notification(paid, "in_fixture")
        self.assertTrue(fact.reversed)
        self.assertTrue(fact.obsolete)
        self.assertFalse(fact.eligible)
        paid._client.invoice_payments.list.assert_not_called()

    def test_modern_payment_relationship_classifies_expiry_and_action(self):
        expired = self._adapter()
        expired._client.payment_intents.retrieve.return_value = {
            "id": "pi_fixture", "status": "requires_payment_method",
            "customer": "cus_fixture", "currency": "usd",
            "last_payment_error": {"decline_code": "expired_card"},
        }
        self.assertEqual(
            "expired_card",
            resolve_invoice_notification(expired, "in_fixture").state,
        )
        action = self._adapter()
        action._client.payment_intents.retrieve.return_value = {
            "id": "pi_fixture", "status": "requires_action",
            "customer": "cus_fixture", "currency": "usd",
        }
        self.assertEqual(
            "action_required",
            resolve_invoice_notification(action, "in_fixture").state,
        )

    def test_void_before_first_failure_never_looks_up_invoice_payments(self):
        adapter = self._adapter(invoice=_invoice(
            status="void", auto_advance=False, amount_remaining=0,
            attempt_count=0,
        ))
        fact = resolve_invoice_notification(adapter, "in_fixture")
        self.assertTrue(fact.obsolete)
        self.assertFalse(fact.eligible)
        adapter._client.invoice_payments.list.assert_not_called()

    def test_real_sdk_http_path_is_intercepted_and_bounded(self):
        seen = []

        async def respond(request):
            seen.append((request.method, request.url.path))
            documents = {
                "/v1/invoices/in_fixture": _invoice(),
                "/v1/customers/cus_fixture": _customer(),
                "/v1/subscriptions/sub_fixture": _subscription(),
                "/v1/invoice_payments": {
                    "object": "list", "has_more": False, "data": [{
                        "id": "inpay_fixture", "object": "invoice_payment",
                        "invoice": "in_fixture", "livemode": False,
                        "currency": "usd", "amount_requested": 1200,
                        "is_default": True, "status": "open",
                        "payment": {
                            "type": "payment_intent",
                            "payment_intent": "pi_fixture",
                        },
                    }],
                },
                "/v1/payment_intents/pi_fixture": {
                    "id": "pi_fixture", "object": "payment_intent",
                    "status": "requires_payment_method",
                    "customer": "cus_fixture", "currency": "usd",
                    "last_payment_error": {"decline_code": "card_declined"},
                },
            }
            return httpx.Response(
                200, content=json.dumps(documents[request.url.path]).encode(),
                headers={"Content-Type": "application/json"},
            )

        adapter = object.__new__(StripeBillingProvider)
        adapter._stripe = stripe
        adapter._settings = _settings()
        adapter._deadline = time.monotonic() + 2
        transport = _BoundedStripeHTTPClient(
            adapter._deadline, transport=httpx.MockTransport(respond)
        )
        adapter._client = stripe.StripeClient(
            "sk_test_fixture", http_client=transport, max_network_retries=0,
            base_addresses={"api": "https://stripe.mock"},
        ).v1
        fact = StripeInvoiceNotificationResolver(
            adapter
        ).resolve_invoice_notification("in_fixture")
        self.assertTrue(fact.eligible)
        self.assertEqual({
            ("GET", "/v1/invoices/in_fixture"),
            ("GET", "/v1/customers/cus_fixture"),
            ("GET", "/v1/subscriptions/sub_fixture"),
            ("GET", "/v1/invoice_payments"),
            ("GET", "/v1/payment_intents/pi_fixture"),
        }, set(seen))


class NotificationCLITests(unittest.TestCase):
    def test_run_once_constructs_one_stripe_resolver_and_is_bounded(self):
        resolver = object()
        with patch.object(
            billing_notification_ops, "_resolver", return_value=resolver
        ) as build, patch.object(
            billing_notification_ops, "enqueue_due_dunning", return_value=1
        ) as enqueue, patch.object(
            billing_notification_ops, "scan_webhook_incidents", return_value=2
        ) as scan, patch.object(
            billing_notification_ops, "dispatch",
            return_value={"claimed": 3, "accepted": 2, "ambiguous": 1},
        ) as dispatch:
            result = billing_notification_ops.run_once(limit=7)
        build.assert_called_once_with()
        enqueue.assert_called_once_with(resolver, limit=7)
        scan.assert_called_once_with(limit=7)
        dispatch.assert_called_once_with(resolver, limit=7)
        self.assertEqual(1, result["ambiguous"])

    def test_mutating_command_requires_explicit_operator_approval(self):
        with patch.object(
            billing_notification_ops, "notification_settings",
            return_value=SimpleNamespace(enabled=True),
        ):
            with self.assertRaises(SystemExit):
                billing_notification_ops.main(["scan"])


if __name__ == "__main__":
    unittest.main()