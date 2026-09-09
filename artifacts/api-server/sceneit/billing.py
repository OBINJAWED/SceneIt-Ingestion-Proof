"""Account-scoped hosted billing and verified paid-coverage lifecycle."""
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from .billing_config import BillingProblem, billing_settings
from .billing_provider import BillingProviderError, StripeBillingProvider
from .billing_time import billing_now
from .db import connection
from psycopg.types.json import Jsonb

ACTIVE_PROVIDER_STATES = frozenset(
    {"active", "trialing", "past_due", "unpaid", "incomplete"}
)
REVERSAL_EVENTS = frozenset({
    "charge.refunded", "charge.dispute.created", "refund.updated",
})
INVOICE_EVENTS = frozenset({"invoice.paid", "invoice.payment_succeeded"})
PAYMENT_PROBLEM_EVENTS = frozenset({
    "invoice.payment_failed", "invoice.payment_action_required",
})
PAYMENT_RESOLUTION_EVENTS = frozenset({
    "invoice.voided", "payment_intent.succeeded",
})
SUBSCRIPTION_EVENTS = frozenset({
    "customer.subscription.created", "customer.subscription.updated",
    "customer.subscription.deleted",
    "customer.subscription.pending_update_applied",
    "customer.subscription.pending_update_expired",
})
SCHEDULE_EVENTS = frozenset({
    "subscription_schedule.created", "subscription_schedule.updated",
    "subscription_schedule.released", "subscription_schedule.canceled",
    "subscription_schedule.completed",
})
CHECKOUT_EVENTS = frozenset({
    "checkout.session.completed", "checkout.session.expired",
})
SUPPORTED_EVENTS = (
    INVOICE_EVENTS | REVERSAL_EVENTS | SUBSCRIPTION_EVENTS | CHECKOUT_EVENTS
    | PAYMENT_PROBLEM_EVENTS | PAYMENT_RESOLUTION_EVENTS | SCHEDULE_EVENTS
)


def _provider(settings, provider):
    return provider or StripeBillingProvider(settings)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def checkout_token(owner_id, key):
    return "sceneit-checkout-" + hashlib.sha256(
        f"{owner_id}\0{key}".encode("utf-8")
    ).hexdigest()


def _uuid(value, code="invalid_idempotency_key"):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise BillingProblem(code, "A UUID identifier is required.", 400) from exc


