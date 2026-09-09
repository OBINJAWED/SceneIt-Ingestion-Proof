"""Account-scoped hosted billing and verified paid-coverage lifecycle."""
import hashlib
import uuid
from datetime import datetime, timezone

from .billing_config import BillingProblem, billing_settings
from .billing_provider import BillingProviderError, StripeBillingProvider
from .db import connection
from psycopg.types.json import Jsonb

ACTIVE_PROVIDER_STATES = frozenset(
    {"active", "trialing", "past_due", "unpaid", "incomplete"}
)
REVERSAL_EVENTS = frozenset({
    "charge.refunded", "charge.dispute.created",
})
INVOICE_EVENTS = frozenset({"invoice.paid", "invoice.payment_succeeded"})
SUBSCRIPTION_EVENTS = frozenset({
    "customer.subscription.created", "customer.subscription.updated",
    "customer.subscription.deleted",
})
CHECKOUT_EVENTS = frozenset({
    "checkout.session.completed", "checkout.session.expired",
})
SUPPORTED_EVENTS = (
    INVOICE_EVENTS | REVERSAL_EVENTS | SUBSCRIPTION_EVENTS | CHECKOUT_EVENTS
)


def _provider(settings, provider):
    return provider or StripeBillingProvider(settings)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def checkout_token(owner_id, key):
    return "sceneit-checkout-" + hashlib.sha256(
        f"{owner_id}\0{key}".encode("utf-8")
    ).hexdigest()


def status(owner_id):
    settings = billing_settings()
    if not settings.enabled:
        return {
            "enabled": False, "environment": None, "membership": "disabled",
            "paidThrough": None, "cancelAtPeriodEnd": False, "usage": None,
        }
    with connection() as conn:
        row = conn.execute(
            "SELECT (SELECT max(c.ends_at) FROM sceneit_paid_coverage c "
            "WHERE c.owner_id=a.owner_id AND NOT c.reversed "
            "AND c.starts_at<=now() AND c.ends_at>now()) AS paid_through,"
            "COALESCE((SELECT bool_or(s.cancel_at_period_end) "
            "FROM sceneit_billing_subscriptions s WHERE s.owner_id=a.owner_id "
            "AND s.environment=a.environment "
            "AND s.status IN ('active','trialing','past_due')),false) AS canceling "
            "FROM sceneit_billing_accounts a WHERE a.owner_id=%s AND a.environment=%s",
            (owner_id, settings.environment),
        ).fetchone()
        from .quota import usage_status
        usage = usage_status(owner_id, conn=conn)
    paid_through = row["paid_through"] if row else None
    return {
        "enabled": True, "environment": settings.environment,
        "membership": "active" if paid_through else "inactive",
        "paidThrough": _iso(paid_through),
        "cancelAtPeriodEnd": bool(row and row["canceling"]),
        "usage": usage,
    }


def _account_for_checkout(owner_id, settings):
    """Persist customer creation intent before crossing the provider boundary."""
    with connection() as conn:
        conn.execute(
            "INSERT INTO sceneit_billing_accounts(owner_id,environment) VALUES (%s,%s) "
            "ON CONFLICT(owner_id) DO NOTHING", (owner_id, settings.environment),
        )
        account = conn.execute(
            "SELECT * FROM sceneit_billing_accounts WHERE owner_id=%s FOR UPDATE",
            (owner_id,),
        ).fetchone()
        if account["environment"] != settings.environment:
            raise BillingProblem("billing_environment_mismatch", "Billing environment changed.", 409)
        if account["customer_id"]:
            return account["customer_id"], None
        if account["customer_attempt_state"] in ("creating", "uncertain"):
            raise BillingProblem(
                "billing_customer_outcome_unknown",
                "Customer creation requires operator reconciliation.", 409,
            )
        conn.execute(
            "UPDATE sceneit_billing_accounts SET customer_attempt_state='creating', "
            "updated_at=now() WHERE owner_id=%s", (owner_id,),
        )
        return None, str(account["customer_attempt_id"])


