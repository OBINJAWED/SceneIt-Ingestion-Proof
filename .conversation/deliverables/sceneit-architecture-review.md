# SceneIt — Architecture Review & Refined MVP

**Review date:** 8 September 2026  
**Status:** Proposed architecture, not an implementation or live-system audit.

**Confirmed source availability:** The user confirmed that the initial collection will have original or authorized video files plus YouTube links. The recommended ingestion path does not need to download media from YouTube. File-to-playback timeline alignment still requires verification.

## Executive recommendation

Keep SceneIt focused on one job: describe a moment, find relevant segments in an administrator-curated video collection, and watch or share a selected segment.

Retain Flask, Twelve Labs, and YouTube playback. Replace the local JSON catalog with Postgres, separate indexing from web requests, and make shared scenes immutable records. Do not introduce RunPod, a vector database, microservices, or a frontend-framework migration for this MVP.

The important change to the handoff is conceptual: **a YouTube playback URL is not an ingestion source.** The application needs an authorized media file for indexing and a separately verified YouTube playback mapping.

### Evidence and limits

The uploaded handoff is the source for descriptions of the existing application. I have not inspected its actual repository, run its tests, accessed its Twelve Labs account, or verified that the current app is running. Statements in the handoff about a working homepage are not fresh verification.

Current provider documentation was checked for ingestion, indexing, model configuration, and player behavior. No paid indexing jobs or account changes were made. Proposed defaults below are recommendations, not established product requirements.

## 1. Correct the ingestion assumption first

The handoff treats direct YouTube-URL ingestion as an unfinished implementation. Current Twelve Labs upload guidance instead requires direct raw-media links and explicitly excludes video-hosting pages and cloud-storage sharing pages. A normal YouTube watch URL does not satisfy that requirement. [1]

Twelve Labs also marks the combined upload-and-index `/tasks` method for future removal. New integrations should use an upload followed by a separate indexing operation. The documented API is still v1.3; a version number alone is not sufficient to identify the correct workflow. [2–4]

**Recommended MVP source policy**

- Index files supplied by their owner or another authorized source, preferably from controlled object storage.
- Store the source object reference and revision separately from its YouTube video ID.
- Require the indexed file and the YouTube video to contain the same edit with the same timeline.
- Keep public users search-only. Do not offer unrestricted “paste any YouTube URL and index it.”
- Do not make yt-dlp the default ingestion path. YouTube’s developer policies restrict downloading or storing copies of YouTube audiovisual content without prior written approval; using an original owner-supplied file avoids relying on that download path, but does not remove other rights and platform-policy obligations. [7]

An owner-supplied file can be uploaded to controlled storage without SceneIt downloading it from YouTube. Where storage already exists, Twelve Labs can ingest a provider-accessible raw-media URL. Generate signed URLs just in time, with enough lifetime for ingestion; do not expose them in public responses or logs.

**Remaining validation gate:** prove this path using one authorized source file and its matching, embeddable YouTube video. Source-file availability is confirmed; actual upload/indexing behavior, embedding permission, and timeline alignment remain untested. Arbitrary YouTube-URL-only ingestion is outside the recommended initial scope.

## 2. Use one application, not a distributed platform

```text
Visitor
   |
   v
Flask web application ----------------------> YouTube iframe
   |                                           playback only
   +--> Search service --> Twelve Labs
   |
   +--> Catalog + shared scenes --> Postgres
   |
Admin --> Authenticated ingestion control --> durable job row
                                                |
                                                v
                                      Scheduled indexing command
                                         |              |
                                         v              v
                                  Controlled media   Twelve Labs
                                  source/storage     upload + index
```

All SceneIt code belongs in one repository. The indexing command is a separate execution path from the web server, not a separate microservice.

| Component | Recommendation | Reason |
|---|---|---|
| Web backend | Flask with an application factory and small route modules | The framework is not the prototype’s main weakness |
| Persistence | Shared Postgres, SQLAlchemy, Alembic migrations | Transactional mappings, job recovery, durable scene links |
| Input/output validation | Explicit schemas, such as Pydantic models | Stable API contracts independent of provider responses |
| Frontend | Jinja, modular JavaScript, compiled CSS | Sufficient for search, playback, sharing, and minimal administration |
| Provider integration | One thin Twelve Labs adapter | Isolate SDK shapes and normalize failures |
| Background work | A resumable command advancing jobs from Postgres | Avoid polling inside requests or relying on web-process survival |
| Media storage | Existing controlled storage, or add it only for uploads | Keep media bytes out of the application filesystem |

