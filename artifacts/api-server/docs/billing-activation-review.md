# Tiered billing activation review

This is an approval checklist, not approval to sell, register for tax, send
email, run a scheduler, migrate production, or publish. All entries start
**awaiting operator review**. Test fixtures do not authorize real offers.

## Catalog and historical continuity

- Review each tier's customer-facing name, capabilities, rank, and finite limits
  for imports, upload attempts, analysis seconds, new searches, frames, media
  bytes, and retained storage bytes. Review application-wide caps separately.
- Review each tier/cadence/currency combination against its Stripe Product and
  Price in the intended environment. Amounts are integer minor units, not
  floating-point display values. Do not infer one currency's price from another.
- Review both monthly and annual Prices. Annual billing still has monthly
  application allowances; scheduled changes take effect at the subscription's
  renewal, not an allowance reset.
- Retain disabled and historical Price associations. Disabling a new sale must
  not prevent invoice reconciliation, refunds, or existing customer management.
- Explicitly map existing single-membership Prices and coverage to a reviewed
  tier. Preserve paid intervals, reversal tombstones, customer/subscription
  bindings, original allowance anchors, and consumed usage. Unmapped historical
  records require review, not guessed entitlement or fresh allowance.
- Preview and review any same-cadence tier upgrade before confirmation. Higher
  access requires verified successful incremental funding. Failed or abandoned
  upgrades keep the independently paid base tier.
- Do not convert a Firebase trial into a commercial account or change pilot
  admission. The configured lifetime trial allowance stays cumulative, requires
  verified email, needs no card, and has no automatic paid conversion.

## Tax and currencies

- Obtain qualified operator review of actual selling jurisdictions and required
  registrations. No jurisdiction, nexus, rate, registration, or tax obligation
  is established by this implementation.
- Review Stripe automatic tax, Product tax classifications, and every Price's
  explicit inclusive/exclusive treatment.
- Review required hosted billing-location collection and whether to allow
  optional tax-ID collection. Keep application identity separate from billing
  identity and billing email.
- Verify a taxed invoice and a legitimate zero-tax invoice in the approved
  sandbox. A failed or incomplete tax calculation is not a zero-tax success.
- Verify full-period, renewal, and prorated debit/credit invoices. Unsupported
  discounts, additional subscription items, FX/currency changes, arbitrary
  quantities, and zero/negative-net upgrade funding fail closed.
- Confirm Checkout displays the tax and final total before the customer commits.
  SceneIt's pre-tax offer display is not a promise of the final charged amount.
- Existing subscriptions keep their original currency through tier and cadence
  changes. Currency conversion and adaptive/FX pricing are not supported.

## Hosted management, receipts, and recovery

- Review the exact Customer Portal configuration: payment-method updates,
  billing email/location updates, invoice/receipt history, and period-end
  cancellation only. Subscription tier/price updates must remain disabled there.
- Review direct hosted cancellation: no retention maze, no automatic immediate
  cancellation/refund. Abandonment leaves provider state unchanged. SceneIt
  reports a request as pending until authoritative reconciliation.
- Verify authenticated expired/ineligible customers can still reach management.
  A card update or hosted-page return alone must never imply a successful charge.
- In Stripe's customer-email settings, review successful-payment receipts,
  paid-invoice emails, and refund receipts. Confirm the intended billing email
  is collected or confirmed for the correctly bound Stripe customer.
- Use the separately authorized sandbox check and designated inbox to prove
  actual receipt delivery. Record **awaiting authorized inbox check** until
  evidence exists. SDK request wiring and SMTP acceptance are not inbox delivery.

## Reminder and alert ownership

- Choose one customer reminder-campaign owner: Stripe or SceneIt. If SceneIt
  reminders are approved, disable overlapping Stripe reminder campaigns.
  Stripe remains the sole owner of payment retries in either case.
- Approve the sender, operator recipient, trusted authenticated billing-page
  URL, finite reminder offsets, retry limit/backoff, TLS SMTP endpoint, and
  bounded scheduler cadence. Store credentials only in the secrets system.
- Review transient, permanent, and ambiguous SMTP outcomes. Ambiguous acceptance
  needs investigation, not automatic resend. Resolve failures before re-enabling
  a stopped or unhealthy notification runner.
- Verify reminder suppression after payment, refund/reversal, voiding, and
  obsolescence using fresh provider facts. A late failure event cannot restart a
  resolved reminder sequence.
- Verify deduplicated incident alerts, cooldowns, resolution notices, and health
  reporting. Alerts must contain safe references, ages, attempts, and a recovery
  action, never raw payloads, hosted URLs, billing PII, or payment credentials.
- Configure and independently test Stripe delivery-failure notifications or an
  external endpoint/database monitor. This is an activation prerequisite:
  an application outbox cannot notify while its runtime/database is unavailable.
  An HTTP 200 with durable pending processing still needs internal monitoring.

## Evidence and controlled rollout

Keep a redacted review record containing approval identity/reference, date,
environment, configuration fingerprint, and outcome for every gate above. Never
include secrets, signatures, card details, billing addresses/emails, cookies,
raw webhook payloads, or hosted URLs.

Evidence must distinguish:

| Layer | What it proves | What it does not prove |
| --- | --- | --- |
| Provider-free SDK/HTTP fixtures | Request/response and failure handling | Stripe-hosted behavior or charges |
| Disposable PostgreSQL/concurrency checks | Persistence, race handling, migration preservation | Production schema or live payment state |
| Intercepted desktop/mobile journeys | SceneIt client contracts and visible states | Real-device Safari or Stripe authentication |
| Approved Test Clock and replay scenarios | Recorded sandbox scenarios only | Live-mode certification |
| Designated inbox check | Delivery of the specifically inspected email | Delivery to every future recipient |

Use the existing hosted-payments verification work for sandbox execution and
its approval record; do not start an independent certification campaign.
Production migration, deployment/runtime changes, public access, sending
notifications, and commercial activation each require their existing explicit
approval. Until then, keep all activation controls off.