# Billing and quota operations

This runbook covers SceneIt's disabled-by-default Stripe-hosted membership mode
and its application-enforced usage ceilings. It does not authorize a production
migration, live Stripe configuration, a worker/runtime change, deployment, or
customer charges. Each of those needs a separate recorded approval.

## Safety model

- SceneIt accepts no card number or CVC. Checkout and account management happen
  only on Stripe-hosted pages created by the authenticated server.
- A success redirect is not an entitlement. Only a verified webhook or bounded
  provider reconciliation can record paid coverage.
- Billing never grants pilot admission or access to another owner's data. New
  checkout requires pilot admission; an authenticated existing customer may
  still open the Customer Portal after admission or membership is lost.
- Billing status, Checkout, and Portal responses are private and must use
  `Cache-Control: private, no-store`. Browser mutations retain the existing
  CSRF, same-origin, request-size, throttling, and session controls.
- Usage reservations happen before expensive work. A timeout, lease expiry, or
  unknown provider outcome is not proof that work stopped and is not a reason
  to release a reservation.
- These ceilings bound work authorized by SceneIt. They do not guarantee a
  fixed infrastructure/provider bill or eliminate baseline compute, retained
  storage, egress, abuse, dispute, refund, tax, or chargeback risk.

## Configuration

Billing is unchanged pilot mode when `SCENEIT_BILLING_ENABLED=false` (the
default). Keep credentials in the platform secret store; never put them in a
checked-in environment file, command line, log, ticket, or evidence bundle.

Non-secret billing controls:

- `SCENEIT_BILLING_ENABLED`: exactly `true` or `false`.
- `SCENEIT_BILLING_ENVIRONMENT`: `test` or `live`.
- `SCENEIT_BILLING_LIVE_APPROVED`: must be exactly `true` before live mode can
  start. This guard is not itself approval; retain the separate approval record.
- `SCENEIT_BILLING_CATALOG`: explicitly reviewed JSON containing `reviewed`,
  `tiers`, and `offers`. Each tier supplies `key`, `name`, `rank`,
  `capabilities`, and all finite `limits`. Each offer supplies `tier`,
  `cadence`, `currency`, `priceId`, `unitAmount`, `taxBehavior`, `taxCode`,
  and `saleEnabled`. Browser requests choose only an approved
  tier/cadence/currency tuple, never a Price, tax rate, or amount.
- Historical Prices remain mapped even when disabled for new sales. The old
  single-membership monthly/yearly environment variables do not define new
  offers. Explicitly review their historical tier association; never infer
  packaging, prices, or allowances from fixture tiers.
- `SCENEIT_BILLING_RETURN_URL`: a fixed trusted HTTPS URL with no query or
  fragment. Do not derive it from request headers or a browser-supplied URL.
- `SCENEIT_STRIPE_PORTAL_CONFIGURATION`: the reviewed Stripe Customer Portal
  configuration for the selected environment.
- Required finite, positive integer legacy member-policy limits:
  `SCENEIT_MEMBER_IMPORTS`, `SCENEIT_MEMBER_UPLOAD_ATTEMPTS`,
  `SCENEIT_MEMBER_ANALYSIS_SECONDS`, `SCENEIT_MEMBER_SEARCHES`,
  `SCENEIT_MEMBER_STORAGE_BYTES`, `SCENEIT_MEMBER_MEDIA_BYTES`, and
  `SCENEIT_MEMBER_FRAMES`.
- Reviewed tier limits come from verified coverage snapshots, not the legacy
  member-policy ceiling. The legacy controls support explicit historical mapping;
  they must not clip a separately reviewed higher tier. Application limits
  remain independently enforced and use the same
  suffixes with `SCENEIT_APP_` instead of `SCENEIT_MEMBER_`.

Secrets:

- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`

All enabled billing configuration is validated together at process start.
Missing, malformed, non-finite, wrong-environment, or unapproved live
configuration fails closed on restart. Liveness does not probe Stripe. Startup,
build, and readiness never run DDL.

## Usage policy

Both monthly and yearly plans receive one monthly allowance. An owner's window
is anchored to the first confirmed paid-coverage start in UTC. The original day
of month is retained: a day that does not exist is clamped to month end, and the
original day returns in a later month that contains it. A yearly subscription
uses the same monthly windows; it does not expose a year's allowance at once.

Owner limits do not roll over and have no automatic overage. Changing tier,
customer, or subscription identity, cancelling and resubscribing, replaying an
event, or deleting work must not reset the anchor, history, or consumed usage.
Application operation budgets reset on UTC calendar-day boundaries. Storage is
concurrent retained occupancy, not monthly consumption, and receives no
periodic release. Pending uploads and retained objects continue to occupy it
until revocation or deletion is confirmed.

`GET /api/billing/status` is the owner-authoritative view of the current window,
remaining metrics, storage occupancy, and operator stop. In disabled pilot mode,
existing import/search limits remain lifetime counters. In enabled commercial
mode, effective import responses and `/api/imports/config` identify their quota
mode as monthly rather than presenting those values as pilot lifetime limits.
The shared proof keeps its independent cumulative 50-search limit and does not
spend a member's allowance.

Commercial failures use `{error, code, state}` plus optional retry fields. The
current domain mappings are:

| HTTP | Codes |
| --- | --- |
| 400 | `invalid_request`, `invalid_plan`, `invalid_idempotency_key`, `invalid_webhook` |
| 401 | `http_401` |
| 402 | `membership_required` |
| 403 | `pilot_not_admitted`, `origin_rejected`, `http_403` |
| 404 | `billing_customer_missing` |
| 409 | `idempotency_conflict`, `checkout_outcome_unknown`, `subscription_exists`, `billing_environment_mismatch`, `billing_customer_outcome_unknown`, `billing_relationship_invalid`, `reservation_conflict`, `reservation_released`, `webhook_rejected`, `invoice_not_paid` |
| 413 | `webhook_too_large` |
| 429 | `participant_throttled`, `owner_quota_exhausted`, `storage_quota_exhausted` |
| 503 | `billing_disabled`, `billing_unavailable`, `billing_provider_unavailable`, `provider_outcome_unknown`, `provider_rejected`, `service_work_stopped`, `service_capacity_exhausted` |

Shared HTTP infrastructure may additionally emit `database_unavailable`,
`database_capacity_exhausted`, or `upstream_unavailable` (503), and
`internal_error` (500). Do not parse the human-readable `error` as policy.

## Payment lifecycle

- Membership becomes active only for verified paid coverage. An open,
  incomplete, past-due, unpaid, or failed payment does not create or extend
  coverage.
- Cancellation at period end preserves already-paid access through
  `paidThrough`; it blocks new work after that instant unless later paid
  coverage is verified.
- A confirmed payment reversal removes only the invoice-scoped coverage it
  funded. Reversal decisions are sticky and cannot be undone by an older,
  duplicate, or reordered success event for that invoice.
- Same-cadence upgrades require an expiring server preview and confirmation.
  Successful verified prorated payment funds only the incremental tier access.
  A failed/abandoned upgrade leaves independently funded base coverage intact.
  Refunding even part of a successfully funded upgrade reverses its increment,
  not the base invoice; pending/failed refunds do not revoke coverage.
- Successive incremental upgrades retain the complete funding dependency chain.
  Reversing an earlier increment also removes higher access that depends on that
  increment. A later independently funded full-period renewal is not dependent
  on those historical increments. None of these reversals replenishes usage.
- An abandoned unpaid upgrade is not terminal merely because its hosted page
  was closed. Verified invoice voiding or pending-update expiry can retire the
  bound change and release its blocker without altering paid base coverage.
  Unknown outcomes remain blocked for reconciliation.
- Downgrades and cadence changes are scheduled for subscription renewal and can
  be withdrawn before taking effect. They do not create a second subscription.
  No change resets usage or allowance anchors. A paid upgrade can raise the
  current ceiling with usage retained; a downgrade over quota denies new work
  without deleting retained media.
- Failed renewals grant neither grace coverage nor a fresh allowance. Already
  paid access lasts to its verified deadline. Expired-card and authentication
  failures offer secure hosted recovery; returning or updating a card is not
  proof of payment.
- Webhook event IDs are durably deduplicated. Processing validates Stripe
  signature/timestamp, test/live environment, customer, subscription, and
  allowlisted price relationships, then reconciles current provider state
  rather than trusting event arrival order.
- The webhook uses the bounded unmodified request bytes for signature
  verification and acknowledges only completed processing or an explicitly
  durable recovery record. It is not a CSRF-exempt general JSON route.

## Test-to-live activation

1. Obtain approval for fixture allowances and Stripe test-mode Prices. Apply the
   additive migration explicitly to a disposable database; run migration status,
   upgrade, status, and a second idempotent upgrade. Do not activate billing yet.
2. Configure test-mode secrets, webhook destination, hosted Checkout/Portal
   settings, fixed return URL, and finite owner/application limits. Restart and
   verify failed-start behavior for malformed and cross-environment fixtures.
3. Use the approved provider-free Stripe-shaped fixtures to verify signature
   rejection, idempotent Checkout, duplicate/reordered events, paid-through
   expiry, cancellation, reversal, yearly monthly windows, quota races, stopped
   work, and bounded recovery. Label this provider-free fixture evidence. It is
   neither Stripe sandbox/network certification nor live certification.
4. Review approved prices and allowances against provider, storage, egress,
   baseline hosting, dispute, refund, and chargeback exposure. Resolve retention
   and webhook backlogs before considering activation.
5. Separately approve production migration, provider-side live objects/webhook,
   deployment, runtime/worker arrangement, live credentials, and charging.
   Record the approver and release/config fingerprints without secret values.
6. Only after those approvals, set the live values and
   `SCENEIT_BILLING_LIVE_APPROVED=true`, restart, and verify readiness and
   redacted diagnostics without creating a real charge as a health check.

No real credential is supplied by this repository. Runtime Price IDs require
explicit configuration; fixture identifiers are not provider resources.

For the approved test resources and subsequent operator-requested pause, see
[the sandbox preparation record](billing-sandbox-verification.md). That record
is not hosted-payment certification; its test Price IDs do not enable billing.
The operator has chosen to leave billing as a disabled placeholder so unrelated
development can continue. Do not request Stripe setup again until the operator
resumes payment work, and do not bypass verification before commercial activation.

Use [the activation review](billing-activation-review.md) for the required tier,
currency, tax, billing-email, receipt, reminder-owner, and independent outage
monitor approvals. All selling terms remain unapproved until explicitly
reviewed. The two test fixture tiers are not production defaults.

The [Test Clock handoff](billing-test-clock.md) documents opt-in isolated
verification and redacted evidence. The [notification runbook](billing-notification-operations.md)
covers one-shot bounded dunning/incident operations. Neither document authorizes
sandbox execution, SMTP delivery, or scheduler activation.

## Operator stop and reservation release

Run operator commands from `artifacts/api-server` with the locked environment.
First inspect the shipped interface with
`uv run --locked python -m sceneit.quota_ops --help`. The intended commands are:

```sh
uv run --locked python -m sceneit.quota_ops status
uv run --locked python -m sceneit.quota_ops stop \
  --operator-approved --evidence "$REDACTED_REASON_AND_EVIDENCE"
