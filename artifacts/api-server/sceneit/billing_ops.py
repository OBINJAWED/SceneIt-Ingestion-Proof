"""Bounded, explicit billing event recovery diagnostics (not a service)."""
import argparse
import json
import re
from datetime import datetime, timezone
import time

from .billing import (
    _classify_verified_schedule_change, _expire_pending_upgrade,
    _reconcile_event, _save_subscription,
    _retrieve_verified_schedule_change, checkout_token, complete_checkout,
    process_event,
)
from .billing_config import BillingProblem, billing_settings
from .billing_provider import BillingProviderError, StripeBillingProvider
from .db import connection
from psycopg.types.json import Jsonb


def pending(limit=50):
    limit = max(1, min(int(limit), 100))
    with connection() as conn:
        return conn.execute(
            "SELECT event_id,event_type,state,attempts,delivery_attempts,"
            "last_error_code,updated_at "
            "FROM sceneit_billing_events WHERE state='pending' "
            "ORDER BY updated_at LIMIT %s", (limit,),
        ).fetchall()


def timeline(
    limit=50, *, state=None, event_type=None, before_received_at=None,
    before_event_id=None, payment_key=None,
):
    """Bounded privacy-safe verified receipt timeline."""
    limit = max(1, min(int(limit), 100))
    allowed_states = {
        "processing", "completed", "pending", "rejected", "ignored",
    }
    if state is not None and state not in allowed_states:
        raise ValueError("unsupported receipt state")
    if event_type is not None and (
        not isinstance(event_type, str) or not 1 <= len(event_type) <= 100
    ):
        raise ValueError("event type is invalid")
    if before_received_at is not None and (
        not isinstance(before_received_at, datetime)
        or before_received_at.tzinfo is None
    ):
        raise ValueError("receipt cursor timestamp is invalid")
    if (before_received_at is None) != (before_event_id is None):
        raise ValueError("both receipt cursor fields are required")
    if before_event_id is not None and not re.fullmatch(r"evt_[A-Za-z0-9]{1,120}", before_event_id):
        raise ValueError("receipt cursor event is invalid")
    if payment_key is not None and not re.fullmatch(r"[A-Za-z0-9_:.-]{1,128}", payment_key):
        raise ValueError("payment reference is invalid")
    clauses, values = [], []
    if state:
        clauses.append("state=%s")
        values.append(state)
    if event_type:
        clauses.append("event_type=%s")
        values.append(event_type)
    if payment_key:
        clauses.append("payment_key=%s")
        values.append(payment_key)
    if before_received_at:
        clauses.append("(received_at,event_id)<(%s,%s)")
        values.extend((before_received_at, before_event_id or ""))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connection() as conn:
        rows = conn.execute(
            "SELECT event_id,event_type,state,attempts,delivery_attempts,"
            "provider_created_at,"
            "received_at,processed_at,object_type,object_id,customer_id,invoice_id,"
            "subscription_id,payment_key,currency,amount,outcome,last_error_code "
            "FROM sceneit_billing_events" + where
            + " ORDER BY received_at DESC,event_id DESC LIMIT %s",
            (*values, limit),
        ).fetchall()
    return [
        {
            "eventId": row["event_id"], "type": row["event_type"],
            "state": row["state"], "attempts": row["attempts"],
            "deliveryAttempts": row["delivery_attempts"],
            "providerCreatedAt": row["provider_created_at"],
            "receivedAt": row["received_at"], "processedAt": row["processed_at"],
            "objectType": row["object_type"], "objectId": row["object_id"],
            "customerId": row["customer_id"], "invoiceId": row["invoice_id"],
            "subscriptionId": row["subscription_id"],
            "paymentKey": row["payment_key"], "currency": row["currency"],
            "amount": row["amount"], "outcome": row["outcome"],
            "errorCode": row["last_error_code"],
        }
        for row in rows
    ]


def receipt_counters():
    """Safe alert eligibility facts; payments are distinct from deliveries."""
    with connection() as conn:
        rows = conn.execute(
            "SELECT state,count(*) count,"
            "count(DISTINCT payment_key) FILTER(WHERE payment_key IS NOT NULL) payments,"
            "count(*) FILTER(WHERE updated_at<now()-interval '10 minutes') aging "
            "FROM sceneit_billing_events GROUP BY state"
        ).fetchall()
    return {
        row["state"]: {
            "receipts": row["count"], "payments": row["payments"],
            "aging": row["aging"],
        } for row in rows
    }


