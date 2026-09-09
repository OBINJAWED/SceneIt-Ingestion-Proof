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

A managed workflow restart can leave a persisted worker permit even after the
old process and its database lock connection have gone.

**Why:** Workflow termination is not guaranteed to let the worker's `finally`
cleanup finish. A replacement may correctly report exhausted capacity until
the existing lease expires; this is not evidence that limits need increasing.

**How to apply:** Check for surviving workers and independently bounded children.
Do not clear a permit merely to make the workflow green; allow normal expiry
and retain uncertain operation reservations.