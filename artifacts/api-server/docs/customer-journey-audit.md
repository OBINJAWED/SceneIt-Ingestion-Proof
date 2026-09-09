# SceneIt customer journey audit

**Audit date: 9 September 2026. Environment: isolated local test workspace.**

This is a testing and findings report, not launch approval. No customer account,
production database, real email, hosted payment, video-provider job, or managed
import worker was used. Product behavior and access policies were not changed.

## Recovery fix verification — 9 September 2026

The three recovery defects from this audit are now fixed in the client:

- **PRODUCT-AUTH-RECOVERY-001:** expired reset recovery opens the reset-request
  form, not account creation. Expired verification opens sign-in for explicit
  resend.
- **PRODUCT-AUTH-RETURN-002:** verification and password-reset completion retain
  sanitized local `returnTo` destinations through fresh sign-in. One-time action
  parameters are still removed from the address bar.
- **PRODUCT-INTENT-004:** the anonymous pending video link survives verification,
  fresh sign-in, and reloads in its original tab. Restored entry text does not
  restore analysis or playback consent and never starts processing.

Those three regressions are ordinary assertions now, not expected failures.
The connected walkthrough uses the restored link rather than manually re-entering
it. Private queries and owner drafts still clear on account changes; explicit
logout also clears anonymous drafts. Recovery closes cached access before
releasing the local signout latch, and a failed initial identity check exposes
retry controls without restoring an owner's draft.

**Local evidence:** workspace typecheck and all 16 frontend behavior tests passed.
The intercepted desktop/mobile-viewport pass had 32 passes and two failures in a
new identity-error case. A focused check exposed an incorrect hidden-control
assertion after the automatic-remount retry loop was fixed; the final two-case
check passed. This is **34 distinct passing browser cases across the main pass
and focused checks**, not a claim of one clean full-suite run. Coverage includes
the three original regressions, existing-session recovery, second-tab
verification with return to the original tab, logout/account switch, and separate
Firebase/private versus Replit/pilot permissions. The web preview also rendered
successfully.

The browser transports remained intercepted, apart from the existing disposable
synthetic-media upload sink in the connected walkthrough. No real email, provider
activation, worker operation, rollout, or payment change was made. Actual email
delivery remains unverified and separately owned. A newly opened independent tab
or device does not acquire another tab's draft.

The heading-semantics defect remains outside this fix and retains its expected
failure. All original audit findings, screenshots, and historical results below
are preserved as the pre-fix record.

## Original audit — bottom line

The safe local journey works from an authorized import through synthetic-media
playback and returning to saved results. It does **not** establish that a real
customer can complete a live trial or purchase today.

**Four confirmed usability defects:** expired-reset recovery opens signup,
verification loses the protected destination, verification/sign-in loses the
pending video link, and private-card section titles lack heading semantics.
No cross-account access defect was established: the corrected browser identity
fixture and actual Flask/PostgreSQL ownership checks passed.

**Customer stop points:** a non-running preview cannot be visited; disabled
email onboarding cannot create a trial; unavailable ingestion cannot process an
import; there is no customer purchase/upgrade/portal screen. Those capabilities
were not activated to make this audit pass.

## Evidence guide

- **Browser fixture:** the real React interface and Firebase browser SDK, with
  application and external HTTP requests intercepted before transport. Synthetic
  accounts and a six-second locally generated H.264/AAC MP4 only.
- **Application boundary:** actual Flask routes, emitted-response contract
  validation, and real PostgreSQL transactions in disposable schemas. Identity,
  storage, search, and cleanup adapters are replaced with explicit test adapters;
  unexpected external HTTP calls fail the new connected test.
- **Static inspection:** source and operations documentation, not a runtime pass.
- **Blocked / not available:** a prerequisite was unavailable or intentionally
  excluded. This is never counted as a pass.

