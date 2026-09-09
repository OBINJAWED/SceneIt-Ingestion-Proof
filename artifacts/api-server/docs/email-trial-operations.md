# Email authentication and lifetime trials

## Status and authorization boundary

This is an additive Firebase Authentication integration, not a backend migration.
Flask, PostgreSQL sessions, private App Storage and the existing ingestion worker
remain authoritative. Public trials are **off by default**. Shipping this code or
passing fixtures does not authorize enabling public access, production migration,
deployment, worker/runtime changes, paid infrastructure or live video processing.
The separately controlled pilot launch and phone-playback work remain separate.

No authorized live Firebase email-delivery smoke check has been performed for
this implementation. Browser interception and signed-token/Admin fixtures prove
application behavior, not email delivery, Firebase project configuration, provider
billing or real-device playback. Do not describe an unconfigured integration as live.

## Operator configuration checklist

Use an operator-approved Firebase project. Do not create billable infrastructure
or change its billing plan merely to run this app. In Firebase Authentication:

1. Enable the **Email/Password** provider, not email-link sign-in or social providers.
2. Register the web app and copy its public web configuration. Set a password
   policy in **Require** mode and enable **email enumeration protection**.
3. Add only approved application hosts to Authentication's authorized domains.
   Development preview and published domains are different; an approved published
   URL must be obtained from the deployment configuration, not guessed from a
   workspace name. Do not use wildcard return destinations.
4. Set the verification and password-reset email templates' custom action URL to
   the approved HTTPS application URL plus `/auth/action`. The app uses Firebase's
   supported action-code APIs, removes action parameters from browser history,
   and gives expired/invalid links a resend/reset recovery path.
5. Verify sender identity, template wording and deliverability with designated
   test inboxes only after authorization. Never paste action codes or links into
   logs, tickets, screenshots or analytics.
6. Review Firebase's per-IP signup limits and email-send quotas. Retain abuse
   protections and restricted web API-key usage; do not raise quotas as a debugging
   workaround. Review supported provider-side bot protection if available for the
   approved project. Browser cooldowns are usability only, not server enforcement.

Supply the following through the workspace's environment/secrets controls.
Never paste private values into chat or commit them in files.

| Name | Purpose |
| --- | --- |
| `SCENEIT_PUBLIC_TRIAL_ENABLED` | Explicit `true` enables public email onboarding/new work when configuration is complete; absent/false stays closed. |
| `FIREBASE_PROJECT_ID` | Expected Firebase project and ID-token audience. |
| `FIREBASE_WEB_API_KEY` | Public web API key for Firebase Authentication. |
| `FIREBASE_AUTH_DOMAIN` | Public Firebase auth domain for the registered app. |
| `FIREBASE_WEB_APP_ID` | Public Firebase web app identifier. |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | **Private** Admin service-account credential with approved Authentication read permissions; backend only. |
| `FIREBASE_TRIAL_HASH_SECRET` | **Private**, stable random secret of at least 32 characters for durable trial eligibility. |
| `SESSION_SECRET` | Existing private application session/flow signing secret; retain existing secure handling. |

The four web values are intentionally returned as public configuration by the
session API; service-account credentials and the trial hash secret never are.
Keep private variables out of `VITE_*`, frontend bundles and build logs. Prefer
least privilege and operator-managed credential rotation.

**Do not rotate the trial hash secret as routine session/credential rotation.**
It is a durable accounting key. Replacing it can derive new email ledgers and
replenish trials. Preserve it in controlled backups; any required change needs an
explicit accounting migration that conserves all usage before reopening trials.
Changing Firebase projects is also an identity migration, not a harmless config
edit. Existing private files must never be auto-linked across projects or emails.

Missing/invalid Firebase configuration closes email processing without disabling
legacy Replit login. `PILOT_ALLOWED_SUBJECTS` is unchanged and only exact legacy
OIDC identities qualify. Firebase signup cannot grant shared-proof admission,
including its reports, history, search operations, frames and media.

### Coexisting subscription capability

The existing `SCENEIT_BILLING_ENABLED` switch and public-trial switch are
independent. Enabling billing does not turn Firebase trials into paid accounts
or grant them checkout/pilot admission. Firebase owners keep their durable
lifetime import/search ledger; worker-facing quota checks retain app-wide safety
caps without requiring membership. Existing commercial Replit owners retain
their recurring coverage and usage-window rules. The session/configuration API
and allowance labels distinguish these modes.

