# Tiered billing: local verification evidence

Date: 2026-09-09

This record covers implementation and provider-free verification only. It does
not certify Stripe-hosted behavior, Test Clock execution, receipt delivery, tax
registrations, SMTP acceptance, inbox delivery, or production readiness.

## Checks completed

| Layer | Result |
| --- | --- |
| Full Python suite with isolated PostgreSQL | 306 tests completed successfully; the one opt-in live App Storage test was intentionally skipped. |
| Final affected billing suite, including delivery-attempt tracking and timeline paging | 89 tests passed with disposable PostgreSQL and provider networking disabled. |
| Final persistence/migration/restore rehearsal | 12 tests passed, including canonical fresh schema, concurrent upgrades, checksum validation, legacy preservation, and restore. |
| Populated billing migration preservation | Passed within the billing suite: original paid periods, reversal state, historical Price, allowance anchor, used allowance, and Firebase lifetime ledger survived additive upgrades. |
| Actual Flask/OpenAPI emissions | Status, Checkout, Portal, preview, confirmation, and withdrawal responses validated against their exact generated contracts using isolated persistence. |
| SDK/HTTP boundary | Intercepted locked Stripe SDK requests exercised taxes, proration/credits, modern invoice-payment relationships, refund settlement, hosted parameters, schedule proof, and bounded recovery. |
| Notification delivery | Provider-free SMTP/SDK and PostgreSQL tests covered current-recipient checks, resolved suppression, concurrent claims, finite backoff, ambiguity, deadline/lease loss, cooldowns, and resolution. |
| Test Clock tooling | Provider-free CLI isolation, standard scenario wiring, phase-specific receipt convergence, tax assertions, cleanup, and process-scoped entitlement time passed. |
| Generated clients and build | Clean code generation matched checked-in clients; type checks, behavior tests, and builds passed. Vite reported a non-blocking bundle-size warning. |
| Browser journeys | One full desktop/mobile pass exercised 60 cases. It found two instances of billing uncertainty lost during initial auth loading and one logout-test navigation race. After correction, the 10 affected/new assertions passed, including uncertainty across reload, matching authoritative recovery, preview-key retry, and account switching. No second broad browser pass was run. |
| Running preview | Flask and viewer workflows restarted successfully; the authenticated billing entry boundary rendered at `/billing` without browser errors. |

### Completion-review follow-up

The final review additionally identified expired client keys, abandoned unpaid
upgrades, and transitive funding across successive upgrades. These were corrected:

- Known terminal/expired operations now allow a fresh, explicit same-tab attempt,
  while valid retries and unknown outcomes retain their keys.
- A persisted upgrade invoice can become terminal only after exact fresh
  provider void/expiry evidence; paid base coverage is preserved.
- Higher-tier access validates every ancestor in its incremental funding chain.
  Refunding the middle increment removes dependent access, not later independently
  paid full-period coverage, and never replenishes usage.

After these corrections, the 89-test billing suite and 12-test persistence suite
passed again with migration 014 included. The 61 scoped quota/trial/admission
checks passed with three-tier, invalid-ancestor, and concurrent-refund cases.
The 10 affected desktop/mobile lifecycle assertions initially exposed two
intercepted-hosted-navigation fixture races. Both fixtures now explicitly simulate
the hosted return in the same tab; all four affected hosted-return checks passed.
The other eight lifecycle assertions passed, including preview expiry/consumption
and unknown-confirmation key retention. No new broad browser pass was performed.

A subsequent SDK-boundary review found that unpaid invoices were still rejected
by the paid-invoice normalizer before terminal recovery. Status inspection now
accepts strictly validated raw open/void facts without a successful-payment
lookup; paid funding retains its full positive-payment identity checks. The
12-test billing PostgreSQL suite and 27 provider/recovery checks passed after
this correction. These now include intercepted raw SDK responses flowing through
the actual pending-update-expiry webhook and operator recovery: void releases
only its matching blocker, open remains blocked, and paid coverage and allowance
anchors remain unchanged.

Staged-mutation recovery was also checked after review identified a schedule
creation that could outlive a rejected configuration request. Post-mutation
validation failures now remain uncertain for schedules, upgrades, and withdrawals.
The 12 billing PostgreSQL cases and 28 provider/recovery checks passed. A new
standalone raw-SDK-to-PostgreSQL regression then passed for create success →
update 429 → durable blocker → verified compensating schedule release → successful
fresh customer change, preserving base coverage and usage. It uses Stripe's
released-subscription resource shape and exposed/fixed empty SDK metadata
normalization before confirming compensation works.

