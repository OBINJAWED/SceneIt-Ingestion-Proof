# Controlled pilot operations

This runbook is for the shared, single-video SceneIt proof and its owner-scoped
private imports. It does not authorize a deployment, paid provider request,
worker/runtime change, or billing change. The web process never migrates the
database, indexes media, or initializes a provider.

## Required configuration

Set secrets in the platform secret store, never in shell history or logs:

- `DATABASE_URL` and `SESSION_SECRET`
- `TWELVE_LABS_API_KEY` is required to submit provider search/index work, but
  intentionally is not required or contacted by liveness/readiness.
- OIDC settings required by the existing Replit sign-in integration
- `PRIVATE_OBJECT_DIR` for controlled persistent media and cached stills

Set and review these non-secret controls:

- `PILOT_ALLOWED_SUBJECTS`: comma-separated, exact, case-sensitive verified
  OIDC subject IDs. Empty means nobody is admitted. Add or remove a subject,
  restart web processes, then verify admission with that account. Sign-in alone
  is not admission. The proof history and its 50-search budget are shared by
  every admitted participant.
- `TRUSTED_HOSTS`: comma-separated externally valid hostnames. Do not use `*`.
- `TRUST_PROXY_HOPS`: `0` without a trusted proxy, otherwise the exact number
  of trusted proxy hops (maximum 2). Never derive this from a client header.
- `SCENEIT_FORWARDED_ALLOW_IPS`: comma-separated proxy source addresses trusted
  by Gunicorn. Keep the default loopback value unless the managed proxy source
  range is known. Both this and `TRUST_PROXY_HOPS` must match the route.
- Resource defaults: 12 database permits, two shared search permits, two media
  permits, one import worker permit, 240-second permit leases, a 180-second
  maximum operation bound, and 30 participant requests/minute; database connect
  5s, statement/idle-transaction 15s, and lock 3s. A permit lease must remain
  longer than the operation bound. Reducing a deadline requires a concurrency
  and failure-mode review.
- A production database role with an operator-managed PostgreSQL
  `CONNECTION LIMIT` is a release prerequisite. Size it for the approved web
  and worker arrangement and keep it consistent with the 12 application
  permits. The application does not alter role attributes.

Gunicorn permits 45-second bounded provider calls, gives an uncertain search a
75-second recovery boundary, uses a 90-second total timeout, and allows 90
seconds for graceful shutdown. Access logs contain method, status, duration,
and request ID only—not paths, queries, addresses, cookies, signed URLs, or
bodies.

The existing private worker handles SIGTERM/SIGINT by finishing its current
bounded cycle, stopping new cycles, and unwinding its client, advisory lock and
resource permit. Pause admission and wait for active work before a release.
A hard-killed worker cannot run cleanup: allow its remaining resource lease
(up to 240 seconds with defaults) to expire before restarting it. Do not delete
an unexpired permit merely to make a restart succeed; it is not proof that
external work has stopped.

## Release and upgrade

1. Freeze writes and pause the import worker. Record the release identifier,
   database target, current readiness, review-required count, proof search
   usage, and active imports without recording user queries.
2. Create and verify an encrypted database backup and confirm persistent object
   storage is readable. Record object generation/version metadata where the
   storage product supports it.
3. Restore that backup into a disposable PostgreSQL database. Run migration
   **status**, then the reviewed explicit **upgrade**, then status again. Run
   upgrade a second time to prove idempotence. Never run DDL from build,
   Gunicorn startup, worker startup, or a post-deploy health hook.
   From `artifacts/api-server`, the commands are
   `uv run --locked python -m sceneit.migrate status` and
   `uv run --locked python -m sceneit.migrate upgrade --operator-approved`.
4. Against the disposable database and controlled provider fixtures, run
   `pnpm run rehearse:restart` and `pnpm run release:check`. The CI PostgreSQL
   service needs no local Docker and must not have a real provider credential.
   `release:check` requires `DATABASE_URL` for the migrated rehearsal database
   and a different `SCENEIT_TEST_DATABASE_URL` whose database name starts with
   `sceneit_test`; it fails instead of reporting success with PostgreSQL tests
   skipped. It also runs frontend behavior tests and both desktop/mobile
   Playwright fixture projects.
   The restart script refuses to run unless provider networking is disabled and
   hashes persistent records before and after two graceful Gunicorn starts.
   Confirm the controlled persistence test also preserves proof IDs, saved
   matches, attempt states, evidence, private imports, and cumulative quota
   while reopening a saved result makes zero provider calls.
   On a local machine with PostgreSQL server/client binaries available,
   `pnpm run quality:local` performs the complete gate in one command. It starts
   a temporary trust-authenticated cluster on a Unix socket only, creates
   separate runtime and transaction databases, migrates and seeds a saved proof
   at quota usage 37, rehearses restarts, and removes the cluster on exit.
5. Review contract drift, route/static rewrite ownership, `/api/healthz`
   liveness, and bounded `/api/readyz` database/schema readiness. Readiness must
   not contact Twelve Labs or external playback.