The browser projects are desktop Chromium (1440 × 900) and Chromium with iPhone
13 viewport/touch settings (390 × 664), plus a 360 × 640 keyboard/layout check.
These are **not physical phones or Safari**. The MP4 really decodes and advances
locally; candidate results, frames, processing progress, and external ingestion
are simulated. Neither external platform playback nor timestamp alignment is
certified.

## Ordered journey scorecard

All rows use the isolated environment above. **B** = browser fixture;
**A** = actual Flask/disposable-PostgreSQL boundary; **S** = static inspection.
“Pass” means only the named local evidence passed. For passing rows the customer
severity is **none observed** and no new approval is needed to repeat the safe
fixture. External use still requires the separate approvals listed below.

| Stage | Verdict / evidence | Reproduction; expected versus observed | Severity / approval |
| --- | --- | --- | --- |
| 1. First arrival | **PASS B; live preview BLOCKED** | Fresh context → `/`. Landing explains private video search; tab/link/file choices and rights consent are visible. Live preview was not running, so its visit failed. | Live blocker; operator runtime approval. |
| 2. Signup and trial entry | **PASS B + A; live email BLOCKED** | Use Sign in to continue → Create account with a synthetic email. Unverified state appears; no import/search starts. Actual session exchange emits the contract and denies unverified private access. | No local defect; actual signup enablement/delivery needs email approval. |
| 3. Verification and fresh sign-in | **PASS B + A; live delivery BLOCKED** | Open controlled valid action → Sign in to continue → sign in. Verified session and lifetime allowance are visible; a second real-route exchange reuses the durable owner. | Actual inbox evidence remains separate. |
| 4. Return to original intent | **FAIL B** | Start with a YouTube link; verify and sign in. Expected the link/protected destination to survive without processing. Observed both pending-link loss and dropped protected `returnTo`; main walkthrough explicitly re-enters the link to continue. | Medium; approved UI/auth recovery fix. |
| 5. Resend/reset/expired actions | **PASS B for valid actions; FAIL B for recovery routing** | Resend, request reset, use a controlled valid reset, then controlled expired actions. Messages/forms work with no email transport. Expired-reset recovery opens signup instead of the promised reset flow. | Medium; UI recovery fix. Live delivery is blocked. |
| 6. Lifetime allowance | **PASS B + A** | Sign in before/after expiry and inspect allowance. Trial shows three lifetime import attempts and five new searches; one created import leaves two attempts. Cancel/reload/return do not restore or duplicate charges. | None locally; no policy change. |
| 7. Rights consent and MP4 selection | **PASS B + A** | Re-enter link, give analysis/playback consent, reserve once, activate chooser with Enter, select the synthetic MP4. Only the selected authorized MP4 is uploaded; consent is not silently restored. | None locally. |
| 8. Transfer and server confirmation | **PASS B + A** | Upload the 107,254-byte MP4 to a disposable loopback sink. Sink verifies every byte; first completion reply is uncertain, Retry server confirmation succeeds without another transfer/reservation. Actual reservation/completion replay preserves one charge. | Real App Storage behavior not verified. |
| 9. Processing, reload and resume | **PASS B + A, simulated processing; live ingestion BLOCKED** | Controlled processing → ready, reload, then fresh sign-in → Resume session. State and ownership persist. A deliberately written ready transition and existing worker tests replace provider ingestion. | Live blocker; runtime/provider approval. |
| 10. Supported-link branch | **PASS B + A for boundaries; live link ingestion BLOCKED** | Paste controlled Vimeo URL → consent → Start processing. Fixture reaches ready without file transfer; linked-source UI appears. Vimeo retrieval/ingestion was not attempted. | External evidence required. |
| 11. Authorized-file fallback | **PASS B + A** | Paste YouTube URL → MP4-required explanation → chooser/transfer. YouTube remains context plus an authorized MP4; backend tests prove the downloader is not invoked for YouTube. | None locally; external playback not certified. |
| 12. Scene search and candidates | **PASS B + A** | Search “moving color bars.” Two controlled ranked candidates show 0:01–0:03 and 0:03–0:05; frame and selection state appear. Actual search persists ownership, history and one debit. | Semantic quality not evaluated. |
| 13. Private playback and selected seek | **PASS B + A for local media; external/real-phone BLOCKED** | Enable owner-only playback, play the synthetic video from zero, observe advancement, seek to four seconds, select candidate one and observe time below 1.6 seconds. Linked timeline remains explicitly unverified. | Physical-device/external evidence separately owned. |
| 14. Revoke/unavailable source/audio | **PASS B + A** | Turn playback off: player removed and route denied. Simulate unavailable source/frame: readable fallbacks appear. Silent metadata removes Audio mode; actual silent-media route tests reject audio search. | No live-storage revocation claim. |
| 15. Saved history and return | **PASS B + A** | Reload → keyboard-select saved query → logout → fresh sign-in → Resume session. Results remain; exactly one import and one successful search persist; no automatic new work or charge. Exhausted-owner replay also succeeds. | None locally. |
| 16. Invalid/interrupted uploads | **PASS B + A** | Select text as media: chooser rejects it. Abort first PUT → error → reload/reselect → second PUT and completion retry. Exactly one full file reaches sink; import count stays one. Actual malformed-media/completion boundaries are tested. | Physical network interruptions remain a gap. |
| 17. Worker/search failures | **PASS B + A; long-poll recovery GAP S** | Sign in with worker unavailable: instructions and disabled creation. Submit empty, failed and uncertain searches: messages appear, only three explicit submissions and no automatic retry. Polling beyond 75 seconds lacks a dedicated private-status refresh control by inspection. | Live worker blocker; later usability confirmation needed. |
| 18. Exhaustion and buying more | **PASS B + A for limits; purchase NOT AVAILABLE S + B** | Exhaust allowance: new work disabled, saved reads retained, expiry does not reset usage. No upgrade, Checkout or Customer Portal control exists. | Commercial blocker; separately approved billing/frontend work. |
| 19. Cancellation and removal | **PASS B + A** | Confirm Cancel upload for pending import and Delete import for ready link import. Cancelled state removes upload controls. Actual routes fence further search and queue cleanup; fake adapters confirm cleanup. | No real object/provider deletion was attempted. |
| 20. Logout, expiry, account switch | **PASS B + A** | Logout clears private state; failed logout stays closed until retry. Expire application session, sign in as synthetic owner B and directly request owner A’s import/history/frame/media URLs: denied, old results absent. Actual backend also denies saved-result routes and unadmitted pilot access. | No cross-account defect established. |
| 21. Keyboard, loading and narrow layout | **PASS B for exercised controls; FAIL B for heading semantics** | Tab through signup, Enter file chooser/candidate/history controls; loading/confirmation errors are visible. 360px overflow assertion passes. Visible import card titles are not headings; screen-reader speech and physical touch remain gaps. | Low; approved accessibility follow-up. |

