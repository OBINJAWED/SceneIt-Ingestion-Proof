"""Bounded Stripe adapter. Business code depends only on normalized records."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import asyncio
import httpx
import os
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
    currency: str | None = None
    item_id: str | None = None
    schedule_id: str | None = None


@dataclass(frozen=True)
class ScheduleChange:
    status: str
    subscription: Subscription


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
    currency: str | None = None
    subtotal: int | None = None
    tax: int | None = None
    total: int | None = None
    tax_complete: bool = False
    proration: bool = False
    provider_created_at: datetime | None = None
    funds_coverage_id: str | None = None
    line_facts: tuple = ()
    mutation_id: str | None = None


@dataclass(frozen=True)
class InvoiceLine:
    price_id: str
    amount: int
    proration: bool
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class ChangePreview:
    id: str
    currency: str
    subtotal: int
    tax: int
    total: int
    effective_at: datetime | None
    expires_at: datetime
    proration_at: datetime


@dataclass(frozen=True)
class PaymentProblem:
    invoice_id: str
    customer_id: str
    subscription_id: str | None
    created_at: datetime
    currency: str | None
    amount_due: int | None
    code: str
    next_action: str
    livemode: bool
    obsolete: bool = False


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


def _preview_identifier(value):
    if (
        not isinstance(value, str) or len(value) > 255
        or not value.startswith(("in_", "upcoming_in_"))
    ):
        raise BillingProviderError("invalid_provider_response")
    return value


def _hosted_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise BillingProviderError("invalid_provider_response")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname:
        raise BillingProviderError("invalid_provider_response")
    return value


def re_full_currency(value):
    return (
        isinstance(value, str) and len(value) == 3
        and value.isascii() and value.islower() and value.isalpha()
    )


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
        if (
            self.transport is None
            and os.environ.get("SCENEIT_DISABLE_PROVIDER_NETWORK") == "1"
        ):
            raise APIConnectionError("Stripe network is disabled by operator policy")
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
        self._settings = settings

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

    def create_checkout(
        self, customer_id, owner_id, price_id, return_url, key,
        *, tax_id_collection=True,
    ):
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
                "billing_address_collection": "required",
                "customer_update": {"address": "auto", "name": "auto"},
                "automatic_tax": {"enabled": True},
                "tax_id_collection": {"enabled": bool(tax_id_collection)},
            },
            options={"idempotency_key": key},
        )
        return HostedSession(
            _identifier(_value(result, "id"), "cs_"), _hosted_url(_value(result, "url")),
            _time(_value(result, "expires_at")),
        )

    def create_portal(
        self, customer_id, return_url, configuration, *,
        action="manage", subscription_id=None,
    ):
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
        if action == "cancel":
            if not subscription_id:
                raise BillingProviderError("invalid_portal_action")
            params["flow_data"] = {
                "type": "subscription_cancel",
                "subscription_cancel": {"subscription": subscription_id},
                "after_completion": {
                    "type": "redirect",
                    "redirect": {"return_url": return_url},
                },
            }
        elif action != "manage":
            raise BillingProviderError("invalid_portal_action")
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

    def verify_price(
        self, price_id, plan, livemode, *,
        currency=None, unit_amount=None, tax_behavior=None, tax_code=None,
        require_active=True,
    ):
        price = self._call(
            self._client.prices.retrieve, price_id,
            *([{"expand": ["product"]}] if tax_code is not None else []),
        )
        recurring = _value(price, "recurring", {})
        expected_interval = "month" if plan == "monthly" else "year"
        amount = _value(price, "unit_amount")
        product = _value(price, "product", {})
        if (
            _value(price, "id") != price_id
            or _value(price, "livemode") is not livemode
            or (require_active and not bool(_value(price, "active")))
            or _value(price, "type") != "recurring"
            or _value(recurring, "interval") != expected_interval
            or _value(recurring, "interval_count") != 1
            or _value(recurring, "usage_type") != "licensed"
            or isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0
            or (currency is not None and _value(price, "currency") != currency)
            or (unit_amount is not None and amount != unit_amount)
            or (
                tax_behavior is not None
                and _value(price, "tax_behavior") != tax_behavior
            )
            or (
                tax_code is not None
                and _value(product, "tax_code") != tax_code
            )
        ):
            raise BillingProviderError("invalid_price_configuration")
        return amount

    def preview_change(
        self, subscription, target_price_id, proration_at, *,
        kind="upgrade", effective_at=None,
    ):
        details = {
            "items": [{
                "id": subscription.item_id,
                "price": target_price_id,
                "quantity": 1,
            }],
            "proration_behavior": (
                "always_invoice" if kind == "upgrade" else "none"
            ),
        }
        if kind == "upgrade":
            details["proration_date"] = int(proration_at.timestamp())
        elif kind == "scheduled" and effective_at is not None:
            pass
        else:
            raise BillingProviderError("invalid_change_preview")
        params = {
            "customer": subscription.customer_id,
            "subscription": subscription.id,
            "subscription_details": details,
            "automatic_tax": {"enabled": True},
        }
        if kind == "scheduled":
            params["preview_mode"] = "recurring"
        result = self._call(self._client.invoices.create_preview, params)
        currency = _value(result, "currency")
        subtotal = _value(result, "subtotal")
        total = _value(result, "total")
        tax = sum(
            _value(item, "amount", 0)
            for item in list(_value(result, "total_taxes", []) or [])
        )
        created = _time(_value(result, "created"))
        if (
            not isinstance(currency, str) or not currency.islower()
            or len(currency) != 3 or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (subtotal, tax, total)
            )
            or total <= 0 or total not in (subtotal, subtotal + tax)
            or _value(_value(result, "automatic_tax", {}), "status")
            not in ("complete", "not_collecting")
            or created is None
        ):
            raise BillingProviderError("invalid_change_preview")
        return ChangePreview(
            _preview_identifier(_value(result, "id")), currency,
            subtotal, tax, total, None,
            created + timedelta(minutes=30),
            proration_at,
        )

    def confirm_upgrade(
        self, subscription, target_price_id, operation_id, proration_at,
    ):
        result = self._call(
            self._client.subscriptions.update,
            subscription.id,
            {
                "items": [{
                    "id": subscription.item_id,
                    "price": target_price_id,
                    "quantity": 1,
                }],
                "payment_behavior": "pending_if_incomplete",
                "proration_behavior": "always_invoice",
                "proration_date": int(proration_at.timestamp()),
                "metadata": {"sceneit_change_id": operation_id},
                "expand": ["latest_invoice.confirmation_secret"],
            },
            options={"idempotency_key": operation_id},
        )
        latest_invoice = _value(result, "latest_invoice")
        try:
            return (
                self._subscription(result),
                _identifier(
                    latest_invoice if isinstance(latest_invoice, str)
                    else _value(latest_invoice, "id"),
                    "in_",
                ),
            )
        except BillingProviderError as exc:
            # The subscription mutation has already succeeded; malformed
            # response normalization cannot prove that no side effect occurred.
            raise BillingProviderError(exc.code, outcome_unknown=True) from exc

    def schedule_change(
        self, subscription, target_price_id, operation_id, *, on_created=None,
        effective_at=None,
    ):
        schedule = self._call(
            self._client.subscription_schedules.create,
            {"from_subscription": subscription.id},
            options={"idempotency_key": operation_id + "-create"},
        )
        try:
            schedule_id = _identifier(_value(schedule, "id"), "sub_sched_")
            if on_created is not None:
                on_created(schedule_id)
            phases = list(_value(schedule, "phases", []) or [])
            if not phases:
                raise BillingProviderError("invalid_schedule_relationship")
            if (
                effective_at is not None
                and _value(phases[0], "end_date")
                != int(effective_at.timestamp())
            ):
                raise BillingProviderError("schedule_effective_at_changed")
        except BillingProviderError as exc:
            raise BillingProviderError(exc.code, outcome_unknown=True) from exc
        current = phases[0]
        try:
            updated = self._call(
                self._client.subscription_schedules.update,
                schedule_id,
                {
                "end_behavior": "release",
                "phases": [
                    {
                        "items": [{"price": subscription.price_id, "quantity": 1}],
                        "start_date": _value(current, "start_date"),
                        "end_date": _value(current, "end_date"),
                        "proration_behavior": "none",
                    },
                    {
                        "items": [{"price": target_price_id, "quantity": 1}],
                        "start_date": _value(current, "end_date"),
                        "proration_behavior": "none",
                    },
                ],
                "metadata": {"sceneit_change_id": operation_id},
                },
                options={"idempotency_key": operation_id + "-update"},
            )
        except BillingProviderError as exc:
            raise BillingProviderError(exc.code, outcome_unknown=True) from exc
        if _value(updated, "id") != schedule_id:
            raise BillingProviderError(
                "invalid_schedule_relationship", outcome_unknown=True
            )
        return schedule_id

    def find_schedule(self, customer_id, operation_id, subscription_id):
        matches = []
        cursor = None
        for _page in range(5):
            params = {"customer": customer_id, "limit": 100}
            if cursor:
                params["starting_after"] = cursor
            result = self._call(
                self._client.subscription_schedules.list, params
            )
            rows = list(_value(result, "data", []))
            for item in rows:
                metadata = _value(item, "metadata", {}) or {}
                linked = _value(item, "subscription")
                if (
                    (
                        _value(metadata, "sceneit_change_id") == operation_id
                        and linked in (None, subscription_id)
                    )
                    or (
                        not _value(metadata, "sceneit_change_id")
                        and linked == subscription_id
                    )
                ):
                    matches.append(item)
            if not bool(_value(result, "has_more")):
                break
            if not rows:
                raise BillingProviderError("schedule_listing_truncated")
            cursor = _value(rows[-1], "id")
        else:
            raise BillingProviderError("schedule_listing_truncated")
        if len(matches) != 1:
            return None
        return _identifier(_value(matches[0], "id"), "sub_sched_")

    def withdraw_schedule(self, schedule_id, operation_id):
        result = self._call(
            self._client.subscription_schedules.release,
            schedule_id, {},
            options={"idempotency_key": operation_id},
        )
        if (
            _value(result, "id") != schedule_id
            or _value(result, "status") != "released"
        ):
            raise BillingProviderError(
                "invalid_schedule_relationship", outcome_unknown=True
            )
        return schedule_id

    def retrieve_schedule_change(
        self, schedule_id, *, expected_customer_id, expected_subscription_id,
        expected_source_price_id, expected_target_price_id,
        expected_operation_id, expected_effective_at, livemode,
    ):
        """Return fresh schedule and subscription state after exact validation."""
        result = self._call(
            self._client.subscription_schedules.retrieve, schedule_id
        )
        status = _value(result, "status")
        linked = (
            _value(result, "released_subscription")
            if status == "released" else _value(result, "subscription")
        )
        if isinstance(linked, dict):
            linked = _value(linked, "id")
        metadata = _value(result, "metadata", {}) or {}
        phases = list(_value(result, "phases", []) or [])
        source_items = (
            list(_value(phases[0], "items", []) or [])
            if len(phases) == 2 else []
        )
        target_items = (
            list(_value(phases[1], "items", []) or [])
            if len(phases) == 2 else []
        )
        effective_timestamp = int(expected_effective_at.timestamp())
        if (
            _value(result, "id") != schedule_id
            or status not in (
                "not_started", "active", "completed", "released", "canceled",
            )
            or _value(result, "customer") != expected_customer_id
            or linked != expected_subscription_id
            or _value(result, "livemode") is not livemode
            or _value(metadata, "sceneit_change_id") != expected_operation_id
            or len(source_items) != 1
            or _value(source_items[0], "price") != expected_source_price_id
            or _value(source_items[0], "quantity", 1) != 1
            or len(target_items) != 1
            or _value(target_items[0], "price") != expected_target_price_id
            or _value(target_items[0], "quantity", 1) != 1
            or _value(phases[0], "end_date") != effective_timestamp
            or _value(phases[1], "start_date") != effective_timestamp
        ):
            raise BillingProviderError("invalid_schedule_relationship")
        subscription = self.retrieve_subscription(expected_subscription_id)
        if (
            subscription.id != expected_subscription_id
            or subscription.customer_id != expected_customer_id
            or subscription.livemode is not livemode
        ):
            raise BillingProviderError("invalid_schedule_relationship")
        return ScheduleChange(status, subscription)

    def retrieve_schedule(
        self, schedule_id, *, expected_price_id=None,
        expected_operation_id=None,
    ):
        result = self._call(
            self._client.subscription_schedules.retrieve, schedule_id
        )
        if _value(result, "id") != schedule_id:
            raise BillingProviderError("invalid_schedule_relationship")
        status = _value(result, "status")
        if status not in ("not_started", "active", "completed", "released", "canceled"):
            raise BillingProviderError("invalid_schedule_relationship")
        if expected_price_id and status in ("not_started", "active"):
            phases = list(_value(result, "phases", []) or [])
            final_items = (
                list(_value(phases[-1], "items", []) or []) if phases else []
            )
            metadata = _value(result, "metadata", {}) or {}
            if (
                len(final_items) != 1
                or _value(final_items[0], "price") != expected_price_id
                or _value(final_items[0], "quantity", 1) != 1
                or (
                    expected_operation_id
                    and _value(metadata, "sceneit_change_id")
                    != expected_operation_id
                )
            ):
                raise BillingProviderError("schedule_target_unverified")
        return status

    def verify_unconfigured_schedule(
        self, schedule_id, *, expected_subscription_id, expected_customer_id,
        expected_source_price_id, livemode, expected_effective_at=None,
        allow_released=False, on_verified=None,
    ):
        """Verify the exact untouched one-phase schedule, including after release."""
        result = self._call(
            self._client.subscription_schedules.retrieve, schedule_id
        )
        status = _value(result, "status")
        linked = (
            _value(result, "released_subscription")
            if status == "released" else _value(result, "subscription")
        )
        if isinstance(linked, dict):
            linked = _value(linked, "id")
        phases = list(_value(result, "phases", []) or [])
        items = list(_value(phases[0], "items", []) or []) if len(phases) == 1 else []
        metadata = _value(result, "metadata", {}) or {}
        metadata = (
            metadata.to_dict()
            if callable(getattr(metadata, "to_dict", None))
            else metadata
        )
        if (
            _value(result, "id") != schedule_id
            or status not in (
                ("not_started", "active", "released")
                if allow_released else ("not_started", "active")
            )
            or linked != expected_subscription_id
            or _value(result, "customer") != expected_customer_id
            or _value(result, "livemode") is not livemode
            or bool(metadata)
            or len(items) != 1
            or _value(items[0], "price") != expected_source_price_id
            or _value(items[0], "quantity", 1) != 1
            or (
                expected_effective_at is not None
                and _value(phases[0], "end_date")
                != int(expected_effective_at.timestamp())
            )
        ):
            raise BillingProviderError("schedule_compensation_unverified")
        phase_end = _time(_value(phases[0], "end_date"))
        subscription = self.retrieve_subscription(expected_subscription_id)
        if (
            phase_end is None
            or subscription.id != expected_subscription_id
            or subscription.customer_id != expected_customer_id
            or subscription.price_id != expected_source_price_id
            or subscription.livemode is not livemode
            or subscription.current_period_end is None
            or int(subscription.current_period_end.timestamp())
            != int(phase_end.timestamp())
        ):
            raise BillingProviderError("schedule_compensation_unverified")
        if on_verified is not None:
            on_verified(phase_end)
        return status

    def release_unconfigured_schedule(
        self, schedule_id, *, expected_subscription_id, expected_customer_id,
        expected_source_price_id, livemode, operation_id,
        expected_effective_at=None, on_verified=None,
    ):
        """Compensate only an untouched schedule created by this operation."""
        phase_proof = []

        def remember_phase(phase_end):
            if on_verified is not None:
                on_verified(phase_end)
            phase_proof.append(phase_end)

        self.verify_unconfigured_schedule(
            schedule_id,
            expected_subscription_id=expected_subscription_id,
            expected_customer_id=expected_customer_id,
            expected_source_price_id=expected_source_price_id,
            livemode=livemode,
            expected_effective_at=expected_effective_at,
            on_verified=remember_phase,
        )
        released = self._call(
            self._client.subscription_schedules.release,
            schedule_id, {},
            options={"idempotency_key": operation_id + "-compensate"},
        )
        if (
            _value(released, "id") != schedule_id
            or _value(released, "status") != "released"
        ):
            raise BillingProviderError(
                "schedule_compensation_unverified", outcome_unknown=True
            )
        try:
            verified = self.verify_unconfigured_schedule(
                schedule_id,
                expected_subscription_id=expected_subscription_id,
                expected_customer_id=expected_customer_id,
                expected_source_price_id=expected_source_price_id,
                livemode=livemode,
                expected_effective_at=phase_proof[0],
                allow_released=True,
            )
        except BillingProviderError as exc:
            raise BillingProviderError(
                "schedule_compensation_unverified", outcome_unknown=True
            ) from exc
        if verified != "released":
            raise BillingProviderError(
                "schedule_compensation_unverified", outcome_unknown=True
            )
        return "released"

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
        elif event_type == "refund.updated":
            refund = self._call(self._client.refunds.retrieve, object_id)
            if _value(refund, "id") != object_id:
                raise BillingProviderError("invalid_refund_relationship")
            if _value(refund, "status") != "succeeded":
                return None
            charge_id = _value(refund, "charge")
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
        if event_type in ("charge.refunded", "refund.updated"):
            amount = _value(charge, "amount")
            if (
                not isinstance(amount, int)
                or self._successful_refund_total(
                    charge_id, amount,
                    expected_refund_id=(
                        object_id if event_type == "refund.updated" else None
                    ),
                ) <= 0
            ):
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

    def _successful_refund_total(
        self, charge_id, charge_amount, *, expected_refund_id=None
    ):
        refunds = self._call(
            self._client.refunds.list, {"charge": charge_id, "limit": 100}
        )
        rows = list(_value(refunds, "data", []))
        if bool(_value(refunds, "has_more")):
            raise BillingProviderError("invalid_refund_relationship")
        successful = [
            _value(item, "amount") for item in rows
            if _value(item, "status") == "succeeded"
        ]
        if any(_value(item, "charge") != charge_id for item in rows):
            raise BillingProviderError("invalid_refund_relationship")
        if expected_refund_id and len([
            item for item in rows
            if _value(item, "id") == expected_refund_id
            and _value(item, "status") == "succeeded"
        ]) != 1:
            raise BillingProviderError("invalid_refund_relationship")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in successful
        ):
            raise BillingProviderError("invalid_refund_relationship")
        total = sum(successful)
        if total < 0 or total > charge_amount:
            raise BillingProviderError("invalid_refund_relationship")
        return total

    def retrieve_subscription(self, subscription_id):
        result = self._call(self._client.subscriptions.retrieve, subscription_id)
        return self._subscription(result)

    def find_upgrade_invoice(self, subscription_id, operation_id):
        """Boundedly recover the unique invoice carrying this mutation identity."""
        result = self._call(
            self._client.invoices.list,
            {"subscription": subscription_id, "limit": 10},
        )
        rows = list(_value(result, "data", []) or [])
        if bool(_value(result, "has_more")):
            raise BillingProviderError("upgrade_invoice_recovery_ambiguous")
        matches = []
        for row in rows:
            invoice_id = _identifier(_value(row, "id"), "in_")
            invoice = self.retrieve_invoice(invoice_id)
            if (
                invoice.subscription_id == subscription_id
                and invoice.mutation_id == operation_id
            ):
                matches.append(invoice.id)
        if len(matches) > 1:
            raise BillingProviderError("upgrade_invoice_recovery_ambiguous")
        return matches[0] if matches else None

    def retrieve_invoice(self, invoice_id):
        result = self._call(
            self._client.invoices.retrieve, invoice_id,
        )
        line_container = _value(result, "lines", {})
        lines = list(_value(line_container, "data", []))
        if bool(_value(line_container, "has_more")) or not 1 <= len(lines) <= 10:
            raise BillingProviderError("invalid_invoice_relationship")
        normalized = []
        subscription_ids = set()
        for line in lines:
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
            subscription_ids.add(_identifier(subscription_id, "sub_"))
            period = _value(line, "period", {})
            starts_at = _time(_value(period, "start"))
            ends_at = _time(_value(period, "end"))
            price = (
                _value(line, "price")
                or _value(_value(line, "pricing", {}), "price_details", {})
            )
            quantity = _value(line, "quantity")
            amount = _value(line, "amount")
            proration = _value(details, "proration", _value(line, "proration"))
            if (
                starts_at is None or ends_at is None or ends_at <= starts_at
                or quantity != 1 or not isinstance(proration, bool)
                or _value(parent, "type") != "subscription_item_details"
                or list(_value(line, "discount_amounts", []) or [])
                or isinstance(amount, bool) or not isinstance(amount, int)
            ):
                raise BillingProviderError("invalid_invoice_relationship")
            normalized.append((
                amount, proration, starts_at, ends_at,
                _identifier(
                    _value(price, "id") or _value(price, "price"), "price_"
                ),
            ))
        if len(subscription_ids) != 1:
            raise BillingProviderError("invalid_invoice_relationship")
        full_lines = [item for item in normalized if not item[1] and item[0] > 0]
        prorations = [item for item in normalized if item[1]]
        if prorations:
            positive = [item for item in prorations if item[0] > 0]
            if len(positive) != 1 or sum(item[0] for item in normalized) <= 0:
                raise BillingProviderError("invalid_invoice_relationship")
            entitlement = positive[0]
        elif len(normalized) == 1 and len(full_lines) == 1:
            entitlement = full_lines[0]
        else:
            raise BillingProviderError("invalid_invoice_relationship")
        line_amount, _, starts_at, ends_at, price_id = entitlement
        livemode = _value(result, "livemode")
        discounts = list(_value(result, "discounts", []) or [])
        amount_paid = _value(result, "amount_paid")
        subtotal = _value(result, "subtotal")
        total = _value(result, "total")
        currency = _value(result, "currency")
        taxes = list(_value(result, "total_taxes", []) or [])
        tax_values = [_value(item, "amount") for item in taxes]
        invalid_tax = any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in tax_values
        )
        tax = sum(tax_values) if not invalid_tax else None
        automatic_tax = _value(result, "automatic_tax", {}) or {}
        tax_status = _value(automatic_tax, "status")
        tax_enabled = bool(_value(automatic_tax, "enabled"))
        created_at = _time(_value(result, "created"))
        status = _value(result, "status")
        paid = status == "paid"
        nonpaid_upgrade = status in ("open", "void")
        if (
            discounts
            or invalid_tax
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (amount_paid, subtotal, total, tax)
            )
            or not isinstance(currency, str)
            or not re_full_currency(currency)
            or total not in (subtotal, subtotal + tax)
            or total <= 0
            or (paid and (total != amount_paid or amount_paid <= 0))
            or (nonpaid_upgrade and amount_paid != 0)
            or not (paid or nonpaid_upgrade)
            or (tax_enabled and tax_status != "complete")
            or (not tax_enabled and tax_status not in (None, "not_collecting"))
            or created_at is None
        ):
            raise BillingProviderError("invalid_invoice_relationship")
        if not isinstance(livemode, bool):
            raise BillingProviderError("invalid_provider_response")
        reversed_payment = (
            self._payment_reversed(
                _value(result, "id"), amount_paid, livemode,
            )
            if paid else False
        )
        return Invoice(
            _identifier(_value(result, "id"), "in_"),
            _identifier(_value(result, "customer"), "cus_"),
            next(iter(subscription_ids)),
            price_id,
            amount_paid, status,
            starts_at, ends_at,
            livemode, reversed_payment, currency, subtotal, tax, total,
            tax_status in ("complete", "not_collecting"), bool(prorations),
            created_at, None,
            tuple(
                InvoiceLine(item[4], item[0], item[1], item[2], item[3])
                for item in normalized
            ),
            (
                _value(
                    _value(result, "metadata", {}) or {}, "sceneit_change_id"
                )
                or _value(
                    _value(
                        _value(
                            _value(result, "parent", {}),
                            "subscription_details", {},
                        ),
                        "metadata", {},
                    ) or {},
                    "sceneit_change_id",
                )
            ),
        )

    def retrieve_payment_problem(self, invoice_id):
        result = self._call(self._client.invoices.retrieve, invoice_id)
        created = _time(_value(result, "created"))
        livemode = _value(result, "livemode")
        amount_due = _value(result, "amount_due")
        currency = _value(result, "currency")
        payments = self._call(
            self._client.invoice_payments.list,
            {"invoice": invoice_id, "limit": 10},
        )
        payment_rows = list(_value(payments, "data", []))
        if bool(_value(payments, "has_more")):
            raise BillingProviderError("invalid_invoice_payment_relationship")
        default_payment = next(
            (item for item in payment_rows if bool(_value(item, "is_default"))),
            None,
        )
        payment = _value(default_payment, "payment", {}) if default_payment else {}
        payment_intent = (
            _value(payment, "payment_intent")
            if _value(payment, "type") == "payment_intent" else None
        )
        subscription = (
            _value(result, "subscription")
            or _value(
                _value(_value(result, "parent", {}), "subscription_details", {}),
                "subscription",
            )
        )
        code = "payment_failed"
        next_action = "manage_billing"
        if payment_intent:
            intent = self._call(
                self._client.payment_intents.retrieve, payment_intent
            )
            intent_status = _value(intent, "status")
            error = _value(intent, "last_payment_error", {}) or {}
            decline = _value(error, "decline_code") or _value(error, "code")
            if intent_status in ("requires_action", "requires_confirmation"):
                code, next_action = (
                    "payment_authentication_required",
                    "authenticate_payment",
                )
            elif decline == "expired_card":
                code = "expired_card"
        if (
            created is None or not isinstance(livemode, bool)
            or (
                amount_due is not None and (
                    isinstance(amount_due, bool) or not isinstance(amount_due, int)
                    or amount_due < 0
                )
            )
            or (
                currency is not None
                and not re_full_currency(currency)
            )
        ):
            raise BillingProviderError("invalid_payment_problem")
        return PaymentProblem(
            _identifier(_value(result, "id"), "in_"),
            _identifier(_value(result, "customer"), "cus_"),
            _identifier(subscription, "sub_") if subscription else None,
            created, currency, amount_due, code, next_action, livemode,
            _value(result, "status") in ("paid", "void")
            or amount_due == 0,
        )

    def invoice_id_for_payment_intent(self, payment_intent_id):
        payment_intent_id = _identifier(payment_intent_id, "pi_")
        result = self._call(
            self._client.invoice_payments.list,
            {"payment": {
                "type": "payment_intent",
                "payment_intent": payment_intent_id,
            }, "limit": 10},
        )
        rows = list(_value(result, "data", []))
        if bool(_value(result, "has_more")) or len(rows) != 1:
            raise BillingProviderError("invalid_invoice_payment_relationship")
        payment = _value(rows[0], "payment", {})
        invoice_id = _value(rows[0], "invoice")
        if (
            _value(payment, "type") != "payment_intent"
            or _value(payment, "payment_intent") != payment_intent_id
        ):
            raise BillingProviderError("invalid_invoice_payment_relationship")
        return _identifier(invoice_id, "in_")

    def resolve_invoice_notification(self, invoice_id):
        from .billing_notification_provider import resolve_invoice_notification
        return resolve_invoice_notification(self, invoice_id)

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
        refunded = self._successful_refund_total(charge_id, amount_paid)
        if (
            _value(intent, "status") != "succeeded"
            or _value(intent, "amount_received") != amount_paid
            or not bool(_value(charge, "paid"))
            or amount != amount_paid
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
            _value(price, "currency"),
            (
                _identifier(_value(items[0], "id"), "si_")
                if _value(items[0], "id") is not None else None
            ),
            (
                _identifier(_value(item, "schedule"), "sub_sched_")
                if _value(item, "schedule") is not None else None
            ),
        )