## Explicit database migration

The additive migration introduces a provider discriminator (existing users default
to Replit), project/issuer/UID-to-owner mappings and durable email trial ledgers.
It leaves existing user IDs, session bearer hashes, private owners, proof data,
quota counters and configured environment limits untouched. It does not adopt
Firestore or run automatically on startup/build.

The Firebase migration follows the existing billing/upload migrations as
`010_firebase_identity.sql`. Earlier migration numbers and checksums are retained;
do not apply an older standalone Firebase migration numbered 007 to this combined
release. Fresh-schema setup includes the same ordered migration set.

Development, only when authorized:

```sh
python3 scripts/migrate-development.py --development --status
python3 scripts/migrate-development.py --development --upgrade
```

Production remains a separate approved one-shot operator operation. From
`artifacts/api-server`, inspect status and backups before an approved upgrade:

```sh
uv run --locked python -m sceneit.migrate status
uv run --locked python -m sceneit.migrate upgrade --operator-approved
```

Fresh-schema definitions and the migration manifest must agree. Never edit applied
migration checksums, manually reset accounting tables, or run Drizzle schema push.
For rollback, close public trials first, retain durable ledgers and usage, and use
the existing backup/forward-repair procedure. Restoring a pre-trial database without
reconciling consumed operations could replenish allowances and is not a safe rollback.

## Session and identity rules

- Passwords stay with Firebase. The browser uses in-memory Firebase persistence;
  raw ID/refresh tokens are not placed in application storage or URLs.
- An explicit fresh sign-in exchanges an ID token through a short-lived,
  same-origin CSRF challenge for the existing secure HttpOnly PostgreSQL session.
  The `__Host-` challenge cookie is scoped to `/` because browsers reject a
  narrower Path on cookies using that prefix; its signed value is still accepted
  only by the exchange route and expires after ten minutes.
  The Admin SDK checks signature, project, issuer, expiry and revocation. The
  application additionally requires the password provider and fresh authentication.
- Unverified accounts can reach verification/recovery UI, but cannot reserve an
  upload, retrieve a remote video, analyze or make a new paid search.
- Provider state is revalidated at most **60 seconds** after the last successful
  check. This is the bounded window for revocation, password-reset revocation,
  disable/delete and material email/verification changes. It is not instant
  revocation. Uncertain provider status fails closed for private access/new work;
  failures do not refresh the trusted timestamp. Invalid state removes local
  identity sessions. Already accepted worker work is not automatically repurchased
  or refunded.
  The stored project and issuer must also match current configuration, including
  during the cached trust window; matching UIDs or emails in another project are
  not the same identity.
- Logout clears provider state, the current application session and private UI
  caches. Authentication/account changes never automatically start or retry an import
  or search. Files must be reselected after an authentication transition.
- A Firebase owner is a new opaque ID, not its UID or email. Trial accounting uses
  a keyed digest of a conservatively normalized verified email, separate from file
  ownership. Case/Unicode normalization does not strip dots or plus suffixes.
  Account recreation shares consumed trial usage, not content. No self-service
  linking or legacy owner migration is implemented.
  Once an identity claims a verified trial email, changing that normalized email
  is unsupported and fails closed rather than assigning a fresh allowance.
  Operator reconciliation must conserve both addresses' consumed usage; it must
  not transfer another identity's files.
- Closing the rollout switch blocks only new reservations/link retrieval/new
  searches. A still-valid verified owner session can read, complete, cancel and
  clean up its own existing records. Replay detection runs before new-work
  eligibility so reopening a saved search is neither charged nor rollout-blocked.
- PostgreSQL-backed exchange IP/subject throttles and request throttles are shared
  across web processes. Ensure trusted proxy hops/hosts match the actual routing;
  spoofable forwarded IPs undermine IP limits. Recovery UI uses generic responses.
- Do not enable a Firebase Auth emulator in a deployment. Tests patch the provider
  boundary in isolated processes; no runtime test login or public auth bypass exists.

## Allowances and failure handling