## Current customer entry conditions

At inventory time the API, website, import worker, and mockup workflows were all
not started. A preview screenshot attempt at `/` failed with
`ERR_HTTP_RESPONSE_CODE_FAILURE`; there is no live-preview screenshot or
authenticated production observation to claim. No managed service was enabled
for this audit. The gate starts and stops its own disposable web/test processes.

Reachable application routes in the code are `/`, `/auth`, `/auth/action`,
`/imports/:id`, `/demo`, and a not-found screen. A signed-in customer needs
verified private access, rights consent, allowance, and an available worker to
start new work.

Firebase trials and the Replit pilot are separate:

- Email signup is default-off and requires approved Firebase configuration.
  Fixtures simulate it being available. Actual delivery and activation remain
  unverified; this audit did not inspect or change secrets or rollout settings.
- A verified Firebase trial owner can use private imports but cannot open the
  shared pilot proof. An admitted Replit pilot participant can open the proof
  but cannot read someone else's private import.
- The current UI has **no customer upgrade, Checkout, or Customer Portal flow**.
  Backend billing tests are not a successful customer purchase. Exhaustion does
  not offer a payment recovery path.
- Production ingestion/runtime approval remains separate. The documented
  web-only deployment cannot by itself provide durable ingestion.

## Reproduce safely