uv run --locked python -m sceneit.quota_ops resume \
  --operator-approved --evidence "$REDACTED_REASON_AND_EVIDENCE"
uv run --locked python -m sceneit.quota_ops release-unused \
  --operation "$OPERATION_ID" --operator-approved \
  --evidence "$REDACTED_REASON_AND_PROOF_REFERENCE"
```

Stop prevents new expensive HTTP and queued worker work across processes.
Billing status/management, status reads, cancellation, deletion, retention, and
safe cleanup remain available. Before resume, confirm the incident is bounded,
active/review-required work is understood, and application ceilings are
available. `status` reports application windows, reserved and effective storage
bytes (including retained pilot/import references created before or after the
commercial migration), and redacted `uploadCleanup` counts for initiating,
uncertain, or revoke-requested upload attempts. It never prints object keys or
private media metadata. `--evidence` is the audited, redacted reason and
evidence reference; do not place private content in it. `release-unused` is exceptional: its
evidence must point to durable proof that no external work occurred. (Confirmed
storage deletion is accounted through the storage cleanup path, not this
operation command.) A timeout, stale lease, process exit, cancellation request,
or provider uncertainty is insufficient evidence. Commands are audited and
idempotent; never edit usage rows directly.

## Webhook recovery and reconciliation

Inspect the bounded operator interface before use:

```sh
uv run --locked python -m sceneit.billing_ops --help
uv run --locked python -m sceneit.billing_ops status
uv run --locked python -m sceneit.billing_ops recover \
  --limit 20 --operator-approved \
  --evidence "$REDACTED_REASON_AND_EVIDENCE"
