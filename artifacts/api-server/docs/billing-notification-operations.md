# Billing notification operations

Billing notifications are an optional outbox, not a payment retry system.
Stripe remains the only owner of card-charge retries. Receipt emails remain
Stripe-managed. No notification runner is started by web or worker startup.

## Activation gates

The feature is off unless `SCENEIT_BILLING_NOTIFICATIONS_ENABLED=true`. Sending
also requires `SCENEIT_BILLING_NOTIFICATION_SCHEDULER_ENABLED=true` and
`SCENEIT_BILLING_NOTIFICATION_ACTIVATION_APPROVED=true`. Dunning and incident
alerts have separate `SCENEIT_BILLING_DUNNING_ENABLED` and
`SCENEIT_BILLING_WEBHOOK_ALERTS_ENABLED` switches.

Before activation, approve the exact sender, operator mailbox, application as
campaign owner, finite schedule, fixed billing action URL, and Message-ID
domain. Record those approvals by setting the corresponding `*_APPROVED`
variables. Configure a unique increasing schedule of at most six offsets within
30 days, one to ten SMTP attempts, bounded SMTP/lease timeouts, incident aging
thresholds, and cooldown. Configuration rejects incomplete approvals.

SMTP must use certificate-verified implicit TLS or STARTTLS. There is no
plaintext or certificate-validation fallback. SMTP acceptance is recorded as
`accepted`; it is not evidence of inbox delivery.

## Required integration

After persisting and validating a current Stripe payment-failure fact, core
billing may call `open_dunning(invoice_id, problem_id, resolver)`. The injected
resolver must implement `resolve_invoice_notification(invoice_id)` and perform
a fresh Stripe retrieval returning `InvoiceNotificationFact`. It must source
`recipient` from that Stripe customer's current billing recipient, never the
application authentication email. The same resolver is called immediately
before each send. Paid, reversed, voided, obsolete, non-positive, or otherwise
resolved invoices are suppressed. The invoice uniqueness tombstone prevents a
late event from restarting a completed campaign. A bounded provider outage must
be surfaced as `NotificationFactUnavailable`, which retries the notification
read without sending mail or retrying a charge.

The supplied Stripe resolver uses the current bounded `invoice_payments`
relationship to find the default open PaymentIntent; it does not depend on the
obsolete Invoice `payment_intent` expansion. It validates current customer and
subscription ownership, reviewed Price/currency, modern payment relationship,
expired-card/authentication state, and suppression state. Already paid/void
invoices are suppressed before attempting to locate a failed invoice payment.

An explicitly operated scheduler may call, in order,
`enqueue_due_dunning(resolver)`, `scan_webhook_incidents()`, and
`dispatch(resolver)`. Calls and batches are bounded. PostgreSQL claims use
`FOR UPDATE SKIP LOCKED`; stable Message-IDs and unique source/sequence keys
deduplicate concurrent work. Transient failures use capped exponential backoff
and a finite attempt limit. Permanent failures stop. A transport loss after
SMTP DATA and every expired lease becomes `ambiguous`/`needs_review` and is
never automatically resent.

Each delivery claim reads a fresh clock and receives its own lease; a slow
earlier item cannot consume a later item's lease. SMTP has one aggregate
connect/TLS/auth/envelope/DATA deadline capped by that lease, in addition to
socket timeouts. No unbounded QUIT round trip follows DATA. Deadline loss after
DATA remains ambiguous.

The same one-pass operations are available without installing a scheduler:

```text
python3 -m sceneit.billing_notification_ops health
python3 -m sceneit.billing_notification_ops enqueue --limit 25 --operator-approved
python3 -m sceneit.billing_notification_ops scan --limit 25 --operator-approved
python3 -m sceneit.billing_notification_ops dispatch --limit 25 --operator-approved
python3 -m sceneit.billing_notification_ops run-once --limit 25 --operator-approved
```

Mutating commands require the configuration activation gates and the command
approval flag. `enqueue`, `dispatch`, and `run-once` construct the locked Stripe
adapter and `StripeInvoiceNotificationResolver` themselves; operators cannot
inject an arbitrary fact function. Commands are finite one-shot passes and do
not install, enable, or imply a scheduler.

Webhook incident scanning covers verified persisted rejected/failed receipts,
aging pending work (including work acknowledged with HTTP 200), and stalled
processing. Alerts include only safe references, attempts, age, reason, and an
operator recovery action. Active alerts use a cooldown; disappearance produces
one resolution notice. Notification runner health is query-only and never
alerts about itself, preventing recursion.

## Outage boundary and verification

Invalid or pre-persistence webhook input must not be inserted as a verified
receipt. `redacted_webhook_signal` contains no payload, signature, customer,
email, hosted URL, or card data and is intended for structured external
telemetry alongside a non-success webhook response.

The outbox cannot report failure while its endpoint, runtime, or PostgreSQL is
unreachable. Activation therefore requires an independent Stripe
delivery-failure notification and an external endpoint/database monitor with a
separately tested operator path.

Before enabling, apply migration `012_billing_notifications.sql` explicitly,
exercise provider-free fake SMTP/fake-time tests, rehearse concurrent claims in
a disposable PostgreSQL database, and perform a separately authorized test-mode
inbox check. No local fixture proves inbox delivery, Stripe recipient accuracy,
or production scheduler operation.