For a small curated collection, scheduled reconciliation is enough if its delay is acceptable. Each run claims due jobs, performs a bounded step, records the result or next poll time, and exits. If immediate/high-volume ingestion becomes necessary, run the same logic continuously; add a queue broker only when load warrants it.

Suggested internal modules: `search`, `catalog`, `ingestion`, `sharing`, `providers/twelvelabs`, and `player` on the frontend. These are modules, not independently deployed services.

## 3. Make ingestion durable across both asynchronous stages

Current documentation describes two distinct asynchronous operations:

1. Create an asset with `POST /v1.3/assets`; wait until the asset is ready.
2. Index that asset with `POST /v1.3/indexes/{index-id}/indexed-assets`; retain the returned indexed-asset ID and monitor indexing status. [3–4]

SceneIt must therefore distinguish the uploaded **asset ID**, the **indexed-asset ID**, the **index ID**, and the **YouTube ID**. Do not assume similarly named legacy search/video/task fields are interchangeable.

Proposed application states:

```text
queued -> uploading -> asset_ready -> indexing -> ready
   |          |              |           |
   +----------+--------------+-----------+--> failed / needs_review
```

These are SceneIt states; the adapter maps actual provider statuses into them.

**Required correctness rules**

- Create the database job before submitting external work.
- Claim jobs atomically with a lease; never let two workers advance the same job concurrently.
- Deduplicate against the source revision and target index. Use database uniqueness constraints, not “check then insert.”
- Persist each provider identifier immediately after receiving it.
- Store attempts, next-attempt time, deadline, lease expiry, and sanitized error code.
- Use bounded exponential backoff with jitter for retryable reads and honor provider rate-limit guidance.
- Treat timed-out writes as ambiguous: the provider may already have accepted them. Reconcile by returned IDs or supported metadata lookups before retrying; if certainty cannot be recovered, mark `needs_review`. Do not promise exactly-once provider execution.
- Expired local polling deadlines do not prove that provider work stopped. Keep identifiers and reconcile late completion rather than automatically purchasing another indexing attempt.
- Do not hold a database transaction open during a provider network call.

Only catalog entries that are ready, timeline-verified, and eligible for public playback should be searchable in the public app.

## 4. Refine the relational model

The handoff’s three tables are a useful start, but job execution and source revisions need explicit representation.

| Table | Minimum purpose and important fields |
|---|---|
| `videos` | Application ID, canonical YouTube ID, title, known duration, public eligibility, playback availability |
| `media_assets` | Video association, immutable source revision, controlled object reference, provider asset ID when available, source duration, timeline-verification status |
| `provider_indexes` | Provider index ID, model/options, generation, lifecycle; one explicitly selected active search index |
| `index_entries` | Media asset + index, indexed-asset or legacy video reference, readiness and verification state |
| `ingestion_jobs` | Entry association, operation/idempotency key, stage, attempts, lease, next poll, deadline, safe failure details |
| `shared_scenes` | Opaque public ID, frozen playback ID, start/end, optional public query, source/index provenance, creation and revocation timestamps |

Use unique constraints for canonical YouTube IDs, source revision identity, provider index IDs, and `(media_asset_id, provider_index_id)`. Preserve foreign-key integrity.

Do not persist expiring signed media URLs as permanent asset identity. Store a controlled object reference and generate access URLs on demand. For externally supplied links, avoid public arbitrary-URL fetching; a controlled-host allowlist is safer than attempting to sanitize the entire internet.

### Timeline integrity is a release condition

Matching duration is necessary but not sufficient to prove that an original file matches its YouTube edit. Compare several recognizable moments across the timeline. Do not guess offsets or silently accept alternate cuts.

For this MVP, require an unchanged prerecorded video with a verified zero-offset timeline. Defer live/DVR sources and transformed edits. Validate every returned range as finite and satisfying `0 <= start < end <= known duration`; reject material inconsistencies and tolerate only tiny documented rounding differences.

## 5. Keep provider upgrades controlled

The handoff names `marengo2.7`. Current index-creation documentation identifies `marengo3.0` for managed search and states that an index’s model configuration cannot be changed after creation. The broader model catalog also describes Marengo 3.5 for embeddings; that is not evidence that it is a drop-in managed-search index upgrade. [5–6]

Do not choose a model solely because its version number is higher.

**Migration approach**

1. Inspect the real account’s existing index and confirm whether it still supports the required operations.
2. Import actual JSON mapping entries into the catalog without inventing missing titles, durations, asset IDs, or readiness.
3. Preserve legacy provider-video references explicitly; validate the new workflow’s search-result ID mapping with a recorded fixture and a real smoke test.
4. Build a new index generation only if needed, after an indexing-cost estimate and authorized source availability are established.
5. Validate catalog coverage and search quality, then switch the active index atomically.
6. Retain the prior generation for an agreed rollback window; clean it up explicitly, not during ordinary deployment.