6. Obtain named operator approval for the schema upgrade and deployment.
   Obtain separate approval for any runtime, worker, autoscaling, billing, or
   provider-spend change. `start:with-worker` remains opt-in and is not suitable
   for the current autoscale web-only deployment without that approval.
7. Apply the exact reviewed migration command as a one-shot operator action,
   deploy web processes, check readiness, then resume the separately managed
   worker. Do not send a test paid search merely to check health.

The repository's `scripts/migrate-development.py --development --status` and
`--development --upgrade` commands remain limited to development/disposable
databases and refuse deployment. Production operators use the explicit module
commands above; upgrade fails unless `--operator-approved` is present. The
post-merge hook is allowed to run the explicit development upgrade, but no web
or worker startup, build, readiness probe, or production hook runs migrations.

## Uncertain work and reconciliation

A search deadline is a fencing and review boundary, not a retry timer. After 75
seconds, the next bounded status read or an import-worker idle tick moves
expired running work to its persisted review-required state. If the worker is
busy or unavailable, run the explicit operator reconciliation command below.
Preserve its attempt ID and consumed quota. Never automatically resubmit,
refund, change the query, or infer failure from a timeout.

From `artifacts/api-server`, the privacy-safe operator commands are:

```sh
uv run --locked python -m sceneit.worker search-operations status
uv run --locked python -m sceneit.worker search-operations status --id SEARCH_UUID
uv run --locked python -m sceneit.worker search-operations reconcile
uv run --locked python -m sceneit.worker search-operations resolve \
  --id SEARCH_UUID --resolution review_retained
uv run --locked python -m sceneit.worker search-operations resolve \
  --id SEARCH_UUID --resolution confirmed_failed
```

`status` omits the search text. `reconcile` only fences expired local attempts;
it does not call Twelve Labs. Twelve Labs does not provide a saved
provider-side identifier for each search submission, so do not claim that a
timed-out search can be looked up there. Use `confirmed_failed` only with
definitive external evidence; otherwise retain review and its quota.

For each review item:

1. Record only the local search ID, attempt ID, timestamps, and state. Do not
   copy the query, raw payload, signed URL, token, or cookie into tickets or
   logs.
2. If definitive failure can be proven outside SceneIt, use the explicit
   confirmed-failed resolution. Otherwise retain review-required and its quota
   reservation. Neither resolution submits another search.
3. Verify late responses are fenced and cannot overwrite the operator
   resolution. Browser refresh/status reads are safe; search submission is
   never an automatic recovery action.

Apply the same rule to imports marked `needs_review` or
`provider_write_uncertain`. The operator worker command may resume only when a
saved provider identity makes the operation unambiguous.

## Media-worker bounds

- Twelve Labs search calls have a true 45-second wall deadline in a child
  process with a parent-death guard.
- Worker-produced proof stills run only while the import worker is idle:
  `prepare_frames` kills its child at 120 seconds under a 150-second shared
  media permit. A failed preparation records a five-minute database cooldown.
- Private-import frame extraction kills its child at 60 seconds under a
  75-second shared media permit.
- Original-proof HTTP frame routes only read cached persistent objects. They
  do not launch ffmpeg, depend on a workspace source file, or call the provider.
  Private-import frame routes retain the existing bounded extraction behavior
  above, using private persistent source media rather than workspace files.

## Backup, restore, and recovery

Use platform-managed encrypted backups when available. A portable rehearsal is:

```sh
pg_dump --format=custom --no-owner --no-acl "$DATABASE_URL" > sceneit.dump
createdb sceneit_restore_rehearsal
pg_restore --exit-on-error --no-owner --no-acl \
  --dbname=sceneit_restore_rehearsal sceneit.dump
```

Use secret-store URLs rather than embedding credentials in these commands.
Restrict and delete local dump files after verification. Restore private object
storage to a non-public rehearsal prefix as well; database restoration alone
does not restore media or cached stills. Verify row counts, schema status,
proof/search quota, saved evidence, import ownership, object readability, and a
restart before declaring the backup usable.

Prefer forward repair after an additive migration has run or new-format data
has been written. Roll application code back only when the old version is
schema-compatible and cannot overwrite newer state. Never down-migrate or
delete proof evidence during incident response. If compatibility is uncertain,
stop writes and the worker, retain the backup and review items, and ship a
reviewed additive repair.

## Release approval record

The approver must record: commit/release ID; lockfile checks; generated-contract
check; unit/HTTP/config/provider fixture results; disposable PostgreSQL
migration, concurrency, backup/restore, and restart results; desktop/mobile
fixture smoke results; unresolved review items; capacity limits; and explicit
go/no-go decisions. Fixture evidence must be labeled as fixture evidence.
The CI mobile project is a Chromium mobile viewport/touch fixture, not a claim
of Safari or real-phone verification.
Live paid-provider validation is a separate, pre-approved activity and is never
part of CI or the default release gate.