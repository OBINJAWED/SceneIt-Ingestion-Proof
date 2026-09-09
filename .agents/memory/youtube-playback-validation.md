---
name: YouTube playback validation
description: Why successful YouTube metadata and player initialization are insufficient evidence of playable, aligned scenes.
---

Treat actual segment playback and visual comparison as the embedding/alignment check—not oEmbed success, a rendered poster, player readiness, or matching durations.

**Why:** During the one-video proof, indexing and metadata checks succeeded and the player rendered a poster, but playback subsequently returned YouTube error 150. A later browser pass played the same edit through both the embedded player and retained watch links. Availability is context-dependent; a past error is not proof of a permanent restriction or a failed Twelve Labs search.

**How to apply:** Keep ingestion readiness separate from playback verification. Describe playback failures in their observed context rather than claiming the video is globally unavailable. Preserve timestamp links and an explicit unverified alignment state. Do not bypass embedding restrictions or expose original media as a fallback without confirming streaming rights.