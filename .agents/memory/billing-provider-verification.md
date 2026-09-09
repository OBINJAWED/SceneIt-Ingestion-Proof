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