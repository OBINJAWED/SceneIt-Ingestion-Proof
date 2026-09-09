# Private import operations

Private imports use `sceneit_imports` and related tables only. They do not
modify, migrate, clean up, re-index, or share storage with the legacy proof.

## Process arrangement

Run the Flask web process and exactly one always-on worker process from the same
release:

```text
web:    python3 -m gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 2 --timeout 180 sceneit.server:app
worker: python3 -m sceneit.import_worker
```

The configured workflow runs the same process as
`pnpm --filter @workspace/api-server run worker`; the package script executes
`python3 -m sceneit.import_worker`. It is a standalone workflow and is not
started by the web server.

The worker holds a PostgreSQL advisory lock, updates its heartbeat, and leases
one job at a time. An autoscaled web-only deployment is insufficient because
work must continue after the browser disconnects. Do not change the production
runtime, deployment, or billing configuration without operator approval. The
development workflow is configured separately by the owning agent.

From the repository root, `pnpm --filter @workspace/api-server run start:with-worker`
is a supervised always-on launch option: it starts both processes and stops its
sibling if either process exits. It is not the active deployment command. Apply
new SQL explicitly with `python3 scripts/migrate-development.py --development`
in development; neither service nor build runs migrations. Production schema
and runtime changes remain separately approved operator work.

`GET /api/imports/config` reports `workerAvailable: true` only when the database
heartbeat is less than 90 seconds old. Alert when it remains false, when a lease
is stale, or when records enter `needs_review`.

## Recovery and provider reconciliation

Every paid/remote mutation stores `provider_write_marker` before transmission.
If a process dies or the response is ambiguous, the worker stops the import in
`needs_review`; it never submits the mutation again automatically. An operator
must use the persisted import ID, index ID, asset ID, marker, and provider
dashboard/read APIs to determine whether the operation exists. Save the
confirmed identifier transactionally, clear the marker, and requeue only after
that determination. If absence cannot be established, leave the record for
review. Search reservations follow the same no-automatic-repurchase rule.

Cancellation deletes indexed assets, source assets, and per-import indexes in
dependency order. A deletion error is `needs_review`, not successful cleanup.
Never erase cumulative usage counters while reconciling or deleting an import.

## Storage and retention

All originals live below `PRIVATE_OBJECT_DIR` and are served only through an
authenticated owner-scoped route. Upload completion pins an immutable object
generation and validates its 15-minute deadline, exact size, content type, and
media contents before provider work. The encrypted resumable-session reference
is server-only. Reselection, cancellation, and expiry revoke that session and
remove unaccepted bytes. Signed upload URLs and private media URLs must never be
logged. File-required/import states inactive for one hour are abandoned. Import
metadata and accepted media expire after seven days; cleanup applies only to
new private-import paths.

For direct-link media, the create-only destination is journaled in Postgres
before upload. A restart reconciles an existing generation by bounded download
and inspection, retries only when the object is confirmed absent, and gives
cleanup enough information to remove a partially completed write.

The 15-minute deadline is an **application acceptance deadline**, not the native
lifetime of the GCS resumable bearer session. GCS owns that longer lifetime;
the application encrypts its session reference and revokes it when abandoned.
Reservations are owner-scoped, origin-bound and exact-size/create-only. Do not
replace this with the sidecar's simple signer, which cannot express all those
upload constraints. Never share, persist in browser storage, or log the bearer
upload URL. Browser-held files do not survive sign-in/reloads: reselect the MP4.
No upload is accepted until its immutable generation/size and media validate.
Uncertain provider operations can require operator cleanup beyond the normal
retention deadline; content access expires on time even during reconciliation.

## Limits and safe logging

Defaults are 200 MB, 4–1200 seconds, three cumulative imports and 50 cumulative
searches per owner, and 30 imports and 500 searches application-wide. Capacity
is reserved transactionally before provider work and is not refunded by
cancellation, deletion, source switching, or failure. Silent videos create
visual-only indexes and reject audio/both searches.

Processing has a cumulative 30-minute deadline. A fenced lease is refreshed
every 20 seconds during long I/O; cancellation or lease loss prevents another
provider action and blocks stale state writes. Read retries are counted
separately from normal provider polling. Readiness requires a supported provider
status, matching source asset identity when the provider supplies it, and a
finite provider duration within one second of the inspected MP4.

The process also verifies its session-level advisory lock through the lock's
own PostgreSQL connection in the main loop, every lease pulse, and before each
provider side effect. Loss terminates the worker. Confirmed provider responses
persist returned IDs even when cancellation won concurrently, while the
`cancel_requested` state remains monotonic so cleanup can delete those IDs.
Provider deletions are themselves marker-fenced; an uncertain deletion remains
`needs_review` with its operation marker.

Owner/fingerprint purchase records survive import cleanup. Confirmed active
fingerprints may reuse their identifiers. Uncertain and deleted fingerprints
are durable tombstones and are never automatically purchased again.

Logs may contain request/import IDs, state transitions, safe error codes, and
durations. Do not log credentials, cookies, CSRF tokens, signed URLs, source
URLs, search text, filenames, extracted metadata payloads, or private media.

Platform extractor upgrades require review of the pinned version, destination
allowlist/DNS protections, redirect limits, maximum bytes, timeout behavior,
and deterministic tests before rollout. YouTube remains link context plus an
authorized MP4 and must never enter a downloader.