def _parameters_hash(*values):
    encoded = json.dumps(values, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _offer(settings, tier, cadence, currency, *, for_sale=False):
    if settings.catalog.offers:
        return settings.catalog.offer(tier, cadence, currency, for_sale=for_sale)
    # Compatibility for historical recovery fixtures created before catalog
    # support. This path is never purchasable in enabled production config.
    price_id = settings.prices.get(cadence)
    if price_id and not for_sale:
        from .billing_config import BillingOffer
        return BillingOffer(
            tier, cadence, currency, price_id, 0, "exclusive",
            "txcd_00000000", False,
        )
    raise BillingProblem("offer_unavailable", "The selected billing offer is unavailable.", 400)


def _purchase_identity_allowed(owner_id, conn):
    row = conn.execute(
        "SELECT COALESCE(to_jsonb(u)->>'provider','replit') provider "
        "FROM sceneit_auth_users u WHERE id=%s", (owner_id,),
    ).fetchone()
    return bool(row and row["provider"] == "replit")


def _require_purchase_eligibility(owner_id, authorized):
    with connection() as conn:
        identity_allowed = _purchase_identity_allowed(owner_id, conn)
    if not authorized or not identity_allowed:
        raise BillingProblem(
            "billing_purchase_ineligible",
            "This account is not eligible for commercial offers.", 403,
        )


def _operation_states(owner_id, conn):
    rows = conn.execute(
        "WITH checkout_rows AS ("
        " SELECT idempotency_key,'checkout'::text kind,state,updated_at "
        " FROM sceneit_billing_checkouts WHERE owner_id=%s "
        " ORDER BY CASE WHEN state IN ('creating','created','uncertain') "
        " THEN 0 ELSE 1 END,updated_at DESC LIMIT 20"
        "), operation_rows AS ("
        " SELECT idempotency_key,kind,state,updated_at "
        " FROM sceneit_billing_operations WHERE owner_id=%s "
        " AND kind IN ('portal','upgrade','schedule','withdraw') "
        " ORDER BY CASE WHEN state IN ('creating','uncertain') "
        " THEN 0 ELSE 1 END,updated_at DESC LIMIT 20"
        ") SELECT idempotency_key,kind,state,updated_at FROM checkout_rows "
        "UNION ALL SELECT idempotency_key,kind,state,updated_at "
        "FROM operation_rows ORDER BY updated_at DESC",
        (owner_id, owner_id),
    ).fetchall()
    return [
        {
            "idempotencyKey": str(item["idempotency_key"]),
            "kind": item["kind"],
            "state": item["state"],
        }
        for item in rows
    ]


def status(owner_id, *, purchase_authorized=False):
    settings = billing_settings()
    if not settings.enabled:
        return {
            "enabled": False, "environment": None, "membership": "disabled",
            "paidThrough": None, "cancelAtPeriodEnd": False, "usage": None,
            "effectiveTier": None, "cadence": None, "currency": None,
            "pendingChange": None, "paymentProblem": None,
            "managementEligible": False, "offers": [],
            "operationStates": [],
        }
    with connection() as conn:
        effective_now = billing_now(conn)
        may_purchase = (
            purchase_authorized and _purchase_identity_allowed(owner_id, conn)
        )
        row = conn.execute(
            "SELECT a.customer_id,"
            "COALESCE((SELECT bool_or(s.cancel_at_period_end) "
            "FROM sceneit_billing_subscriptions s WHERE s.owner_id=a.owner_id "
            "AND s.environment=a.environment "
            "AND s.status IN ('active','trialing','past_due')),false) AS canceling "
            "FROM sceneit_billing_accounts a WHERE a.owner_id=%s AND a.environment=%s",
            (owner_id, settings.environment),
        ).fetchone()
        from .quota import effective_coverage, usage_status
        effective = effective_coverage(conn, owner_id, effective_now) if row else None
        pending_change = conn.execute(
            "SELECT change_id,state,target_tier_key,target_cadence,currency,effective_at "
            "FROM sceneit_billing_changes WHERE owner_id=%s "
            "AND state IN ('confirming','payment_pending','scheduled','uncertain') "
            "ORDER BY created_at DESC LIMIT 1", (owner_id,),
        ).fetchone()
        problem = conn.execute(
            "SELECT invoice_id,code,state,tier_key,cadence,currency,amount_due,"
            "next_action,provider_created_at,resolved_at "
            "FROM sceneit_billing_payment_problems WHERE owner_id=%s AND state='open' "
            "ORDER BY provider_created_at DESC LIMIT 1", (owner_id,),
        ).fetchone()
        operation_states = _operation_states(owner_id, conn)
        usage = usage_status(owner_id, conn=conn)
    paid_through = effective["ends_at"] if effective else None
    return {
        "enabled": True, "environment": settings.environment,
        "membership": "active" if paid_through else "inactive",
        "paidThrough": _iso(paid_through),
        "cancelAtPeriodEnd": bool(row and row["canceling"]),
        "usage": usage,
        "effectiveTier": effective["tier_key"] if effective else None,
        "cadence": effective["cadence"] if effective else None,
        "currency": effective["currency"] if effective else None,
        "pendingChange": (
            {
                "changeId": str(pending_change["change_id"]),
                "state": pending_change["state"],
                "tier": pending_change["target_tier_key"],
                "cadence": pending_change["target_cadence"],
                "currency": pending_change["currency"],
                "effectiveAt": _iso(pending_change["effective_at"]),
            } if pending_change else None
        ),
        "paymentProblem": (
            {
                "invoiceId": problem["invoice_id"], "code": problem["code"],
                "state": problem["state"], "tier": problem["tier_key"],
                "cadence": problem["cadence"], "currency": problem["currency"],
                "amountDue": problem["amount_due"],
                "nextAction": problem["next_action"],
                "occurredAt": _iso(problem["provider_created_at"]),
                "resolvedAt": _iso(problem["resolved_at"]),
            } if problem else None
        ),
        "managementEligible": bool(row and row["customer_id"]),
        "offers": settings.catalog.public_offers() if may_purchase else [],
        "operationStates": operation_states,
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


def create_checkout(
    owner_id, tier, cadence, currency, idempotency_key, *,
    purchase_authorized=False, provider=None
):
    settings = billing_settings()
    if not settings.enabled:
        raise BillingProblem("billing_disabled", "Billing is disabled.", 503)
    _require_purchase_eligibility(owner_id, purchase_authorized)
    offer = _offer(settings, tier, cadence, currency, for_sale=True)
    key = _uuid(idempotency_key)
    parameters_hash = _parameters_hash(
        "checkout", tier, cadence, currency, offer.price_id,
        offer.unit_amount, offer.tax_behavior,
    )
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
            raise BillingProblem(
                "billing_outcome_unknown" if exc.outcome_unknown else exc.code,
                "The billing provider could not create the customer.", 503,
            ) from exc
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
            "SELECT plan,tier_key,cadence,currency,parameters_hash,state,hosted_url,expires_at,"
            "(expires_at IS NULL OR expires_at > now()) AS usable "
            "FROM sceneit_billing_checkouts "
            "WHERE owner_id=%s AND idempotency_key=%s", (owner_id, key),
        ).fetchone()
    if prior:
        if prior["parameters_hash"] != parameters_hash:
            raise BillingProblem(
                "idempotency_conflict",
                "Idempotency key was used for another plan.", 409,
            )
        if prior["state"] == "created":
            return {
                "operationId": key, "url": prior["hosted_url"],
                "expiresAt": _iso(prior["expires_at"]),
            }
        raise BillingProblem(
            "checkout_outcome_unknown", "Checkout requires reconciliation.", 409
        )

    with connection() as conn:
        opened = conn.execute(
            "SELECT plan,tier_key,cadence,currency,parameters_hash,state,hosted_url,"
            "expires_at,provider_session_id,subscription_id,"
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
        if opened["parameters_hash"] != parameters_hash:
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
            return {
                "operationId": key, "url": opened["hosted_url"],
                "expiresAt": _iso(opened["expires_at"]),
            }
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

    price_id = offer.price_id
    adapter.verify_price(
        price_id, cadence, settings.environment == "live",
        currency=currency, unit_amount=offer.unit_amount,
        tax_behavior=offer.tax_behavior, tax_code=offer.tax_code,
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
            if existing["parameters_hash"] != parameters_hash:
                raise BillingProblem("idempotency_conflict", "Idempotency key was used for another plan.", 409)
            if existing["state"] == "created":
                return {
                    "operationId": key, "url": existing["hosted_url"],
                    "expiresAt": _iso(existing["expires_at"]),
                }
            raise BillingProblem("checkout_outcome_unknown", "Checkout requires reconciliation.", 409)
        other = conn.execute(
            "SELECT plan,tier_key,cadence,currency,parameters_hash,state,hosted_url,"
            "expires_at FROM sceneit_billing_checkouts "
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
                if other["parameters_hash"] != parameters_hash:
                    raise BillingProblem(
                        "checkout_plan_conflict",
                        "The open checkout is for a different billing plan.", 409,
                    )
                return {
                    "operationId": key, "url": other["hosted_url"],
                    "expiresAt": _iso(other["expires_at"]),
                }
            raise BillingProblem(
                "checkout_outcome_unknown", "Checkout requires reconciliation.", 409
            )
        conn.execute(
            "INSERT INTO sceneit_billing_checkouts(owner_id,idempotency_key,plan,price_id,"
            "tier_key,cadence,currency,parameters_hash,state) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'creating')",
            (owner_id, key, cadence, price_id, tier, cadence, currency, parameters_hash),
        )
    try:
        hosted = adapter.create_checkout(
            customer_id, owner_id, price_id, settings.return_url,
            checkout_token(owner_id, key),
            tax_id_collection=settings.tax_id_collection,
        )
    except BillingProviderError as exc:
        with connection() as conn:
            conn.execute(
                "UPDATE sceneit_billing_checkouts SET state=%s,updated_at=now() "
                "WHERE owner_id=%s AND idempotency_key=%s",
                ("uncertain" if exc.outcome_unknown else "expired", owner_id, key),
            )
        raise BillingProblem(
            "billing_outcome_unknown" if exc.outcome_unknown else exc.code,
            "The billing provider could not create checkout.", 503,
        ) from exc
    with connection() as conn:
        conn.execute(
            "UPDATE sceneit_billing_checkouts SET state='created',provider_session_id=%s,"
            "hosted_url=%s,expires_at=%s,updated_at=now() WHERE owner_id=%s AND idempotency_key=%s",
            (hosted.id, hosted.url, hosted.expires_at, owner_id, key),
        )
    return {
        "operationId": key, "url": hosted.url,
        "expiresAt": _iso(hosted.expires_at),
    }


def create_portal(
    owner_id, action="manage", idempotency_key=None, *, provider=None
):
    settings = billing_settings()
    if not settings.enabled:
        raise BillingProblem("billing_disabled", "Billing is disabled.", 503)
    if action not in ("manage", "cancel"):
        raise BillingProblem("invalid_portal_action", "Portal action is invalid.", 400)
    key = _uuid(idempotency_key)
    params_hash = _parameters_hash("portal", action)
    with connection() as conn:
        account = conn.execute(
            "SELECT customer_id,environment FROM sceneit_billing_accounts WHERE owner_id=%s",
            (owner_id,),
        ).fetchone()
    if not account or not account["customer_id"]:
        raise BillingProblem("billing_customer_missing", "No billing account exists.", 404)
    if account["environment"] != settings.environment:
        raise BillingProblem("billing_environment_mismatch", "Billing environment changed.", 409)
    with connection() as conn:
        conn.execute(
            "SELECT owner_id FROM sceneit_billing_accounts WHERE owner_id=%s FOR UPDATE",
            (owner_id,),
        )
        prior = conn.execute(
            "SELECT * FROM sceneit_billing_operations "
            "WHERE owner_id=%s AND idempotency_key=%s FOR UPDATE",
            (owner_id, key),
        ).fetchone()
        if prior:
            if prior["parameters_hash"] != params_hash or prior["kind"] != "portal":
                raise BillingProblem("idempotency_conflict", "Idempotency key was reused.", 409)
            if prior["state"] == "created" and (
                prior["expires_at"] is None or prior["expires_at"] > datetime.now(timezone.utc)
            ):
                return {
                    "operationId": str(prior["operation_id"]),
                    "url": prior["hosted_url"], "expiresAt": _iso(prior["expires_at"]),
                }
            raise BillingProblem("portal_outcome_unknown", "Portal operation requires reconciliation.", 409)
        subscription = None
        if action == "cancel":
            subscription = conn.execute(
                "SELECT subscription_id FROM sceneit_billing_subscriptions "
                "WHERE owner_id=%s AND status IN ('active','trialing','past_due') "
                "ORDER BY updated_at DESC LIMIT 1", (owner_id,),
            ).fetchone()
            if not subscription:
                raise BillingProblem("subscription_missing", "No cancellable subscription exists.", 404)
        operation_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO sceneit_billing_operations(operation_id,owner_id,kind,"
            "idempotency_key,parameters_hash,state) VALUES(%s,%s,'portal',%s,%s,'creating')",
            (operation_id, owner_id, key, params_hash),
        )
    try:
        hosted = _provider(settings, provider).create_portal(
            account["customer_id"], settings.return_url,
            settings.portal_configuration, action=action,
            subscription_id=subscription["subscription_id"] if subscription else None,
        )
    except BillingProviderError as exc:
        with connection() as conn:
            conn.execute(
                "UPDATE sceneit_billing_operations SET state=%s,last_error_code=%s,"
                "updated_at=now() WHERE operation_id=%s",
                ("uncertain" if exc.outcome_unknown else "failed", exc.code, operation_id),
            )
        raise BillingProblem(exc.code, "The billing portal could not be created.", 503) from exc
    portal_expires = (
        hosted.expires_at
        or datetime.now(timezone.utc) + timedelta(minutes=5)
    )
    with connection() as conn:
        conn.execute(
            "UPDATE sceneit_billing_operations SET state='created',provider_object_id=%s,"
            "hosted_url=%s,expires_at=%s,updated_at=now() WHERE operation_id=%s",
            (hosted.id, hosted.url, portal_expires, operation_id),
        )
    return {
        "operationId": operation_id, "url": hosted.url,
        "expiresAt": _iso(portal_expires),
    }


def _validate_subscription(subscription, owner_id, customer_id, settings):
    expected_live = settings.environment == "live"
    allowed_prices = (
        set(settings.catalog.prices)
        if settings.catalog.prices else set(settings.prices.values())
    )
    if (
        subscription.customer_id != customer_id
        or subscription.livemode != expected_live
        or subscription.price_id not in allowed_prices
        or subscription.owner_id != owner_id
    ):
        raise BillingProblem(
            "billing_relationship_invalid", "Provider billing relationship is invalid.", 409
        )


def _offer_for_price(settings, price_id):
    offer = settings.catalog.prices.get(price_id)
    if offer is not None:
        return offer
    for cadence, candidate in settings.prices.items():
        if candidate == price_id and cadence in ("monthly", "yearly"):
            return _offer(settings, "legacy", cadence, "usd")
    raise BillingProblem("billing_relationship_invalid", "Price relationship is invalid.", 409)


def _is_monotone_upgrade(source_tier, target_tier):
    return (
        target_tier.rank > source_tier.rank
        and set(source_tier.capabilities) <= set(target_tier.capabilities)
        and all(
            target_tier.limits[metric] >= value
            for metric, value in source_tier.limits.items()
        )
    )


def _current_subscription(owner_id, settings, *, lock=False):
    suffix = " FOR UPDATE" if lock else ""
    with connection() as conn:
        row = conn.execute(
            "SELECT s.*,a.environment FROM sceneit_billing_subscriptions s "
            "JOIN sceneit_billing_accounts a ON a.owner_id=s.owner_id "
            "WHERE s.owner_id=%s AND s.status IN "
            "('active','trialing','past_due','unpaid','incomplete') "
            "ORDER BY s.updated_at DESC LIMIT 1" + suffix,
            (owner_id,),
        ).fetchone()
    if not row or row["environment"] != settings.environment:
        raise BillingProblem("subscription_missing", "No changeable subscription exists.", 404)
    return row


def preview_change(
    owner_id, tier, cadence, currency, idempotency_key, *,
    purchase_authorized=False, provider=None
):
    settings = billing_settings()
    if not settings.enabled:
        raise BillingProblem("billing_disabled", "Billing is disabled.", 503)
    _require_purchase_eligibility(owner_id, purchase_authorized)
    key = _uuid(idempotency_key)
    target = _offer(settings, tier, cadence, currency, for_sale=True)
    current = _current_subscription(owner_id, settings)
    source = _offer_for_price(settings, current["price_id"])
    if source.currency != currency:
        raise BillingProblem("currency_change_unsupported", "Subscription currency cannot change.", 409)
    if source.price_id == target.price_id:
        raise BillingProblem("change_not_required", "The selected offer is already current.", 409)
    source_tier = settings.catalog.tiers[source.tier]
    target_tier = settings.catalog.tiers[target.tier]
    kind = (
        "upgrade"
        if source.cadence == target.cadence
        and _is_monotone_upgrade(source_tier, target_tier)
        else "scheduled"
    )
    params_hash = _parameters_hash(
        "preview", current["subscription_id"], source.price_id, target.price_id,
        tier, cadence, currency, kind,
    )
    with connection() as conn:
        prior = conn.execute(
            "SELECT * FROM sceneit_billing_change_previews "
            "WHERE owner_id=%s AND idempotency_key=%s",
            (owner_id, key),
        ).fetchone()
    if prior:
        if prior["parameters_hash"] != params_hash:
            raise BillingProblem("idempotency_conflict", "Preview key was reused.", 409)
        if prior["state"] != "open" or prior["expires_at"] <= datetime.now(timezone.utc):
            raise BillingProblem("preview_expired", "The change preview expired.", 409)
        return _present_preview(prior)
    adapter = _provider(settings, provider)
    subscription = adapter.retrieve_subscription(current["subscription_id"])
    _validate_subscription(
        subscription, owner_id, current["customer_id"], settings
    )
    if subscription.currency != currency:
        raise BillingProblem("currency_change_unsupported", "Subscription currency cannot change.", 409)
    adapter.verify_price(
        target.price_id, target.cadence, settings.environment == "live",
        currency=target.currency, unit_amount=target.unit_amount,
        tax_behavior=target.tax_behavior, tax_code=target.tax_code,
    )
    with connection() as conn:
        proration_at = billing_now(conn).replace(microsecond=0)
    effective_at = None if kind == "upgrade" else subscription.current_period_end
    result = adapter.preview_change(
        subscription, target.price_id, proration_at,
        kind=kind, effective_at=effective_at,
    )
    if result.currency != currency:
        raise BillingProblem("billing_relationship_invalid", "Preview currency is invalid.", 409)
    preview_id = str(uuid.uuid4())
    expires_at = min(result.expires_at, datetime.now(timezone.utc) + timedelta(minutes=30))
    if kind == "scheduled" and effective_at is not None:
        expires_at = min(expires_at, effective_at)
    with connection() as conn:
        row = conn.execute(
            "INSERT INTO sceneit_billing_change_previews(preview_id,owner_id,"
            "idempotency_key,parameters_hash,subscription_id,source_price_id,"
            "target_price_id,target_tier_key,target_cadence,currency,kind,subtotal,"
            "tax,total,proration_at,effective_at,expires_at,provider_preview_id) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(owner_id,idempotency_key) DO NOTHING RETURNING *",
            (preview_id, owner_id, key, params_hash, subscription.id,
             source.price_id, target.price_id, tier, cadence, currency, kind,
             result.subtotal, result.tax, result.total, result.proration_at, effective_at,
             expires_at, result.id),
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT * FROM sceneit_billing_change_previews "
                "WHERE owner_id=%s AND idempotency_key=%s", (owner_id, key),
            ).fetchone()
    if row["parameters_hash"] != params_hash:
        raise BillingProblem("idempotency_conflict", "Preview key was reused.", 409)
    return _present_preview(row)


def _present_preview(row):
    return {
        "previewId": str(row["preview_id"]), "kind": row["kind"],
        "effectiveAt": _iso(row["effective_at"]),
        "expiresAt": _iso(row["expires_at"]), "currency": row["currency"],
        "subtotal": row["subtotal"], "tax": row["tax"], "total": row["total"],
    }


def confirm_change(
    owner_id, preview_id, idempotency_key, *,
    purchase_authorized=False, provider=None
):
    settings = billing_settings()
    if not settings.enabled:
        raise BillingProblem("billing_disabled", "Billing is disabled.", 503)
    _require_purchase_eligibility(owner_id, purchase_authorized)
    preview_key, key = _uuid(preview_id, "invalid_preview"), _uuid(idempotency_key)
    adapter = _provider(settings, provider)
    subscription = None
    with connection() as conn:
        conn.execute(
            "SELECT owner_id FROM sceneit_billing_accounts WHERE owner_id=%s FOR UPDATE",
            (owner_id,),
        )
        preview = conn.execute(
            "SELECT * FROM sceneit_billing_change_previews "
            "WHERE preview_id=%s AND owner_id=%s FOR UPDATE",
            (preview_key, owner_id),
        ).fetchone()
        if not preview:
            raise BillingProblem("preview_missing", "Change preview was not found.", 404)
        params_hash = _parameters_hash(
            "confirm", preview_key, preview["parameters_hash"]
        )
        prior = conn.execute(
            "SELECT o.*,c.change_id,c.state change_state,c.effective_at "
            "FROM sceneit_billing_operations o LEFT JOIN sceneit_billing_changes c "
            "ON c.operation_id=o.operation_id "
            "WHERE o.owner_id=%s AND o.idempotency_key=%s FOR UPDATE OF o",
            (owner_id, key),
        ).fetchone()
        if prior:
            if prior["parameters_hash"] != params_hash:
                raise BillingProblem("idempotency_conflict", "Confirmation key was reused.", 409)
            return _present_change(prior)
        if preview["state"] != "open" or preview["expires_at"] <= datetime.now(timezone.utc):
            conn.execute(
                "UPDATE sceneit_billing_change_previews SET state='expired' "
                "WHERE preview_id=%s", (preview_key,),
            )
            raise BillingProblem("preview_expired", "The change preview expired.", 409)
        relationship = conn.execute(
            "SELECT s.price_id,s.customer_id,a.customer_id account_customer "
            "FROM sceneit_billing_subscriptions s JOIN sceneit_billing_accounts a "
            "ON a.owner_id=s.owner_id WHERE s.owner_id=%s AND s.subscription_id=%s "
            "FOR UPDATE OF s,a",
            (owner_id, preview["subscription_id"]),
        ).fetchone()
        if (
            not relationship
            or relationship["price_id"] != preview["source_price_id"]
            or relationship["customer_id"] != relationship["account_customer"]
        ):
            raise BillingProblem(
                "preview_relationship_changed",
                "The subscription changed after this preview.", 409,
            )
        if preview["kind"] == "scheduled":
            try:
                subscription = adapter.retrieve_subscription(
                    preview["subscription_id"]
                )
                _validate_subscription(
                    subscription, owner_id,
                    relationship["account_customer"], settings,
                )
            except BillingProviderError as exc:
                raise BillingProblem(
                    exc.code,
                    "The current subscription could not be verified.", 503,
                ) from exc
            if (
                preview["effective_at"] is None
                or datetime.now(timezone.utc) >= preview["effective_at"]
                or subscription.price_id != preview["source_price_id"]
                or subscription.current_period_end is None
                or int(subscription.current_period_end.timestamp())
                != int(preview["effective_at"].timestamp())
            ):
                conn.execute(
                    "UPDATE sceneit_billing_change_previews SET state='expired' "
                    "WHERE preview_id=%s AND state='open'", (preview_key,),
                )
                raise BillingProblem(
                    "preview_expired",
                    "The renewal date changed. Create a fresh change preview.",
                    409,
                )
        if preview["kind"] == "upgrade":
            from .quota import effective_coverage
            source_coverage = effective_coverage(
                conn, owner_id, billing_now(conn),
                subscription_id=preview["subscription_id"],
            )
            if (
                not source_coverage
                or source_coverage["price_id"] != preview["source_price_id"]
            ):
                raise BillingProblem(
                    "upgrade_source_coverage_missing",
                    "The current subscription price is not independently "
                    "funded by effective paid coverage.", 409,
                )
        operation_id, change_id = str(uuid.uuid4()), str(uuid.uuid4())
        operation_kind = "upgrade" if preview["kind"] == "upgrade" else "schedule"
        conn.execute(
            "INSERT INTO sceneit_billing_operations(operation_id,owner_id,kind,"
            "idempotency_key,parameters_hash,state) VALUES(%s,%s,%s,%s,%s,'creating')",
            (operation_id, owner_id, operation_kind, key, params_hash),
        )
        conn.execute(
            "INSERT INTO sceneit_billing_changes(change_id,owner_id,preview_id,"
            "operation_id,subscription_id,kind,target_tier_key,target_cadence,"
            "currency,target_price_id,state,effective_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'confirming',%s)",
            (change_id, owner_id, preview_key, operation_id,
             preview["subscription_id"], preview["kind"],
             preview["target_tier_key"], preview["target_cadence"],
             preview["currency"], preview["target_price_id"],
             preview["effective_at"]),
        )
        conn.execute(
            "UPDATE sceneit_billing_change_previews SET state='confirmed' "
            "WHERE preview_id=%s", (preview_key,),
        )
    upgrade_mutated = False
    try:
        if subscription is None:
            subscription = adapter.retrieve_subscription(
                preview["subscription_id"]
            )
            _validate_subscription(
                subscription, owner_id,
                relationship["account_customer"], settings,
            )
        if subscription.currency != preview["currency"]:
            raise BillingProblem("currency_change_unsupported", "Subscription currency cannot change.", 409)
        refreshed = adapter.preview_change(
            subscription, preview["target_price_id"], preview["proration_at"],
            kind=preview["kind"], effective_at=preview["effective_at"],
        )
        if (
            refreshed.currency != preview["currency"]
            or refreshed.subtotal != preview["subtotal"]
            or refreshed.tax != preview["tax"]
            or refreshed.total != preview["total"]
        ):
            raise BillingProblem(
                "preview_changed", "The authoritative change amount changed.", 409
            )
        if preview["kind"] == "upgrade":
            updated, pending_invoice_id = adapter.confirm_upgrade(
                subscription, preview["target_price_id"], operation_id,
                preview["proration_at"],
            )
            upgrade_mutated = True
            if (
                updated.id != subscription.id
                or updated.customer_id != relationship["account_customer"]
                or updated.owner_id != owner_id
            ):
                raise BillingProblem("billing_relationship_invalid", "Upgrade result is invalid.", 409)
            if (
                not isinstance(pending_invoice_id, str)
                or not pending_invoice_id.startswith("in_")
            ):
                raise BillingProblem(
                    "billing_relationship_invalid",
                    "Upgrade invoice relationship is invalid.", 409,
                )
            state, provider_object = "payment_pending", updated.id
        else:
            def remember_schedule(schedule_id):
                with connection() as conn:
                    conn.execute(
                        "UPDATE sceneit_billing_operations SET provider_object_id=%s,"
                        "updated_at=now() WHERE operation_id=%s AND state='creating'",
                        (schedule_id, operation_id),
                    )
                    conn.execute(
                        "UPDATE sceneit_billing_changes SET provider_schedule_id=%s,"
                        "updated_at=now() WHERE change_id=%s AND state='confirming'",
                        (schedule_id, change_id),
                    )
            provider_object = adapter.schedule_change(
                subscription, preview["target_price_id"], operation_id,
                on_created=remember_schedule,
                effective_at=preview["effective_at"],
            )
            state = "scheduled"
    except (BillingProviderError, BillingProblem) as exc:
        unknown = isinstance(exc, BillingProviderError) and exc.outcome_unknown
        unknown = unknown or (operation_kind == "upgrade" and upgrade_mutated)
        if operation_kind == "schedule":
            with connection() as conn:
                created_schedule = conn.execute(
                    "SELECT provider_schedule_id FROM sceneit_billing_changes "
                    "WHERE change_id=%s", (change_id,),
                ).fetchone()
            # Creating an attached schedule is itself a provider side effect.
            # Any later configuration failure requires exact-ID recovery even
            # when Stripe classified the failed update as a definite rejection.
            unknown = bool(
                created_schedule and created_schedule["provider_schedule_id"]
            ) or unknown
        with connection() as conn:
            conn.execute(
                "UPDATE sceneit_billing_operations SET state=%s,last_error_code=%s,"
                "updated_at=now() WHERE operation_id=%s",
                ("uncertain" if unknown else "failed", getattr(exc, "code", "change_failed"), operation_id),
            )
            conn.execute(
                "UPDATE sceneit_billing_changes SET state=%s,updated_at=now() "
                "WHERE change_id=%s",
                ("uncertain" if unknown else "failed", change_id),
            )
        if isinstance(exc, BillingProblem) and not unknown:
            raise
        raise BillingProblem(
            "billing_outcome_unknown" if unknown else exc.code,
            "The billing change could not be confirmed.", 503,
        ) from exc
    with connection() as conn:
        conn.execute(
            "UPDATE sceneit_billing_operations SET state=%s,provider_object_id=%s,"
            "updated_at=now() WHERE operation_id=%s AND state='creating'",
            ("confirmed" if state == "payment_pending" else "scheduled",
             provider_object, operation_id),
        )
        row = conn.execute(
            "UPDATE sceneit_billing_changes SET state=%s,provider_schedule_id=%s,"
            "pending_invoice_id=%s,"
            "updated_at=now() WHERE change_id=%s AND state='confirming' "
            "RETURNING change_id,state,effective_at",
            (
                state, provider_object if state == "scheduled" else None,
                pending_invoice_id if state == "payment_pending" else None,
                change_id,
            ),
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT change_id,state,effective_at FROM sceneit_billing_changes "
                "WHERE change_id=%s", (change_id,),
            ).fetchone()
    return _present_change(row)


def _present_change(row):
    state = row.get("change_state", row["state"])
    return {
        "changeId": str(row["change_id"]),
        "state": "outcome_unknown" if state == "uncertain" else state,
        "effectiveAt": _iso(row["effective_at"]),
        "hostedAction": None,
    }


def _retrieve_verified_schedule_change(adapter, change, settings):
    """Fetch one exact schedule and its actual subscription as a single fact."""
    verified = adapter.retrieve_schedule_change(
        change["provider_schedule_id"],
        expected_customer_id=change["customer_id"],
        expected_subscription_id=change["subscription_id"],
        expected_source_price_id=change["source_price_id"],
        expected_target_price_id=change["target_price_id"],
        expected_operation_id=str(change["operation_id"]),
        expected_effective_at=change["effective_at"],
        livemode=settings.environment == "live",
    )
    _validate_subscription(
        verified.subscription, change["owner_id"], change["customer_id"], settings
    )
    return verified


def _classify_verified_schedule_change(verified, change):
    subscription = verified.subscription
    if subscription.price_id == change["target_price_id"]:
        return "effective"
    same_source_period = (
        subscription.price_id == change["source_price_id"]
        and subscription.current_period_end is not None
        and int(subscription.current_period_end.timestamp())
        == int(change["effective_at"].timestamp())
    )
    if same_source_period and verified.status == "released":
        return "withdrawn"
    if (
        same_source_period
        and verified.status in ("not_started", "active")
        and change["effective_at"] > datetime.now(timezone.utc)
    ):
        return "scheduled"
    return "uncertain"


def withdraw_change(owner_id, change_id, idempotency_key, *, provider=None):
    settings = billing_settings()
    if not settings.enabled:
        raise BillingProblem("billing_disabled", "Billing is disabled.", 503)
    change_key, key = _uuid(change_id, "invalid_change"), _uuid(idempotency_key)
    params_hash = _parameters_hash("withdraw", change_key)
    with connection() as conn:
        conn.execute(
            "SELECT owner_id FROM sceneit_billing_accounts WHERE owner_id=%s FOR UPDATE",
            (owner_id,),
        )
        change = conn.execute(
            "SELECT * FROM sceneit_billing_changes WHERE change_id=%s "
            "AND owner_id=%s FOR UPDATE", (change_key, owner_id),
        ).fetchone()
        if not change:
            raise BillingProblem("change_missing", "Pending change was not found.", 404)
        prior = conn.execute(
            "SELECT * FROM sceneit_billing_operations "
            "WHERE owner_id=%s AND idempotency_key=%s FOR UPDATE", (owner_id, key),
        ).fetchone()
        if prior:
            if (
                prior["parameters_hash"] != params_hash
                or prior["kind"] != "withdraw"
            ):
                raise BillingProblem("idempotency_conflict", "Withdrawal key was reused.", 409)
            if (
                prior["state"] in ("completed", "withdrawn")
                and change["state"] == "withdrawn"
            ):
                return {"changeId": change_key, "state": "withdrawn"}
            if prior["state"] == "failed":
                raise BillingProblem(
                    "withdrawal_failed",
                    "The scheduled change was not withdrawn.", 409,
                )
            raise BillingProblem(
                "withdrawal_outcome_unknown",
                "The withdrawal requires provider reconciliation.", 409,
            )
        change = conn.execute(
            "SELECT c.*,p.source_price_id,a.customer_id,"
            "s.current_period_end persisted_period_end "
            "FROM sceneit_billing_changes c "
            "JOIN sceneit_billing_change_previews p ON p.preview_id=c.preview_id "
            "JOIN sceneit_billing_accounts a ON a.owner_id=c.owner_id "
            "JOIN sceneit_billing_subscriptions s ON "
            "s.subscription_id=c.subscription_id AND s.owner_id=c.owner_id "
            "WHERE c.change_id=%s AND c.owner_id=%s FOR UPDATE OF c,a,s",
            (change_key, owner_id),
        ).fetchone()
        if not change:
            raise BillingProblem(
                "billing_relationship_invalid",
                "Provider billing relationship is invalid.", 409,
            )
        if (
            change["state"] != "scheduled"
            or not change["provider_schedule_id"]
            or change["effective_at"] is None
        ):
            raise BillingProblem("change_not_withdrawable", "Only a scheduled change can be withdrawn.", 409)
        operation_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO sceneit_billing_operations(operation_id,owner_id,kind,"
            "idempotency_key,parameters_hash,state,provider_object_id) "
            "VALUES(%s,%s,'withdraw',%s,%s,'creating',%s)",
            (operation_id, owner_id, key, params_hash,
             change["provider_schedule_id"]),
        )
    adapter = _provider(settings, provider)
    release_attempted = False
    try:
        before = _retrieve_verified_schedule_change(adapter, change, settings)
        if before.subscription.price_id == change["target_price_id"]:
            _save_subscription(before.subscription, settings)
            with connection() as conn:
                conn.execute(
                    "UPDATE sceneit_billing_operations SET state='failed',"
                    "last_error_code='change_not_withdrawable',updated_at=now() "
                    "WHERE operation_id=%s", (operation_id,),
                )
                conn.execute(
                    "UPDATE sceneit_billing_changes SET state='effective',"
                    "updated_at=now() WHERE change_id=%s AND state='scheduled'",
                    (change_key,),
                )
            raise BillingProblem(
                "change_not_withdrawable",
                "The scheduled change is already effective and cannot be withdrawn.",
                409,
            )
        if (
            before.subscription.price_id != change["source_price_id"]
            or before.subscription.current_period_end is None
            or int(before.subscription.current_period_end.timestamp())
            != int(change["effective_at"].timestamp())
            or before.status not in ("not_started", "active")
        ):
            raise BillingProviderError("invalid_schedule_relationship")
        if change["effective_at"] <= datetime.now(timezone.utc):
            with connection() as conn:
                conn.execute(
                    "UPDATE sceneit_billing_operations SET state='uncertain',"
                    "last_error_code='change_not_withdrawable',updated_at=now() "
                    "WHERE operation_id=%s", (operation_id,),
                )
                conn.execute(
                    "UPDATE sceneit_billing_changes SET state='uncertain',"
                    "updated_at=now() WHERE change_id=%s AND state='scheduled'",
                    (change_key,),
                )
            raise BillingProblem(
                "change_not_withdrawable",
                "The scheduled change deadline has passed and requires reconciliation.",
                409,
            )
        release_attempted = True
        adapter.withdraw_schedule(change["provider_schedule_id"], operation_id)
        after = _retrieve_verified_schedule_change(adapter, change, settings)
        if after.subscription.price_id == change["target_price_id"]:
            _save_subscription(after.subscription, settings)
            with connection() as conn:
                conn.execute(
                    "UPDATE sceneit_billing_operations SET state='failed',"
                    "last_error_code='change_not_withdrawable',updated_at=now() "
                    "WHERE operation_id=%s", (operation_id,),
                )
                conn.execute(
                    "UPDATE sceneit_billing_changes SET state='effective',"
                    "updated_at=now() WHERE change_id=%s AND state='scheduled'",
                    (change_key,),
                )
            raise BillingProblem(
                "change_not_withdrawable",
                "The scheduled change became effective and was not withdrawn.",
                409,
            )
        if (
            after.status != "released"
            or after.subscription.price_id != change["source_price_id"]
            or after.subscription.current_period_end is None
            or before.subscription.current_period_end is None
            or int(after.subscription.current_period_end.timestamp())
            != int(before.subscription.current_period_end.timestamp())
        ):
            raise BillingProviderError(
                "withdrawal_verification_failed", outcome_unknown=True
            )
    except BillingProblem:
        raise
    except BillingProviderError as exc:
        unknown = release_attempted or exc.outcome_unknown
        with connection() as conn:
            conn.execute(
                "UPDATE sceneit_billing_operations SET state=%s,last_error_code=%s,"
                "updated_at=now() WHERE operation_id=%s",
                ("uncertain" if unknown else "failed", exc.code, operation_id),
            )
            if unknown:
                conn.execute(
                    "UPDATE sceneit_billing_changes SET state='uncertain',updated_at=now() "
                    "WHERE change_id=%s", (change_key,),
                )
        raise BillingProblem(
            "billing_outcome_unknown" if unknown else exc.code,
            "The scheduled change could not be withdrawn.", 503,
        ) from exc
    with connection() as conn:
        conn.execute(
            "UPDATE sceneit_billing_operations SET state='withdrawn',updated_at=now() "
            "WHERE operation_id=%s", (operation_id,),
        )
        conn.execute(
            "UPDATE sceneit_billing_changes SET state='withdrawn',updated_at=now() "
            "WHERE change_id=%s", (change_key,),
        )
    return {"changeId": change_key, "state": "withdrawn"}


def _event_object_id(event):
    data = event.get("data", {}).get("object", {})
    return data.get("id"), data


def process_event(event, *, provider=None, delivered=True):
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
    object_id, event_object = _event_object_id(event)
    customer_reference = event_object.get("customer")
    subscription_reference = event_object.get("subscription")
    if isinstance(subscription_reference, dict):
        subscription_reference = subscription_reference.get("id")
    invoice_reference = event_object.get("invoice")
    if isinstance(invoice_reference, dict):
        invoice_reference = invoice_reference.get("id")
    # Recovery needs provider references, not the webhook's customer/payment
    # snapshot. Persisting only this narrow shape avoids retaining billing PII.
    if event_type.startswith("invoice."):
        invoice_reference = object_id
    provider_created = event.get("created")
    provider_created_at = (
        datetime.fromtimestamp(provider_created, timezone.utc)
        if isinstance(provider_created, int) and not isinstance(provider_created, bool)
        else None
    )
    currency = event_object.get("currency")
    amount = event_object.get("amount_paid")
    if amount is None:
        amount = event_object.get("amount_due")
    if not isinstance(currency, str) or len(currency) != 3:
        currency = None
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        amount = None
    object_type = event_type.split(".", 1)[0]
    payment_key = (
        str(invoice_reference) if isinstance(invoice_reference, str)
        else str(event_object.get("payment_intent"))
        if isinstance(event_object.get("payment_intent"), str) else None
    )
    stored_event = {
        "id": event_id, "type": event_type, "livemode": bool(livemode),
        "data": {"object": {
            "id": object_id, "invoice": invoice_reference,
            "customer": customer_reference,
            "subscription": subscription_reference,
        }},
    }
    with connection() as conn:
        inserted = conn.execute(
            "INSERT INTO sceneit_billing_events(event_id,environment,event_type,state,payload,"
            "provider_created_at,object_type,object_id,customer_id,invoice_id,"
            "subscription_id,payment_key,currency,amount,outcome) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING RETURNING event_id",
            (event_id, settings.environment, event_type,
             "processing" if event_type in SUPPORTED_EVENTS else "ignored",
             Jsonb(stored_event), provider_created_at, object_type, object_id,
             customer_reference, invoice_reference, subscription_reference,
             payment_key, currency, amount,
             None if event_type in SUPPORTED_EVENTS else "ignored"),
        ).fetchone()
        if not inserted:
            prior = conn.execute(
                "SELECT state FROM sceneit_billing_events WHERE event_id=%s FOR UPDATE",
                (event_id,)
            ).fetchone()
            if delivered:
                conn.execute(
                    "UPDATE sceneit_billing_events SET delivery_attempts="
                    "CASE WHEN delivery_attempts<9223372036854775807 "
                    "THEN delivery_attempts+1 ELSE delivery_attempts END,"
                    "updated_at=now() WHERE event_id=%s", (event_id,),
                )
            if prior["state"] in ("completed", "ignored"):
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
    if event_type not in SUPPORTED_EVENTS:
        with connection() as conn:
            conn.execute(
                "UPDATE sceneit_billing_events SET processed_at=now(),updated_at=now() "
                "WHERE event_id=%s", (event_id,),
            )
        return "completed"
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
            "outcome='reconciled',processed_at=now(),updated_at=now() "
            "WHERE event_id=%s", (event_id,),
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
        with connection() as conn:
            pending = conn.execute(
                "SELECT pending_invoice_id FROM sceneit_billing_changes "
                "WHERE owner_id=(SELECT owner_id FROM sceneit_billing_subscriptions "
                "WHERE subscription_id=%s) AND subscription_id=%s "
                "AND kind='upgrade' AND state IN "
                "('confirming','payment_pending','uncertain') "
                "AND pending_invoice_id IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (subscription.id, subscription.id),
            ).fetchone()
        if pending:
            pending_invoice = adapter.retrieve_invoice(
                pending["pending_invoice_id"]
            )
            if pending_invoice.status == "void":
                _expire_pending_upgrade(pending_invoice, settings)
        return
    if event_type.startswith("subscription_schedule."):
        with connection() as conn:
            change = conn.execute(
                "SELECT change_id,operation_id,state "
                "FROM sceneit_billing_changes "
                "WHERE provider_schedule_id=%s", (object_id,),
            ).fetchone()
        if not change:
            raise BillingProblem(
                "billing_relationship_invalid",
                "Schedule relationship is invalid.", 409,
            )
        # Terminal outcomes are durable. In particular, a released
        # one-phase schedule can be the already-verified compensation for a
        # failed schedule creation and must not be reinterpreted as a customer
        # withdrawal.
        if change["state"] in (
            "effective", "withdrawn", "failed", "expired",
        ):
            return
        with connection() as conn:
            change = conn.execute(
                "SELECT c.*,p.source_price_id,a.customer_id "
                "FROM sceneit_billing_changes c "
                "JOIN sceneit_billing_change_previews p "
                "ON p.preview_id=c.preview_id "
                "JOIN sceneit_billing_accounts a ON a.owner_id=c.owner_id "
                "JOIN sceneit_billing_subscriptions s ON "
                "s.subscription_id=c.subscription_id "
                "AND s.owner_id=c.owner_id AND s.customer_id=a.customer_id "
                "WHERE c.provider_schedule_id=%s AND c.change_id=%s",
                (object_id, change["change_id"]),
            ).fetchone()
        if not change or change["effective_at"] is None:
            raise BillingProblem(
                "billing_relationship_invalid",
                "Schedule relationship is invalid.", 409,
            )
        try:
            verified = _retrieve_verified_schedule_change(
                adapter, change, settings
            )
        except (BillingProviderError, BillingProblem):
            with connection() as conn:
                conn.execute(
                    "UPDATE sceneit_billing_changes SET state='uncertain',"
                    "updated_at=now() WHERE change_id=%s "
                    "AND state NOT IN ('effective','withdrawn')",
                    (change["change_id"],),
                )
                conn.execute(
                    "UPDATE sceneit_billing_operations SET state='uncertain',"
                    "updated_at=now() WHERE operation_id=%s "
                    "AND state NOT IN ('completed','withdrawn')",
                    (change["operation_id"],),
                )
            raise
        subscription = verified.subscription
        target = _classify_verified_schedule_change(verified, change)
        if target == "effective":
            _save_subscription(subscription, settings)
        elif target == "uncertain":
            with connection() as conn:
                conn.execute(
                    "UPDATE sceneit_billing_changes SET state='uncertain',"
                    "updated_at=now() WHERE change_id=%s "
                    "AND state NOT IN ('effective','withdrawn','failed','expired')",
                    (change["change_id"],),
                )
                conn.execute(
                    "UPDATE sceneit_billing_operations SET state='uncertain',"
                    "last_error_code='schedule_phase_unresolved',"
                    "updated_at=now() WHERE operation_id=%s "
                    "AND state NOT IN ('completed','withdrawn','failed','expired')",
                    (change["operation_id"],),
                )
            raise BillingProviderError("schedule_phase_unresolved")
        with connection() as conn:
            locked = conn.execute(
                "SELECT change_id,operation_id,state FROM sceneit_billing_changes "
                "WHERE provider_schedule_id=%s AND change_id=%s FOR UPDATE",
                (object_id, change["change_id"]),
            ).fetchone()
            if not locked:
                raise BillingProblem(
                    "billing_relationship_invalid",
                    "Schedule relationship changed during reconciliation.", 409,
                )
            if locked["state"] in ("effective", "withdrawn", "failed", "expired"):
                return
            conn.execute(
                "UPDATE sceneit_billing_changes SET state=%s,updated_at=now() "
                "WHERE change_id=%s AND state NOT IN "
                "('effective','withdrawn','failed','expired')",
                (target, change["change_id"]),
            )
            conn.execute(
                "UPDATE sceneit_billing_operations SET state=%s,updated_at=now() "
                "WHERE operation_id=%s",
                ("completed" if target in ("effective", "withdrawn") else "scheduled",
                 change["operation_id"]),
            )
        return
    if event_type in PAYMENT_PROBLEM_EVENTS:
        problem = adapter.retrieve_payment_problem(object_id)
        owner_id = _owner_for_customer(problem.customer_id, settings)
        if problem.livemode != (settings.environment == "live"):
            raise BillingProblem("billing_relationship_invalid", "Payment environment is invalid.", 409)
        offer = None
        if problem.subscription_id:
            subscription = adapter.retrieve_subscription(problem.subscription_id)
            _validate_subscription(
                subscription, owner_id, problem.customer_id, settings
            )
            offer = _offer_for_price(settings, subscription.price_id)
            _save_subscription(subscription, settings)
        with connection() as conn:
            # Provider timestamp ordering prevents a delayed old failure from
            # reopening a problem resolved by a later paid/voided event.
            conn.execute(
                "INSERT INTO sceneit_billing_payment_problems(invoice_id,owner_id,"
                "subscription_id,provider_created_at,tier_key,cadence,currency,"
                "amount_due,code,state,next_action) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(invoice_id) DO UPDATE SET "
                "code=EXCLUDED.code,next_action=EXCLUDED.next_action,"
                "amount_due=EXCLUDED.amount_due,"
                "state=CASE WHEN EXCLUDED.state='resolved' THEN 'resolved' "
                "ELSE sceneit_billing_payment_problems.state END,"
                "resolved_at=CASE WHEN EXCLUDED.state='resolved' THEN now() "
                "ELSE sceneit_billing_payment_problems.resolved_at END,"
                "updated_at=now() "
                "WHERE sceneit_billing_payment_problems.state='open' "
                "AND sceneit_billing_payment_problems.provider_created_at"
                "<=EXCLUDED.provider_created_at",
                (problem.invoice_id, owner_id, problem.subscription_id,
                 problem.created_at, offer.tier if offer else None,
                 offer.cadence if offer else None, problem.currency,
                 problem.amount_due, problem.code,
                 "resolved" if problem.obsolete else "open",
                 problem.next_action),
            )
            if problem.obsolete:
                conn.execute(
                    "UPDATE sceneit_billing_payment_problems SET resolved_at=now() "
                    "WHERE invoice_id=%s", (problem.invoice_id,),
                )
        from .billing_notifications import (
            NotificationFactUnavailable, open_dunning,
        )
        try:
            open_dunning(
                problem.invoice_id, problem.invoice_id, adapter,
                environment=settings.environment,
            )
        except NotificationFactUnavailable as exc:
            raise BillingProviderError("notification_fact_unavailable") from exc
        return
    if event_type in PAYMENT_RESOLUTION_EVENTS:
        invoice_id = (
            object_id if event_type == "invoice.voided"
            else obj.get("invoice")
            or adapter.invoice_id_for_payment_intent(object_id)
        )
        if invoice_id:
            if event_type == "invoice.voided":
                invoice = adapter.retrieve_invoice(str(invoice_id))
                if invoice.status != "void":
                    raise BillingProblem(
                        "invoice_state_invalid",
                        "Invoice is not confirmed void.", 409,
                    )
                _expire_pending_upgrade(invoice, settings)
            with connection() as conn:
                conn.execute(
                    "UPDATE sceneit_billing_payment_problems SET state='resolved',"
                    "resolved_at=now(),updated_at=now() WHERE invoice_id=%s "
                    "AND state='open'", (invoice_id,),
                )
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
    offer = _offer_for_price(settings, invoice.price_id)
    configured_amount = adapter.verify_price(
        invoice.price_id, offer.cadence, settings.environment == "live",
        currency=offer.currency, unit_amount=offer.unit_amount,
        tax_behavior=offer.tax_behavior, tax_code=offer.tax_code,
        require_active=False,
    )
    duration_days = (invoice.ends_at - invoice.starts_at).total_seconds() / 86400
    valid_period = (
        27 <= duration_days <= 32
        if offer.cadence == "monthly"
        else 365 <= duration_days <= 366
    )
    valid_tax = (
        invoice.tax_complete
        and invoice.currency == offer.currency
        and invoice.total == invoice.amount_paid
        and invoice.tax is not None
        and (
            (
                offer.tax_behavior == "exclusive"
                and invoice.subtotal == configured_amount
                and invoice.total == invoice.subtotal + invoice.tax
            )
            or (
                offer.tax_behavior == "inclusive"
                and invoice.subtotal == configured_amount
                and invoice.total == invoice.subtotal
                and invoice.tax <= invoice.total
            )
        )
    )
    pending_change = None
    if invoice.proration:
        with connection() as conn:
            pending_change = conn.execute(
                "SELECT c.*,p.total preview_total,"
                "p.source_price_id source_price_id FROM sceneit_billing_changes c "
                "JOIN sceneit_billing_change_previews p ON p.preview_id=c.preview_id "
                "WHERE c.owner_id=%s AND c.subscription_id=%s "
                "AND c.target_price_id=%s AND c.kind='upgrade' "
                "AND c.state IN ('confirming','payment_pending','uncertain','effective') "
                "AND (c.funded_invoice_id IS NULL OR c.funded_invoice_id=%s) "
                "ORDER BY c.created_at LIMIT 1",
                (owner_id, invoice.subscription_id, invoice.price_id, invoice.id),
            ).fetchone()
        lines = tuple(invoice.line_facts)
        positive = [
            line for line in lines
            if line.amount > 0 and line.price_id == invoice.price_id
        ]
        credits = [line for line in lines if line.amount < 0]
        valid_lines = bool(
            lines and len(positive) == 1
            and len(lines) == len(positive) + len(credits)
            and all(line.proration for line in lines)
            and all(
                line.price_id == pending_change["source_price_id"]
                for line in credits
            ) if pending_change else False
        )
        if valid_lines:
            funded_period = (positive[0].starts_at, positive[0].ends_at)
            valid_lines = (
                all(
                    (line.starts_at, line.ends_at) == funded_period
                    for line in lines
                )
                and sum(line.amount for line in lines) == invoice.subtotal
            )
        valid_invoice = bool(
            pending_change and valid_lines
            and invoice.mutation_id == str(pending_change["operation_id"])
            and invoice.amount_paid > 0
            and invoice.total == pending_change["preview_total"]
            and invoice.currency == offer.currency and invoice.tax_complete
        )
    else:
        valid_invoice = (
            configured_amount == invoice.subtotal and valid_period and valid_tax
        )
    if not valid_invoice:
        raise BillingProblem(
            "billing_relationship_invalid", "Invoice amount or period is invalid.", 409
        )
    _save_subscription(subscription, settings)
    if event_type in INVOICE_EVENTS:
        if invoice.status != "paid" or invoice.amount_paid <= 0:
            raise BillingProblem("invoice_not_paid", "Invoice is not confirmed paid.", 409)
        _save_coverage(
            owner_id, invoice, reversed=invoice.reversed,
            offer=offer, settings=settings, pending_change=pending_change,
        )
        with connection() as conn:
            conn.execute(
                "INSERT INTO sceneit_billing_payment_problems(invoice_id,owner_id,"
                "subscription_id,provider_created_at,tier_key,cadence,currency,"
                "amount_due,code,state,next_action,resolved_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,0,'payment_failed','resolved',"
                "'manage_billing',now()) ON CONFLICT(invoice_id) DO UPDATE SET "
                "state='resolved',resolved_at=now(),updated_at=now()",
                (invoice.id, owner_id, invoice.subscription_id,
                 invoice.provider_created_at or datetime.now(timezone.utc),
                 offer.tier, offer.cadence, invoice.currency),
            )
    elif event_type in REVERSAL_EVENTS and invoice.reversed:
        _save_coverage(
            owner_id, invoice, reversed=True, offer=offer, settings=settings,
            pending_change=pending_change,
        )


def _expire_pending_upgrade(invoice, settings):
    """Terminalize only the exact fresh void upgrade invoice relationship."""
    owner_id = _owner_for_customer(invoice.customer_id, settings)
    if invoice.livemode != (settings.environment == "live"):
        raise BillingProblem(
            "billing_relationship_invalid", "Invoice environment is invalid.", 409
        )
    offer = _offer_for_price(settings, invoice.price_id)
    with connection() as conn:
        conn.execute(
            "SELECT owner_id FROM sceneit_billing_accounts "
            "WHERE owner_id=%s FOR UPDATE", (owner_id,),
        )
        change = conn.execute(
            "SELECT change_id,operation_id,subscription_id,target_price_id,state "
            "FROM sceneit_billing_changes WHERE owner_id=%s "
            "AND pending_invoice_id=%s FOR UPDATE",
            (owner_id, invoice.id),
        ).fetchone()
        if not change:
            return False
        if (
            invoice.status != "void"
            or invoice.subscription_id != change["subscription_id"]
            or invoice.price_id != change["target_price_id"]
            or invoice.currency != offer.currency
            or not invoice.tax_complete
            or invoice.mutation_id != str(change["operation_id"])
        ):
            raise BillingProblem(
                "billing_relationship_invalid",
                "Upgrade invoice relationship is invalid.", 409,
            )
        if change["state"] not in (
            "confirming", "payment_pending", "uncertain"
        ):
            return False
        conn.execute(
            "UPDATE sceneit_billing_changes SET state='expired',updated_at=now() "
            "WHERE change_id=%s AND state IN "
            "('confirming','payment_pending','uncertain')",
            (change["change_id"],),
        )
        conn.execute(
            "UPDATE sceneit_billing_operations SET state='expired',"
            "last_error_code='upgrade_invoice_expired',updated_at=now() "
            "WHERE operation_id=%s AND state NOT IN ('completed','expired')",
            (change["operation_id"],),
        )
    return True


def _owner_for_customer(customer_id, settings):
    with connection() as conn:
        row = conn.execute(
            "SELECT owner_id,environment FROM sceneit_billing_accounts WHERE customer_id=%s",
            (customer_id,),
        ).fetchone()
    if not row or row["environment"] != settings.environment:
        raise BillingProblem("billing_relationship_invalid", "Customer relationship is invalid.", 409)
    return row["owner_id"]


def _save_subscription(
    subscription, settings, *, expected_owner_id=None, connection_factory=None,
):
    owner_id = (
        expected_owner_id
        if expected_owner_id is not None
        else _owner_for_customer(subscription.customer_id, settings)
    )
    _validate_subscription(subscription, owner_id, subscription.customer_id, settings)
    offer = _offer_for_price(settings, subscription.price_id)
    if subscription.currency is not None and subscription.currency != offer.currency:
        raise BillingProblem("billing_relationship_invalid", "Subscription currency is invalid.", 409)
    with (connection_factory or connection)() as conn:
        conn.execute(
            "INSERT INTO sceneit_billing_subscriptions(subscription_id,owner_id,customer_id,"
            "environment,price_id,status,cancel_at_period_end,current_period_end,"
            "tier_key,cadence,currency,schedule_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(subscription_id) DO UPDATE SET "
            "price_id=EXCLUDED.price_id,status=CASE WHEN sceneit_billing_subscriptions.status "
            "IN ('canceled','incomplete_expired') THEN sceneit_billing_subscriptions.status "
            "ELSE EXCLUDED.status END,"
            "cancel_at_period_end=EXCLUDED.cancel_at_period_end,"
            "current_period_end=EXCLUDED.current_period_end,"
            "tier_key=EXCLUDED.tier_key,cadence=EXCLUDED.cadence,"
            "currency=EXCLUDED.currency,schedule_id=EXCLUDED.schedule_id,updated_at=now()",
            (subscription.id, owner_id, subscription.customer_id, settings.environment,
             subscription.price_id, subscription.status, subscription.cancel_at_period_end,
             subscription.current_period_end, offer.tier, offer.cadence,
             subscription.currency or offer.currency, subscription.schedule_id),
        )
        conn.execute(
            "UPDATE sceneit_billing_changes SET state='effective',effective_at=now(),"
            "updated_at=now() WHERE owner_id=%s AND subscription_id=%s "
            "AND target_price_id=%s AND kind='scheduled' "
            "AND state IN ('scheduled','uncertain')",
            (owner_id, subscription.id, subscription.price_id),
        )
        conn.execute(
            "UPDATE sceneit_billing_operations o SET state='completed',updated_at=now() "
            "FROM sceneit_billing_changes c WHERE c.operation_id=o.operation_id "
            "AND c.owner_id=%s AND c.subscription_id=%s "
            "AND c.target_price_id=%s AND c.state='effective'",
            (owner_id, subscription.id, subscription.price_id),
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


def _save_coverage(
    owner_id, invoice, *, reversed, offer=None, settings=None,
    pending_change=None,
):
    tier = (
        settings.catalog.tiers.get(offer.tier)
        if settings is not None and offer is not None else None
    )
    coverage_kind = "upgrade" if pending_change else "period"
    funds_coverage_id = None
    with connection() as conn:
        conn.execute(
            "SELECT owner_id FROM sceneit_billing_accounts WHERE owner_id=%s FOR UPDATE",
            (owner_id,),
        )
        if pending_change:
            existing = conn.execute(
                "SELECT funds_coverage_id FROM sceneit_paid_coverage "
                "WHERE id=%s AND owner_id=%s FOR UPDATE",
                (invoice.id, owner_id),
            ).fetchone()
            funded = (
                {"id": existing["funds_coverage_id"]}
                if existing and existing["funds_coverage_id"] else
                None
            )
            if not funded:
                from .quota import effective_coverage
                source = effective_coverage(
                    conn, owner_id, invoice.starts_at,
                    subscription_id=invoice.subscription_id,
                )
                if (
                    source
                    and source["price_id"] == pending_change["source_price_id"]
                    and source["starts_at"] <= invoice.starts_at
                    and source["ends_at"] >= invoice.ends_at
                ):
                    funded = {"id": source["coverage_id"]}
            if not funded and not reversed:
                raise BillingProblem(
                    "upgrade_base_coverage_missing",
                    "The immediate paid source coverage for this upgrade "
                    "is not verified.", 409,
                )
            funds_coverage_id = funded["id"] if funded else None
        if reversed:
            # Reversal is invoice-scoped and sticky. An event arriving before its paid
            # event creates a tombstone which a delayed paid event cannot resurrect.
            if tier and (coverage_kind != "upgrade" or funds_coverage_id):
                conn.execute(
                    "INSERT INTO sceneit_paid_coverage(id,owner_id,subscription_id,"
                    "starts_at,ends_at,reversed,coverage_kind,funds_coverage_id,"
                    "tier_key,tier_rank,capabilities_snapshot,limits_snapshot,"
                    "cadence,currency,price_id,subtotal,tax,total,amount_paid,"
                    "tax_behavior,provider_created_at,provider_receipt_at) "
                    "VALUES(%s,%s,%s,%s,%s,true,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                    "%s,%s,%s,%s,%s,%s,now()) "
                    "ON CONFLICT(id) DO UPDATE SET reversed=true,updated_at=now()",
                    (invoice.id, owner_id, invoice.subscription_id,
                     invoice.starts_at, invoice.ends_at, coverage_kind,
                     funds_coverage_id, offer.tier, tier.rank,
                     Jsonb(list(tier.capabilities)), Jsonb(tier.limits),
                     offer.cadence, invoice.currency, invoice.price_id,
                     invoice.subtotal, invoice.tax, invoice.total,
                     invoice.amount_paid, offer.tax_behavior,
                     invoice.provider_created_at),
                )
            else:
                conn.execute(
                    "INSERT INTO sceneit_paid_coverage(id,owner_id,subscription_id,starts_at,"
                    "ends_at,reversed) VALUES (%s,%s,%s,%s,%s,true) "
                    "ON CONFLICT(id) DO UPDATE SET reversed=true,updated_at=now()",
                    (invoice.id, owner_id, invoice.subscription_id,
                     invoice.starts_at, invoice.ends_at),
                )
            _terminalize_reversed_upgrade(
                conn, owner_id, invoice, pending_change=pending_change,
            )
        else:
            conn.execute(
                "INSERT INTO sceneit_paid_coverage(id,owner_id,subscription_id,starts_at,"
                "ends_at,reversed,coverage_kind,funds_coverage_id,tier_key,tier_rank,"
                "capabilities_snapshot,limits_snapshot,cadence,currency,price_id,"
                "subtotal,tax,total,amount_paid,tax_behavior,provider_created_at,"
                "provider_receipt_at) "
                "VALUES (%s,%s,%s,%s,%s,false,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "%s,%s,%s,%s,%s,now()) "
                "ON CONFLICT(id) DO UPDATE SET starts_at=EXCLUDED.starts_at,"
                "ends_at=EXCLUDED.ends_at,updated_at=now()",
                (invoice.id, owner_id, invoice.subscription_id,
                 invoice.starts_at, invoice.ends_at, coverage_kind,
                 funds_coverage_id, offer.tier if offer else None,
                 tier.rank if tier else None,
                 Jsonb(list(tier.capabilities)) if tier else None,
                 Jsonb(tier.limits) if tier else None,
                 offer.cadence if offer else None,
                 invoice.currency, invoice.price_id, invoice.subtotal,
                 invoice.tax, invoice.total, invoice.amount_paid,
                 offer.tax_behavior if offer else None,
                 invoice.provider_created_at),
            )
            conn.execute(
                "UPDATE sceneit_billing_accounts SET allowance_anchor="
                "COALESCE(allowance_anchor,%s),updated_at=now() WHERE owner_id=%s",
                (invoice.starts_at, owner_id),
            )
            if pending_change:
                if (
                    pending_change["state"] == "effective"
                    and pending_change["funded_invoice_id"] == invoice.id
                ):
                    changed = pending_change
                else:
                    changed = conn.execute(
                        "UPDATE sceneit_billing_changes SET state='effective',"
                        "funded_invoice_id=%s,effective_at=now(),updated_at=now() "
                        "WHERE change_id=%s AND state IN "
                        "('confirming','payment_pending','uncertain') "
                        "RETURNING change_id",
                        (invoice.id, pending_change["change_id"]),
                    ).fetchone()
                if not changed:
                    raise BillingProblem(
                        "upgrade_state_invalid",
                        "The paid upgrade state is no longer eligible.", 409,
                    )
                conn.execute(
                    "UPDATE sceneit_billing_operations SET state='completed',"
                    "updated_at=now() WHERE operation_id=%s",
                    (pending_change["operation_id"],),
                )


def _terminalize_reversed_upgrade(
    conn, owner_id, invoice, *, pending_change=None,
):
    """Fail only the upgrade mutation exactly identified by this reversal."""
    change = pending_change
    if change is None:
        change = conn.execute(
            "SELECT c.change_id,c.operation_id,c.subscription_id,"
            "c.target_price_id,c.state,c.pending_invoice_id,o.state operation_state,"
            "a.customer_id "
            "FROM sceneit_billing_changes c "
            "JOIN sceneit_billing_operations o "
            "ON o.operation_id=c.operation_id AND o.owner_id=c.owner_id "
            "JOIN sceneit_billing_accounts a ON a.owner_id=c.owner_id "
            "WHERE c.owner_id=%s AND c.kind='upgrade' "
            "AND c.pending_invoice_id=%s FOR UPDATE OF c,o",
            (owner_id, invoice.id),
        ).fetchone()
    else:
        # Re-read under the account lock. Callers may hold a pre-provider
        # snapshot and terminalization must be based on current durable facts.
        change = conn.execute(
            "SELECT c.change_id,c.operation_id,c.subscription_id,"
            "c.target_price_id,c.state,c.pending_invoice_id,o.state operation_state,"
            "a.customer_id "
            "FROM sceneit_billing_changes c "
            "JOIN sceneit_billing_operations o "
            "ON o.operation_id=c.operation_id AND o.owner_id=c.owner_id "
            "JOIN sceneit_billing_accounts a ON a.owner_id=c.owner_id "
            "WHERE c.owner_id=%s AND c.change_id=%s FOR UPDATE OF c,o",
            (owner_id, pending_change["change_id"]),
        ).fetchone()
    if not change:
        return False
    if (
        change["customer_id"] != invoice.customer_id
        or change["subscription_id"] != invoice.subscription_id
        or change["target_price_id"] != invoice.price_id
        or change["pending_invoice_id"] != invoice.id
        or invoice.mutation_id != str(change["operation_id"])
    ):
        raise BillingProblem(
            "billing_relationship_invalid",
            "Reversed upgrade relationship is invalid.", 409,
        )
    if change["state"] in ("failed", "expired"):
        return change["operation_state"] in ("failed", "expired")
    if change["state"] not in ("confirming", "payment_pending", "uncertain"):
        return False
    conn.execute(
        "UPDATE sceneit_billing_changes SET state='failed',updated_at=now() "
        "WHERE change_id=%s AND state IN "
        "('confirming','payment_pending','uncertain')",
        (change["change_id"],),
    )
    conn.execute(
        "UPDATE sceneit_billing_operations SET state='failed',"
        "last_error_code='upgrade_invoice_reversed',updated_at=now() "
        "WHERE operation_id=%s AND state IN "
        "('creating','confirmed','uncertain')",
        (change["operation_id"],),
    )
    terminal = conn.execute(
        "SELECT c.state change_state,o.state operation_state "
        "FROM sceneit_billing_changes c JOIN sceneit_billing_operations o "
        "ON o.operation_id=c.operation_id WHERE c.change_id=%s",
        (change["change_id"],),
    ).fetchone()
    return bool(
        terminal
        and terminal["change_state"] == "failed"
        and terminal["operation_state"] == "failed"
    )