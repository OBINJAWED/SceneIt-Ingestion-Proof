"""Bounded, explicit billing event recovery diagnostics (not a service)."""
import argparse
import time

from .billing import (
    _reconcile_event, _save_subscription, checkout_token, complete_checkout,
    process_event,
)
from .billing_config import BillingProblem, billing_settings
from .billing_provider import BillingProviderError, StripeBillingProvider
from .db import connection


def pending(limit=50):
    limit = max(1, min(int(limit), 100))
    with connection() as conn:
        return conn.execute(
            "SELECT event_id,event_type,state,attempts,last_error_code,updated_at "
            "FROM sceneit_billing_events WHERE state='pending' "
            "ORDER BY updated_at LIMIT %s", (limit,),
        ).fetchall()


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
            results.append(process_event(row["payload"]))
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())