"""Provider-free checks for normalized billing boundaries."""
from datetime import datetime, timezone
from types import SimpleNamespace
import time
import threading
import unittest
import json
import os
from urllib.parse import parse_qs
from unittest.mock import Mock, patch

import httpx
import stripe

from sceneit.billing import _is_monotone_upgrade, _iso, _validate_subscription
from sceneit.billing_config import (
    BillingProblem, BillingSettings, BillingTier, _catalog,
)
from sceneit.billing_provider import (
    BillingProviderError, StripeBillingProvider, Subscription,
    _BoundedStripeHTTPClient,
)
from sceneit.billing_routes import _valid_signature_timestamp


def _settings():
    return BillingSettings(
        enabled=True, environment="test",
        prices={"monthly": "price_monthly", "yearly": "price_yearly"},
    )


class BillingProviderTests(unittest.TestCase):
    def test_staged_mutation_validation_failures_require_recovery(self):
        adapter = object.__new__(StripeBillingProvider)
        adapter._client = SimpleNamespace(
            subscription_schedules=SimpleNamespace(
                create=Mock(), update=Mock(), release=Mock(),
            ),
            subscriptions=SimpleNamespace(update=Mock()),
        )
        subscription = Subscription(
            "sub_1", "cus_1", "price_old", "active", False,
            datetime.fromtimestamp(1_700_000_000, timezone.utc),
            "owner", False, "usd", "si_1",
        )
        created = {
            "id": "sub_sched_1",
            "phases": [{
                "start_date": 1_700_000_000,
                "end_date": 1_702_592_000,
            }],
        }
        adapter._call = Mock(side_effect=[
            created, BillingProviderError("provider_rate_limited"),
        ])
        remembered = []
        with self.assertRaises(BillingProviderError) as raised:
            adapter.schedule_change(
                subscription, "price_new", "operation",
                on_created=remembered.append,
            )
        self.assertEqual(["sub_sched_1"], remembered)
        self.assertTrue(raised.exception.outcome_unknown)

        adapter._call = Mock(return_value={"id": "sub_sched_wrong"})
        with self.assertRaises(BillingProviderError) as raised:
            adapter.withdraw_schedule("sub_sched_1", "operation")
        self.assertTrue(raised.exception.outcome_unknown)

        adapter._call = Mock(return_value={
            "id": "sub_1", "customer": "cus_wrong", "livemode": False,
            "latest_invoice": {"id": "in_1"}, "metadata": {},
            "items": {"data": []},
        })
        with self.assertRaises(BillingProviderError) as raised:
            adapter.confirm_upgrade(
                subscription, "price_new", "operation",
                datetime.fromtimestamp(1_700_000_000, timezone.utc),
            )
        self.assertTrue(raised.exception.outcome_unknown)

    def test_catalog_retains_historical_prices_without_a_sale_offer(self):
        limits = {
            "imports": 1, "upload_attempts": 1, "analysis_seconds": 1,
            "searches": 1, "media_bytes": 1, "frames": 1,
            "storage_bytes": 1,
        }
        document = {
            "reviewed": True,
            "tiers": [{
                "key": "fixture", "name": "Fixture", "rank": 1,
                "capabilities": ["imports"], "limits": limits,
            }],
            "offers": [
                {
                    "tier": "fixture", "cadence": "monthly", "currency": "usd",
                    "priceId": "price_old", "unitAmount": 1000,
                    "taxBehavior": "exclusive", "taxCode": "txcd_10000000",
                    "saleEnabled": False,
                },
                {
                    "tier": "fixture", "cadence": "monthly", "currency": "usd",
                    "priceId": "price_new", "unitAmount": 1200,
                    "taxBehavior": "exclusive", "taxCode": "txcd_10000000",
                    "saleEnabled": False,
                },
            ],
        }
        with patch.dict(
            os.environ, {"SCENEIT_BILLING_CATALOG": json.dumps(document)}
        ):
            catalog = _catalog()
        self.assertEqual({"price_old", "price_new"}, set(catalog.prices))
        self.assertEqual([], catalog.public_offers())

    def test_upgrade_requires_rank_capabilities_and_limits_to_be_monotone(self):
        base = BillingTier("base", "Base", 1, ("imports",), {"imports": 10})
        higher = BillingTier(
            "higher", "Higher", 2, ("imports", "searches"), {"imports": 20}
        )
        missing = BillingTier("missing", "Missing", 3, (), {"imports": 20})
        reduced = BillingTier("reduced", "Reduced", 3, ("imports",), {"imports": 9})
        self.assertTrue(_is_monotone_upgrade(base, higher))
        self.assertFalse(_is_monotone_upgrade(base, missing))
        self.assertFalse(_is_monotone_upgrade(base, reduced))

    def test_provider_network_guard_allows_only_injected_transport(self):
        transport = _BoundedStripeHTTPClient(time.monotonic() + 1)
        with patch.dict(os.environ, {"SCENEIT_DISABLE_PROVIDER_NETWORK": "1"}):
            with self.assertRaises(Exception):
                transport.request("get", "https://api.stripe.com/v1/prices/x", {})

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

    def test_real_sdk_scheduled_preview_has_no_immediate_proration(self):
        requests = []
        async def respond(request):
            requests.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={
                "id": "upcoming_in_fixture", "object": "invoice",
                "currency": "usd", "subtotal": 1200, "total": 1200,
                "created": 1_700_000_000,
                "total_taxes": [], "automatic_tax": {
                    "enabled": False, "status": "not_collecting",
                },
            })
        adapter = object.__new__(StripeBillingProvider)
        adapter._stripe = stripe
        adapter._deadline = time.monotonic() + 1
        adapter._client = stripe.StripeClient(
            "sk_test_fixture",
            http_client=_BoundedStripeHTTPClient(
                adapter._deadline, transport=httpx.MockTransport(respond)
            ),
            max_network_retries=0,
            base_addresses={"api": "https://stripe.mock"},
        ).v1
        at = datetime.fromtimestamp(1_700_000_000, timezone.utc)
        subscription = Subscription(
            "sub_1", "cus_1", "price_old", "active", False, at,
            "owner", False, "usd", "si_1",
        )
        adapter.preview_change(
            subscription, "price_new", at, kind="scheduled",
            effective_at=at,
        )
        body = requests[0]
        self.assertEqual(
            ["none"], body["subscription_details[proration_behavior]"]
        )
        self.assertEqual(
            ["recurring"], body["preview_mode"],
        )
        self.assertNotIn("subscription_details[billing_cycle_anchor]", body)
        self.assertNotIn("subscription_details[proration_date]", body)

    def test_latest_invoice_shape_uses_pricing_and_current_payment_state(self):
        adapter = object.__new__(StripeBillingProvider)
        adapter._stripe = SimpleNamespace()
        invoice = {
            "id": "in_1", "customer": "cus_1", "status": "paid",
            "amount_paid": 1200, "livemode": False, "discounts": [],
            "subtotal": 1200, "total": 1200, "currency": "usd",
            "created": 1_700_000_000, "total_taxes": [],
            "parent": {"subscription_details": {
                "metadata": {"sceneit_change_id": "operation-fixture"},
            }},
            "automatic_tax": {"enabled": False, "status": "not_collecting"},
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
            refunds=SimpleNamespace(list=lambda _params: {
                "has_more": False, "data": [],
            }),
        )
        result = adapter.retrieve_invoice("in_1")
        self.assertEqual(result.price_id, "price_monthly")
        self.assertFalse(result.reversed)
        self.assertEqual("operation-fixture", result.mutation_id)

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

    def test_only_successful_refunds_reverse_coverage(self):
        adapter = object.__new__(StripeBillingProvider)
        adapter._stripe = SimpleNamespace()
        rows = [{
            "id": "re_1", "status": "pending", "amount": 200,
            "charge": "ch_1",
        }]
        adapter._client = SimpleNamespace(
            refunds=SimpleNamespace(list=lambda _params: {
                "has_more": False, "data": rows,
            })
        )
        self.assertEqual(0, adapter._successful_refund_total("ch_1", 1000))
        rows[0]["status"] = "failed"
        self.assertEqual(0, adapter._successful_refund_total("ch_1", 1000))
        rows[0]["status"] = "succeeded"
        self.assertEqual(200, adapter._successful_refund_total("ch_1", 1000))

    def test_refund_updated_waits_for_fresh_succeeded_refund(self):
        adapter = object.__new__(StripeBillingProvider)
        adapter._stripe = SimpleNamespace()
        refund = {
            "id": "re_1", "status": "pending", "charge": "ch_1",
            "amount": 200,
        }
        adapter._client = SimpleNamespace(
            refunds=SimpleNamespace(
                retrieve=lambda _id: refund,
                list=lambda _params: {
                    "has_more": False, "data": [refund],
                },
            ),
            charges=SimpleNamespace(retrieve=lambda _id: {
                "payment_intent": "pi_1", "amount": 1000,
            }),
            invoice_payments=SimpleNamespace(list=lambda _params: {
                "has_more": False, "data": [{
                    "invoice": "in_1",
                    "payment": {
                        "type": "payment_intent",
                        "payment_intent": "pi_1",
                    },
                }],
            }),
        )
        self.assertIsNone(
            adapter.invoice_id_for_reversal("refund.updated", "re_1")
        )
        refund["status"] = "succeeded"
        self.assertEqual(
            "in_1",
            adapter.invoice_id_for_reversal("refund.updated", "re_1"),
        )

    def test_payment_intent_success_uses_invoice_payments_relationship(self):
        adapter = object.__new__(StripeBillingProvider)
        adapter._stripe = SimpleNamespace()
        adapter._client = SimpleNamespace(
            invoice_payments=SimpleNamespace(list=lambda params: {
                "has_more": False,
                "data": [{
                    "invoice": "in_1",
                    "payment": {
                        "type": "payment_intent",
                        "payment_intent": params["payment"]["payment_intent"],
                    },
                }],
            })
        )
        self.assertEqual(
            "in_1", adapter.invoice_id_for_payment_intent("pi_fixture")
        )

    def test_schedule_update_carries_invoice_mutation_metadata(self):
        adapter = object.__new__(StripeBillingProvider)
        captured = {}
        def create(params, options=None):
            captured["create"] = (params, options)
            return {
                "id": "sub_sched_fixture",
                "phases": [{
                    "start_date": 1_700_000_000,
                    "end_date": 1_702_592_000,
                }],
            }
        def update(schedule_id, params, options=None):
            captured["update"] = (schedule_id, params, options)
            return {"id": schedule_id}
        adapter._client = SimpleNamespace(
            subscription_schedules=SimpleNamespace(
                create=create, update=update,
            )
        )
        adapter._call = lambda function, *args, **kwargs: function(
            *args, **kwargs
        )
        subscription = Subscription(
            "sub_1", "cus_1", "price_old", "active", False,
            datetime.now(timezone.utc), "owner", False, "usd", "si_1",
        )
        created = []
        adapter.schedule_change(
            subscription, "price_new", "operation-fixture",
            on_created=created.append,
        )
        self.assertEqual(["sub_sched_fixture"], created)
        self.assertEqual(
            {"from_subscription": "sub_1"}, captured["create"][0]
        )
        self.assertEqual(
            {"sceneit_change_id": "operation-fixture"},
            captured["update"][1]["metadata"],
        )
        self.assertEqual(
            "operation-fixture-update",
            captured["update"][2]["idempotency_key"],
        )

    def test_schedule_recovery_requires_exact_future_target_and_mutation(self):
        adapter = object.__new__(StripeBillingProvider)
        schedule = {
            "id": "sub_sched_fixture", "status": "not_started",
            "metadata": {"sceneit_change_id": "operation-fixture"},
            "phases": [{"items": [{
                "price": "price_wrong", "quantity": 1,
            }]}],
        }
        adapter._client = SimpleNamespace(
            subscription_schedules=SimpleNamespace(
                retrieve=lambda _schedule: schedule
            )
        )
        adapter._call = lambda function, *args, **kwargs: function(
            *args, **kwargs
        )
        with self.assertRaises(BillingProviderError):
            adapter.retrieve_schedule(
                "sub_sched_fixture", expected_price_id="price_new",
                expected_operation_id="operation-fixture",
            )
        schedule["phases"][0]["items"][0]["price"] = "price_new"
        self.assertEqual(
            "not_started", adapter.retrieve_schedule(
                "sub_sched_fixture", expected_price_id="price_new",
                expected_operation_id="operation-fixture",
            )
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