Existing shared scenes must not change when the active index changes. Ordinary app startup must never create a fresh index or reindex the catalog.

## 6. Define a small, stable application API

These are proposed **SceneIt endpoints**, not Twelve Labs endpoints.

| Endpoint | Contract |
|---|---|
| `POST /api/v1/search` | Accept a query and optional eligible video filter; return ranked matches |
| `POST /api/v1/scenes` | Accept a server-issued match token and an explicit query-sharing choice; create a frozen scene |
| `GET /scene/{public_id}` | Render the stored scene with server-rendered metadata; never rerun search |
| `POST /api/v1/admin/videos` | Authenticated administrator submits a controlled source reference and YouTube playback mapping; return job ID |
| `GET /api/v1/admin/jobs/{job_id}` | Return sanitized ingestion state and actionable next step |
| `GET /healthz` / `GET /readyz` | Liveness; configuration/database readiness without a paid provider request |

A minimal authenticated admin form is sufficient. A protected command can use the same ingestion service before an admin UI is worth building.

### Search example

```json
{
  "query": "a person walking through the rain",
  "limit": 5
}
```

```json
{
  "request_id": "request-identifier",
  "matches": [
    {
      "video": {
        "id": "catalog-video-id",
        "youtube_id": "YouTubeID01",
        "title": "Example video"
      },
      "start_seconds": 42.1,
      "end_seconds": 48.7,
      "rank": 1,
      "confidence_label": null,
      "match_token": "signed-short-lived-match-snapshot"
    }
  ],
  "partial": false
}
```

Proposed defaults: query length 1–500 characters after trimming, five results by default, maximum ten. Return `200` with an empty match list only for a legitimate no-match outcome. Use documented structured errors for invalid requests, rate limits, unavailable providers, and deadlines.

Do not turn provider failures or an entirely broken mapping into “no matches.” If some otherwise useful provider results are dropped because their catalog mappings are unusable, return the valid results with `partial: true` and log the issue. If no results are usable because of system faults, return an availability error.

Preserve provider ranking. Do not fabricate confidence percentages from rank or convert “high” into “95%.” A numeric provider score is not automatically a probability. Optional provider labels are acceptable when faithfully reported.

The adapter should perform only bounded result over-fetching/filtering to obtain eligible matches; do not turn one search into an unbounded sequence of billable calls.

### Share semantics

The signed match token contains the validated playback snapshot and provenance, preventing clients from substituting arbitrary timestamps or video IDs. Validate its signature, expiry, current catalog eligibility, and timestamp bounds before storing a scene.

Store the scene only when the user chooses Share. Query publication should be explicit and off by default, because a search phrase can contain private information. The public page and its metadata must use the same visibility decision.

Use high-entropy IDs and describe these URLs as **unlisted, not private**. A link can be forwarded. Add `noindex` by default and allow administrator revocation. An unavailable or later-edited YouTube video must display an honest playback limitation; database durability cannot guarantee external media permanence.

## 7. Fix playback without rebuilding the frontend

Keep YouTube controls, keyboard navigation, fullscreen, pause/play, and volume available. Never automatically undo the user’s pause.

The iframe API supports start/end parameters and reports embedding, playback, and autoplay problems. Its documentation also describes keyframe-based start behavior, so promise an approximate scene loop—not frame-accurate editing. [8]

Recommended behavior:

- Show one selected match prominently, with a few alternatives.
- Make looping a visible user-controlled option; provide a Replay scene button.
- Maintain one player instance and one active loop timer; tear both down on replacement.
- Use a request sequence guard and AbortController so an older search cannot replace a newer result.
- Treat browser cancellation as a UI optimization, not proof that a paid upstream request was cancelled.
- Bound iframe initialization and search waits; offer a user-gesture play fallback when autoplay is blocked.
- Handle removed/private videos, embedding-disabled videos, and iframe error 153, which concerns missing referrer or equivalent client identification. [8]
- Provide an ordinary “Open on YouTube” timestamp link outside the player.
- Announce loading/error changes accessibly, respect reduced motion, and report clipboard failures honestly.

Do not obscure YouTube branding or overlay custom controls on its player.

## 8. Minimum production safeguards

These are launch requirements, not a later hardening phase:

- Admin authentication and an explicit admin allowlist; protect cookie-authenticated writes against CSRF.
- Server-side provider credentials; no keys, private media URLs, or raw provider errors in the browser.
- Shared rate and concurrency limits across web instances, plus an application-level provider-usage budget and shutoff.
- Limits on body size, query length, result count, source size, and indexing concurrency.
- Explicit connect/read/overall deadlines. Search retries should fit the latency and cost budget.
- Structured request/job IDs, duration and outcome metrics, and sanitized logs. Avoid logging query text by default.
- Database migrations, backups, and a tested restore path.
- Locked dependencies and a reproducible production asset build.

For low traffic, transactional Postgres counters can implement shared quotas. An in-memory limiter is not a global limiter when the web service has multiple instances. Add Redis only if measurement shows a need.

Observability can start with search success/error rates, latency, job age, provider-call count, and indexing volume. Do not display fabricated dollar-cost precision when provider billing data is unavailable.

## 9. Build in four gated increments

| Increment | Work | Exit condition |
|---|---|---|
| A — Prove ingestion and alignment | One authorized media file, one provider asset/index entry, matching YouTube playback, recorded API responses | Several visual and audio queries return usable, timeline-aligned segments |
| B — Stabilize the current search MVP | Catalog migration, adapter, typed search responses, safe errors, accessible player, quotas | Existing searchable content is preserved; no synthetic confidence; error states are distinguishable |
| C — Make operations and sharing durable | Resumable ingestion, deduplication, admin status, scene snapshots and public pages | Restarting during indexing recovers safely; another browser opens the exact stored segment |
| D — Release validation | Regression evaluation, backup restore, deployment checks, cost controls | Correctness and abuse tests pass; staging smoke test succeeds on authorized media |

Authentication, request limits, and safe error handling enter as soon as their routes exist; they are not deferred until the final increment.

### Essential tests

- Concurrent duplicate submissions create one logical ingestion operation.
- Restart before/after provider acceptance does not blindly resubmit work.
- An expired lease is recoverable; a timed-out job can reconcile late completion.
- Legacy and new provider result IDs both resolve correctly.
- Invalid, nonfinite, reversed, and out-of-range timestamps are rejected.
- Source-edit mismatch prevents a video from becoming publicly ready.
- Index cutover and rollback preserve mappings and existing shared scenes.
- Expired/tampered match tokens and revoked scenes fail safely.
- Opening a shared link performs no new semantic search.
- Repeated searches leave one player and no stale result replacement.
- Pause, keyboard controls, autoplay rejection, unavailable video, and clipboard failure work correctly.
- Quotas remain effective with multiple web instances.

Separate automated mocked-provider tests from a small, opt-in live smoke suite, which can incur provider charges.

Create a compact evaluation set spanning visual actions, spoken content, ambiguous wording, multiple plausible matches, and genuine no-match queries. Measure whether a relevant segment appears in the top few results and whether the returned time range is usable; do not rely on the provider’s confidence label alone.

## 10. What should wait

Defer RunPod/Qwen, self-managed embeddings, a vector database, unrestricted ingestion, full public accounts, collections, recommendation features, and a broad video-library UI.

The new architecture should make future changes possible without paying for all of them now. The provider adapter is a boundary around Twelve Labs—not a speculative universal AI framework.

**Recommended next action:** with authorized source-file availability confirmed, implement a one-video ingestion/alignment spike before committing to a larger rebuild. Success means an asset reaches indexing readiness, several visual/audio queries find usable moments, and the linked YouTube video plays those same moments. This is a recommendation for the next implementation step, not permission to begin a full rebuild.

## Sources

**Handoff:** `Pasted-SceneIt-is-a-prototype-web-application-that-lets-someon_1788915782445.txt`, uploaded in this conversation. Prototype observations come from this document, not fresh repository inspection.

1. [Twelve Labs — Upload content: raw-media URLs and unsupported hosting/sharing pages](https://docs.twelvelabs.io/agents/guides/upload-content)
2. [Twelve Labs — Legacy combined video indexing task and recommended replacement](https://docs.twelvelabs.io/api-reference/upload-content/tasks/create)
3. [Twelve Labs — Create an asset](https://docs.twelvelabs.io/api-reference/upload-files/direct-uploads/create.md)
4. [Twelve Labs — Index an asset](https://docs.twelvelabs.io/v1.3/api-reference/index-content/create)
5. [Twelve Labs — Create an index and immutable model configuration](https://docs.twelvelabs.io/api-reference/indexes/create.md)
6. [Twelve Labs — Marengo model roles](https://docs.twelvelabs.io/docs/concepts/models/marengo)
7. [YouTube — API Services Developer Policies](https://developers.google.com/youtube/terms/developer-policies)
8. [YouTube — IFrame Player API reference](https://developers.google.com/youtube/iframe_api_reference)