From the workspace root:

```sh
pnpm run quality:local
```

This is the preferred command. It creates a temporary Unix-socket-only PostgreSQL
cluster, separate runtime/transaction databases, applies migrations only there,
seeds synthetic proof data, rehearses two web restarts, and runs the release
checks. Cleanup removes the cluster on exit. Do not substitute the application
or production database.

The gate unsets real video-provider, Stripe, and Firebase configuration in its
child process, disables provider networking/billing/public trials, and uses
fixture-only identity settings within individual tests. The live App Storage
smoke-test opt-in is explicitly cleared; the direct release check rejects that
opt-in. Managed worker configuration, deployed behavior, and secrets are
unchanged.

To repeat only the intercepted browser walkthrough:

```sh
SCENEIT_TEST_CHROMIUM_EXECUTABLE="$(command -v chromium)" \
  pnpm --filter @workspace/sceneit run test:ui
```

It owns a loopback Vite test server on port 4177; no running app/API is needed.
Service workers are blocked. Unknown APIs and non-allowlisted external requests
are blocked. The only mutation sent outside interception is to an exact,
disposable loopback upload sink owned by the test; it verifies synthetic bytes,
has no persistence, and is closed after each case. Do not remove those guards to
make a failing test pass.

Important files:

- `artifacts/sceneit/tests/complete-customer-journey.spec.ts`
- `artifacts/sceneit/tests/email-trial.spec.ts`
- `artifacts/sceneit/tests/controlled-pilot.spec.ts`
- `artifacts/sceneit/tests/assets/README.md` — synthetic-media recipe
- `artifacts/api-server/tests/test_customer_journey.py`
- `artifacts/api-server/tests/test_contract_emissions.py`
- `artifacts/api-server/tests/test_persistence_postgres.py`
- `scripts/pilot-local-gate.sh`, `scripts/release-check.sh`

## Validation results and reproducibility notes

| Check | Observed result |
| --- | --- |
| Safe local gate | Executed once. Disposable migrations and two graceful Gunicorn restarts passed, retaining seeded saved proof and quota usage 37. Installs, API contract drift checks, typechecks, 12 auth tests and 13 frontend-behavior checks passed. |
| Full Python boundary suite | 248 discovered: 246 passed, one intentional live-storage skip, one setup error in the new fixture. The gate stopped before its browser/build stages; this initial run is **not reported as a clean gate pass**. |
| Corrected connected Flask/PostgreSQL test | Removed a nonexistent mock target, reran only this test in a new disposable cluster: **1 test OK**, with external guards and actual emitted-response contract validation. Other passing backend checks were not repeated. |
| First complete browser pass | 62 cases: 52 accepted by Playwright, 10 fixture/locator/timing failures. Four of the 52 were intentional reproductions of the two existing recovery defects, not successful product behavior. |
| Focused browser repair checks | Only failed/changed titles were rerun. Corrected selectors, navigation races, byte-transfer evidence and refresh-token identity handling. Main connected journey and interrupted-transfer branch now genuinely pass on desktop and mobile viewport. |
| Final unique browser coverage | **66 distinct cases exercised across the initial pass and focused checks: 58 actual passes and 8 expected-failure reproductions** (four defects × two viewports). This is cumulative evidence, not a claim of one clean 66-case run. |
| Final focused run | Eight cases accepted in 45.1 seconds: main journey and interruption passed on both viewports; pending-intent and heading defects reproduced on both. Playwright prints expected failures as “passed”; the scorecard correctly calls the affected product behavior **FAIL**. |
| Builds/static checks | Workspace build, final frontend TypeScript and diff whitespace checks passed. Nonfatal existing build warnings: label sourcemap resolution and a JavaScript chunk above 500 kB. No performance claim. |