Final terminal-state regressions cover refund-before-paid reconciliation, sticky
reversal replay, operator recovery, and truthful withdrawal retries. Reversed
upgrades now settle only their matching blockers; a new prorated upgrade cannot
use an unfunded provider tier as its source. Withdrawal API success requires
confirmed `withdrawn` state, and the browser independently checks that outcome.
After correcting the isolated usage fixture and the withdrawal reload guard,
all **93 billing tests passed**. The eight initially passing withdrawal browser
checks remain valid, and the four corrected desktop/mobile unknown/concurrent
withdrawal checks passed on their focused rerun. No provider or email was contacted.

The final scheduled-withdrawal checks include delayed renewal webhooks and a
target phase that starts during release. Four new raw-SDK/PostgreSQL cases prove
that only an unchanged source subscription is reported withdrawn; already-applied
targets remain effective without granting coverage from phase evidence. Cached
withdrawal replay behavior and partial-schedule compensation are preserved.
All **97 billing tests passed** after the phase and wire-time-precision fixes.
Matching scheduled confirmation now clears only its own browser uncertainty,
while pending-change purchase protection remains. Seven new desktop/mobile
scheduled-recovery checks passed initially; the remaining mobile fixture race was
corrected by awaiting persisted uncertainty, and both affected mismatch checks
passed on the focused rerun. No additional broad browser pass was run.

Schedule webhooks now use the same verified phase/subscription path as customer
withdrawal and operator recovery. Four additional raw-SDK event regressions cover
release before renewal delivery, unchanged-source release, interleaving with
withdrawal, and replay after target application. Firebase link-worker storage is
also regression-tested with billing enabled and no commercial account: occupancy
remains application-only, private ownership remains intact, cleanup releases it,
and lifetime import/search consumption is unchanged. The final combined billing,
quota, and upload-attempt run passed **138 tests** against isolated PostgreSQL.

Scheduled-operation recovery also now shares that same verified outcome logic,
including synchronization of the actual subscription rather than interpreting
terminal schedule statuses as failure. Five additional raw-provider/PostgreSQL
regressions cover released target/source recovery, unresolved ambiguity, preview
expiry at renewal, and a phase boundary shifting during creation. The latter
retains the created schedule's recovery blocker and never configures an unapproved
later date. The complete billing suite passed **106 tests** after these additions;
the previously passing quota/trial checks were not invalidated.

Compensation recovery now retains intent before release and independently proven
source-phase facts. Raw SDK/PostgreSQL regressions cover a lost release response,
a lost verification read, and rejection of an already-released resource without
durable intent. The create-boundary race also now runs through successful cleanup
and a fresh, explicitly approved change. All **109 billing tests passed**, with
paid coverage, consumed usage, and allowance anchors preserved. These remain
provider-free checks, not Stripe sandbox certification or email-delivery evidence.

Withdrawal recovery now distinguishes the schedule's state from the withdrawal's
outcome. New raw SDK/PostgreSQL cases prove original-key reconciliation, recovery
of legacy misclassified operations, expiry-window refusal, and truthful customer
replay. The final billing run covered **111 tests**: 110 passed initially, and the
one legacy operation-state assertion passed its focused rerun after alignment
with `completed` (the change itself remains `withdrawn`). The focused intercepted
desktop/mobile follow-up passed **16 tests**, including retaining unknown
withdrawal keys through nonterminal statuses and clearing them only after verified
completion. Workspace type checking also passed.

Tests used a separate temporary Unix-socket PostgreSQL cluster and isolated
schemas. Stripe and video-provider credentials were removed from test processes;
provider networking was disabled. Browser fixtures intercepted all API/provider
activity. Tests did not charge, send email, analyze video, or migrate the ordinary
application database.

## Important review corrections

- An incomplete upgrade may retain its old Stripe Price while payment is
  pending; that is not permission to release the mutation blocker.
- Both webhook and operator recovery require the exact configured schedule
  phase, quantity, and mutation identity before reporting a scheduled change.
- Refund settlement is reconciled after a pending refund becomes successful.
- Full reconciliation tests include debit and credit lines, rather than
  testing only the final coverage-write helper.
- The Test Clock command validates and binds its approved isolated database
  before provider mutation or database writes; normal application connection
  settings are not redirected.
- Verified delivery counts remain distinct from processing/replay attempts.
- Local uncertainty clears only for a matching server-confirmed operation
  outcome; lack of a row or return from a hosted page is not confirmation.

## Not activated or certified here

- Migrations are checked in, not applied to the ordinary development or
  production application database.
- No commercial offers, public admission, notification scheduler, SMTP delivery,
  tax registration, deployment, or production runtime configuration was enabled.
- The pre-existing private import worker capacity guard remains unchanged; this
  billing verification did not restart it or claim video-processing readiness.
- The separate authorized hosted-payments verification remains the owner of
  actual Stripe, Test Clock, tax, refund/replay, and designated-inbox evidence.

Complete the [activation review](billing-activation-review.md), the
[notification runbook](billing-notification-operations.md), and the
[Test Clock handoff](billing-test-clock.md) before enabling their respective
operator-approved activities.