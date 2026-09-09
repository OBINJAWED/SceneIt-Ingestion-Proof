"""Free, deterministic checks; these never call Twelve Labs or object storage."""
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from pydantic import ValidationError

from sceneit.proof import ProofError, SceneQuery, alignment_evidence, normalize_matches
from sceneit.worker import publish_source, run_step
from sceneit.storage import parse_object_path, safe_content_range
from sceneit.server import app
from copy import deepcopy


class SearchBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.proof = {
            "indexed_asset_id": "indexed-video", "asset_id": "uploaded-file",
            "media": {"duration": 100}, "youtube_id": "vLqagjJAvU8",
        }

    def test_empty_overlong_and_unknown_modality_rejected(self):
        for value in (
            {"query": "   "}, {"query": "x" * 501},
            {"query": "A scene", "modality": "unsupported"},
            {"query": "A scene", "index_id": "another-video"},
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                SceneQuery.model_validate(value)

    def test_provider_labels_preserved_without_synthetic_scores(self):
        matches, partial = normalize_matches(
            {"data": [{"video_id": "indexed-video", "start": 20, "end": 25, "confidence": "high"}]},
            self.proof, "search",
        )
        self.assertFalse(partial)
        self.assertEqual(matches[0]["confidenceLabel"], "high")
        self.assertEqual(matches[0]["startSeconds"], 20)
        self.assertNotIn("score", matches[0])

    def test_source_asset_is_not_a_search_video_id(self):
        with self.assertRaises(ProofError):
            normalize_matches(
                {"data": [{"video_id": "uploaded-file", "start": 20, "end": 25}]},
                self.proof, "search",
            )

    def test_invalid_ranges_never_reach_playback(self):
        for start, end in ((-1, 4), (10, 9), (90, 101), (float("nan"), 20), (True, 5)):
            with self.subTest(start=start, end=end), self.assertRaises(ProofError):
                normalize_matches(
                    {"data": [{"video_id": "indexed-video", "start": start, "end": end}]},
                    self.proof, "search",
                )

    def test_source_playback_accepts_only_single_byte_ranges(self):
        self.assertEqual(safe_content_range("bytes=0-999"), "bytes=0-999")
        self.assertEqual(safe_content_range("bytes=1000-"), "bytes=1000-")
        self.assertIsNone(safe_content_range("bytes=0-1,5-9"))
        self.assertIsNone(safe_content_range("bytes=999-0"))
        self.assertIsNone(safe_content_range("bytes=-"))
        self.assertIsNone(safe_content_range("items=0-1"))

    def test_private_object_path_parser_rejects_incomplete_paths(self):
        self.assertEqual(parse_object_path("/bucket/private/file.mp4"), ("bucket", "private/file.mp4"))
        for path in ("", "/", "/bucket"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                parse_object_path(path)

    def test_empty_provider_results_remain_empty(self):
        self.assertEqual(normalize_matches({"data": []}, self.proof, "search"), ([], False))

    def test_ambiguous_upload_or_index_is_not_submitted_again(self):
        for state, asset_id in (("uploading", None), ("indexing", "uploaded-file")):
            proof = {
                "state": state, "index_id": "index", "asset_id": asset_id,
                "indexed_asset_id": None,
            }
            client = Mock()
            with self.subTest(state=state), patch("sceneit.worker.get_proof", return_value=proof), \
                    patch("sceneit.worker.update_proof") as update:
                self.assertTrue(run_step(client))
                client.upload_asset.assert_not_called()
                client.index_asset.assert_not_called()
                self.assertEqual(update.call_args.kwargs["state"], "needs_review")

    def test_source_publication_requires_explicit_permission(self):
        with self.assertRaises(RuntimeError):
            publish_source(False, "public-app-viewers")

class AlignmentEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.proof = {
            "id": "test-proof", "title": "Test proof",
            "source_sha256": "a" * 64, "youtube_id": "vLqagjJAvU8",
            "state": "ready", "message": "Ready", "asset_id": "uploaded-file",
            "provider_duration": 100, "searches_used": 0, "search_limit": 50,
            "updated_at": datetime(2026, 9, 9, tzinfo=timezone.utc),
            "media": {
                "duration": 100, "width": 1280, "height": 720,
                "size": 1000, "hasAudio": True,
                "alignmentObservation": {
                    "status": "verified", "sampleCount": 4,
                    "sourceSha256": "a" * 64, "youtubeVideoId": "vLqagjJAvU8",
                },
            },
        }

    def evidence(self):
        return alignment_evidence(
            self.proof["media"], source_sha256=self.proof.get("source_sha256"),
            youtube_id=self.proof.get("youtube_id"),
        )

    def test_alignment_requires_a_recorded_observation(self):
        media = self.proof["media"]
        del media["alignmentObservation"]
        self.assertEqual(self.evidence()[:2], ("unverified", "unverified"))
        for observation in (None, {}, [], "verified", {"status": "verified"}):
            with self.subTest(observation=observation):
                media["alignmentObservation"] = observation
                self.assertEqual(self.evidence()[:2], ("unverified", "unverified"))

    def test_matching_identities_preserve_the_recorded_result(self):
        for status, check_status in (("verified", "passed"), ("mismatch", "failed")):
            with self.subTest(status=status):
                self.proof["media"]["alignmentObservation"]["status"] = status
                self.assertEqual(self.evidence()[:2], (status, check_status))

    def test_replacing_either_video_invalidates_the_observation(self):
        original = deepcopy(self.proof)
        for status in ("verified", "mismatch"):
            for field, replacement in (
                ("source_sha256", "b" * 64),
                ("youtube_id", "dQw4w9WgXcQ"),
            ):
                with self.subTest(status=status, field=field):
                    self.proof = deepcopy(original)
                    observation = self.proof["media"]["alignmentObservation"]
                    observation["status"] = status
                    saved_observation = deepcopy(observation)
                    self.proof[field] = replacement
                    self.assertEqual(self.evidence()[:2], ("unverified", "unverified"))
                    self.assertEqual(observation, saved_observation)

    def test_missing_or_empty_identities_never_match(self):
        original = deepcopy(self.proof)
        for status in ("verified", "mismatch"):
            for current_field, recorded_field in (
                ("source_sha256", "sourceSha256"),
                ("youtube_id", "youtubeVideoId"),
            ):
                for missing_from in ("current", "recorded", "both"):
                    for value in (None, "", "   ", False, 0, "absent"):
                        with self.subTest(
                            status=status, field=current_field,
                            missing_from=missing_from, value=value,
                        ):
                            self.proof = deepcopy(original)
                            observation = self.proof["media"]["alignmentObservation"]
                            observation["status"] = status
                            targets = []
                            if missing_from in ("current", "both"):
                                targets.append((self.proof, current_field))
                            if missing_from in ("recorded", "both"):
                                targets.append((observation, recorded_field))
                            for target, key in targets:
                                if value == "absent":
                                    del target[key]
                                else:
                                    target[key] = value
                            self.assertEqual(self.evidence()[:2], ("unverified", "unverified"))

    def test_public_proof_drops_verified_badge_for_either_replacement(self):
        original = deepcopy(self.proof)
        app.config["TESTING"] = True
        client = app.test_client()
        for changes in (
            {}, {"source_sha256": "b" * 64}, {"youtube_id": "dQw4w9WgXcQ"},
            {"source_sha256": None}, {"youtube_id": None},
        ):
            with self.subTest(changes=changes):
                proof = deepcopy(original)
                proof.update(changes)
                connection = Mock()
                connection.execute.return_value.fetchone.return_value = {"n": 0}
                with patch("sceneit.proof.get_proof", return_value=proof), \
                        patch("sceneit.proof.connection") as connect:
                    connect.return_value.__enter__.return_value = connection
                    response = client.get("/api/proof")
                self.assertEqual(response.status_code, 200)
                result = response.get_json()
                alignment = next(check for check in result["checks"] if check["id"] == "alignment")
                self.assertEqual(result["timelineStatus"], "unverified" if changes else "verified")
                self.assertEqual(alignment["status"], "unverified" if changes else "passed")


class ProofReportRouteTests(unittest.TestCase):
    VERIFIED_LIMIT = (
        "Timeline verification covers representative saved scenes, not every frame of either edit."
    )
    MISMATCH_LIMIT = (
        "Paired playback found a YouTube edit/timeline mismatch; retained timestamps "
        "may not match the indexed source."
    )
    UNVERIFIED_LIMIT = "YouTube edit/timeline alignment is not independently verified."

    def setUp(self):
        self.enterContext(patch.dict(app.config, TESTING=True))
        self.client = app.test_client()
        recorded_at = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        self.proof = {
            "id": "one-video-proof", "title": "Alignment regression fixture",
            "youtube_id": "vLqagjJAvU8", "state": "ready", "message": "Ready to search.",
            "asset_id": "uploaded-file", "provider_duration": 100.0,
            "searches_used": 1, "search_limit": 50, "updated_at": recorded_at,
            "media": {
                "duration": 100.0, "width": 1920, "height": 1080,
                "size": 123456, "hasAudio": True,
                # These passed checks must never imply cross-edit verification.
                "youtubeMetadataVerified": True,
                "playbackObservation": {"status": "played"},
            },
        }
        saved_search = {
            "id": "saved-search", "query": "A person walking", "modality": "visual",
            "created_at": recorded_at, "latency_ms": 120, "partial": False, "matches": [],
        }
        # Stub persistence only: the status, report, saved-search presentation and
        # Flask JSON response all run unchanged.
        self.enterContext(patch("sceneit.proof.get_proof", return_value=self.proof))
        conn = self.enterContext(patch("sceneit.proof.connection")).return_value.__enter__.return_value
        conn.execute.return_value.fetchone.return_value = {"n": 1}
        conn.execute.return_value.fetchall.return_value = [saved_search]

        # Fail before provider setup or HTTP traffic, even if a future read path
        # tries to refresh evidence or sign a storage URL. Also catch swallowed errors.
        for target in ("sceneit.proof.TwelveLabsClient", "httpx.Client.send", "httpx.AsyncClient.send"):
            guard = self.enterContext(patch(
                target, side_effect=AssertionError("Evidence reads must not make external calls.")
            ))
            self.addCleanup(guard.assert_not_called)

    def assert_alignment_report(self, timeline_status, check_status, detail, limitation):
        status_response = self.client.get("/api/proof")
        report_response = self.client.get("/api/proof/report")
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(report_response.status_code, 200)
        self.assertTrue(status_response.is_json)
        self.assertTrue(report_response.is_json)
        status = status_response.get_json()
        report = report_response.get_json()
        self.assertEqual(report["proof"], status)
        self.assertEqual(status["timelineStatus"], timeline_status)
        alignment_checks = [check for check in status["checks"] if check["id"] == "alignment"]
        self.assertEqual(len(alignment_checks), 1)
        self.assertEqual(alignment_checks[0]["status"], check_status)
        self.assertEqual(alignment_checks[0]["detail"], detail)
        # Check the downloaded list itself, not another call to the mapping helper.
        self.assertEqual(report["limitations"].count(limitation), 1)
        for other in (self.VERIFIED_LIMIT, self.MISMATCH_LIMIT, self.UNVERIFIED_LIMIT):
            if other != limitation:
                self.assertNotIn(other, report["limitations"])
        self.assertNotIn("four representative", " ".join(report["limitations"]))
        self.assertEqual(report["searches"], [{
            "id": "saved-search", "query": "A person walking", "modality": "visual",
            "createdAt": "2026-09-01T12:00:00+00:00", "latencyMs": 120,
            "provider": "Twelve Labs", "partial": False, "matches": [],
        }])
        return status

    def test_verified_report_limits_claim_to_sampled_moments(self):
        for sample_count in (4, 2, None):
            with self.subTest(sample_count=sample_count):
                observation = {"status": "verified"}
                if sample_count is not None:
                    observation["sampleCount"] = sample_count
                self.proof["media"]["alignmentObservation"] = observation
                prefix = (
                    f"{sample_count} representative saved scenes" if sample_count
                    else "Representative saved scenes"
                )
                self.assert_alignment_report(
                    "verified", "passed",
                    f"{prefix} matched the YouTube edit at their retained timestamps during paired playback. "
                    "No stable offset or edit divergence was observed; verification covers the sampled moments.",
                    self.VERIFIED_LIMIT,
                )

    def test_mismatched_report_warns_about_retained_timestamps(self):
        for summary in ("The YouTube edit omits the opening scene.", None):
            with self.subTest(summary=summary):
                observation = {"status": "mismatch"}
                if summary is not None:
                    observation["summary"] = summary
                self.proof["media"]["alignmentObservation"] = observation
                self.assert_alignment_report(
                    "mismatch", "failed",
                    summary or "Paired playback showed that the indexed source and YouTube edit do not share one timeline.",
                    self.MISMATCH_LIMIT,
                )

    def test_unverified_report_is_not_promoted_by_other_passed_checks(self):
        for observation in (None, {}, {"status": "unverified"}, {"status": "unknown"}):
            with self.subTest(observation=observation):
                if observation is None:
                    self.proof["media"].pop("alignmentObservation", None)
                else:
                    self.proof["media"]["alignmentObservation"] = observation
                status = self.assert_alignment_report(
                    "unverified", "unverified",
                    "Compare paired source and YouTube playback at saved timestamps. "
                    "A matching link, still, or duration alone does not verify the edit.",
                    self.UNVERIFIED_LIMIT,
                )
                for check in status["checks"]:
                    if check["id"] in ("youtube", "embed", "duration"):
                        self.assertEqual(check["status"], "passed")


class SourcePlaybackRouteTests(unittest.TestCase):
    @staticmethod
    @contextmanager
    def proof_connection(playback):
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = {
            "media": {"sourcePlayback": playback}
        }
        yield connection

    @staticmethod
    def playback(permission=True):
        return {
            "permissionConfirmed": permission,
            "rightsPolicy": "public-app-viewers",
            "objectPath": "/bucket/private/source.mp4",
            "contentType": "video/mp4",
        }

    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_streaming_boundary_requires_confirmed_permission(self):
        with patch("sceneit.server.connection", lambda: self.proof_connection(self.playback(False))):
            response = self.client.get("/api/proof/source")
        self.assertEqual(response.status_code, 404)

    def test_upstream_unsatisfied_range_is_propagated(self):
        upstream = Mock(status_code=416, headers={"Content-Range": "bytes */100"})
        storage_client = Mock()
        storage_client.send.return_value = upstream
        with patch("sceneit.server.connection", lambda: self.proof_connection(self.playback())), \
                patch("sceneit.server.signed_url", return_value="https://storage.invalid/source"), \
                patch("sceneit.server.httpx.Client", return_value=storage_client):
            response = self.client.get("/api/proof/source", headers={"Range": "bytes=200-300"})
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["Content-Range"], "bytes */100")
        storage_client.close.assert_called_once()

    def test_range_request_never_falls_back_to_full_body(self):
        upstream = Mock(status_code=200, headers={})
        storage_client = Mock()
        storage_client.send.return_value = upstream
        with patch("sceneit.server.connection", lambda: self.proof_connection(self.playback())), \
                patch("sceneit.server.signed_url", return_value="https://storage.invalid/source"), \
                patch("sceneit.server.httpx.Client", return_value=storage_client):
            response = self.client.get("/api/proof/source", headers={"Range": "bytes=0-99"})
        self.assertEqual(response.status_code, 503)
        upstream.close.assert_called_once()
        storage_client.close.assert_called_once()

if __name__ == "__main__":
    unittest.main()
