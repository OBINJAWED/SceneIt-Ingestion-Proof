# Private import verification

## Reproducible free checks

- `pnpm --filter @workspace/api-server test`: deterministic Python boundaries and
  isolated PostgreSQL transactions. The database suite creates and removes only
  a randomly named test schema, never the public proof or real owner counters.
- `pnpm --filter @workspace/api-spec run codegen`: regenerate the OpenAPI client
  and Zod contracts, then check shared TypeScript projects.
- `pnpm run typecheck`: Python compilation and workspace TypeScript checks.
- `PORT=24046 BASE_PATH=/ pnpm --filter @workspace/sceneit build`: local build
  check using the artifact's configured development values, not a deployment.
- Optional: from `artifacts/api-server`,
  `SCENEIT_LIVE_STORAGE_TEST=1 python3 -m unittest tests.test_auth_storage.LivePrivateStorageTests`.
  This creates and removes only tiny synthetic private-storage objects. It
  performs no Twelve Labs calls and never prints bearer URLs.

The free suite covers real signed JWT verification, CSRF, owner isolation,
immutable reservations, malformed cookies/URLs/media, silent visual indexing,
quota/idempotency races, cancellation during confirmed responses, virtual
long-upload lease renewal, uncertain provider writes, provider identity/duration
checks, fingerprint reuse/tombstones, expiry access, and preserved proof routes.
Provider/network behavior in these tests is mocked, not evidence of live
retrieval from external platforms.

## Real storage evidence

A tiny synthetic App Storage round trip passed: exact-size upload, oversized
rejection, replay immutability, generation pinning, 206 and 416 byte ranges,
bounded download, and generation-matched deletion. No proof assets were used.

## Live development journey

Verified through the UI with signed test-issuer OIDC claims (no credential
entry or application auth bypass), on desktop and mobile 390×844:

- The pre-sign-in link returned intact, while rights remained unchecked.
- A generated, rights-cleared 12-second H.264 silent MP4 uploaded with no link,
  validated, indexed and became visually searchable.
- One real visual search returned one finite 0–12-second candidate. Reopening
  history did not spend another search.
- The same file through YouTube link-plus-file reused the identical provider
  index, source asset and indexed-asset identifiers. Only one indexing resource
  and one search were created for these checks.
- Owner media requests returned 200/full and 206/range. Anonymous media and
  history returned 401. Playback authorization could be revoked and re-enabled.
- Logout worked; private analysis required sign-in again. New entry and analysis
  layouts had no horizontal overflow at the mobile viewport.
- The legacy demo remained ready with its original three saved searches and
  3/50 usage; no new proof search or indexing was performed.

After verification, both synthetic imports were cancelled through the durable
cleanup queue and the test session was revoked. Cumulative pilot usage remains
two import attempts and one search; cleanup did not reset the allowance. Normal
Replit sign-in was restored. The final free suite ran 68 tests successfully with
the one opt-in storage test skipped (its live round trip was run separately).

**Playback verification limitation:** the automated Chromium runtime reports no
H.264 codec support. Native playback and a temporary Blob URL both failed with
`DEMUXER_ERROR_NO_SUPPORTED_STREAMS`. Owner-fetched full bytes exactly matched
the local synthetic file's SHA-256 and length, and the native media request
returned a correct full-range 206. This is not evidence of successful decoding
or seeking. The UI now detects missing H.264 support and offers a clear supported
browser/private-download path rather than presenting working seek controls.
Actual H.264 playback on physical devices remains part of the separate
real-phone verification work; do not label this browser run as playback proof.

## External limitations

- No YouTube downloader is invoked. Watch, Shorts and short links are source
  context only; an authorized MP4 supplies indexable media.
- Explicit live/collection paths are rejected. An ordinary YouTube watch URL's
  current live/upcoming status is not verified by public oEmbed, so the app does
  not claim to have verified it. No live stream is ingested from that link.
- TikTok, X/Twitter and Vimeo use a pinned, isolated extractor and bounded
  public-destination progressive-MP4 retrieval. Transport-policy compatibility is
  tested deterministically. Live downloads from arbitrary third-party accounts
  were not authorized as test fixtures. Platform restrictions, split-only media,
  expired media URLs and unsupported delivery may require file fallback.
- An official player, source metadata or matching durations cannot establish
  that an uploaded file is the same edit as a supplied platform link.
- Mobile browser emulation is not evidence of behavior on a physical iPhone or
  Android device; real-phone verification belongs to the separate playback work.

No production deployment, production migration, billing change, or re-indexing
of the original proof is part of these checks.