Defaults remain three lifetime import attempts and 50 lifetime **new** searches
per account/trial ledger, 30 imports and 500 searches app-wide:
`SCENEIT_OWNER_IMPORT_LIMIT`, `SCENEIT_OWNER_SEARCH_LIMIT`,
`SCENEIT_APP_IMPORT_LIMIT`, `SCENEIT_APP_SEARCH_LIMIT`.
Preserve explicitly configured values; migration does not raise limits.

These are operation allowances, **not a guaranteed dollar spending ceiling**.
Limits are cumulative, with no monthly reset and no payment requirement. Reserved
failed/cancelled attempts remain consumed. Deletion, seven-day expiry, logout,
worker failure and identity recreation never refund them. The existing 200 MB,
4–1200-second and seven-day rules remain in force.

Transactions lock both the durable ledger and app counter before external work.
Idempotent reservations are charged once; ambiguous provider writes remain under
operator reconciliation, never an automatic retry. Completion and cleanup of
already-reserved work remain distinct from new spending. Saved-result reads use no
new search allowance and remain available while the underlying import is retained.

The session response exposes lifetime used/limit/remaining values even before the
first import and after expired import records are absent. The UI distinguishes
verification, closed/unconfigured access, account exhaustion, shared app capacity
and unavailable worker states. Exhaustion does not lead to checkout or an upgrade
button; payments are intentionally deferred.

Multiple distinct verified email addresses can still claim distinct trials. This
is not a custom fraud-prevention platform. Keep the global caps, Firebase abuse
controls and operator monitoring; do not promise perfect Sybil resistance.

## Verification and controlled rollout

`pnpm run quality:local` creates a disposable Unix-socket PostgreSQL cluster and
uses provider-blocked fixtures. It runs migration/preservation/concurrency tests,
Python boundaries, generated contract drift, typechecks, intercepted browser tests
and builds. It neither migrates the app database nor deploys. Firebase config and
video-provider credentials are removed from this isolated gate.

### Implementation verification record — 2026-09-09

- Disposable PostgreSQL migration, repeat migration, preservation, restart,
  quota-concurrency and identity-exchange checks passed. Successful exchange
  responses were validated against the checked-in OpenAPI schema.
- The provider-free Python suite passed (167 tests at the full-suite pass;
  the opt-in live App Storage test was intentionally not enabled). Additional
  focused checks passed for malformed Firebase configuration and expanded
  exchange/email-change/recreation/concurrency and route-admission cases.
- All 40 desktop/mobile-viewport browser cases passed across the initial pass and
  targeted reruns of corrected failures. These intercept Firebase and application
  mutations. Initial fixture mismatches and a pilot-loading cache invalidation
  regression were corrected; unaffected browser cases were not rerun.
- Generated-contract drift, TypeScript, 13 frontend behavior checks and production
  builds passed. The API/web preview was restarted and the default-off onboarding
  screen inspected. The separately managed ingestion worker was not changed.
- No Firebase project was activated, no application/production database was
  migrated, and no live email, App Storage integration test, or video processing
  was performed. Configuration and authorized live-email delivery remain operator
  prerequisites, not verified outcomes of these fixtures.
- Combined-subscription reconciliation passed fresh/repeated migrations and
  restart preservation checks in disposable PostgreSQL. A 247-test backend run
  exposed merged error-enum omissions and an autocommit-fixture concurrency race.
  After correction, the focused authentication/security/contract/persistence
  checks and all 13 PostgreSQL quota cases passed; the concurrent identity exchange
  also passed three repeat runs. No new browser pass or live-provider smoke check
  is claimed for reconciliation.

Before any separately authorized rollout, record:

- Exact approved project/domains/action URL and private credential readiness.
- Explicit migration status and retained counters/configured limits.
- Authorized email smoke results: signup, delivered verification, resend,
  password-reset delivery, expired link recovery and logout/revocation window.
- A non-processing session/eligibility check. Do not use real video analysis as
  an auth smoke test; it consumes lifetime and app allowances.
- Separate worker availability and release approval from the existing pilot
  operations process. This document does not approve those changes.

References:
[Firebase password auth](https://firebase.google.com/docs/auth/web/password-auth),
[custom action handlers](https://firebase.google.com/docs/auth/custom-email-handler),
[session revocation](https://firebase.google.com/docs/auth/admin/manage-sessions).