# To constrain recovery to one known paid invoice:
uv run --locked python -m sceneit.billing_ops recover \
  --invoice "$INVOICE_ID" --limit 1 --operator-approved \
  --evidence "$REDACTED_REASON_AND_EVIDENCE"
# Continue one known customer's bounded provider page:
uv run --locked python -m sceneit.billing_ops recover \
  --customer "$CUSTOMER_ID" --cursor "$NEXT_CURSOR" --limit 20 \
  --operator-approved --evidence "$REDACTED_REASON_AND_EVIDENCE"
uv run --locked python -m sceneit.billing_ops replay \
  --limit 20 --operator-approved \
  --evidence "$REDACTED_REASON_AND_EVIDENCE"
uv run --locked python -m sceneit.billing_ops reconcile \
  --limit 20 --operator-approved \
  --evidence "$REDACTED_REASON_AND_EVIDENCE"
```

Every mutating recovery command requires `--operator-approved` and an audited
8–500 character redacted `--evidence` reason/proof reference. `replay` handles
only a bounded set of durably retained verified pending events. `reconcile`
refreshes a bounded set of known subscriptions. `recover` additionally matches
uncertain writes and current paid invoices under a hard limit and deadline; it
reports a continuation cursor instead of silently scanning an unbounded account
population. Prefer `--invoice` for one known paid invoice, or pair `--customer`
with `--cursor` to continue only that known customer's provider page. Do not
replay an arbitrary payload, infer entitlement from Checkout redirect
parameters, or log raw event/customer data. Repeated recovery must be
idempotent and must not mint a second allowance or reverse a sticky invoice
reversal. Keep unresolved records pending review rather than acknowledging or
retrying blindly.

Account traversal returns `nextOwner`; continue with `--owner-after` only after
any reported `nextCustomer`, `nextCursor`, and `retryInvoices` have been handled.
Failures hold the affected owner/page position rather than skipping an invoice.
`unresolvedOwners` includes accounts that could not be visited before the work
budget ended. Never treat the last fetched account as the last reconciled one.
Customer-specific pages process only that customer's account and never advance
the global `nextOwner`. If you are resolving the global walk's `nextCustomer`,
follow its customer cursor until `customerComplete=true`, then resume that same
walk with `--owner-after` set to the returned `customerOwner`. Do not use the
result of an unrelated targeted customer recovery to advance a global walk.
Successful final customer pages clear `nextCustomer` and `nextCursor`; failed
pages preserve their original start and report the invoices to retry.
Repeated work on earlier successful items is harmless and does not reissue
customer/Checkout creation. A completed Checkout stays blocked only while its
bound subscription is current; confirmed canceled/incomplete-expired state
releases that blocker regardless of completion/cancellation event order.
Checkout conflicts use HTTP 409 codes `checkout_completed`, `checkout_exists`,
`checkout_plan_conflict`, and `checkout_state_invalid`; clients must not retry
with arbitrary new identifiers to bypass them.

## Secret rotation, rollback, and incidents


### Tiered history and recovery commands

`timeline` is a read-only, JSON, keyset-paginated receipt view. It includes
provider/receipt/processing timestamps, delivery and processing counts, safe
references, currency/minor-unit amounts, and outcomes. Follow the returned
`nextCursor` using both `--before-received-at` and `--before-event-id`.
`--state`, `--event-type`, and `--payment-key` narrow the view. Amounts belong to
event receipts and **must not be summed**; repeated events share a payment key.
`pageDistinctPayments` is only the distinct count on that page.

```sh
uv run --locked python -m sceneit.billing_ops timeline --limit 25
uv run --locked python -m sceneit.billing_ops timeline \
  --payment-key "$PAYMENT_REFERENCE" --limit 25
uv run --locked python -m sceneit.billing_ops recover-operations \
  --limit 20 --operator-approved --evidence "$REVIEWED_EVIDENCE"
uv run --locked python -m sceneit.billing_ops map-legacy-coverage \
  --limit 25 --after-id "$LAST_INVOICE_ID" \
  --operator-approved --evidence "$REVIEWED_EVIDENCE"