The initial account-switch failure was **a fixture error**, not a product finding:
the fake token-refresh response incorrectly minted the previous owner's
identity. Refresh responses now follow the account that actually signed in,
and the test checks the exchanged owner B identity before testing denial.
Application ownership checks were not weakened.

The focused final command was:

```sh
cd artifacts/sceneit
SCENEIT_TEST_CHROMIUM_EXECUTABLE="$(command -v chromium)" \
  pnpm exec playwright test --config playwright.config.ts \
  -g 'repeatable complete private customer journey|interrupted transfer|PRODUCT-INTENT|PRODUCT-A11Y' \
  --output=/tmp/sceneit-journey-final-evidence
```

The actual-route focused invocation was
`uv run --locked python -m unittest tests.test_customer_journey -v`, **only
inside a newly initialized disposable database environment**. Use the safe local
gate for repeatability rather than pointing that command at any application
database. Without the disposable environment, database suites skip; such a skip
does not establish ownership or transaction behavior.

Screenshots below contain only synthetic fixture accounts/media. External
YouTube iframe failure imagery is expected because its transport was blocked;
the separate private MP4 is the media that was actually played.

![Desktop: synthetic MP4 playback, ranked candidates, saved history and the explicit unverified linked-timeline warning.](customer-journey-evidence/ranked-playback-desktop.png)

![Mobile viewport: authorized MP4 fallback and separate source-link instructions, not a physical-phone capture.](customer-journey-evidence/file-fallback-mobile.png)

![Mobile viewport: the same synthetic private playback and stacked result/history layout.](customer-journey-evidence/ranked-playback-mobile.png)

## Confirmed customer defects

### Medium — expired reset recovery opens account creation

**ID: PRODUCT-AUTH-RECOVERY-001. Browser fixture; desktop and mobile viewport.**

Reproduce with a controlled expired reset action at
`/auth/action?mode=resetPassword&oobCode=expired-fixture`, then press **Request a
new reset link**.

Expected: a reset request form, or a clearly identified sign-in form with its
password-recovery control. Observed: navigation to `/auth`, which defaults to
**Create an account**. A customer must find “Already have an account? Sign in,”
then “Forgot password?” to recover. The expired verification action uses the
same plain `/auth` destination by static inspection; its recovery click was not
separately browser-confirmed.

Affected code: `artifacts/sceneit/src/pages/auth-action.tsx` recovery handler and
`artifacts/sceneit/src/pages/auth.tsx` initial mode. Requires approval for a
product fix, not an email-provider configuration change. The regression asserts
the promised recovery destination and is explicitly marked as an expected
failure **after** reaching and clicking the control.

![Desktop fixture: requesting another reset link incorrectly opens Create an account.](customer-journey-evidence/expired-reset-desktop.png)

### Medium — verification loses the intended private-video destination

**ID: PRODUCT-AUTH-RETURN-002. Browser fixture; desktop and mobile viewport.**

Reproduce with a controlled successful verification action carrying
`returnTo=%2Fimports%2Fprivate-intent`, then press **Sign in to continue**.

Expected: the protected destination survives into fresh sign-in, without starting
processing. Observed: `/auth?mode=signin` omits `returnTo`. Ordinary sign-in then
defaults to the home page. The sign-in form itself is usable, but this recovery
route does not preserve the intended private video. This is a direct action-URL
test, **not** proof of how a real Firebase email currently constructs that URL.
Pending source-link restoration is a separate failure described below.

Affected code: `artifacts/sceneit/src/pages/auth-action.tsx`. A later approved
fix must preserve only safe local destinations and must not introduce automatic
processing. The screenshot shows the destination form; the browser URL assertion
establishes the missing return parameter.

