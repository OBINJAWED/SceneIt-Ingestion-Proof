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