```

Review legacy mappings against each original provider invoice, not the current
subscription Price. Mapping may populate missing immutable snapshots; it must
not alter original coverage periods, reversals, usage, or allowance anchors.
An unknown schedule remains unknown until its exact future Price, quantity, and
mutation identity are verified through every reconciliation entry point.

`expire-uncertain-portal --operation ... --operator-approved --evidence ...`
is the explicit terminal recovery for a Portal operation uncertain for at least
24 hours. It clears hosted data and records the operator's evidence; it does not
retry a purchase or claim a cancellation/payment succeeded. Never manually
clear uncertain financial changes to bypass their durable blockers.

After any successful step of a multi-request provider mutation, later rejection
or response-validation failure remains recovery-required. A created schedule ID
is retained even when its configuration fails. Recovery first checks the exact
intended schedule; an identity-validated, unconfigured one-phase schedule at the
persisted source Price may instead be released using its dedicated compensation
key. Only a fresh confirmation that this exact schedule is released makes the
failed setup terminal and permits a new change. This never cancels the subscription;
ambiguous release or altered schedule facts retain the blocker. The same
post-mutation uncertainty rule applies to upgrade and withdrawal responses.

Customer withdrawal additionally verifies the effective deadline and fresh
provider phase before release, then verifies the released subscription remains
on the unchanged source Price and period. A release that races an applied target
phase is reconciled as effective, not withdrawn; phase evidence alone never grants
paid coverage. The same checks apply to uncertain-withdrawal recovery.
Scheduled-operation recovery and schedule webhooks use this same phase proof;
a terminal provider schedule status alone does not mean the change failed.

Scheduled previews expire no later than the quoted renewal. Confirmation must
still match the freshly retrieved source period, and schedule creation must
retain that exact phase boundary. A crossed renewal requires a fresh preview and
customer confirmation; it never silently shifts the change to the following term.

Compensating release is also recoverable after a lost response or verification
read. Recovery must find the persisted release intent and separately verified
source-phase proof before accepting an already-released, unconfigured schedule.
It does not rewrite the customer's quote or issue another release. Without that
proof, the uncertain-operation blocker remains for operator review.

An active pre-renewal schedule is not a completed withdrawal. Recovery keeps that
withdrawal uncertain and may retry only the original provider operation key within
its retention window, after fresh phase verification. It checks the resulting
subscription again before resolving the request. Older uncertain withdrawals
remain for review rather than receiving a fresh mutation key.

The browser retains an operation key for retries and uncertain outcomes. A
verified expired, completed, or failed operation may be retired before the
customer explicitly starts a fresh operation. It must not reuse an expired
Portal/Checkout session or consumed preview indefinitely, and it must not rotate
keys solely because of a timeout, reload, or missing status row.
An exact matching scheduled operation can clear the local uncertainty marker
without retiring its key. Its pending change still prevents another purchase
while allowing a valid pre-renewal withdrawal.

For secret rotation, create the replacement in the same Stripe environment,
update the secret store, restart, verify readiness, then revoke the old key.
Webhook-secret rotation requires a controlled overlap or paused delivery plus
bounded replay of durably retained verified events; record timestamps and event
IDs, never payloads or secret values.

To stop commercial admission, use the operator stop first and let active bounded
work settle. Disabling billing and restarting returns import/search behavior to
the unchanged pilot lifetime mode; it does not cancel Stripe subscriptions,
refund charges, erase customers/events/anchors/history, release storage, or
authorize a down-migration. Coordinate provider-side subscription handling
before rollback. Prefer additive forward repair after migration.

Confirmed cleanup continues to update the storage ledger while billing is
disabled. If a pilot object had no reservation, confirmed deletion writes an
audited absence record so stale import references cannot keep charging its
occupancy after activation. These records survive import-record removal and
worker restarts. Toggling billing alone never releases storage; ambiguous
sessions, unconfirmed deletion, and shared live references remain reserved.

Alert on pending/failed webhook recovery, approaching owner/application limits,
the global stop, stale workers, uncertain reservations, and retention/deletion
failures. Evidence may include non-secret IDs, states, timestamps, counts,
reason, approver, and release/config fingerprints. Exclude keys, signatures,
cookies, CSRF tokens, hosted/signed URLs, source URLs, filenames, search text,
raw provider payloads, and personal billing details.