def recover_operations(limit=20, *, evidence):
    """Reconcile uncertain mutations, narrowly replaying withdrawal intent."""
    limit = max(1, min(int(limit), 50))
    settings = billing_settings()
    if not settings.enabled:
        return {"checked": 0, "resolved": 0, "unresolved": []}
    with connection() as conn:
        rows = conn.execute(
            "SELECT o.operation_id,o.owner_id,o.kind,o.provider_object_id,"
            "o.last_error_code,o.expires_at compensation_phase_end,c.change_id,"
            "(o.created_at>now()-interval '24 hours') "
            "AS within_idempotency_window,"
            "c.subscription_id,c.target_price_id,c.pending_invoice_id,"
            "c.effective_at,c.operation_id schedule_operation_id,"
            "p.source_price_id,"
            "a.customer_id "
            "FROM sceneit_billing_operations o "
            "LEFT JOIN sceneit_billing_changes c ON "
            "(c.operation_id=o.operation_id OR "
            "(o.kind='withdraw' AND c.provider_schedule_id=o.provider_object_id)) "
            "LEFT JOIN sceneit_billing_change_previews p ON p.preview_id=c.preview_id "
            "JOIN sceneit_billing_accounts a ON a.owner_id=o.owner_id "
            "WHERE o.state='uncertain' OR "
            "(o.kind='withdraw' AND o.state='scheduled') OR "
            "(o.state IN ('creating','confirmed') "
            "AND o.updated_at<now()-interval '10 minutes') OR "
            "(c.state='payment_pending' "
            "AND c.updated_at<now()-interval '10 minutes') "
            "ORDER BY o.updated_at LIMIT %s", (limit,),
        ).fetchall()
    provider = StripeBillingProvider(settings)
    resolved, unresolved = 0, []
    deadline = time.monotonic() + 60
    for row in rows:
        if time.monotonic() >= deadline:
            unresolved.append(str(row["operation_id"]))
            continue
        if row["kind"] == "withdraw":
            # Older recovery code incorrectly treated an active schedule as a
            # resolved withdrawal. Keep those rows eligible for exact replay.
            with connection() as conn:
                conn.execute(
                    "UPDATE sceneit_billing_operations SET state='uncertain',"
                    "updated_at=now() WHERE operation_id=%s AND kind='withdraw' "
                    "AND state='scheduled'",
                    (row["operation_id"],),
                )
        try:
            if row["kind"] == "upgrade" and row["subscription_id"]:
                pending_invoice_id = row["pending_invoice_id"]
                if not pending_invoice_id:
                    pending_invoice_id = provider.find_upgrade_invoice(
                        row["subscription_id"], str(row["operation_id"])
                    )
                    if pending_invoice_id:
                        with connection() as conn:
                            conn.execute(
                                "UPDATE sceneit_billing_changes SET "
                                "pending_invoice_id=%s,updated_at=now() "
                                "WHERE change_id=%s AND pending_invoice_id IS NULL "
                                "AND state IN ('confirming','uncertain')",
                                (pending_invoice_id, row["change_id"]),
                            )
                if not pending_invoice_id:
                    unresolved.append(str(row["operation_id"]))
                    continue
                invoice = provider.retrieve_invoice(pending_invoice_id)
                if (
                    invoice.id != pending_invoice_id
                    or invoice.subscription_id != row["subscription_id"]
                    or invoice.customer_id != row["customer_id"]
                    or invoice.price_id != row["target_price_id"]
                    or invoice.mutation_id != str(row["operation_id"])
                    or invoice.livemode != (settings.environment == "live")
                ):
                    unresolved.append(str(row["operation_id"]))
                    continue
                if invoice.status == "paid" and invoice.amount_paid > 0:
                    _reconcile_event(
                        "invoice.paid",
                        {
                            "id": "operator-recovery",
                            "type": "invoice.paid",
                            "data": {"object": {"id": invoice.id}},
                        },
                        provider, settings,
                    )
                    with connection() as conn:
                        terminal = conn.execute(
                            "SELECT c.state change_state,o.state operation_state "
                            "FROM sceneit_billing_changes c "
                            "JOIN sceneit_billing_operations o "
                            "ON o.operation_id=c.operation_id "
                            "WHERE c.change_id=%s AND c.owner_id=%s "
                            "AND c.subscription_id=%s "
                            "AND c.target_price_id=%s "
                            "AND c.pending_invoice_id=%s "
                            "AND c.operation_id=%s",
                            (
                                row["change_id"], row["owner_id"],
                                invoice.subscription_id, invoice.price_id,
                                invoice.id, row["operation_id"],
                            ),
                        ).fetchone()
                    expected = (
                        ("failed", "failed")
                        if invoice.reversed else ("effective", "completed")
                    )
                    if not terminal or (
                        terminal["change_state"],
                        terminal["operation_state"],
                    ) != expected:
                        unresolved.append(str(row["operation_id"]))
                        continue
                    resolved += 1
                    continue
                if invoice.status == "void":
                    _expire_pending_upgrade(invoice, settings)
                    with connection() as conn:
                        terminal = conn.execute(
                            "SELECT c.state change_state,o.state operation_state "
                            "FROM sceneit_billing_changes c "
                            "JOIN sceneit_billing_operations o "
                            "ON o.operation_id=c.operation_id "
                            "WHERE c.change_id=%s AND c.owner_id=%s "
                            "AND c.subscription_id=%s "
                            "AND c.target_price_id=%s "
                            "AND c.pending_invoice_id=%s "
                            "AND c.operation_id=%s",
                            (
                                row["change_id"], row["owner_id"],
                                invoice.subscription_id, invoice.price_id,
                                invoice.id, row["operation_id"],
                            ),
                        ).fetchone()
                    if not terminal or (
                        terminal["change_state"],
                        terminal["operation_state"],
                    ) != ("expired", "expired"):
                        unresolved.append(str(row["operation_id"]))
                        continue
                    resolved += 1
                    continue
                unresolved.append(str(row["operation_id"]))
                continue
            elif row["kind"] in ("schedule", "withdraw"):
                schedule_id = row["provider_object_id"]
                if (
                    not schedule_id and row["kind"] == "schedule"
                    and row["subscription_id"]
                ):
                    schedule_id = provider.find_schedule(
                        row["customer_id"], str(row["operation_id"]),
                        row["subscription_id"],
                    )
                    if schedule_id:
                        with connection() as conn:
                            conn.execute(
                                "UPDATE sceneit_billing_operations SET "
                                "provider_object_id=%s "
                                "WHERE operation_id=%s AND provider_object_id IS NULL",
                                (schedule_id, row["operation_id"]),
                            )
                            conn.execute(
                                "UPDATE sceneit_billing_changes SET "
                                "provider_schedule_id=%s "
                                "WHERE operation_id=%s AND provider_schedule_id IS NULL",
                                (schedule_id, row["operation_id"]),
                            )
                if not schedule_id:
                    unresolved.append(str(row["operation_id"]))
                    continue
                if not (
                    row["change_id"] and row["subscription_id"]
                    and row["source_price_id"] and row["target_price_id"]
                    and row["effective_at"] and row["customer_id"]
                ):
                    unresolved.append(str(row["operation_id"]))
                    continue
                verified_change = dict(row)
                verified_change["provider_schedule_id"] = schedule_id
                verified_change["operation_id"] = row["schedule_operation_id"]
                try:
                    verified = _retrieve_verified_schedule_change(
                        provider, verified_change, settings,
                    )
                except (BillingProviderError, BillingProblem):
                    if row["kind"] != "schedule":
                        raise
                    def finish_compensation():
                        with connection() as conn:
                            conn.execute(
                                "UPDATE sceneit_billing_operations SET "
                                "state='failed',"
                                "last_error_code='schedule_configuration_failed',"
                                "updated_at=now() WHERE operation_id=%s "
                                "AND state IN ('uncertain','creating')",
                                (row["operation_id"],),
                            )
                            conn.execute(
                                "UPDATE sceneit_billing_changes SET state='failed',"
                                "updated_at=now() WHERE change_id=%s "
                                "AND state IN ('uncertain','confirming')",
                                (row["change_id"],),
                            )

                    def persist_compensation_intent(phase_end):
                        with connection() as conn:
                            updated = conn.execute(
                                "UPDATE sceneit_billing_operations SET "
                                "last_error_code='schedule_compensation_releasing',"
                                "expires_at=%s,"
                                "updated_at=now() WHERE operation_id=%s "
                                "AND state IN ('uncertain','creating') "
                                "RETURNING operation_id",
                                (phase_end, row["operation_id"]),
                            ).fetchone()
                        if not updated:
                            raise BillingProviderError(
                                "schedule_compensation_unverified"
                            )

                    if row["last_error_code"] == "schedule_compensation_releasing":
                        if row["compensation_phase_end"] is None:
                            raise BillingProviderError(
                                "schedule_compensation_unverified"
                            )
                        compensated = provider.verify_unconfigured_schedule(
                            schedule_id,
                            expected_subscription_id=row["subscription_id"],
                            expected_customer_id=row["customer_id"],
                            expected_source_price_id=row["source_price_id"],
                            livemode=settings.environment == "live",
                            expected_effective_at=row[
                                "compensation_phase_end"
                            ],
                            allow_released=True,
                        )
                        if compensated == "released":
                            finish_compensation()
                            resolved += 1
                            continue
                    # The only mutation recovery may issue is compensation for
                    # an exact, untouched one-phase schedule.
                    compensated = provider.release_unconfigured_schedule(
                        schedule_id,
                        expected_subscription_id=row["subscription_id"],
                        expected_customer_id=row["customer_id"],
                        expected_source_price_id=row["source_price_id"],
                        livemode=settings.environment == "live",
                        operation_id=str(row["operation_id"]),
                        on_verified=persist_compensation_intent,
                    )
                    if compensated != "released":
                        raise BillingProviderError(
                            "schedule_compensation_unverified"
                        )
                    finish_compensation()
                    resolved += 1
                    continue
                subscription = verified.subscription
                _save_subscription(
                    subscription, settings,
                    expected_owner_id=row["owner_id"],
                    connection_factory=connection,
                )
                outcome = _classify_verified_schedule_change(
                    verified, verified_change
                )
                if (
                    row["kind"] == "withdraw"
                    and outcome == "scheduled"
                    and row["within_idempotency_window"]
                ):
                    # Reissue only the original mutation with its original
                    # Stripe key while that key is still retained. The shared
                    # verifier above proved the exact active schedule, source
                    # subscription, customer, phase boundary, and deadline.
                    # Once the call starts, the schedule fact is unknown until
                    # the shared post-release proof succeeds.
                    with connection() as conn:
                        conn.execute(
                            "UPDATE sceneit_billing_changes SET "
                            "state='uncertain',updated_at=now() "
                            "WHERE change_id=%s AND state='scheduled'",
                            (row["change_id"],),
                        )
                    provider.withdraw_schedule(
                        schedule_id, str(row["operation_id"])
                    )
                    verified = _retrieve_verified_schedule_change(
                        provider, verified_change, settings,
                    )
                    subscription = verified.subscription
                    _save_subscription(
                        subscription, settings,
                        expected_owner_id=row["owner_id"],
                        connection_factory=connection,
                    )
                    outcome = _classify_verified_schedule_change(
                        verified, verified_change
                    )
                if outcome == "effective":
                    if row["kind"] == "withdraw":
                        state = "failed"
                    else:
                        state = "completed"
                    change_state = "effective"
                elif outcome == "withdrawn":
                    state = "completed"
                    change_state = "withdrawn"
                elif outcome == "scheduled":
                    if row["kind"] == "withdraw":
                        state = "uncertain"
                        change_state = "scheduled"
                    else:
                        state = change_state = "scheduled"
                else:
                    state = change_state = "uncertain"
                if row["kind"] == "withdraw" and outcome == "effective":
                    with connection() as conn:
                        conn.execute(
                            "UPDATE sceneit_billing_operations SET "
                            "last_error_code='change_not_withdrawable' "
                            "WHERE operation_id=%s", (row["operation_id"],),
                        )
            else:
                unresolved.append(str(row["operation_id"]))
                continue
        except (BillingProviderError, BillingProblem):
            unresolved.append(str(row["operation_id"]))
            continue
        if state == "uncertain":
            unresolved.append(str(row["operation_id"]))
            continue
        with connection() as conn:
            conn.execute(
                "UPDATE sceneit_billing_operations SET state=%s,updated_at=now() "
                    "WHERE operation_id=%s "
                    "AND state IN ('uncertain','creating','confirmed','scheduled')",
                (state, row["operation_id"]),
            )
            if row["change_id"]:
                conn.execute(
                    "UPDATE sceneit_billing_changes SET state=%s,updated_at=now() "
                    "WHERE change_id=%s "
                    "AND state IN "
                    "('uncertain','confirming','payment_pending','scheduled')",
                    (change_state, row["change_id"]),
                )
        resolved += 1
    _audit("operation_reconcile", evidence, resolved)
    return {
        "checked": len(rows), "resolved": resolved, "unresolved": unresolved,
    }


