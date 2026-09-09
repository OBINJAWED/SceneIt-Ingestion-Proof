"""Provider-free checks for normalized billing boundaries."""
from datetime import datetime, timezone
from types import SimpleNamespace
import time
import threading
import unittest
import json

import httpx
import stripe

from sceneit.billing import _iso, _validate_subscription
from sceneit.billing_config import BillingProblem, BillingSettings
from sceneit.billing_provider import (
    StripeBillingProvider, _BoundedStripeHTTPClient,
)
from sceneit.billing_routes import _valid_signature_timestamp


def _settings():
    return BillingSettings(
        enabled=True, environment="test",
        prices={"monthly": "price_monthly", "yearly": "price_yearly"},
    )


class BillingProviderTests(unittest.TestCase):
    def test_webhook_timestamp_rejects_far_future(self):
        self.assertTrue(_valid_signature_timestamp("t=1000,v1=x", 1000))
        self.assertTrue(_valid_signature_timestamp("t=1030,v1=x", 1000))
        self.assertFalse(_valid_signature_timestamp("t=1031,v1=x", 1000))
        self.assertFalse(_valid_signature_timestamp("t=87400,v1=x", 1000))
        self.assertFalse(_valid_signature_timestamp("t=699,v1=x", 1000))

    def test_real_sdk_transport_is_bounded_on_worker_thread(self):
        async def slow(_request):
            await __import__("asyncio").sleep(0.2)
            return httpx.Response(200, json={"id": "price_monthly"})

        transport = _BoundedStripeHTTPClient(
            time.monotonic() + 0.04, transport=httpx.MockTransport(slow)
        )
        client = stripe.StripeClient(
            "sk_test_fixture", http_client=transport, max_network_retries=0,
            base_addresses={"api": "https://stripe.mock"},
        ).v1
        result = []

        def invoke():
            try:
                client.prices.retrieve("price_monthly")
            except Exception as exc:
                result.append(type(exc).__name__)

        started = time.monotonic()
        thread = threading.Thread(target=invoke)
        thread.start()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 0.15)
        self.assertTrue(result)

    def test_real_sdk_decodes_mocked_transport_response(self):
        async def respond(_request):
            return httpx.Response(
                200, content=json.dumps({
                    "id": "price_monthly", "object": "price",
                    "active": True, "livemode": False, "type": "recurring",
                    "unit_amount": 1200,
                    "recurring": {
                        "interval": "month", "interval_count": 1,
                        "usage_type": "licensed",
                    },
                }).encode(),
                headers={"Content-Type": "application/json"},
            )

        transport = _BoundedStripeHTTPClient(
            time.monotonic() + 1, transport=httpx.MockTransport(respond)
        )
        client = stripe.StripeClient(
            "sk_test_fixture", http_client=transport, max_network_retries=0,
            base_addresses={"api": "https://stripe.mock"},
        ).v1
        self.assertEqual(
            client.prices.retrieve("price_monthly").id, "price_monthly"
        )

    def test_latest_invoice_shape_uses_pricing_and_current_payment_state(self):
        adapter = object.__new__(StripeBillingProvider)
        adapter._stripe = SimpleNamespace()
        invoice = {
            "id": "in_1", "customer": "cus_1", "status": "paid",
            "amount_paid": 1200, "livemode": False, "discounts": [],
            "lines": {"data": [{
                "amount": 1200, "quantity": 1, "proration": False,
                "discount_amounts": [], "period": {
                    "start": 1_700_000_000, "end": 1_702_592_000,
                },
                "parent": {
                    "type": "subscription_item_details",
                    "subscription_item_details": {
                        "subscription": "sub_1", "proration": False,
                    },
                },
                "pricing": {
                    "price_details": {"price": "price_monthly"}
                },
            }]},
        }

        def retrieve_invoice(_invoice, params=None):
            self.assertIsNone(params)
            return invoice

        adapter._client = SimpleNamespace(
            invoices=SimpleNamespace(retrieve=retrieve_invoice),
            invoice_payments=SimpleNamespace(list=lambda _params: {
                "has_more": False,
                "data": [{"payment": {
                    "type": "payment_intent",
                    "payment_intent": "pi_1",
                }, "invoice": "in_1", "amount_paid": 1200,
                    "livemode": False, "is_default": True}],
            }),
            payment_intents=SimpleNamespace(
                retrieve=lambda _intent: {
                    "latest_charge": "ch_1", "status": "succeeded",
                    "amount_received": 1200,
                }
            ),
            charges=SimpleNamespace(retrieve=lambda _charge: {
                "paid": True, "amount": 1200, "amount_refunded": 0,
                "disputed": False, "invoice": "in_1", "livemode": False,
                "payment_intent": "pi_1",
            }),
            disputes=SimpleNamespace(list=lambda _params: {
                "has_more": False, "data": [],
            }),
        )
        result = adapter.retrieve_invoice("in_1")
        self.assertEqual(result.price_id, "price_monthly")
        self.assertFalse(result.reversed)

    def test_dispute_is_resolved_through_current_charge_invoice(self):
        adapter = object.__new__(StripeBillingProvider)
        adapter._stripe = SimpleNamespace()
        adapter._client = SimpleNamespace(
            charges=SimpleNamespace(
                retrieve=lambda _charge: {
                    "invoice": "in_1", "livemode": False, "disputed": True,
                    "payment_intent": "pi_1",
                }
            ),
            invoice_payments=SimpleNamespace(list=lambda _params: {
                "has_more": False,
                "data": [{"invoice": "in_1", "payment": {
                    "type": "payment_intent", "payment_intent": "pi_1",
                }}],
            }),
            disputes=SimpleNamespace(
                retrieve=lambda _dispute: {
                    "id": "dp_1", "charge": "ch_1",
                },
                list=lambda _params: {
                    "has_more": False,
                    "data": [{"id": "dp_1", "status": "needs_response"}],
                },
            ),
        )
        self.assertEqual(
            adapter.invoice_id_for_reversal(
                "charge.dispute.created", "dp_1"
            ),
            "in_1",
        )

    def test_price_policy_rejects_metered_or_wrong_interval(self):
        adapter = object.__new__(StripeBillingProvider)
        adapter._stripe = SimpleNamespace()
        price = {
            "id": "price_monthly", "livemode": False, "active": True,
            "type": "recurring", "unit_amount": 1200,
            "recurring": {
                "interval": "month", "interval_count": 1,
                "usage_type": "licensed",
            },
        }
        adapter._client = SimpleNamespace(
            prices=SimpleNamespace(retrieve=lambda _price: price)
        )
        self.assertEqual(
            adapter.verify_price("price_monthly", "monthly", False), 1200
        )
        price["recurring"]["usage_type"] = "metered"
        with self.assertRaises(Exception) as caught:
            adapter.verify_price("price_monthly", "monthly", False)
        self.assertEqual(caught.exception.code, "invalid_price_configuration")

    def test_portal_policy_is_verified_before_session_creation(self):
        adapter = object.__new__(StripeBillingProvider)
        adapter._stripe = SimpleNamespace()
        created = []
        config = {
            "active": True,
            "features": {
                "payment_method_update": {"enabled": True},
                "subscription_cancel": {
                    "enabled": True, "mode": "at_period_end",
                },
                "subscription_update": {"enabled": False},
            },
        }
        adapter._client = SimpleNamespace(
            billing_portal=SimpleNamespace(
                configurations=SimpleNamespace(
                    retrieve=lambda _configuration: config
                ),
                sessions=SimpleNamespace(
                    create=lambda params: (
                        created.append(params)
                        or {"id": "bps_1", "url": "https://billing.stripe.com/x"}
                    )
                ),
            )
        )
        hosted = adapter.create_portal(
            "cus_1", "https://sceneit.example/account", "bpc_1"
        )
        self.assertEqual(hosted.id, "bps_1")
        self.assertEqual(len(created), 1)
        config["features"]["subscription_update"]["enabled"] = True
        with self.assertRaises(Exception) as caught:
            adapter.create_portal(
                "cus_1", "https://sceneit.example/account", "bpc_1"
            )
        self.assertEqual(caught.exception.code, "unsafe_portal_configuration")

    def test_subscription_normalization_keeps_only_relationship_fields(self):
        adapter = object.__new__(StripeBillingProvider)
        result = adapter._subscription({
            "id": "sub_1", "customer": "cus_1", "status": "active",
            "livemode": False, "cancel_at_period_end": True,
            "metadata": {"sceneit_owner_id": "owner-1"},
            "items": {"data": [{
                "price": {"id": "price_monthly"},
                "current_period_end": 1_700_000_000,
            }]},
        })
        self.assertEqual(result.id, "sub_1")
        self.assertEqual(result.owner_id, "owner-1")
        self.assertEqual(result.price_id, "price_monthly")
        self.assertEqual(result.current_period_end.tzinfo, timezone.utc)

    def test_subscription_with_multiple_items_is_rejected(self):
        adapter = object.__new__(StripeBillingProvider)
        with self.assertRaises(Exception) as caught:
            adapter._subscription({
                "items": {"data": [
                    {"price": {"id": "price_monthly"}},
                    {"price": {"id": "price_yearly"}},
                ]}
            })
        self.assertEqual(
            getattr(caught.exception, "code", None),
            "invalid_subscription_relationship",
        )

    def test_relationship_requires_environment_customer_price_and_owner(self):
        valid = SimpleNamespace(
            customer_id="cus_1", livemode=False, price_id="price_monthly",
            owner_id="owner-1",
        )
        _validate_subscription(valid, "owner-1", "cus_1", _settings())
        for change in (
            {"customer_id": "cus_other"},
            {"livemode": True},
            {"price_id": "price_unapproved"},
            {"owner_id": "owner-other"},
            {"owner_id": None},
        ):
            candidate = SimpleNamespace(**{**vars(valid), **change})
            with self.assertRaises(BillingProblem) as caught:
                _validate_subscription(candidate, "owner-1", "cus_1", _settings())
            self.assertEqual(caught.exception.code, "billing_relationship_invalid")

    def test_hosted_expiry_uses_utc_wire_format(self):
        value = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        self.assertEqual(_iso(value), "2025-01-02T03:04:05Z")
        self.assertIsNone(_iso(None))