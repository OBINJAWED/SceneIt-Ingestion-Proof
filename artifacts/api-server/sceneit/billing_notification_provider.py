"""Fresh, bounded Stripe facts used only by billing notifications."""
import re

from .billing_notifications import (
    InvoiceNotificationFact, NotificationFactUnavailable,
)
from .billing_provider import BillingProviderError, _identifier, _value


def _invalid(message):
    raise ValueError(message)


def _email(value):
    if (
        not isinstance(value, str) or len(value) > 254
        or "\r" in value or "\n" in value
        or not re.fullmatch(r"[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+", value)
    ):
        _invalid("Stripe billing recipient is invalid")
    return value


def _owner(metadata):
    owner = _value(metadata or {}, "sceneit_owner_id")
    if (
        not isinstance(owner, str) or not 1 <= len(owner) <= 255
        or any(ord(char) < 33 for char in owner)
    ):
        _invalid("Stripe billing ownership metadata is invalid")
    return owner


def resolve_invoice_notification(adapter, invoice_id):
    """Resolve authoritative notification facts through an existing adapter.

    Exact delegation hook for ``StripeBillingProvider``:

    ``return resolve_invoice_notification(self, invoice_id)``
    """
    if (
        not isinstance(invoice_id, str)
        or not re.fullmatch(r"in_[A-Za-z0-9]{1,252}", invoice_id)
    ):
        raise ValueError("invoice reference is invalid")
    settings = getattr(adapter, "_settings", None)
    client = getattr(adapter, "_client", None)
    if (
        settings is None or client is None
        or settings.environment not in ("test", "live")
        or not getattr(settings, "enabled", False)
    ):
        raise ValueError("billing provider is not configured")
    expected_live = settings.environment == "live"
    try:
        invoice = adapter._call(
            client.invoices.retrieve, invoice_id,
        )
        if (
            _value(invoice, "id") != invoice_id
            or _value(invoice, "livemode") is not expected_live
        ):
            _invalid("invoice identity or environment is invalid")
        customer_id = _identifier(_value(invoice, "customer"), "cus_")
        subscription_id = _identifier(
            _value(invoice, "subscription")
            or _value(
                _value(_value(invoice, "parent", {}), "subscription_details", {}),
                "subscription",
            ),
            "sub_",
        )

        # Both objects are fetched now. Invoice snapshots and application auth
        # email are never used as the delivery authority.
        customer = adapter._call(client.customers.retrieve, customer_id)
        subscription = adapter._call(
            client.subscriptions.retrieve, subscription_id
        )
        if (
            _value(customer, "id") != customer_id
            or bool(_value(customer, "deleted"))
            or _value(customer, "livemode") is not expected_live
            or _value(subscription, "id") != subscription_id
            or _value(subscription, "livemode") is not expected_live
            or _value(subscription, "customer") != customer_id
        ):
            _invalid("billing customer/subscription relationship is invalid")
        owner = _owner(_value(customer, "metadata", {}))
        if _owner(_value(subscription, "metadata", {})) != owner:
            _invalid("billing ownership is inconsistent")
        subscription_status = _value(subscription, "status")
        if subscription_status not in (
            "incomplete", "incomplete_expired", "trialing", "active",
            "past_due", "canceled", "unpaid", "paused",
        ):
            _invalid("subscription state is invalid")

        recipient = _email(_value(customer, "email"))
        invoice_email = _value(invoice, "customer_email")
        if (
            invoice_email is not None
            and _email(invoice_email).casefold() != recipient.casefold()
        ):
            _invalid("invoice and current billing recipient are inconsistent")

        items = list(_value(_value(subscription, "items", {}), "data", []) or [])
        if len(items) != 1 or _value(items[0], "quantity", 1) != 1:
            _invalid("subscription item relationship is unsupported")
        subscription_price = _value(items[0], "price", {})
        price_id = _identifier(_value(subscription_price, "id"), "price_")
        offer = settings.catalog.prices.get(price_id)
        if offer is None:
            _invalid("subscription Price is not reviewed")
        currency = _value(invoice, "currency")
        if (
            currency != offer.currency
            or _value(subscription_price, "currency") != offer.currency
        ):
            _invalid("billing currency is inconsistent")

        line_container = _value(invoice, "lines", {})
        lines = list(_value(line_container, "data", []) or [])
        if (
            bool(_value(line_container, "has_more"))
            or not 1 <= len(lines) <= 10
        ):
            _invalid("invoice lines are incomplete")
        for line in lines:
            details = _value(
                _value(line, "parent", {}), "subscription_item_details", {}
            )
            line_subscription = (
                _value(line, "subscription")
                or _value(details, "subscription")
                or subscription_id
            )
            price = (
                _value(line, "price")
                or _value(_value(line, "pricing", {}), "price_details", {})
            )
            line_price_id = _identifier(
                _value(price, "id") or _value(price, "price"), "price_"
            )
            line_offer = settings.catalog.prices.get(line_price_id)
            quantity = _value(line, "quantity", 1)
            if (
                line_subscription != subscription_id
                or line_offer is None or line_offer.currency != currency
                or quantity != 1
            ):
                _invalid("invoice line relationship is invalid")

        status = _value(invoice, "status")
        if status not in ("draft", "open", "paid", "uncollectible", "void"):
            _invalid("invoice collection state is invalid")
        collection_method = _value(invoice, "collection_method")
        auto_advance = _value(invoice, "auto_advance")
        amount_due = _value(invoice, "amount_remaining")
        attempts = _value(invoice, "attempt_count")
        amount_paid = _value(invoice, "amount_paid", 0)
        if (
            collection_method != "charge_automatically"
            or not isinstance(auto_advance, bool)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (amount_due, attempts, amount_paid)
            )
        ):
            _invalid("invoice collection facts are invalid")

        obsolete = (
            status in ("draft", "paid", "void")
            or amount_due == 0 or attempts == 0 or not auto_advance
            or subscription_status in ("canceled", "incomplete_expired")
        )
        state = status
        intent = None
        if not obsolete:
            payments = adapter._call(
                client.invoice_payments.list,
                {"invoice": invoice_id, "limit": 10},
            )
            payment_rows = list(_value(payments, "data", []) or [])
            if bool(_value(payments, "has_more")) or not 1 <= len(payment_rows) <= 10:
                _invalid("invoice payment relationship is incomplete")
            defaults = [
                row for row in payment_rows if bool(_value(row, "is_default"))
                and _value(row, "status") == "open"
            ]
            if len(defaults) != 1:
                _invalid("current invoice payment relationship is invalid")
            invoice_payment = defaults[0]
            payment = _value(invoice_payment, "payment", {})
            requested = _value(invoice_payment, "amount_requested")
            if (
                _value(invoice_payment, "invoice") != invoice_id
                or _value(invoice_payment, "livemode") is not expected_live
                or _value(invoice_payment, "currency") not in (None, currency)
                or (
                    requested is not None
                    and (
                        isinstance(requested, bool) or not isinstance(requested, int)
                        or requested < amount_due
                    )
                )
                or _value(payment, "type") != "payment_intent"
            ):
                _invalid("invoice payment relationship is invalid")
            intent_id = _identifier(
                _value(payment, "payment_intent"), "pi_"
            )
            intent = adapter._call(
                client.payment_intents.retrieve, intent_id
            )
            if _value(intent, "id") != intent_id:
                _invalid("payment intent identity is invalid")
        if intent is not None:
            intent_status = _value(intent, "status")
            if (
                _value(intent, "customer") not in (None, customer_id)
                or _value(intent, "currency") not in (None, currency)
            ):
                _invalid("payment intent relationship is invalid")
            if intent_status in ("requires_action", "requires_confirmation"):
                state = "action_required"
            elif intent_status in (
                "requires_payment_method", "processing", "canceled", "succeeded"
            ):
                if intent_status == "requires_payment_method":
                    error = _value(intent, "last_payment_error", {}) or {}
                    decline = _value(error, "decline_code") or _value(error, "code")
                    state = "expired_card" if decline == "expired_card" else "past_due"
                else:
                    state = intent_status
            else:
                _invalid("payment intent state is invalid")
            if intent_status in ("canceled", "succeeded"):
                obsolete = True
        elif not obsolete and status == "open":
            # Open automatic invoices without current failure evidence do not
            # authorize an application reminder.
            obsolete = True
        elif status == "uncollectible":
            state = "uncollectible"

        reversed_payment = False
        if amount_paid > 0:
            reversed_payment = adapter._payment_reversed(
                invoice_id, amount_paid, expected_live
            )
        return InvoiceNotificationFact(
            invoice_id=invoice_id,
            recipient=recipient,
            state=state,
            tier=offer.tier,
            currency=currency,
            amount_due=amount_due,
            attempts=attempts,
            reversed=reversed_payment,
            obsolete=obsolete,
        )
    except ValueError:
        raise
    except BillingProviderError as exc:
        raise NotificationFactUnavailable(exc.code) from exc


class StripeInvoiceNotificationResolver:
    """Object interface consumed by the durable notification runner."""

    def __init__(self, adapter):
        self.adapter = adapter

    def resolve_invoice_notification(self, invoice_id):
        return resolve_invoice_notification(self.adapter, invoice_id)