![Mobile-viewport fixture: verification reaches sign-in, but the URL has lost the protected return destination.](customer-journey-evidence/verification-return-mobile.png)

### Low — private-card section titles are not semantic headings

**ID: PRODUCT-A11Y-HEADINGS-003. Browser fixture; both viewports.**

The upload and linked-source section labels are visible, but their `CardTitle`
elements are plain divs rather than headings. Browser role-based navigation
could not find “Choose your authorized MP4” or “Linked platform source” as
headings; text-based locators could continue. This does not block mouse/keyboard
operation, but heading navigation for assistive technology is less useful.
Screen-reader speech was not tested. No visual or semantic markup was changed.

### Medium — the pending video link disappears during verification/sign-in

**ID: PRODUCT-INTENT-004. Browser fixture; both viewports.**

On `/`, choose Paste a Link, enter the synthetic YouTube URL, give consent and
choose Sign in to continue. Follow the controlled verification action, then fresh
email sign-in. Expected: the entry URL is restored but processing waits for
explicit consent/action. Observed: the home page returns to the upload tab and
the link is gone. No import is automatically created, which is correct; the lost
intent is not. The connected walkthrough manually re-enters the link to test the
remaining independent stages, rather than silently calling this step a pass.

Relevant code: `lib/replit-auth-web/src/use-auth.ts`,
`artifacts/sceneit/src/pages/auth.tsx`, and
`artifacts/sceneit/src/pages/index.tsx`. Any approved fix must preserve nonprivate
entry intent without weakening account-switch cache clearing or consent.

![Desktop fixture: after verification/sign-in, the original link intent is gone and the upload entry is shown.](customer-journey-evidence/lost-entry-intent-desktop.png)

Resend success is also rendered in both status and toast text. This is a
duplicate-announcement risk, not a confirmed screen-reader failure.

## Separately owned external evidence

These are not dependencies for completing this safe local audit, and were not
repeated:

| Existing work | Still required |
| --- | --- |
| Email delivery, task 17 | Approved project, action domains/templates, designated inboxes, actual verification/reset arrival. |
| Hosted Stripe testing, task 19 | Explicit test-mode approval and hosted Checkout/Portal evidence; no real charges. |
| Real-phone seeking, task 5; regression coverage, task 12 | Physical-device browser playback and segment-seek evidence. |
| Pilot launch, task 14 | Operator approval for deployment, admission, runtime/worker arrangement and activation. |
| Tiered billing, task 21 | Separately approved billing implementation/lifecycle work; this audit neither changes nor certifies it. |
| Signup observation, task 18 | Separately owned analytics/visitor-funnel work, not simulated customer evidence. |

## Evidence limits and remaining gaps

- Provider ingestion, semantic quality, safely retrievable external-link success,
  live storage resumability/revocation, real email delivery, and hosted payments
  remain blocked/not verified.
- The connected backend journey uses a deliberate terminal database transition
  for processing. Existing worker suites corroborate sequencing and cancellation;
  no real provider processes the synthetic MP4.
- Browser file transfer reaches a byte-verifying disposable loopback endpoint,
  not App Storage. Interruption is a simulated failed PUT, not physical
  mobile-network loss.
- Frames are small synthetic fixture images, not evidence of semantic accuracy
  or actual worker extraction. Flask media checks verify access boundaries with
  fixture bytes, not decoding.
- The Replit browser pilot uses a controlled session response; it does not perform
  a real OAuth login. Existing authentication tests cover OIDC boundaries.
- Seven-day retention is not tested by waiting seven days. Expired states and
  disposable persistence checks cover access behavior.
- Long-running polling after its 75-second budget, drag-and-drop multi-file
  rejection, physical touch playback, screen-reader output, and real network
  handoff remain explicit gaps. No broad performance or security audit is claimed.
- Static concerns requiring later confirmation: root import-config/current-import
  read failures have no dedicated retry/error explanation; history failure can
  appear alongside the empty-history message. These are not presented as
  browser-confirmed defects.