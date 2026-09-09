# Stripe Test Clock operator handoff

This is tooling and a verification plan, not sandbox evidence. Nothing in this
document authorizes Stripe mutations, email, billing activation, production
migration, or customer charges. The existing hosted-payments verification task
owns approval and execution. All scenarios below remain **unverified** until its
operator records actual redacted results.

## Non-negotiable boundaries

- Use only an explicitly approved `sk_test_`/`rk_test_` credential and a Stripe
  account response with `livemode=false`. The harness rejects live keys and live
  responses.
- Set `SCENEIT_STRIPE_TEST_CLOCK_APPROVED=true` only for the approved command.
  Keep billing disabled everywhere else. Never place a key in arguments,
  manifests, evidence, source control, or output.
- `SCENEIT_TEST_CLOCK_DATABASE_URL` must name the approved
  `billing_clock_*` namespace, must differ from `DATABASE_URL`, and must contain
  no shared/persistent customer state. Apply migrations explicitly to that
  disposable database. The CLI opens this URL directly, validates the actual
  database/schema and billing tables before any provider resource or database
  mutation, and injects that factory process-locally into billing/quota calls;
  it never redirects `DATABASE_URL` or uses the normal connection factory.
- Every clock scenario gets a new clock-bound customer tagged with its run and
  scenario. Do not attach an existing customer or copy production rows.
- The harness uses the locked Stripe SDK, zero automatic retries, a 1 MiB
  response cap, an aggregate deadline, finite request count, and finite poll
  count. Unknown mutations stop for reconciliation; do not improvise a second
  customer/subscription.
- Test Clock time is provider test time only. Do not change system time, patch
  `now()`, weaken the 300-second webhook signature tolerance/future timestamp
  check, or add a production entitlement-time override. The authorized verifier
  must provide an isolated application/database convergence adapter and assert
  the application's state at each checkpoint.
- Evidence may contain scenario names, safe provider IDs, integer amounts and
  currencies, timestamps, states, counts, release/config fingerprints, and
  pass/fail reasons. Exclude keys, webhook signatures or payloads, email/address
  details, card data, cookies/tokens, and hosted/signed URLs.

## Approval and preparation

Create a private approval JSON after approvals are recorded:

```json
{
  "approved": true,
  "runId": "clock-release-001",
  "databaseNamespace": "billing_clock_release001",
  "frozenTime": 1800000000,
  "scenarios": ["monthly_renewal", "renewal_failure_recovery"],
  "maxRequests": 50,
  "maxPollAttempts": 20,
  "maxRunSeconds": 600,
  "fixtures": {
    "monthly_renewal": {
      "ownerId": "approved-isolated-owner",
      "baseTier": "approved_fixture_tier",
      "targetTier": null,
      "cadence": "monthly",
      "targetCadence": null,
      "currency": "usd",
      "taxLocation": {"country": "US", "postalCode": "94107"},
      "zeroTaxLocation": null
    }
  }
}
```

Values (including the synthetic location) are examples of shape, not an
approved selling jurisdiction, price, date, allowance, or run authorization.
The operator must replace them with the reviewed sandbox fixture. List the
available matrix without credentials or network work:

```sh
uv run --locked python -m sceneit.billing_test_clock matrix
```

After the hosted-payments operator confirms approved test Prices/tax/product
settings, webhook endpoint, restricted Portal, disposable database, fixture
allowances, recipient, and resource cleanup:

```sh
export SCENEIT_STRIPE_TEST_CLOCK_APPROVED=true
export SCENEIT_TEST_CLOCK_DATABASE_URL='approved disposable URL containing billing_clock_release001'
# STRIPE_SECRET_KEY is supplied by the platform secret store.
uv run --locked python -m sceneit.billing_test_clock prepare \
  --operator-approved --approval "$PRIVATE_APPROVAL_JSON" \
  --manifest "$PRIVATE_RESOURCE_MANIFEST"
```

Preparation is rerun-safe through stable idempotency keys and a private
mode-0600 resource manifest. It creates clocks/customers only. Subscription,
payment-method, webhook, database, hosted, refund, replay, and email operations
remain with the authorized verifier and its integration adapter.

The integration entry point is
`TestClockHarness.run_scenario(scenario, actions, verifier)`. The approved
mutation adapter implements only `provision` and `perform`; it cannot report a
scenario pass. Use the shipped `IsolatedDatabaseVerifier` with non-secret
`ScenarioFixture` identities. The verifier binds provider time through
`billing_time.isolated_verification_time` only after checking the approved
database/schema and required billing tables, then polls authoritative
application status, owner/customer linkage, completed verified webhook
receipts, active/reversed coverage, payment problems, pending changes,
tier/cadence/currency, tax totals, and annual allowance windows. Only controlled
states and a restricted safe code are retained. The harness advances the
approved clock according to its fixed plan and stops on the first non-pass. It
does not provide a normalized fake-event path. Receipt-producing phases require
a completed receipt newer than that phase's recorded baseline; an earlier
successful delivery cannot satisfy later convergence. Inclusive, exclusive,
and legitimate zero-tax checkpoints assert their exact expected shape.

