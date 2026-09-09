# Saved-scene YouTube alignment review

## Status: verified for four representative saved scenes

Observed on 2026-09-09 in the automated desktop browser (1280 × 720).
This is sampled cross-edit evidence, not a frame-by-frame identity certificate.

- Proof: `resident-evil-proof`
- YouTube edit: `vLqagjJAvU8`
- Indexed source SHA-256: `b8e6dc56dceb9b9b1fff94eb1e7ea0152e94f33d917a50c2b006dd6cb7a80834`
- Saved searches before and after the pass: 3; quota remained 3 / 50.
- No searches were submitted, source permissions changed, or YouTube media downloaded.

## Playback and source evidence actually observed

The retained watch links below rendered moving footage. Source frames were
extracted from the authorized original at multiple points inside each saved
interval, and the same distinctive scene sequences appeared in YouTube playback
at the retained timestamps. Elapsed page-load time was not treated as an offset.

| Saved match | Indexed-source interval (seconds) | Retained YouTube link | Observed YouTube playback | Visual observations |
| --- | --- | --- | --- | --- |
| Sunglasses query, rank 1 (`99aeb21a-0398-4e6a-a55d-6d258db9f1f2`) | 101.800003–106.000000 | https://www.youtube.com/watch?v=vLqagjJAvU8&t=101s | Clock advanced from 1:41 to 1:46 | The source and YouTube showed the same sunglasses/character sequence at the retained time. |
| Jill-sandwich query, rank 1 (`c60b4ba8-be99-41e9-a06e-e5f0514d8753`) | 195.399994–207.533340 | https://www.youtube.com/watch?v=vLqagjJAvU8&t=195s | Playback began at 3:15 and advanced through the saved interval | The Barry/Jill doorway sequence appeared in both; subsequent Jill footage remained within the same sequence. |
| Dancing query, rank 1 (`a8c8e4d0-4b3b-4a56-87ed-637a96241d3e`) | 334.733337–338.799988 | https://www.youtube.com/watch?v=vLqagjJAvU8&t=334s | Playback began at 5:34 | Both showed the same group dancing on a nighttime street, including the woman in red. |
| Jill-sandwich query, rank 5 (`c60b4ba8-be99-41e9-a06e-e5f0514d8753`) | 520.799988–527.066650 | https://www.youtube.com/watch?v=vLqagjJAvU8&t=520s | Playback began at 8:40 and advanced through 8:47 | Both entered the same “2” bumper and Leon/facial-animation lower-third sequence. |

The embedded SceneIt YouTube player also rendered changing footage and advanced
from approximately 1:41 to 1:49. This was YouTube playback, not original-source
playback. The earlier persisted automated-preview observation of error 150 is
historical; it did not recur during this pass and does not establish a permanent
restriction.

## Original-source playback authorization

The owner explicitly confirmed the rights and permitted streaming to anyone with
app access. The authorized original was published to controlled object storage;
the proof reports source playback available, and a byte-range request returned
HTTP 206. No public download route or YouTube downloader was added.

## Conclusion and completion requirement

- **Same timeline:** observed across all four representative saved scenes.
- **Stable offset:** none observed; apply no offset.
- **Edit divergence:** none observed in the sampled moments.
- **Proof alignment status:** `verified`, explicitly limited to the four samples.

Fresh static captures of the exact watch links sometimes showed YouTube's common
paused title card despite the requested clock. They were retained as timestamp
evidence but were not used as scene-match evidence. The conclusion relies on
moving YouTube playback plus the corresponding source sequence.

## Retained artifacts

- `screenshots/alignment-source-contact-sheet.jpg` — source frames sampled inside all four intervals.
- `screenshots/youtube-alignment-101.png`
- `screenshots/youtube-alignment-195.png`
- `screenshots/youtube-alignment-334.png`
- `screenshots/youtube-alignment-520.png`
- `screenshots/sceneit-alignment-verified.jpg` — final proof UI and paired-playback controls.