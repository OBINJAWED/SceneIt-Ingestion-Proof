---
name: Quota-safe UI validation
description: Preserve finite shared and account allowances while checking SceneIt's interface.
---

Use existing saved results for live, read-only interface checks. Exercise new searches, import creation, upload progress, and permission changes with explicitly intercepted test responses instead of real provider work.

**Why:** SceneIt's demo and private pilot have finite allowances. A visual regression check must not spend an attempt, purchase analysis, or change ownership/media permissions merely to reach a screen.

**How to apply:** Block unhandled mutation requests in UI tests before interacting with forms, and label fixture-based checks as interface verification only. They do not prove provider ingestion, external playback, or timeline alignment.