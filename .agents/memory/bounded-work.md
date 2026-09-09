---
name: Bounded external work
description: Why HTTP timeouts and expiring leases alone cannot enforce pilot resource ceilings.
---

Treat a lease expiry and a request timeout as accounting boundaries, not proof
that the underlying work stopped. Releasing a permit requires acknowledged
termination, and a child must enforce its own deadline if its parent dies.

**Why:** A slow-drip response can outlive per-read HTTP timeouts. Returning from a
timed-out daemon thread does not prove DNS or transport work stopped, and a
detached media process can survive the web process that was meant to kill it.
Both let repeated timeouts exceed a nominal cross-process limit.

**How to apply:** Keep killable external-work boundaries and independent
parent-death/deadline guards when changing search or media processing. Keep
lease lifetimes above the enforced whole-operation bound, and retain consumed
quota when a paid request may have reached the provider.