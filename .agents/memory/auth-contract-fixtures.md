---
name: Authentication contract fixtures
description: Why browser interception alone is insufficient for authentication exchange verification
---

Pair intercepted authentication journeys with an actual application exchange against isolated persistence, validating the emitted response against the API contract.

**Why:** A hand-written browser fixture can return the promised session shape even when the real successful exchange returns only an acknowledgement. The browser then passes while real sign-in fails. Signed-token verification alone also misses this application boundary.

**How to apply:** When changing session exchange or identity state, cover the real route's successful response as well as rejection cases. Keep provider transports fixture-backed and database state disposable; do not use live processing to establish auth correctness.

For recovery journeys, follow the visible control and assert its destination and
usable form, not just the presence of a recovery button. Do not replace that
navigation with a test-side redirect.

**Why:** Earlier isolated action-page checks established that recovery controls
existed, but missed that they could send customers to signup instead of the
promised recovery flow. Hand-directed navigation can hide that failure.

**How to apply:** Treat expected product failures separately from fixture setup
errors; only mark an assertion as an expected failure after the relevant screen
and action have actually been reached.

Identity-provider fixtures must preserve identity through token refresh, not
derive it from the application's current session.

**Why:** A shared fake refresh response can mint the previous owner's identity
after a different account signs in, creating a false cross-account-access
finding even while the application enforces ownership correctly.

**How to apply:** Bind synthetic refresh tokens to the original synthetic
identity and verify the exchanged claims before evaluating account isolation.