def create_checkout(owner_id, plan, idempotency_key, *, provider=None):
    settings = billing_settings()
    if not settings.enabled:
        raise BillingProblem("billing_disabled", "Billing is disabled.", 503)
    if plan not in ("monthly", "yearly") or plan not in settings.prices:
        raise BillingProblem("invalid_plan", "Choose monthly or yearly billing.", 400)
    try:
        key = str(uuid.UUID(str(idempotency_key)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise BillingProblem("invalid_idempotency_key", "A UUID idempotency key is required.", 400) from exc
    adapter = _provider(settings, provider)
    customer_id, customer_attempt = _account_for_checkout(owner_id, settings)
    if customer_attempt:
        try:
            customer_id = adapter.create_customer(owner_id, f"sceneit-customer-{customer_attempt}")
        except BillingProviderError as exc:
            with connection() as conn:
                conn.execute(
                    "UPDATE sceneit_billing_accounts SET customer_attempt_state=%s,"
                    "updated_at=now() WHERE owner_id=%s",
                    ("uncertain" if exc.outcome_unknown else "new", owner_id),
                )
            raise BillingProblem(exc.code, "The billing provider could not create the customer.", 503) from exc
        with connection() as conn:
            conn.execute(
                "UPDATE sceneit_billing_accounts SET customer_id=%s,"
                "customer_attempt_state='created',updated_at=now() "
                "WHERE owner_id=%s AND customer_attempt_state='creating'",
                (customer_id, owner_id),
            )

    # Honor our durable result before consulting changing subscription state.
    # In particular, a replay never creates another hosted Session merely
    # because Stripe's idempotency-key retention window has elapsed.
    with connection() as conn:
        prior = conn.execute(
            "SELECT plan,state,hosted_url,expires_at,"
            "(expires_at IS NULL OR expires_at > now()) AS usable "
            "FROM sceneit_billing_checkouts "
            "WHERE owner_id=%s AND idempotency_key=%s", (owner_id, key),
        ).fetchone()
    if prior:
        if prior["plan"] != plan:
            raise BillingProblem(
                "idempotency_conflict",
                "Idempotency key was used for another plan.", 409,
            )
        if prior["state"] == "created":
            return {"url": prior["hosted_url"], "expiresAt": _iso(prior["expires_at"])}
        raise BillingProblem(
            "checkout_outcome_unknown", "Checkout requires reconciliation.", 409
        )

    with connection() as conn:
        opened = conn.execute(
            "SELECT plan,state,hosted_url,expires_at,provider_session_id,subscription_id,"
            "(expires_at IS NULL OR expires_at > now()) AS usable "
            "FROM sceneit_billing_checkouts WHERE owner_id=%s "
            "AND state IN ('creating','created','uncertain','completed') "
            "ORDER BY created_at DESC LIMIT 1", (owner_id,),
        ).fetchone()
    if opened and opened["state"] == "completed" and opened["subscription_id"]:
        if complete_checkout(
                opened["provider_session_id"], opened["subscription_id"],
                adapter, settings) == "expired":
            opened = None
    if opened:
        if opened["plan"] != plan:
            raise BillingProblem(
                "checkout_plan_conflict",
                "The open checkout is for a different billing plan.", 409,
            )
        if opened["state"] == "completed":
            raise BillingProblem(
                "checkout_completed",
                "The prior checkout completed and its subscription is current.", 409,
            )
        if (
            opened["state"] == "created"
            and opened["usable"]
            and opened["hosted_url"]
        ):
            return {"url": opened["hosted_url"], "expiresAt": _iso(opened["expires_at"])}
        if opened["state"] == "created" and opened["expires_at"]:
            hosted, provider_state, provider_subscription = adapter.retrieve_checkout(
                opened["provider_session_id"]
            )
            if provider_state == "expired":
                with connection() as conn:
                    conn.execute(
                        "UPDATE sceneit_billing_checkouts SET state='expired',updated_at=now() "
                        "WHERE owner_id=%s AND provider_session_id=%s AND state='created'",
                        (owner_id, hosted.id),
                    )
            elif provider_state == "complete":
                if not provider_subscription:
                    raise BillingProblem(
                        "billing_relationship_invalid",
                        "Completed checkout has no subscription.", 409,
                    )
                if complete_checkout(
                        hosted.id, provider_subscription, adapter, settings) != "expired":
                    raise BillingProblem(
                        "checkout_completed",
                        "The prior checkout completed and is reconciling.", 409,
                    )
            else:
                raise BillingProblem(
                    "checkout_exists", "An existing checkout is still open or completed.", 409
                )
        else:
            raise BillingProblem(
                "checkout_outcome_unknown", "Checkout requires reconciliation.", 409
            )

    adapter.verify_price(
        price_id := settings.prices[plan], plan, settings.environment == "live"
    )
    for subscription in adapter.list_subscriptions(customer_id):
        _validate_subscription(subscription, owner_id, customer_id, settings)
        if subscription.status in ACTIVE_PROVIDER_STATES:
            raise BillingProblem(
                "subscription_exists", "This account already has a subscription.", 409
            )

    with connection() as conn:
        conn.execute(
            "SELECT owner_id FROM sceneit_billing_accounts WHERE owner_id=%s FOR UPDATE",
            (owner_id,),
        )
        existing = conn.execute(
            "SELECT * FROM sceneit_billing_checkouts WHERE owner_id=%s AND idempotency_key=%s "
            "FOR UPDATE", (owner_id, key),
        ).fetchone()
        if existing:
            if existing["plan"] != plan:
                raise BillingProblem("idempotency_conflict", "Idempotency key was used for another plan.", 409)
            if existing["state"] == "created":
                return {"url": existing["hosted_url"], "expiresAt": _iso(existing["expires_at"])}
            raise BillingProblem("checkout_outcome_unknown", "Checkout requires reconciliation.", 409)
        other = conn.execute(
            "SELECT plan,state,hosted_url,expires_at FROM sceneit_billing_checkouts "
            "WHERE owner_id=%s "
            "AND state IN ('creating','created','uncertain','completed') "
            "LIMIT 1", (owner_id,),
        ).fetchone()
        if other:
            if other["state"] == "completed":
                raise BillingProblem(
                    "checkout_completed",
                    "The prior checkout completed and its subscription is current.", 409,
                )
            if other["state"] == "created":
                if other["plan"] != plan:
                    raise BillingProblem(
                        "checkout_plan_conflict",
                        "The open checkout is for a different billing plan.", 409,
                    )
                return {"url": other["hosted_url"], "expiresAt": _iso(other["expires_at"])}
            raise BillingProblem(
                "checkout_outcome_unknown", "Checkout requires reconciliation.", 409
            )
        conn.execute(
            "INSERT INTO sceneit_billing_checkouts(owner_id,idempotency_key,plan,price_id,state) "
            "VALUES (%s,%s,%s,%s,'creating')", (owner_id, key, plan, price_id),
        )
    try:
        hosted = adapter.create_checkout(
            customer_id, owner_id, price_id, settings.return_url,
            checkout_token(owner_id, key),
        )
    except BillingProviderError as exc:
        with connection() as conn:
            conn.execute(
                "UPDATE sceneit_billing_checkouts SET state=%s,updated_at=now() "
                "WHERE owner_id=%s AND idempotency_key=%s",
                ("uncertain" if exc.outcome_unknown else "expired", owner_id, key),
            )
        raise BillingProblem(exc.code, "The billing provider could not create checkout.", 503) from exc
    with connection() as conn:
        conn.execute(
            "UPDATE sceneit_billing_checkouts SET state='created',provider_session_id=%s,"
            "hosted_url=%s,expires_at=%s,updated_at=now() WHERE owner_id=%s AND idempotency_key=%s",
            (hosted.id, hosted.url, hosted.expires_at, owner_id, key),
        )
    return {"url": hosted.url, "expiresAt": _iso(hosted.expires_at)}


def create_portal(owner_id, *, provider=None):
    settings = billing_settings()
    if not settings.enabled:
        raise BillingProblem("billing_disabled", "Billing is disabled.", 503)
    with connection() as conn:
        account = conn.execute(
            "SELECT customer_id,environment FROM sceneit_billing_accounts WHERE owner_id=%s",
            (owner_id,),
        ).fetchone()
    if not account or not account["customer_id"]:
        raise BillingProblem("billing_customer_missing", "No billing account exists.", 404)
    if account["environment"] != settings.environment:
        raise BillingProblem("billing_environment_mismatch", "Billing environment changed.", 409)
    hosted = _provider(settings, provider).create_portal(
        account["customer_id"], settings.return_url, settings.portal_configuration
    )
    return {"url": hosted.url, "expiresAt": _iso(hosted.expires_at)}


def _validate_subscription(subscription, owner_id, customer_id, settings):
    expected_live = settings.environment == "live"
    if (
        subscription.customer_id != customer_id
        or subscription.livemode != expected_live
        or subscription.price_id not in set(settings.prices.values())
        or subscription.owner_id != owner_id
    ):
        raise BillingProblem(
            "billing_relationship_invalid", "Provider billing relationship is invalid.", 409
        )


def _event_object_id(event):
    data = event.get("data", {}).get("object", {})
    return data.get("id"), data


def process_event(event, *, provider=None):
    """Durably deduplicate then reconcile current provider state."""
    settings = billing_settings()
    if not settings.enabled:
        raise BillingProblem("billing_disabled", "Billing is disabled.", 503)
    event_id = event.get("id")
    event_type = event.get("type")
    livemode = event.get("livemode")
    if (
        not isinstance(event_id, str) or not event_id.startswith("evt_")
        or len(event_id) > 255 or not isinstance(event_type, str)
        or len(event_type) > 100
    ):
        raise BillingProblem("invalid_webhook", "Webhook event is invalid.", 400)
    if (
        not isinstance(livemode, bool)
        or livemode != (settings.environment == "live")
    ):
        raise BillingProblem("billing_environment_mismatch", "Webhook environment is invalid.", 400)
    if event_type not in SUPPORTED_EVENTS:
        # Correctly signed but irrelevant events require no recovery state.
        return "completed"
    object_id, event_object = _event_object_id(event)
    invoice_reference = event_object.get("invoice")
    if isinstance(invoice_reference, dict):
        invoice_reference = invoice_reference.get("id")
    # Recovery needs provider references, not the webhook's customer/payment
    # snapshot. Persisting only this narrow shape avoids retaining billing PII.
    stored_event = {
        "id": event_id, "type": event_type, "livemode": bool(livemode),
        "data": {"object": {"id": object_id, "invoice": invoice_reference}},
    }
    with connection() as conn:
        inserted = conn.execute(
            "INSERT INTO sceneit_billing_events(event_id,environment,event_type,state,payload) "
            "VALUES (%s,%s,%s,'processing',%s) ON CONFLICT DO NOTHING RETURNING event_id",
            (event_id, settings.environment, event_type, Jsonb(stored_event)),
        ).fetchone()
        if not inserted:
            prior = conn.execute(
                "SELECT state FROM sceneit_billing_events WHERE event_id=%s FOR UPDATE",
                (event_id,)
            ).fetchone()
            if prior["state"] == "completed":
                return "completed"
            if prior["state"] == "processing":
                # A concurrent delivery cannot take over in-flight processing.
                # The original request will either complete or durably mark pending.
                return "pending"
            if prior["state"] == "rejected":
                raise BillingProblem(
                    "webhook_rejected", "Webhook was previously rejected.", 409
                )
            conn.execute(
                "UPDATE sceneit_billing_events SET state='processing',attempts=attempts+1,"
                "updated_at=now() WHERE event_id=%s", (event_id,),
            )
    try:
        _reconcile_event(
            event_type, stored_event, _provider(settings, provider), settings
        )
    except BillingProviderError as exc:
        with connection() as conn:
            conn.execute(
                "UPDATE sceneit_billing_events SET state='pending',last_error_code=%s,"
                "updated_at=now() WHERE event_id=%s", (exc.code, event_id),
            )
        return "pending"
    except BillingProblem as exc:
        with connection() as conn:
            conn.execute(
                "UPDATE sceneit_billing_events SET state='rejected',last_error_code=%s,"
                "processed_at=now(),updated_at=now() WHERE event_id=%s", (exc.code, event_id),
            )
        raise
    with connection() as conn:
        conn.execute(
            "UPDATE sceneit_billing_events SET state='completed',last_error_code=NULL,"
            "processed_at=now(),updated_at=now() WHERE event_id=%s", (event_id,),
        )
    return "completed"


def _reconcile_event(event_type, event, adapter, settings):
    object_id, obj = _event_object_id(event)
    if event_type in CHECKOUT_EVENTS:
        hosted, provider_state, subscription_id = adapter.retrieve_checkout(object_id)
        expected = (
            "completed" if event_type == "checkout.session.completed"
            else "expired"
        )
        if provider_state not in ("complete", "expired"):
            raise BillingProblem(
                "checkout_state_invalid", "Checkout state is not terminal.", 409
            )
        target = "completed" if provider_state == "complete" else "expired"
        if target == "completed" and not subscription_id:
            raise BillingProblem(
                "billing_relationship_invalid",
                "Completed checkout has no subscription.", 409,
            )
        if target != expected:
            # Current state wins over a stale event, but both are terminal.
            target = "completed" if provider_state == "complete" else "expired"
        if target == "completed":
            complete_checkout(hosted.id, subscription_id, adapter, settings)
            return
        with connection() as conn:
            changed = conn.execute(
                "UPDATE sceneit_billing_checkouts SET state=%s,subscription_id=%s,"
                "updated_at=now() WHERE provider_session_id=%s RETURNING owner_id",
                (target, subscription_id, hosted.id),
            ).fetchone()
        if not changed:
            raise BillingProblem(
                "billing_relationship_invalid", "Checkout relationship is invalid.", 409
            )
        return
    if event_type.startswith("customer.subscription."):
        subscription = adapter.retrieve_subscription(object_id)
        _save_subscription(subscription, settings)
        return
    invoice_id = (
        object_id if event_type.startswith("invoice.")
        else adapter.invoice_id_for_reversal(event_type, object_id)
    )
    if not invoice_id:
        return
    invoice = adapter.retrieve_invoice(str(invoice_id))
    owner_id = _owner_for_customer(invoice.customer_id, settings)
    if invoice.livemode != (settings.environment == "live"):
        raise BillingProblem("billing_relationship_invalid", "Invoice environment is invalid.", 409)
    if invoice.reversed:
        with connection() as conn:
            covered = conn.execute(
                "SELECT owner_id,subscription_id FROM sceneit_paid_coverage "
                "WHERE id=%s", (invoice.id,),
            ).fetchone()
        if covered:
            if (
                covered["owner_id"] != owner_id
                or covered["subscription_id"] != invoice.subscription_id
            ):
                raise BillingProblem(
                    "billing_relationship_invalid",
                    "Historical invoice relationship is invalid.", 409,
                )
            _save_coverage(owner_id, invoice, reversed=True)
            return
        # A reversal arriving before payment may create a tombstone only for a
        # currently allowlisted relationship; archived coverage above needs no
        # active Price or current subscription correspondence.
    subscription = adapter.retrieve_subscription(invoice.subscription_id)
    _validate_subscription(subscription, owner_id, invoice.customer_id, settings)
    if invoice.price_id != subscription.price_id:
        raise BillingProblem("billing_relationship_invalid", "Invoice relationship is invalid.", 409)
    plan = next(
        (name for name, price in settings.prices.items() if price == invoice.price_id),
        None,
    )
    if plan is None:
        raise BillingProblem("billing_relationship_invalid", "Invoice price is invalid.", 409)
    configured_amount = adapter.verify_price(
        invoice.price_id, plan, settings.environment == "live"
    )
    duration_days = (invoice.ends_at - invoice.starts_at).total_seconds() / 86400
    valid_period = (
        27 <= duration_days <= 32
        if plan == "monthly"
        else 365 <= duration_days <= 366
    )
    if configured_amount != invoice.amount_paid or not valid_period:
        raise BillingProblem(
            "billing_relationship_invalid", "Invoice amount or period is invalid.", 409
        )
    _save_subscription(subscription, settings)
    if event_type in INVOICE_EVENTS:
        if invoice.status != "paid" or invoice.amount_paid <= 0:
            raise BillingProblem("invoice_not_paid", "Invoice is not confirmed paid.", 409)
        _save_coverage(owner_id, invoice, reversed=invoice.reversed)
    elif event_type in REVERSAL_EVENTS and invoice.reversed:
        _save_coverage(owner_id, invoice, reversed=True)


def _owner_for_customer(customer_id, settings):
    with connection() as conn:
        row = conn.execute(
            "SELECT owner_id,environment FROM sceneit_billing_accounts WHERE customer_id=%s",
            (customer_id,),
        ).fetchone()
    if not row or row["environment"] != settings.environment:
        raise BillingProblem("billing_relationship_invalid", "Customer relationship is invalid.", 409)
    return row["owner_id"]


def _save_subscription(subscription, settings):
    owner_id = _owner_for_customer(subscription.customer_id, settings)
    _validate_subscription(subscription, owner_id, subscription.customer_id, settings)
    with connection() as conn:
        conn.execute(
            "INSERT INTO sceneit_billing_subscriptions(subscription_id,owner_id,customer_id,"
            "environment,price_id,status,cancel_at_period_end,current_period_end) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(subscription_id) DO UPDATE SET "
            "price_id=EXCLUDED.price_id,status=CASE WHEN sceneit_billing_subscriptions.status "
            "IN ('canceled','incomplete_expired') THEN sceneit_billing_subscriptions.status "
            "ELSE EXCLUDED.status END,"
            "cancel_at_period_end=EXCLUDED.cancel_at_period_end,"
            "current_period_end=EXCLUDED.current_period_end,updated_at=now()",
            (subscription.id, owner_id, subscription.customer_id, settings.environment,
             subscription.price_id, subscription.status, subscription.cancel_at_period_end,
             subscription.current_period_end),
        )
        conn.execute(
            "UPDATE sceneit_billing_checkouts SET state='expired',updated_at=now() "
            "WHERE subscription_id=%s AND state='completed' AND EXISTS ("
            "SELECT 1 FROM sceneit_billing_subscriptions s WHERE s.subscription_id=%s "
            "AND s.status IN ('canceled','incomplete_expired'))",
            (subscription.id, subscription.id),
        )


def complete_checkout(session_id, subscription_id, adapter, settings):
    """Bind completion to current subscription state, regardless of event order."""
    with connection() as conn:
        checkout = conn.execute(
            "SELECT c.owner_id,c.price_id,b.customer_id FROM sceneit_billing_checkouts c "
            "JOIN sceneit_billing_accounts b ON b.owner_id=c.owner_id "
            "WHERE c.provider_session_id=%s AND b.environment=%s",
            (session_id, settings.environment),
        ).fetchone()
    if not checkout:
        raise BillingProblem(
            "billing_relationship_invalid", "Checkout relationship is invalid.", 409)
    subscription = adapter.retrieve_subscription(subscription_id)
    _validate_subscription(
        subscription, checkout["owner_id"], checkout["customer_id"], settings)
    if subscription.id != subscription_id or subscription.price_id != checkout["price_id"]:
        raise BillingProblem(
            "billing_relationship_invalid", "Checkout subscription is invalid.", 409)
    _save_subscription(subscription, settings)
    with connection() as conn:
        row = conn.execute(
            "UPDATE sceneit_billing_checkouts SET subscription_id=%s,"
            "state=CASE WHEN EXISTS (SELECT 1 FROM sceneit_billing_subscriptions s "
            "WHERE s.subscription_id=%s AND s.status IN ('canceled','incomplete_expired')) "
            "THEN 'expired' ELSE 'completed' END,updated_at=now() "
            "WHERE provider_session_id=%s RETURNING state",
            (subscription_id, subscription_id, session_id),
        ).fetchone()
    if not row:
        raise BillingProblem(
            "billing_relationship_invalid", "Checkout relationship is invalid.", 409)
    return row["state"]


def _save_coverage(owner_id, invoice, *, reversed):
    with connection() as conn:
        conn.execute(
            "SELECT owner_id FROM sceneit_billing_accounts WHERE owner_id=%s FOR UPDATE",
            (owner_id,),
        )
        if reversed:
            # Reversal is invoice-scoped and sticky. An event arriving before its paid
            # event creates a tombstone which a delayed paid event cannot resurrect.
            conn.execute(
                "INSERT INTO sceneit_paid_coverage(id,owner_id,subscription_id,starts_at,"
                "ends_at,reversed) VALUES (%s,%s,%s,%s,%s,true) "
                "ON CONFLICT(id) DO UPDATE SET reversed=true,updated_at=now()",
                (invoice.id, owner_id, invoice.subscription_id,
                 invoice.starts_at, invoice.ends_at),
            )
        else:
            conn.execute(
                "INSERT INTO sceneit_paid_coverage(id,owner_id,subscription_id,starts_at,"
                "ends_at,reversed) VALUES (%s,%s,%s,%s,%s,false) "
                "ON CONFLICT(id) DO UPDATE SET starts_at=EXCLUDED.starts_at,"
                "ends_at=EXCLUDED.ends_at,updated_at=now()",
                (invoice.id, owner_id, invoice.subscription_id,
                 invoice.starts_at, invoice.ends_at),
            )
            conn.execute(
                "UPDATE sceneit_billing_accounts SET allowance_anchor="
                "COALESCE(allowance_anchor,%s),updated_at=now() WHERE owner_id=%s",
                (invoice.starts_at, owner_id),
            )