---
name: Authentication contract fixtures
description: Why browser interception alone is insufficient for authentication exchange verification
---

Pair intercepted authentication journeys with an actual application exchange against isolated persistence, validating the emitted response against the API contract.

**Why:** A hand-written browser fixture can return the promised session shape even when the real successful exchange returns only an acknowledgement. The browser then passes while real sign-in fails. Signed-token verification alone also misses this application boundary.

**How to apply:** When changing session exchange or identity state, cover the real route's successful response as well as rejection cases. Keep provider transports fixture-backed and database state disposable; do not use live processing to establish auth correctness.