---
name: OIDC certificate trust in the test environment
description: Replit's test issuer needs the verified runtime system CA store rather than certifi alone.
---

Use verified runtime system TLS trust for OIDC HTTP requests; never disable
certificate verification to make sign-in tests pass.

**Why:** Replit's testing issuer can depend on CA trust available in the runtime
system store but absent from certifi. This is a trust-store distinction, not a
reason to relax token or certificate verification.

**How to apply:** If a Replit OIDC test fails before redirection with a TLS
connection error, compare verified system trust against the client's default
store. Keep signature, issuer, audience, expiry, nonce, state, and PKCE checks
unchanged.

Verify token issuer equality against the exact discovered identifier after
validating the discovery origin, including a significant trailing slash.

**Why:** The testing issuer's identifier includes a final slash; OIDC issuer
equality is exact, not URL-normalized.

**How to apply:** Preserve the discovered issuer when validating claims. Tests
must not omit the discovery issuer or normalize the token claim.