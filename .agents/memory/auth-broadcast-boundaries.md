---
name: Authentication broadcast boundaries
description: Why auth recovery must close local cached access independently of cross-tab announcements
---

Treat cross-tab announcements as synchronization, not as the mechanism that
completes the originating tab's session transition.

**Why:** BroadcastChannel excludes the sending channel object, not every
subscriber in the same tab. Multiple hook subscriptions can receive their own
page's announcement and reset a newly exchanged session, losing restored entry
intent. Suppressing that self-echo without explicitly closing the local cached
session has the opposite bug: recovery can briefly republish stale access and
skip the fresh-sign-in form.

**How to apply:** Ignore originating-tab echoes, but finish its local transition
first. On confirmed recovery, cancel stale session reads and publish closed
client access before releasing the signout latch. Keep failed logout fail-closed
and cover both an already-signed-in action page and a second-tab verification.