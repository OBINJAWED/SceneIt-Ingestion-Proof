# Billing placeholder — verification deferred, not certified

## Operator decision

On 2026-09-09, the project operator explicitly approved the test-only setup
below, then asked to defer further setup and payment verification after reviewing
Stripe's webhook quickstart. No further credential requests or payment tests
should be made until the operator chooses to resume.

The operator subsequently clarified that billing should remain a placeholder
and must not hold up unrelated development. The current deliverable is therefore
the disabled billing preparation and handoff, not hosted-payment certification.
Keep the existing disabled-mode behavior: no fake credentials, simulated payment
success, or paid-access grants. Missing Stripe setup must not become a startup
requirement while billing is disabled. Verification remains a prerequisite for
commercial activation, not for continued non-billing development.

This is a preparation record, not a passing certification. It does not authorize
live billing, real customer charges, publishing, production schema changes,
worker/runtime changes for production, or opening pilot admission.

## Approved test configuration

- Product: **SceneIt Sandbox Membership**.
- Prices: USD $10 monthly and USD $100 yearly; test amounts, not selling prices.
  No trials, coupons, or automatic tax.
- Portal: payment-method updates, invoice history, and period-end cancellation
  allowed. Customer details, plan/quantity changes, and subscription pause
  disabled; no cancellation proration.
- Fixed return destination: the existing development preview root.
- Webhook destination: that development preview's `/api/billing/webhook`.
- Isolation: disposable PostgreSQL and a temporary test runtime, synthetic users,
  and Stripe test payment methods only. No real media processing or storage
  provider work.

| Metric | Member ceiling | Application ceiling |
| --- | ---: | ---: |
| Imports | 3 | 10 |
| Upload attempts | 5 | 20 |
| Analysis seconds | 120 | 600 |
| Searches | 10 | 50 |
| Storage bytes | 67,108,864 | 268,435,456 |
| Media bytes | 67,108,864 | 268,435,456 |
| Frames | 12 | 60 |

Operation windows are monthly per member and daily for the application;
storage is concurrent retained occupancy.

## Provider resources created

Creation responses reported `livemode=false` for every resource below:

| Resource | ID | Last confirmed state |
| --- | --- | --- |
| Product | `prod_VEFf5cMnFLeESL` | Active |
| Monthly Price | `price_1UDnA3RGsAhnsPICuBoKIchl` | Active; USD 1,000 cents/month |
| Yearly Price | `price_1UDnA4RGsAhnsPICmmxgvdFP` | Active; USD 10,000 cents/year |
| Portal configuration | `bpc_1UDnA4RGsAhnsPICNQh1mIQu` | Active; restrictions above |
| Webhook endpoint | `we_1UDnA5RGsAhnsPIC80wEeX6v` | **Disabled on operator deferral** |

The webhook event selection was `invoice.paid`, `invoice.payment_succeeded`,
`invoice.payment_failed`, `charge.refunded`, `charge.dispute.created`,
`customer.subscription.created`, `customer.subscription.updated`,
`customer.subscription.deleted`, `checkout.session.completed`, and
`checkout.session.expired`. Selection alone is not delivery or processing
evidence; in particular, the application does not grant coverage from a failed
invoice event.

The Product, Prices, and Portal configuration are retained for a possible later
resumption. Read current provider state and confirm test mode before reusing
them; do not create duplicates or assume this record proves current state.

## What actually ran

- Approved resources were created through the connected Stripe API.
- Temporary Unix-socket-only PostgreSQL clusters were started; migration SQL
  and one synthetic owner were inserted only there. The clusters were stopped
  when their shell commands exited.
- Both attempts to invoke the billing core stopped at configuration validation,
  before creating a Customer, Checkout Session, or Portal Session. The stored
  webhook credential did not satisfy the signing-secret format.
- Secret presence did not establish secret validity. Restarting the managed API
  did not resolve that configuration rejection.
- No temporary hosted Flask listener was launched or connected to the webhook.
  No hosted URL evidence was produced.
- The test webhook was subsequently disabled; its update response confirmed
  `status=disabled` and `livemode=false`.
- The normal application's billing enablement and live-approval switches were
  not set. No production schema or pilot-admission configuration was changed.

## Still unverified

Real hosted monthly/yearly Checkout, Customer Portal interactions, genuine
signed webhook delivery, failed renewal, cancellation, refund/dispute coverage,
missed-event recovery, and quota denials in the sandbox lifecycle remain
unverified. Existing provider-free fixtures remain separate evidence and cannot
substitute for these tests.

When the operator resumes, first supply the correct test endpoint signing secret
through the secure secret flow and validate configuration without logging values.
Stripe's [webhook quickstart](https://docs.stripe.com/webhooks/quickstart) locates
API/Dashboard endpoint signing secrets in Workbench's Webhooks settings. A Stripe
CLI listener has its own secret; it is not interchangeable with this endpoint's.
Do not weaken signature validation to get past setup.

Then follow [billing-operations.md](billing-operations.md), including explicit
migration status/upgrade/idempotency checks in a disposable database, before
reenabling the test webhook and exercising the remaining lifecycle.