The standard runner resolves Price IDs only from the enabled reviewed test
catalog, binds the approved isolated owner to the new clock-bound customer, uses
allowlisted Stripe test payment-method tokens, creates one idempotent
subscription, drives failure/recovery/cancellation through Stripe, and drives
upgrade/schedule changes through SceneIt's billing functions:

```sh
uv run --locked python -m sceneit.billing_test_clock run \
  --operator-approved --approval "$PRIVATE_APPROVAL_JSON" \
  --manifest "$PRIVATE_RESOURCE_MANIFEST" \
  --scenario monthly_renewal
```

`fixtures` is required for `run`. Tier/cadence/currency must resolve to the
reviewed sale-enabled test catalog; Price IDs are deliberately not accepted
from the command or approval file. Supply a reviewed synthetic `taxLocation`
and, for the zero-tax phase, a separately reviewed `zeroTaxLocation`. Output contains only controlled
phase/status codes; resource IDs stay in the private manifest or Stripe
metadata.

## Scenario matrix and checkpoints

For every provider mutation, wait for Test Clock `ready`, then bounded signed
webhook convergence, then query authoritative billing status and isolated
database facts. A redirect or card update is never a success assertion.

| Scenario | Required assertions | Initial status |
| --- | --- | --- |
| Monthly renewal | one paid renewal coverage row; paid-through advances once; usage anchor/history retained | Unverified |
| Renewal failure and recovery | failure/action-required or expired-card state; no new coverage/allowance; hosted recovery; later paid invoice resolves only the matching problem | Unverified |
| Annual monthly allowances | yearly coverage remains valid while monthly windows advance; consumed usage does not reset early or expose an annual lump sum | Unverified |
| Paid tier upgrade | previewed integer proration/tax/currency; old tier before payment; target ceiling after verified paid upgrade; base coverage survives upgrade refund | Unverified |
| Scheduled downgrade | one subscription, pending renewal date, withdrawal behavior, lower ceiling at renewal, retained usage and over-limit denial | Unverified |
| Scheduled cadence change | unchanged currency; effective only at subscription renewal, not allowance boundary | Unverified |
| Cancel and expire | period-end cancellation preserves access to exact paid-through; no new work after expiry; management remains available | Unverified |
| Inclusive tax | reviewed inclusive Price and tax code; authoritative subtotal/tax/total/paid facts; supported invoice lines | Unverified |
| Exclusive/zero tax | reviewed exclusive jurisdiction and a legitimate zero-tax case; incomplete tax never funds access | Unverified |
| Currency offer | allowlisted three-letter currency and integer minor units retained through change; unsupported currency and FX conversion rejected | Unverified |
| Refund companion | approved full and partial refunds, pending/failed outcome, duplicate/reordered delivery, sticky invoice-scoped reversal | Unverified |
| Replay/inbox companion | bounded pending-event replay/reconcile is idempotent; designated recipient observes an approved Stripe receipt/reminder and records SMTP/provider limitations separately | Unverified |

Expired-card simulation, authentication-required payment methods, some tax
locations, refunds, disputes, receipt sends, and inbox delivery are not driven
by clock advancement alone. Run them as approved companion operations and never
mark them passed from a fixture. Stripe may not support automatic test-mode
emails or every dispute/tax transition; mark each such case **unsupported** or
**blocked**, with the provider limitation, rather than substituting a mock.

At each phase retain a redacted record of: run/scenario, code and catalog
fingerprints, clock/provider state and timestamp, event receipt IDs/states,
subscription/invoice safe IDs, payment problem, effective/pending tier,
cadence/currency/tax integer facts, paid-through, usage window/consumption,
request/poll counts, and assertion outcome. Keep browser-hosted, Test Clock,
refund/replay, provider-free, and actual-inbox evidence in separate sections.
Provider-free tests are not Stripe sandbox or delivery certification.

## Cleanup and reruns

First stop scenario work and reconcile any uncertain mutation. Cancel/delete
only subscriptions and payment methods tagged for this approved run, then:

```sh
uv run --locked python -m sceneit.billing_test_clock cleanup \
  --operator-approved --approval "$PRIVATE_APPROVAL_JSON" \
  --manifest "$PRIVATE_RESOURCE_MANIFEST"
```

Cleanup deletes each manifest customer before its clock and records incomplete
items without discarding their IDs. Repeat cleanup safely until complete; never
bulk-delete account resources. Retain the redacted scorecard, delete the private
resource/approval files under the evidence retention policy, destroy the
disposable database, unset opt-in/database variables, and revoke temporary
restricted credentials if one was issued. A failed cleanup is an open operator
item, not a passed run.

The `run` command performs standard lifecycle mutations and concrete isolated
assertions. Hosted browser, refund/replay, and inbox companions remain separate
approval-owned operations. Billing's time seam has no production environment
override: it defaults to fresh database wall time and can only be scoped
in-process after disposable database identity checks. Until authorized sandbox
execution occurs, all provider lifecycle, hosted, webhook, tax, refund, replay,
and inbox outcomes above are honestly unverified.