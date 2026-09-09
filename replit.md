# SceneIt — private single-video imports and preserved demo

SceneIt accepts a private authorized MP4 without any external link, or a supported
video link with authorized direct retrieval/file fallback. The original one-video
proof remains shared among explicitly admitted pilot participants, with its
existing quota, evidence and media permissions. This is not a full video library
or cross-video search product.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the Flask API using the workflow-provided PORT
- `pnpm --filter @workspace/sceneit run dev` — proof viewer; use its managed workflow
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm run release:check` — locked, provider-free release quality gate
- `pnpm run quality:local` — one-command isolated local gate using a temporary Unix-socket PostgreSQL cluster and controlled saved-proof fixture
- `pnpm run check:contract-drift` — regenerate and compare generated trees to their exact pre-run contents
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- Python dependencies: `uv sync --locked` (managed Python environment)
- Required database secret: `DATABASE_URL`; `TWELVE_LABS_API_KEY` is required
  only for approved provider work and is neither required nor contacted by
  liveness/readiness. Never print secret values.
- In `artifacts/api-server`: `python3 -m sceneit.worker run --max-seconds 1200` resumes ingestion without submitting another upload when identifiers exist.
- In `artifacts/api-server`: `python3 -m sceneit.worker refresh-youtube-metadata` performs a fresh oEmbed check of the preserved demo's current YouTube ID. Exit 0 means metadata resolved; 1 means the failed check was saved as unverified; 2 means the link changed or the proof disappeared and nothing was saved. Failures clear a previous metadata pass. This never uploads/reindexes media or checks playback/alignment; it preserves unrelated evidence. Legacy metadata remains unverified until a fresh check binds it to the checked ID.
- The worker is operator-only. It is never invoked from web requests.
- New private imports use a separate always-on worker: `pnpm --filter @workspace/api-server run worker`.
- Inspect/apply development migrations explicitly with `python3 scripts/migrate-development.py --development --status` or `--development --upgrade`.
- The post-merge hook may run that explicit development upgrade only; web/worker startup and production builds never migrate.
- Production migration is a one-shot operator action from `artifacts/api-server`: `uv run --locked python -m sceneit.migrate status`, then `uv run --locked python -m sceneit.migrate upgrade --operator-approved`. It is never part of startup/build.
- New auth uses verified OIDC + PKCE and PostgreSQL sessions. Files are private App Storage objects; every new media/status/search operation is owner-scoped.
- Development runs web and import worker as separate workflows. `start:with-worker` supplies a supervised production launch option, but is **not enabled**. The current autoscale web-only deployment cannot run durable ingestion; obtain approval before changing runtime, billing, or deployment.
- See `artifacts/api-server/docs/import-operations.md` for limits, retention, extractor controls, and operator reconciliation.
- See `artifacts/api-server/docs/pilot-operations.md` for admission/proxy settings, release approval, uncertain-search reconciliation, backup/restore, and rollback/forward-repair.
- See `artifacts/api-server/docs/billing-operations.md` for disabled-by-default billing configuration, payment policy, usage windows, operator stops, reconciliation, and separately approved activation.
- Fixture and development-upgrade evidence is recorded in `artifacts/api-server/docs/pilot-verification.md`; it is not live provider or real-phone playback certification.
- Development DDL lives in `artifacts/api-server/sceneit/schema.sql`. Do not run the unused Drizzle schema push against these Python-owned tables. Do not apply schema changes on application startup or in a production build.

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Flask + Gunicorn, with httpx provider adapter
- DB: PostgreSQL + psycopg, explicit development SQL schema
- Backend validation: Pydantic; frontend types generated from OpenAPI
- API codegen: Orval (from OpenAPI spec)
- Viewer: React + Vite, compiled Tailwind

## Where things live

- `artifacts/api-server/sceneit/`: provider adapter, resumable worker, database access, search service, Flask routes.
- `artifacts/sceneit/`: small new proof viewer; no original SceneIt frontend source was supplied to migrate.
- `lib/api-spec/openapi.yaml`: application API contract; keep title `Api`.
- `pyproject.toml` and `uv.lock`: Python runtime dependencies.
- `.local/conversation-workspace/files/deliverables/sceneit-architecture-review.md`: original proposed rebuild architecture, broader than this proof's scope.

## Architecture decisions

- Keep Flask for application logic. **Why:** the approved review rejected a backend framework rewrite as unrelated to the ingestion risk.
- The new React viewer is a narrow proof surface, not a migration of the original product UI. **Why:** the supplied material was a handoff document, not the old Flask/Jinja source.
- Index the authorized file and keep its YouTube ID separate. **Why:** a YouTube watch page is not a supported raw-media ingestion URL.
- Do not auto-retry ambiguous provider writes. **Why:** an accepted upload/index operation may already exist even when the client lost its response.
- Source/provider duration agreement is not YouTube timeline verification. **Why:** alternate edits can have equal duration. Keep the alignment check unverified until the actual edits are compared.

## Product

- Main entry: authorized standalone MP4 or YouTube/X/TikTok/Vimeo link. YouTube is context plus an authorized MP4, never downloading. Other platforms attempt only safely retrievable direct MP4s, otherwise request an authorized file.
- The preserved demo uses one fixed source video, real visual/audio semantic search, provider-ranked timestamp matches, YouTube playback and optional approximate loops.
- Source stills are cached, worker-produced objects in controlled persistent storage; requests do not spawn extraction. The original MP4 is not exposed by a public download route.
- Postgres-backed job identifiers and previous successful searches survive web restarts.
- Reopening a saved search makes no new provider call. A shared, transactional 50-submission limit bounds this proof's search use.
- Search phrases are stored in proof history shared among admitted pilot participants; do not enter private information.
- The approved source material is an original/authorized file plus its matching YouTube link. No YouTube downloader is part of the ingestion pipeline.
- New uploads and accounts are separate private-import functionality; no anonymous uploads, general video library, RunPod, public sharing, or full shared-scene system.

## User preferences

The user approved a one-video proof before a broader rebuild, and supplied the source file and matching playback URL for this purpose.

## Gotchas

- Upload asset ID and indexed-asset ID are different identities. Search `video_id` resolves to the indexed asset, never blindly to the uploaded asset.
- Run OpenAPI codegen after every contract change; generated files are not hand-edited.
- Application startup does not create indexes, upload media, migrate tables, or execute search.
- `python3 -m sceneit.worker search-operations status|reconcile|resolve` is the explicit privacy-safe search recovery interface; timed-out searches have no provider-side per-search identifier and are never blindly retried.
- `needs_review` halts ingestion rather than silently repurchasing work. Inspect the saved provider identifiers before operator recovery.
- An interrupted search remains recorded; its provider result is not assumed absent. Use the evidence report and operator inspection rather than automatic resubmission.
- Media inspection and still production require `ffmpeg`/`ffprobe`; serving proof frames must use controlled persistent cached objects, not a workspace-only source file.
- Pilot access fails closed unless `PILOT_ALLOWED_SUBJECTS` is configured. `TRUSTED_HOSTS`, `TRUST_PROXY_HOPS`, and Gunicorn's `SCENEIT_FORWARDED_ALLOW_IPS` must match the managed route.
- The original empty Express scaffold remains unused; the registered API workflow now serves Flask.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
