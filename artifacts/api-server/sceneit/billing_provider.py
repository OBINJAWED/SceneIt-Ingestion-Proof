"""Bounded Stripe adapter. Business code depends only on normalized records."""
from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
import httpx
import time
from urllib.parse import urlsplit

from stripe import APIConnectionError, HTTPClient


class BillingProviderError(RuntimeError):
    def __init__(self, code, *, outcome_unknown=False):
        super().__init__(code)
        self.code = code
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True)
class HostedSession:
    id: str
    url: str | None
    expires_at: datetime | None


@dataclass(frozen=True)
class Subscription:
    id: str
    customer_id: str
    price_id: str
    status: str
    cancel_at_period_end: bool
    current_period_end: datetime | None
    owner_id: str | None
    livemode: bool


@dataclass(frozen=True)
class Invoice:
    id: str
    customer_id: str
    subscription_id: str
    price_id: str
    amount_paid: int
    status: str
    starts_at: datetime
    ends_at: datetime
    livemode: bool
    reversed: bool = False


def _time(value):
    return datetime.fromtimestamp(value, timezone.utc) if value is not None else None


def _value(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _identifier(value, prefix):
    if not isinstance(value, str) or not value.startswith(prefix) or len(value) > 255:
        raise BillingProviderError("invalid_provider_response")
    return value


def _hosted_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise BillingProviderError("invalid_provider_response")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname:
        raise BillingProviderError("invalid_provider_response")
    return value


class _BoundedStripeHTTPClient(HTTPClient):
    """Thread-safe aggregate deadline with a fresh event loop per request."""
    name = "sceneit_httpx_bounded"
    MAX_RESPONSE_BYTES = 1024 * 1024

    def __init__(self, deadline, *, transport=None):
        super().__init__()
        self.deadline = deadline
        self.transport = transport

    async def _request(self, method, url, headers, post_data, remaining):
        options = {
            "timeout": httpx.Timeout(min(5, remaining)),
            "follow_redirects": False,
        }
        if self.transport is not None:
            options["transport"] = self.transport
        async with httpx.AsyncClient(**options) as client:
            async with client.stream(
                method, url, headers=headers,
                content=post_data if post_data else None,
            ) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self.MAX_RESPONSE_BYTES:
                        raise APIConnectionError(
                            "Stripe response exceeded the safe size limit"
                        )
                return bytes(content), response.status_code, response.headers

    def request(self, method, url, headers, post_data=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise APIConnectionError("Stripe operation deadline elapsed")
        try:
            return asyncio.run(asyncio.wait_for(
                self._request(method, url, headers, post_data, remaining),
                timeout=remaining,
            ))
        except APIConnectionError:
            raise
        except (TimeoutError, asyncio.TimeoutError, httpx.HTTPError) as exc:
            raise APIConnectionError("Stripe transport failed") from exc


class StripeBillingProvider:
    """Supported Stripe SDK client with no automatic network retries."""

    def __init__(self, settings):
        try:
            import stripe
        except ImportError as exc:
            raise RuntimeError("The Stripe SDK is not installed") from exc
        self._stripe = stripe
        self._deadline = time.monotonic() + 30
        http_client = _BoundedStripeHTTPClient(self._deadline)
        self._client = stripe.StripeClient(
            settings.secret_key, max_network_retries=0, http_client=http_client
        ).v1
        self._webhook_secret = settings.webhook_secret

    def _call(self, function, *args, **kwargs):
        deadline = getattr(self, "_deadline", float("inf"))
        if time.monotonic() >= deadline:
            raise BillingProviderError(
                "provider_deadline_exceeded", outcome_unknown=True
            )
        try:
            result = function(*args, **kwargs)
            if time.monotonic() >= deadline:
                raise BillingProviderError(
                    "provider_deadline_exceeded", outcome_unknown=True
                )
            return result
        except BillingProviderError:
            raise
        except Exception as exc:
            stripe = self._stripe
            uncertain_types = tuple(
                item for item in (
                    getattr(stripe, "APIConnectionError", None),
                    getattr(stripe, "APIError", None),
                ) if isinstance(item, type)
            )
            uncertain = isinstance(exc, (TimeoutError, ConnectionError) + uncertain_types)
            raise BillingProviderError(
                "provider_outcome_unknown" if uncertain else "provider_rejected",
                outcome_unknown=uncertain,
            ) from exc

    def construct_event(self, payload, signature):
        try:
            return self._stripe.Webhook.construct_event(
                payload, signature, self._webhook_secret, tolerance=300
            )
        except Exception as exc:
            raise BillingProviderError("invalid_webhook") from exc

    def create_customer(self, owner_id, idempotency_key):
        result = self._call(
            self._client.customers.create,
            {"metadata": {"sceneit_owner_id": owner_id}},
            options={"idempotency_key": idempotency_key},
        )
        return _identifier(_value(result, "id"), "cus_")

    def create_checkout(self, customer_id, owner_id, price_id, return_url, key):
        result = self._call(
            self._client.checkout.sessions.create,
            {
                "mode": "subscription",
                "customer": customer_id,
                "line_items": [{"price": price_id, "quantity": 1}],
                "success_url": return_url,
                "cancel_url": return_url,
                "client_reference_id": owner_id,
                "metadata": {
                    "sceneit_owner_id": owner_id,
                    "sceneit_checkout_token": key,
                },
                "subscription_data": {"metadata": {"sceneit_owner_id": owner_id}},
            },
            options={"idempotency_key": key},
        )
        return HostedSession(
            _identifier(_value(result, "id"), "cs_"), _hosted_url(_value(result, "url")),
            _time(_value(result, "expires_at")),
        )

    def create_portal(self, customer_id, return_url, configuration):
        config = self._call(
            self._client.billing_portal.configurations.retrieve, configuration
        )
        features = _value(config, "features", {})
        payment = _value(features, "payment_method_update", {})
        cancel = _value(features, "subscription_cancel", {})
        update = _value(features, "subscription_update", {})
        if (
            not bool(_value(config, "active"))
            or not bool(_value(payment, "enabled"))
            or not bool(_value(cancel, "enabled"))
            or _value(cancel, "mode") != "at_period_end"
            or bool(_value(update, "enabled"))
        ):
            raise BillingProviderError("unsafe_portal_configuration")
        params = {"customer": customer_id, "return_url": return_url}
        if configuration:
            params["configuration"] = configuration
        result = self._call(self._client.billing_portal.sessions.create, params)
        return HostedSession(
            _identifier(_value(result, "id"), "bps_"),
            _hosted_url(_value(result, "url")), None,
        )

    def list_subscriptions(self, customer_id):
        result = self._call(
            self._client.subscriptions.list,
            {"customer": customer_id, "status": "all", "limit": 20},
        )
        if bool(_value(result, "has_more")):
            # Never conclude that no duplicate exists from a truncated listing.
            raise BillingProviderError("subscription_listing_truncated")
        return [self._subscription(item) for item in list(_value(result, "data", []))[:20]]

    def verify_price(self, price_id, plan, livemode):
        price = self._call(self._client.prices.retrieve, price_id)
        recurring = _value(price, "recurring", {})
        expected_interval = "month" if plan == "monthly" else "year"
        amount = _value(price, "unit_amount")
        if (
            _value(price, "id") != price_id
            or _value(price, "livemode") is not livemode
            or not bool(_value(price, "active"))
            or _value(price, "type") != "recurring"
            or _value(recurring, "interval") != expected_interval
            or _value(recurring, "interval_count") != 1
            or _value(recurring, "usage_type") != "licensed"
            or isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0
        ):
            raise BillingProviderError("invalid_price_configuration")
        return amount

    def retrieve_checkout(self, session_id):
        result = self._call(self._client.checkout.sessions.retrieve, session_id)
        state = str(_value(result, "status", ""))
        raw_url = _value(result, "url")
        url = (
            _hosted_url(raw_url)
            if raw_url is not None
            else None
        )
        subscription_id = _value(result, "subscription")
        if subscription_id is not None:
            subscription_id = _identifier(subscription_id, "sub_")
        return HostedSession(
            _identifier(_value(result, "id"), "cs_"),
            url, _time(_value(result, "expires_at")),
        ), state, subscription_id

    def find_customer(self, owner_id):
        escaped = owner_id.replace("\\", "\\\\").replace("'", "\\'")
        result = self._call(self._client.customers.search, {
            "query": f"metadata['sceneit_owner_id']:'{escaped}'", "limit": 100,
        })
        if _value(result, "next_page"):
            raise BillingProviderError("customer_listing_truncated")
        matches = [
            item for item in list(_value(result, "data", []))
            if _value(_value(item, "metadata", {}) or {}, "sceneit_owner_id") == owner_id
            and not bool(_value(item, "deleted"))
        ]
        if len(matches) != 1:
            return None
        return _identifier(_value(matches[0], "id"), "cus_")

    def find_checkout(self, customer_id, owner_id, token):
        matches = []
        cursor = None
        for _page in range(5):
            params = {"customer": customer_id, "limit": 100}
            if cursor:
                params["starting_after"] = cursor
            result = self._call(self._client.checkout.sessions.list, params)
            rows = list(_value(result, "data", []))
            for item in rows:
                metadata = _value(item, "metadata", {}) or {}
                if (
                    _value(item, "client_reference_id") == owner_id
                    and _value(metadata, "sceneit_owner_id") == owner_id
                    and _value(metadata, "sceneit_checkout_token") == token
                ):
                    matches.append(item)
            if not bool(_value(result, "has_more")):
                break
            if not rows:
                raise BillingProviderError("checkout_listing_truncated")
            cursor = _value(rows[-1], "id")
        else:
            raise BillingProviderError("checkout_listing_truncated")
        if len(matches) != 1:
            return None
        item = matches[0]
        state = str(_value(item, "status", ""))
        raw_url = _value(item, "url")
        subscription_id = _value(item, "subscription")
        if subscription_id is not None:
            subscription_id = _identifier(subscription_id, "sub_")
        return HostedSession(
            _identifier(_value(item, "id"), "cs_"),
            _hosted_url(raw_url) if raw_url is not None else None,
            _time(_value(item, "expires_at")),
        ), state, subscription_id

    def list_paid_invoice_ids(self, customer_id, limit=20, starting_after=None):
        params = {
            "customer": customer_id, "status": "paid", "limit": min(limit, 100)
        }
        if starting_after:
            params["starting_after"] = starting_after
        result = self._call(
            self._client.invoices.list, params,
        )
        ids = [
            _identifier(_value(item, "id"), "in_")
            for item in list(_value(result, "data", []))[:limit]
        ]
        return ids, (ids[-1] if ids and bool(_value(result, "has_more")) else None)

    def invoice_id_for_reversal(self, event_type, object_id):
        if event_type == "charge.dispute.created":
            event_dispute = self._call(self._client.disputes.retrieve, object_id)
            charge_id = _value(event_dispute, "charge")
        elif event_type == "charge.refunded":
            charge_id = object_id
        else:
            return object_id
        if not isinstance(charge_id, str):
            raise BillingProviderError("invalid_reversal_relationship")
        charge = self._call(self._client.charges.retrieve, charge_id)
        intent_id = _value(charge, "payment_intent")
        if not isinstance(intent_id, str):
            raise BillingProviderError("invalid_reversal_relationship")
        payments = self._call(
            self._client.invoice_payments.list,
            {"payment": {
                "type": "payment_intent", "payment_intent": intent_id,
            }, "limit": 10},
        )
        data = list(_value(payments, "data", []))
        if bool(_value(payments, "has_more")) or len(data) != 1:
            raise BillingProviderError("invalid_reversal_relationship")
        invoice_payment = data[0]
        payment = _value(invoice_payment, "payment", {})
        invoice_id = _value(invoice_payment, "invoice")
        if (
            _value(payment, "type") != "payment_intent"
            or _value(payment, "payment_intent") != intent_id
            or not isinstance(invoice_id, str)
        ):
            raise BillingProviderError("invalid_reversal_relationship")
        if event_type == "charge.refunded":
            refunded = _value(charge, "amount_refunded")
            if not isinstance(refunded, int) or refunded <= 0:
                # A stale refund event cannot reverse currently unreversed money.
                return None
        else:
            disputes = self._call(
                self._client.disputes.list,
                {"charge": charge_id, "limit": 100},
            )
            current = list(_value(disputes, "data", []))
            if bool(_value(disputes, "has_more")):
                raise BillingProviderError("invalid_reversal_relationship")
            matched = next(
                (item for item in current if _value(item, "id") == object_id),
                None,
            )
            if matched is None:
                raise BillingProviderError("invalid_reversal_relationship")
            if any(
                _value(item, "status") not in ("won", "warning_closed")
                for item in current
            ) is False:
                return None
        return invoice_id

    def retrieve_subscription(self, subscription_id):
        result = self._call(self._client.subscriptions.retrieve, subscription_id)
        return self._subscription(result)

    def retrieve_invoice(self, invoice_id):
        result = self._call(
            self._client.invoices.retrieve, invoice_id,
        )
        lines = list(_value(_value(result, "lines", {}), "data", []))
        if len(lines) != 1:
            raise BillingProviderError("invalid_invoice_relationship")
        line = lines[0]
        parent = _value(line, "parent", {})
        details = _value(parent, "subscription_item_details", {})
        subscription_id = (
            _value(result, "subscription")
            or _value(line, "subscription")
            or _value(details, "subscription")
            or _value(
                _value(_value(result, "parent", {}), "subscription_details", {}),
                "subscription",
            )
        )
        period = _value(line, "period", {})
        price = _value(line, "price") or _value(_value(line, "pricing", {}), "price_details", {})
        starts_at = _time(_value(period, "start"))
        ends_at = _time(_value(period, "end"))
        livemode = _value(result, "livemode")
        quantity = _value(line, "quantity")
        line_amount = _value(line, "amount")
        discounts = list(_value(result, "discounts", []) or [])
        discount_amounts = list(_value(line, "discount_amounts", []) or [])
        if (
            starts_at is None or ends_at is None or ends_at <= starts_at
            or quantity != 1 or _value(details, "proration") is not False
            or _value(parent, "type") != "subscription_item_details"
            or discounts or discount_amounts
            or not isinstance(line_amount, int) or line_amount <= 0
            or line_amount != int(_value(result, "amount_paid", 0))
        ):
            raise BillingProviderError("invalid_invoice_relationship")
        if not isinstance(livemode, bool):
            raise BillingProviderError("invalid_provider_response")
        reversed_payment = self._payment_reversed(
            _value(result, "id"), int(_value(result, "amount_paid", 0)),
            livemode,
        )
        return Invoice(
            _identifier(_value(result, "id"), "in_"),
            _identifier(_value(result, "customer"), "cus_"),
            _identifier(subscription_id, "sub_"),
            _identifier(_value(price, "id") or _value(price, "price"), "price_"),
            int(_value(result, "amount_paid", 0)), str(_value(result, "status", "")),
            starts_at, ends_at,
            livemode, reversed_payment,
        )

    def _payment_reversed(self, invoice_id, amount_paid, livemode):
        payments = self._call(
            self._client.invoice_payments.list,
            {"invoice": invoice_id, "status": "paid", "limit": 10},
        )
        data = list(_value(payments, "data", []))
        if bool(_value(payments, "has_more")) or len(data) != 1:
            raise BillingProviderError("invalid_invoice_payment_relationship")
        invoice_payment = data[0]
        payment = _value(invoice_payment, "payment", {})
        if (
            _value(invoice_payment, "invoice") != invoice_id
            or _value(invoice_payment, "amount_paid") != amount_paid
            or _value(invoice_payment, "livemode") is not livemode
            or not bool(_value(invoice_payment, "is_default"))
        ):
            raise BillingProviderError("invalid_invoice_payment_relationship")
        if _value(payment, "type") != "payment_intent":
            raise BillingProviderError("invalid_invoice_payment_relationship")
        intent = self._call(
            self._client.payment_intents.retrieve,
            _value(payment, "payment_intent"),
        )
        charge_id = _value(intent, "latest_charge")
        charge = self._call(self._client.charges.retrieve, charge_id)
        amount = _value(charge, "amount")
        refunded = _value(charge, "amount_refunded", 0)
        if (
            _value(intent, "status") != "succeeded"
            or _value(intent, "amount_received") != amount_paid
            or not bool(_value(charge, "paid"))
            or amount != amount_paid
            or not isinstance(refunded, int)
            or refunded < 0 or refunded > amount_paid
            or _value(charge, "payment_intent")
            != _value(payment, "payment_intent")
            or _value(charge, "livemode") is not livemode
        ):
            raise BillingProviderError("invalid_invoice_payment_relationship")
        disputes = self._call(
            self._client.disputes.list, {"charge": charge_id, "limit": 100}
        )
        dispute_rows = list(_value(disputes, "data", []))
        if bool(_value(disputes, "has_more")):
            raise BillingProviderError("invalid_invoice_payment_relationship")
        active_dispute = any(
            _value(item, "status") not in ("won", "warning_closed")
            for item in dispute_rows
        )
        return refunded > 0 or active_dispute

    def _subscription(self, item):
        items = list(_value(_value(item, "items", {}), "data", []))
        if len(items) != 1:
            raise BillingProviderError("invalid_subscription_relationship")
        price = _value(items[0], "price", {})
        metadata = _value(item, "metadata", {}) or {}
        livemode = _value(item, "livemode")
        if not isinstance(livemode, bool):
            raise BillingProviderError("invalid_provider_response")
        return Subscription(
            _identifier(_value(item, "id"), "sub_"),
            _identifier(_value(item, "customer"), "cus_"),
            _identifier(_value(price, "id"), "price_"), str(_value(item, "status")),
            bool(_value(item, "cancel_at_period_end")),
            _time(_value(items[0], "current_period_end")),
            _value(metadata, "sceneit_owner_id"), livemode,
        )