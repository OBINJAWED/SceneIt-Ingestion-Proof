---
name: Private upload session constraints
description: Why private uploads use create-only resumable sessions and distinguish app deadlines from storage bearer lifetimes.
---

Treat the application acceptance deadline separately from the storage provider's
resumable-session lifetime. Never describe an application reservation deadline
as a guarantee that its bearer upload URL has expired.

**Why:** Replit's simple App Storage signer could not express exact-size and
create-only generation constraints. GCS resumable initiation supported them in
live checks, but GCS controls its longer bearer-session lifetime. The application
must reject late completion and retain an encrypted reference for revocation.

**How to apply:** Preserve generation pinning, explicit deadline validation, and
session revocation when changing uploads. Test size enforcement and replay
immutability against the real storage service; mocked SDK calls alone cannot
establish those provider guarantees. Never log or save upload URLs as evidence.