def expire_uncertain_portal(operation_id, *, evidence):
    """Terminalize an old non-financial Portal session with operator evidence."""
    try:
        operation_id = str(__import__("uuid").UUID(str(operation_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("operation id must be a UUID") from exc
    with connection() as conn:
        row = conn.execute(
            "SELECT operation_id FROM sceneit_billing_operations "
            "WHERE operation_id=%s AND kind='portal' AND state='uncertain' "
            "AND created_at<now()-interval '24 hours' FOR UPDATE",
            (operation_id,),
        ).fetchone()
        if not row:
            raise ValueError(
                "portal operation is not uncertain or has not aged 24 hours"
            )
        conn.execute(
            "UPDATE sceneit_billing_operations SET state='expired',"
            "hosted_url=NULL,expires_at=NULL,updated_at=now() "
            "WHERE operation_id=%s", (operation_id,),
        )
    _audit("portal_uncertain_expired", evidence, 1)
    return operation_id


def map_legacy_coverage(limit=50, *, evidence, after_id=""):
    """Apply reviewed catalog snapshots to legacy coverage without moving it."""
    limit = max(1, min(int(limit), 100))
    settings = billing_settings()
    if not settings.enabled:
        return {"mapped": 0, "nextId": None, "unmapped": []}
    provider = StripeBillingProvider(settings)
    with connection() as conn:
        rows = conn.execute(
            "SELECT c.id,c.owner_id,c.subscription_id,s.customer_id "
            "FROM sceneit_paid_coverage c "
            "JOIN sceneit_billing_subscriptions s "
            "ON s.subscription_id=c.subscription_id AND s.owner_id=c.owner_id "
            "WHERE c.tier_key IS NULL AND c.id>%s ORDER BY c.id LIMIT %s",
            (after_id, limit),
        ).fetchall()
    mapped, unmapped = 0, []
    for row in rows:
        try:
            invoice = provider.retrieve_invoice(row["id"])
        except BillingProviderError:
            unmapped.append(row["id"])
            continue
        offer = settings.catalog.prices.get(invoice.price_id)
        tier = settings.catalog.tiers.get(offer.tier) if offer else None
        if (
            not offer or not tier
            or invoice.customer_id != row["customer_id"]
            or invoice.subscription_id != row["subscription_id"]
            or invoice.livemode != (settings.environment == "live")
        ):
            unmapped.append(row["id"])
            continue
        with connection() as conn:
            # The same account lock serializes quota admission, invoice
            # reconciliation, reversal tombstones, and this reviewed backfill.
            conn.execute(
                "SELECT owner_id FROM sceneit_billing_accounts "
                "WHERE owner_id=%s FOR UPDATE", (row["owner_id"],),
            )
            changed = conn.execute(
                "UPDATE sceneit_paid_coverage SET tier_key=%s,tier_rank=%s,"
                "capabilities_snapshot=%s,limits_snapshot=%s,cadence=%s,"
                "currency=%s,price_id=%s,tax_behavior=%s,subtotal=%s,tax=%s,"
                "total=%s,amount_paid=%s,provider_created_at=%s,"
                "provider_receipt_at=now(),updated_at=now() "
                "WHERE id=%s AND owner_id=%s AND tier_key IS NULL RETURNING id",
                (offer.tier, tier.rank, Jsonb(list(tier.capabilities)),
                 Jsonb(tier.limits), offer.cadence, offer.currency,
                 offer.price_id, offer.tax_behavior, invoice.subtotal,
                 invoice.tax, invoice.total, invoice.amount_paid,
                 invoice.provider_created_at, row["id"], row["owner_id"]),
            ).fetchone()
        mapped += int(bool(changed))
    _audit("legacy_coverage_mapping", evidence, mapped)
    return {
        "mapped": mapped,
        "nextId": rows[-1]["id"] if rows else None,
        "unmapped": unmapped,
    }


def _audit(action, evidence, affected):
    if not isinstance(evidence, str) or not 8 <= len(evidence) <= 500:
        raise ValueError("evidence must contain 8 to 500 characters")
    with connection() as conn:
        conn.execute(
            "INSERT INTO sceneit_billing_audit(action,evidence,affected) "
            "VALUES (%s,%s,%s)", (action, evidence, affected),
        )


def replay(limit=20, *, evidence):
    limit = max(1, min(int(limit), 50))
    with connection() as conn:
        rows = conn.execute(
            "SELECT payload FROM sceneit_billing_events WHERE state='pending' "
            "ORDER BY updated_at LIMIT %s", (limit,),
        ).fetchall()
    deadline = time.monotonic() + 60
    results = []
    for row in rows:
        if time.monotonic() >= deadline:
            break
        try:
            results.append(process_event(row["payload"], delivered=False))
            _audit("event_replay_succeeded", evidence, 1)
        except BillingProblem:
            results.append("failed")
            _audit("event_replay_failed", evidence, 0)
    _audit("event_replay", evidence, len(results))
    return results


def reconcile(limit=20, *, evidence, owner_after=""):
    """Refresh a bounded set of known customer subscriptions from Stripe."""
    limit = max(1, min(int(limit), 50))
    settings = billing_settings()
    if not settings.enabled:
        return 0
    with connection() as conn:
        rows = conn.execute(
            "SELECT customer_id FROM sceneit_billing_accounts "
            "WHERE environment=%s AND customer_id IS NOT NULL "
            "AND owner_id>%s ORDER BY owner_id LIMIT %s",
            (settings.environment, owner_after, limit),
        ).fetchall()
    provider = StripeBillingProvider(settings)
    count = 0
    for row in rows:
        try:
            subscriptions = provider.list_subscriptions(row["customer_id"])
            for subscription in subscriptions:
                _save_subscription(subscription, settings)
                count += 1
            _audit("subscription_reconcile_succeeded", evidence, len(subscriptions))
        except (BillingProviderError, BillingProblem):
            _audit("subscription_reconcile_failed", evidence, 0)
            continue
    _audit("subscription_reconcile", evidence, count)
    return count


def recover(
    limit=20, *, evidence, invoice_id=None, customer_id=None, cursor=None,
    owner_after="",
):
    """Explicitly match uncertain writes and recover current paid invoices."""
    limit = max(1, min(int(limit), 50))
    if cursor and not customer_id:
        raise ValueError("an invoice cursor requires its customer reference")
    if invoice_id and (customer_id or cursor):
        raise ValueError("choose either one invoice or a customer page")
    if not isinstance(evidence, str) or not 8 <= len(evidence) <= 500:
        raise ValueError("evidence must contain 8 to 500 characters")
    settings = billing_settings()
    if not settings.enabled:
        return {
            "affected": 0, "nextCursor": None, "nextOwner": None,
            "nextCustomer": None, "retryInvoices": [],
        }
    provider = StripeBillingProvider(settings)
    deadline = time.monotonic() + 60
    affected = 0
    with connection() as conn:
        # A delivery abandoned by process loss is safe to replay: reconciliation
        # reads current provider state and invoice coverage/reversals are sticky.
        changed = conn.execute(
            "UPDATE sceneit_billing_events SET state='pending',updated_at=now() "
            "WHERE event_id IN (SELECT event_id FROM sceneit_billing_events "
            "WHERE state='processing' AND updated_at < now()-interval '10 minutes' "
            "ORDER BY updated_at LIMIT %s) RETURNING event_id", (limit,),
        ).fetchall() if not (invoice_id or customer_id) else []
        affected += len(changed)
        if customer_id:
            accounts = conn.execute(
                "SELECT owner_id,customer_id,customer_attempt_state FROM sceneit_billing_accounts "
                "WHERE environment=%s AND customer_id=%s",
                (settings.environment, customer_id),
            ).fetchall()
            if not accounts:
                raise ValueError("customer reference is not a known account")
        elif invoice_id:
            accounts = []
        else:
            accounts = conn.execute(
                "SELECT owner_id,customer_id,customer_attempt_state FROM sceneit_billing_accounts "
                "WHERE environment=%s AND owner_id>%s ORDER BY owner_id LIMIT %s",
                (settings.environment, owner_after, limit),
            ).fetchall()
    owners = [account["owner_id"] for account in accounts]
    blocked_owners = set()
    for account in accounts:
        if time.monotonic() >= deadline:
            blocked_owners.add(account["owner_id"])
            break
        if account["customer_attempt_state"] not in ("creating", "uncertain"):
            continue
        try:
            matched_customer_id = (
                account["customer_id"]
                or provider.find_customer(account["owner_id"])
            )
        except BillingProviderError:
            _audit("recover_customer_failed", evidence, 0)
            blocked_owners.add(account["owner_id"])
            continue
        if matched_customer_id:
            with connection() as conn:
                updated = conn.execute(
                    "UPDATE sceneit_billing_accounts SET customer_id=%s,"
                    "customer_attempt_state='created',updated_at=now() "
                    "WHERE owner_id=%s AND customer_attempt_state IN ('creating','uncertain') "
                    "RETURNING owner_id",
                    (matched_customer_id, account["owner_id"]),
                ).fetchone()
            affected += bool(updated)
            _audit("recover_customer_succeeded", evidence, int(bool(updated)))
            account["customer_id"] = matched_customer_id
        else:
            blocked_owners.add(account["owner_id"])
    with connection() as conn:
        checkouts = conn.execute(
            "SELECT c.owner_id,c.idempotency_key,c.state,a.customer_id "
            "FROM sceneit_billing_checkouts c JOIN sceneit_billing_accounts a "
            "ON a.owner_id=c.owner_id WHERE a.environment=%s "
            "AND c.state IN ('creating','uncertain') AND c.owner_id=ANY(%s) "
            "ORDER BY c.owner_id LIMIT %s",
            (settings.environment, owners, limit),
        ).fetchall() if owners else []
    for row in checkouts:
        if time.monotonic() >= deadline or not row["customer_id"]:
            blocked_owners.add(row["owner_id"])
            continue
        try:
            found = provider.find_checkout(
                row["customer_id"], row["owner_id"],
                checkout_token(row["owner_id"], str(row["idempotency_key"])),
            )
        except BillingProviderError:
            _audit("recover_checkout_failed", evidence, 0)
            blocked_owners.add(row["owner_id"])
            continue
        if found:
            hosted, state, subscription_id = found
            if state == "complete" and not subscription_id:
                _audit("recover_checkout_failed", evidence, 0)
                blocked_owners.add(row["owner_id"])
                continue
            target = (
                "expired" if state == "expired"
                # Persist the verified binding first. The shared completion
                # helper then reconciles current subscription state and chooses
                # completed versus expired without an ordering race.
                else "uncertain" if state == "complete"
                else "created"
            )
            with connection() as conn:
                updated = conn.execute(
                    "UPDATE sceneit_billing_checkouts SET state=%s,"
                    "provider_session_id=%s,subscription_id=%s,hosted_url=%s,"
                    "expires_at=%s,updated_at=now() "
                    "WHERE owner_id=%s AND idempotency_key=%s "
                    "AND state IN ('creating','uncertain') RETURNING owner_id",
                    (target, hosted.id, subscription_id, hosted.url, hosted.expires_at,
                     row["owner_id"], row["idempotency_key"]),
                ).fetchone()
            affected += bool(updated)
            if updated and state == "complete":
                try:
                    complete_checkout(
                        hosted.id, subscription_id, provider, settings
                    )
                except (BillingProviderError, BillingProblem):
                    blocked_owners.add(row["owner_id"])
                    _audit("recover_checkout_failed", evidence, 0)
                    continue
            _audit("recover_checkout_succeeded", evidence, int(bool(updated)))
        else:
            blocked_owners.add(row["owner_id"])
            _audit("recover_checkout_unmatched", evidence, 0)
    next_cursor = None
    next_customer = None
    retry_invoices = []
    cursor_locked = False
    if invoice_id:
        if not invoice_id.startswith("in_"):
            raise ValueError("invoice reference must start with in_")
        try:
            _reconcile_event(
                "invoice.paid",
                {"data": {"object": {"id": invoice_id}}},
                provider, settings,
            )
            affected += 1
            _audit("recover_invoice_succeeded", evidence, 1)
        except (BillingProviderError, BillingProblem):
            _audit("recover_invoice_failed", evidence, 0)
            retry_invoices.append(invoice_id)
    else:
        customers = [
            {"owner_id": account["owner_id"],
             "customer_id": account["customer_id"]}
            for account in accounts if account["customer_id"]
        ]
        remaining = limit
        for account in customers:
            account_owner = account.get("owner_id")
            if remaining <= 0:
                blocked_owners.add(account_owner)
                break
            if time.monotonic() >= deadline:
                if account_owner:
                    blocked_owners.add(account_owner)
                if not cursor_locked:
                    next_customer = account["customer_id"]
                    next_cursor = cursor if customer_id else None
                break
            page_start = cursor if customer_id else None
            try:
                invoice_ids, provider_cursor = provider.list_paid_invoice_ids(
                    account["customer_id"], min(remaining, 20),
                    page_start,
                )
            except BillingProviderError:
                _audit("recover_invoice_listing_failed", evidence, 0)
                if account_owner:
                    blocked_owners.add(account_owner)
                if not cursor_locked:
                    next_cursor = page_start
                    next_customer = account["customer_id"]
                    cursor_locked = True
                continue
            page_failed = False
            for current_invoice in invoice_ids:
                if remaining <= 0 or time.monotonic() >= deadline:
                    retry_invoices.append(current_invoice)
                    page_failed = True
                    if account_owner:
                        blocked_owners.add(account_owner)
                    break
                try:
                    _reconcile_event(
                        "invoice.paid",
                        {"data": {"object": {"id": current_invoice}}},
                        provider, settings,
                    )
                    affected += 1
                    _audit("recover_invoice_succeeded", evidence, 1)
                except (BillingProviderError, BillingProblem):
                    _audit("recover_invoice_failed", evidence, 0)
                    retry_invoices.append(current_invoice)
                    page_failed = True
                    if account_owner:
                        blocked_owners.add(account_owner)
                finally:
                    remaining -= 1
            if page_failed:
                # Retain the page start. Successful earlier invoices are
                # idempotent, so replay cannot skip the failed invoice.
                if not cursor_locked:
                    next_cursor = page_start
                    next_customer = account["customer_id"]
                    cursor_locked = True
            elif provider_cursor:
                if not cursor_locked:
                    next_cursor = provider_cursor
                    next_customer = account["customer_id"]
                    cursor_locked = True
                if account_owner:
                    blocked_owners.add(account_owner)
    # Only advance through the contiguous, fully reconciled prefix, in database
    # ordering (which need not match Python's string ordering). Fetching an
    # account is not evidence that its invoices were visited.
    last_owner = owner_after or None
    for owner in owners:
        if owner in blocked_owners:
            break
        last_owner = owner
    if not owners or invoice_id or customer_id:
        last_owner = None
    customer_owner = owners[0] if customer_id and owners else None
    _audit("operator_recovery", evidence, affected)
    return {
        "affected": affected, "nextCursor": next_cursor,
        "nextOwner": last_owner, "nextCustomer": next_customer,
        "retryInvoices": retry_invoices,
        # A scoped page never moves the global traversal cursor. When resolving
        # the global walk's nextCustomer, this explicitly identifies completion
        # of that same account; arbitrary targeted recovery must not skip others.
        "customerOwner": customer_owner,
        "customerComplete": bool(
            customer_owner and customer_owner not in blocked_owners and not next_customer),
        "unresolvedOwners": [owner for owner in owners if owner in blocked_owners],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bounded billing recovery")
    commands = parser.add_subparsers(dest="command", required=True)
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--limit", type=int, default=50)
    replay_parser = commands.add_parser("replay")
    replay_parser.add_argument("--limit", type=int, default=20)
    replay_parser.add_argument("--evidence", required=True)
    replay_parser.add_argument("--operator-approved", action="store_true")
    reconcile_parser = commands.add_parser("reconcile")
    reconcile_parser.add_argument("--limit", type=int, default=20)
    reconcile_parser.add_argument("--evidence", required=True)
    reconcile_parser.add_argument("--operator-approved", action="store_true")
    reconcile_parser.add_argument("--owner-after", default="")
    recover_parser = commands.add_parser("recover")
    recover_parser.add_argument("--limit", type=int, default=20)
    recover_parser.add_argument("--invoice")
    recover_parser.add_argument("--customer")
    recover_parser.add_argument("--cursor")
    recover_parser.add_argument("--owner-after", default="")
    recover_parser.add_argument("--evidence", required=True)
    recover_parser.add_argument("--operator-approved", action="store_true")
    operations_parser = commands.add_parser("recover-operations")
    operations_parser.add_argument("--limit", type=int, default=20)
    operations_parser.add_argument("--evidence", required=True)
    operations_parser.add_argument("--operator-approved", action="store_true")
    timeline_parser = commands.add_parser("timeline")
    timeline_parser.add_argument("--limit", type=int, default=50)
    timeline_parser.add_argument("--state")
    timeline_parser.add_argument("--event-type")
    timeline_parser.add_argument("--payment-key")
    timeline_parser.add_argument("--before-received-at", type=datetime.fromisoformat)
    timeline_parser.add_argument("--before-event-id")
    legacy_parser = commands.add_parser("map-legacy-coverage")
    legacy_parser.add_argument("--limit", type=int, default=50)
    legacy_parser.add_argument("--after-id", default="")
    legacy_parser.add_argument("--evidence", required=True)
    legacy_parser.add_argument("--operator-approved", action="store_true")
    portal_parser = commands.add_parser("expire-uncertain-portal")
    portal_parser.add_argument("--operation", required=True)
    portal_parser.add_argument("--evidence", required=True)
    portal_parser.add_argument("--operator-approved", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "status":
        for row in pending(args.limit):
            print(row["event_id"], row["event_type"], row["state"], row["attempts"],
                  row["last_error_code"] or "-")
    elif args.command == "replay":
        if not args.operator_approved:
            parser.error("replay requires --operator-approved")
        results = replay(args.limit, evidence=args.evidence)
        print(f"Processed {len(results)} event(s).")
    elif args.command == "reconcile":
        if not args.operator_approved:
            parser.error("reconcile requires --operator-approved")
        count = reconcile(
            args.limit, evidence=args.evidence, owner_after=args.owner_after
        )
        print(
            f"Reconciled {count} subscription(s)."
        )
    elif args.command == "recover":
        if not args.operator_approved:
            parser.error("recover requires --operator-approved")
        result = recover(
            args.limit, evidence=args.evidence, invoice_id=args.invoice,
            customer_id=args.customer, cursor=args.cursor,
            owner_after=args.owner_after,
        )
        print(
            f"Recovered {result['affected']} record(s); "
            f"next cursor: {result['nextCursor'] or '-'}; "
            f"next owner: {result['nextOwner'] or '-'}; "
            f"next customer: {result['nextCustomer'] or '-'}; "
            f"retry invoices: {','.join(result['retryInvoices']) or '-'}; "
            f"customer owner: {result.get('customerOwner') or '-'}; "
            f"customer complete: {bool(result.get('customerComplete'))}; "
            f"unresolved owners: {','.join(result.get('unresolvedOwners', [])) or '-'}"
        )
    elif args.command == "recover-operations":
        if not args.operator_approved:
            parser.error("recover-operations requires --operator-approved")
        result = recover_operations(args.limit, evidence=args.evidence)
        print(
            f"Checked {result['checked']} operation(s); resolved "
            f"{result['resolved']}; unresolved: "
            f"{','.join(result['unresolved']) or '-'}"
        )
    elif args.command == "timeline":
        try:
            rows = timeline(
                args.limit, state=args.state, event_type=args.event_type,
                payment_key=args.payment_key,
                before_received_at=args.before_received_at,
                before_event_id=args.before_event_id,
            )
        except ValueError as exc:
            parser.error(str(exc))
        # Events are receipts, not transactions. Never sum event amounts.
        print(json.dumps({
            "events": rows,
            "pageDistinctPayments": len({row["paymentKey"] for row in rows if row["paymentKey"]}),
            "nextCursor": {
                "receivedAt": rows[-1]["receivedAt"],
                "eventId": rows[-1]["eventId"],
            } if len(rows) == max(1, min(args.limit, 100)) else None,
        }, default=lambda value: value.isoformat()))
    elif args.command == "map-legacy-coverage":
        if not args.operator_approved:
            parser.error("map-legacy-coverage requires --operator-approved")
        result = map_legacy_coverage(
            args.limit, evidence=args.evidence, after_id=args.after_id
        )
        print(
            f"Mapped {result['mapped']} coverage row(s); next id: "
            f"{result['nextId'] or '-'}; unmapped: "
            f"{','.join(result['unmapped']) or '-'}"
        )
    elif args.command == "expire-uncertain-portal":
        if not args.operator_approved:
            parser.error("expire-uncertain-portal requires --operator-approved")
        resolved = expire_uncertain_portal(
            args.operation, evidence=args.evidence
        )
        print(f"Expired uncertain Portal operation {resolved}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())