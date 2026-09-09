# Controlled-pilot verification

## Scope and evidence

Verification used controlled fixtures and disposable PostgreSQL, not live paid
provider calls. These results do not establish YouTube alignment, Safari
compatibility, or real-phone playback.

Completed on 2026-09-09:

- Locked pnpm and uv dependency resolution, TypeScript checks, Python static
  compilation, clean Orval regeneration, and production builds passed.
- The integrated backend run with a separate disposable PostgreSQL database ran 147
  tests successfully. The opt-in live App Storage test was intentionally skipped;
  private delivery, ownership, ranges, and storage failures used controlled
  fixtures. The separate persistence gate passed all 10 checks. Two additional
  graceful-worker-shutdown regressions passed after the restart rehearsal
  exposed the old worker's default SIGTERM cleanup gap.
- Real PostgreSQL coverage included fresh installation, populated legacy
  upgrade, repeated/concurrent migrations, checksum and structural rejection,
  quota races, attempt fencing, resource saturation, and pg_dump/restore.
  Final focused cases verified atomic rejection of an incompatible integrated
  legacy schema and rejection of an ineffective superuser connection cap.
- Two graceful Gunicorn restarts preserved a seeded proof, saved result, and
  cumulative usage of 37. Provider networking was disabled and no provider
  credential was present.
- The desktop/mobile Chromium fixture scenarios cover admission, shared saved history, failed
  reads and still refresh, uncertain paid submissions without retry, logout
  cleanup, and the existing private-import surface. The final recovery fixture
  models a sustained outage until explicit recovery, rather than assuming a
  particular request number despite the application's bounded read retries.
  The earlier 12-scenario gate passed; after the separately merged visual
  refresh, the eight proof/recovery/logout checks passed with updated accessible
  selectors. Admission and filesystem checks passed in the integrated run.
  The private-import smoke checks reached the correct rendered surfaces on
  desktop and mobile but used two obsolete headings; the corrected expectations
  were verified against both captured DOM snapshots, without another browser run.
- Development filesystem access is restricted in both web preview services:
  direct workspace-source and raw-file requests cannot bypass the protected
  media API. The additional HTTP-only fixture checks this boundary without a
  browser or provider call.
- The actual **development** legacy upgrade preserved the original fields of
  one proof and all three saved searches, including identifiers, evidence,
  timestamp matches, and cumulative quota. The comparison hashed data in memory;
  private content was not printed or written to an evidence file.

## Reproduce

Run `pnpm run quality:local` with the checked-in Nix tools available. It creates
two isolated, temporary, Unix-socket-only PostgreSQL databases, removes the
provider credential from its child environment, disables provider networking,
seeds a proof, rehearses restarts, runs the release checks, and removes the
temporary cluster. CI uses the same release/restart scripts with its disposable
PostgreSQL service.

The local browser uses Nix's Chromium wrapper to supply its system libraries;
CI installs Playwright's Chromium. Mobile coverage is viewport/touch emulation,
not a substitute for separately approved device verification.

## Not performed

No production deployment or production database mutation, paid runtime/billing
change, new indexing, live provider search, or live playback certification was
performed. Pilot admission remains fail-closed until an operator configures
approved verified subjects. Production host/proxy settings, a non-superuser
connection-limited runtime database role, private-media availability, and release
approval must be confirmed using [the operations runbook](pilot-operations.md).