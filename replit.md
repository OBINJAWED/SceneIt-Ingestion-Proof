# SceneIt — private single-video imports and preserved demo

SceneIt accepts a private authorized MP4 without any external link, or a supported
video link with authorized direct retrieval/file fallback. The original one-video
proof remains a separate public demo with its existing quota, evidence and media
permissions. This is not a full video library or cross-video search product.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the Flask API using the workflow-provided PORT
- `pnpm --filter @workspace/sceneit run dev` — proof viewer; use its managed workflow
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- Python dependencies: `uv sync --locked` (managed Python environment)
- Required secrets: `TWELVE_LABS_API_KEY`, `DATABASE_URL`; never print values
- In `artifacts/api-server`: `python3 -m sceneit.worker run --max-seconds 1200` resumes ingestion without submitting another upload when identifiers exist.
- The worker is operator-only. It is never invoked from web requests.
- New private imports use a separate always-on worker: `pnpm --filter @workspace/api-server run worker`.
- Apply additive **development-only** migrations explicitly with `python3 scripts/migrate-development.py --development`.
- New auth uses verified OIDC + PKCE and PostgreSQL sessions. Files are private App Storage objects; every new media/status/search operation is owner-scoped.
- Development runs web and import worker as separate workflows. `start:with-worker` supplies a supervised production launch option, but is **not enabled**. The current autoscale web-only deployment cannot run durable ingestion; obtain approval before changing runtime, billing, or deployment.
- See `artifacts/api-server/docs/import-operations.md` for limits, retention, extractor controls, and operator reconciliation.
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
- Source stills are derived on demand at each match midpoint. The original MP4 is not exposed by a public download route.
- Postgres-backed job identifiers and previous successful searches survive web restarts.
- Reopening a saved search makes no new provider call. A shared, transactional 50-submission limit bounds this proof's search use.
- Search phrases are stored in the shared proof; do not enter private information.
- The approved source material is an original/authorized file plus its matching YouTube link. No YouTube downloader is part of the ingestion pipeline.
- New uploads and accounts are separate private-import functionality; no anonymous uploads, general video library, RunPod, public sharing, or full shared-scene system.

## User preferences

The user approved a one-video proof before a broader rebuild, and supplied the source file and matching playback URL for this purpose.

## Gotchas

- Upload asset ID and indexed-asset ID are different identities. Search `video_id` resolves to the indexed asset, never blindly to the uploaded asset.
- Run OpenAPI codegen after every contract change; generated files are not hand-edited.
- Application startup does not create indexes, upload media, migrate tables, or execute search.
- `needs_review` halts ingestion rather than silently repurchasing work. Inspect the saved provider identifiers before operator recovery.
- An interrupted search remains recorded; its provider result is not assumed absent. Use the evidence report and operator inspection rather than automatic resubmission.
- Source still extraction depends on the uploaded workspace file and ffmpeg. A future multi-video production system should move authorized media to controlled persistent object storage.
- The original empty Express scaffold remains unused; the registered API workflow now serves Flask.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
