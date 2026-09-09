---
name: Billing provider evidence
description: Verify payment adapters against the installed SDK transport and raw resource shapes, not normalized fixtures alone.
---

Use the locked SDK's real request path with network-intercepted raw provider
responses when verifying billing. Normalized domain fixtures alone are not
evidence that a hosted checkout or paid-invoice reconciliation can work.

**Why:** An adapter can pass lifecycle fixtures while its SDK rejects synchronous
calls, or while it expects invoice references removed from current charge
objects. Those failures are invisible after tests have already normalized the
provider response. Old integration examples are not authoritative.

**How to apply:** Check the installed SDK's transport requirements and resource
relationships before adapting billing code. Include raw paid, refunded, and
disputed payment shapes in provider-free contract tests. Test deadlines from
request threads as well as the main thread; signal-only timeouts do not protect
threaded web workers.

Diagnose credential validation failures before attributing them to environment
propagation or attempting another provider run.

**Why:** A saved, present credential can still be the wrong credential type.
Repeated runtime restarts and disposable-database setup do not repair that, and
claiming a propagation problem without evidence sends the operator down the
wrong path.

**How to apply:** Check the specific configuration constraint with a redacted
pass/fail result inside the runtime. Distinguish absence, invalid format, and
provider rejection; never expose values. For hosted webhooks, do not substitute
an API key or a CLI listener's secret for the destination's signing secret.

Intercepted SDK tests must also check parameter semantics against the provider's
documented request contract, and drive verified events through the real
persistence path rather than calling only the final coverage-write helper.

**Why:** The SDK's type annotations do not necessarily validate outgoing values
at runtime. A permissive mock can return success for a request the provider would
reject; helper-only tests can also miss failures in the preceding reconciliation.

**How to apply:** Treat documented request constraints, captured transport
assertions, real event-handler/database tests, and authorized hosted evidence as
separate layers. None substitutes for the others. Include raw unpaid and terminal
resources in end-to-end persistence regressions, not just successful payments.

Status/relationship inspection must remain separate from funded-access
verification.

**Why:** A paid-only normalizer can reject a legitimately voided invoice before
recovery sees its terminal state, leaving an abandoned financial operation
permanently blocked even though normalized database fixtures pass.

**How to apply:** Carry exact nonpaid facts through the real adapter into recovery,
while retaining strict positive, fully paid, validated funding requirements at
every coverage-grant boundary.

Classify outcomes for the whole provider operation, not only its final request.

**Why:** A definite rejection of a later configuration request does not undo an
earlier successful creation or release. Likewise, malformed success responses
do not prove that the provider mutation failed.

**How to apply:** Preserve identifiers and mutation blockers after any confirmed
side effect until authoritative reconciliation or a verified compensating action
settles the operation. Test create-success/update-rejection and malformed-success
responses through persistence as well as transport.

A released subscription schedule is not proof that its planned change was
prevented.

**Why:** Stripe leaves the subscription in place when releasing a schedule. If
the target phase has already started, release does not undo that applied Price.

**How to apply:** Customer withdrawal and its recovery must inspect fresh phase
and subscription facts, including after release, and never equate an already
applied change with a successful withdrawal.

A scheduled-change quote is bound to its quoted renewal, not merely a generic
preview lifetime.

**Why:** Crossing renewal can make Stripe attach a newly created schedule to the
following period, silently changing the date the customer approved.

**How to apply:** Expire the quote at renewal, revalidate the provider's period
before mutation, and verify the created phase boundary before configuration.
If creation already succeeded across the boundary, retain its recovery blocker.

Compensation needs its own durable intent and proof, separate from the original
financial quote.

**Why:** A release can succeed while its response is lost, and cleanup may need
to prove a source period different from a quote that expired during creation.

**How to apply:** Persist cleanup intent before mutation, retain the independently
verified source facts, and recognize completed cleanup without repeating it or
rewriting the customer's approved terms.

Classify provider resource facts separately from the requested operation's outcome.

**Why:** The same active schedule can prove that setup succeeded while proving
nothing about whether a withdrawal succeeded. Sharing the resource state as the
operation state can remove recovery eligibility without fulfilling the intent.

**How to apply:** Resolve each operation kind only from its own success or terminal
failure evidence; keep uncertainty and original request identity otherwise.
