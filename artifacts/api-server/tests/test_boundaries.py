"""Free, deterministic checks; these never call Twelve Labs or object storage."""
import json
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, Mock, patch

import httpx
from pydantic import ValidationError

from sceneit.db import PROOF_ID
from sceneit.proof import ProofError, SceneQuery, alignment_evidence, normalize_matches
from sceneit.worker import initialize, main as worker_main, publish_source, run_step
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


class YoutubeEvidenceIdentityTests(unittest.TestCase):
    YOUTUBE_ID = "vLqagjJAvU8"
    BLOCKED_DETAIL = (
        "YouTube refused embedded playback in the automated preview (error 150). "
        "Timestamp links remain available."
    )

    def setUp(self):
        self.enterContext(patch.dict(app.config, TESTING=True))
        self.client = app.test_client()
        self.proof = {
            "id": "youtube-identity-proof", "title": "YouTube identity fixture",
            "source_sha256": "a" * 64, "youtube_id": self.YOUTUBE_ID,
            "state": "ready", "message": "Ready", "asset_id": "uploaded-file",
            "provider_duration": 10.0, "searches_used": 0, "search_limit": 50,
            "updated_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
            "media": {
                "duration": 10.0, "width": 1280, "height": 720,
                "size": 1000, "hasAudio": True,
                "youtubeMetadataVerified": True,
                "youtubeMetadataVideoId": self.YOUTUBE_ID,
                "playbackObservation": {
                    "status": "played", "youtubeVideoId": self.YOUTUBE_ID,
                },
            },
        }
        self.saved_proof = None
        self.get_proof = self.enterContext(
            patch("sceneit.proof.get_proof", side_effect=self._read_proof)
        )
        self.connection = Mock()
        self.connection.execute.return_value.fetchone.return_value = {"n": 0}
        self.connection.execute.return_value.fetchall.return_value = []
        connect = self.enterContext(patch("sceneit.proof.connection"))
        connect.return_value.__enter__.return_value = self.connection
        for target in ("sceneit.proof.TwelveLabsClient", "httpx.Client.send", "httpx.AsyncClient.send"):
            guard = self.enterContext(patch(
                target, side_effect=AssertionError("Proof reads must not make external calls.")
            ))
            self.addCleanup(guard.assert_not_called)

    def _read_proof(self):
        self.saved_proof = deepcopy(self.proof)
        return self.proof

    def checks(self, route="/api/proof"):
        response = self.client.get(route)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        proof = body["proof"] if route.endswith("/report") else body
        return body, {check["id"]: check for check in proof["checks"]}

    def test_matching_playback_identity_preserves_played_and_blocked_results(self):
        for status, expected in (("played", "passed"), ("blocked", "failed")):
            with self.subTest(status=status):
                self.proof["media"]["playbackObservation"]["status"] = status
                report, checks = self.checks("/api/proof/report")
                self.assertEqual(checks["embed"]["status"], expected)
                self.assertEqual(checks["youtube"]["status"], "passed")
                self.assertEqual(self.BLOCKED_DETAIL in report["limitations"], status == "blocked")

    def test_link_replacement_invalidates_both_youtube_checks_and_blocked_report_detail(self):
        self.proof["youtube_id"] = "dQw4w9WgXcQ"
        for status in ("played", "blocked"):
            for route in ("/api/proof", "/api/proof/report"):
                with self.subTest(status=status, route=route):
                    self.proof["media"]["playbackObservation"]["status"] = status
                    body, checks = self.checks(route)
                    self.assertEqual(checks["youtube"]["status"], "unverified")
                    self.assertEqual(checks["embed"]["status"], "unverified")
                    self.assertNotIn("metadata matches", checks["youtube"]["detail"])
                    if route.endswith("/report"):
                        self.assertNotIn(self.BLOCKED_DETAIL, body["limitations"])

    def test_absent_null_empty_and_invalid_recorded_ids_are_unverified(self):
        original = deepcopy(self.proof)
        invalid_values = (None, "", "   ", False, 0, [], "dQw4w9WgXcQ")
        for field, container, status in (
            ("youtubeVideoId", "playbackObservation", "played"),
            ("youtubeVideoId", "playbackObservation", "blocked"),
            ("youtubeMetadataVideoId", None, None),
        ):
            for value in ("absent", *invalid_values):
                with self.subTest(field=field, status=status, value=value):
                    proof = deepcopy(original)
                    self.proof = proof
                    if container:
                        proof["media"][container]["status"] = status
                        target = proof["media"][container]
                    else:
                        target = proof["media"]
                    if value == "absent":
                        target.pop(field, None)
                    else:
                        target[field] = value
                    _, checks = self.checks()
                    check_id = "embed" if container else "youtube"
                    self.assertEqual(checks[check_id]["status"], "unverified")

    def test_missing_or_invalid_current_id_invalidates_both_gates(self):
        original = deepcopy(self.proof)
        for youtube_id in ("absent", None, "", "   ", False, 0):
            with self.subTest(youtube_id=youtube_id):
                self.proof = deepcopy(original)
                if youtube_id == "absent":
                    self.proof.pop("youtube_id")
                else:
                    self.proof["youtube_id"] = youtube_id
                _, checks = self.checks()
                self.assertEqual(checks["youtube"]["status"], "unverified")
                self.assertEqual(checks["embed"]["status"], "unverified")

    def test_metadata_and_playback_identity_gates_are_independent(self):
        cases = (
            ("dQw4w9WgXcQ", self.YOUTUBE_ID, "unverified", "passed"),
            (self.YOUTUBE_ID, "dQw4w9WgXcQ", "passed", "unverified"),
        )
        for metadata_id, playback_id, youtube_status, embed_status in cases:
            with self.subTest(metadata_id=metadata_id, playback_id=playback_id):
                self.proof["media"]["youtubeMetadataVideoId"] = metadata_id
                self.proof["media"]["playbackObservation"]["youtubeVideoId"] = playback_id
                _, checks = self.checks()
                self.assertEqual(checks["youtube"]["status"], youtube_status)
                self.assertEqual(checks["embed"]["status"], embed_status)

    def test_unknown_or_malformed_playback_observations_are_unverified(self):
        for observation in (
            "absent", None, [], "played", {}, {"status": "unknown"},
            {"status": None}, {"status": 1},
        ):
            with self.subTest(observation=observation):
                if observation == "absent":
                    self.proof["media"].pop("playbackObservation", None)
                else:
                    self.proof["media"]["playbackObservation"] = observation
                _, checks = self.checks()
                self.assertEqual(checks["embed"]["status"], "unverified")

    def test_false_metadata_flag_is_unverified_even_with_matching_identity(self):
        self.proof["media"]["youtubeMetadataVerified"] = False
        _, checks = self.checks()
        self.assertEqual(checks["youtube"]["status"], "unverified")

    def test_reads_do_not_mutate_persisted_evidence(self):
        self.proof["media"]["playbackObservation"]["status"] = "blocked"
        for youtube_id in (self.YOUTUBE_ID, "dQw4w9WgXcQ"):
            with self.subTest(youtube_id=youtube_id):
                self.proof["youtube_id"] = youtube_id
                self.checks("/api/proof/report")
                self.assertEqual(self.proof, self.saved_proof)
        self.assertTrue(self.connection.execute.call_args_list)
        for call in self.connection.execute.call_args_list:
            self.assertTrue(call.args[0].lstrip().upper().startswith("SELECT"))


class InitializeYoutubeMetadataIdentityTests(unittest.TestCase):
    def test_insert_binds_verified_metadata_to_checked_youtube_id_only_on_success(self):
        youtube_id = "vLqagjJAvU8"
        metadata = {
            "format": {"duration": "12.5"},
            "streams": [
                {"codec_type": "video", "width": 1280, "height": 720},
                {"codec_type": "audio"},
            ],
        }
        for success in (True, False):
            with self.subTest(success=success), TemporaryDirectory() as temp:
                root = Path(temp)
                assets = root / "attached_assets"
                assets.mkdir()
                source = assets / "source.mp4"
                source.write_bytes(b"deterministic source")
                completed = Mock(stdout=json.dumps(metadata).encode())
                response = Mock(is_success=success)
                response.json.return_value = {"title": "Checked title"}
                connection = Mock()
                connection.execute.return_value.fetchone.return_value = None
                manager = MagicMock()
                manager.__enter__.return_value = connection
                with patch("sceneit.worker.ROOT", root), \
                        patch("sceneit.worker.subprocess.run", return_value=completed) as ffprobe, \
                        patch("sceneit.worker.httpx.get", return_value=response) as oembed, \
                        patch("sceneit.worker.connection", return_value=manager):
                    initialize("attached_assets/source.mp4", youtube_id)
                ffprobe.assert_called_once()
                oembed.assert_called_once_with(
                    "https://www.youtube.com/oembed",
                    params={
                        "url": f"https://www.youtube.com/watch?v={youtube_id}",
                        "format": "json",
                    },
                    timeout=20,
                )
                insert = next(
                    call for call in connection.execute.call_args_list
                    if "INSERT INTO sceneit_proofs" in call.args[0]
                )
                inserted_media = insert.args[1][5].obj
                self.assertIs(inserted_media["youtubeMetadataVerified"], success)
                if success:
                    self.assertEqual(inserted_media["youtubeMetadataVideoId"], youtube_id)
                    response.json.assert_called_once_with()
                else:
                    self.assertNotIn("youtubeMetadataVideoId", inserted_media)
                    response.json.assert_not_called()


class RefreshYoutubeMetadataCommandTests(unittest.TestCase):
    YOUTUBE_ID = "vLqagjJAvU8"
    REPLACEMENT_ID = "dQw4w9WgXcQ"

    def setUp(self):
        self.proof = {
            "id": PROOF_ID, "title": "Preserved source title",
            "youtube_id": self.YOUTUBE_ID, "source_sha256": "a" * 64,
            "source_path": "attached_assets/source.mp4", "state": "ready",
            "message": "Ready", "index_id": "index", "asset_id": "asset",
            "indexed_asset_id": "indexed-asset", "index_name": "preserved-index",
            "provider_duration": 10, "searches_used": 3, "search_limit": 50,
            "updated_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
            "media": {
                "duration": 10, "width": 1280, "height": 720, "size": 1000,
                "hasAudio": True, "youtubeMetadataVerified": True,
                # Legacy observations must not acquire an identity from this check.
                "playbackObservation": {"status": "played"},
                "alignmentObservation": {"status": "verified", "sampleCount": 4},
                "sourcePlayback": {"permissionConfirmed": False},
            },
        }
        self.enterContext(patch("sceneit.worker.get_proof", side_effect=lambda: deepcopy(self.proof)))
        self.connection = Mock()
        self.connection.execute.side_effect = self._conditional_update
        connect = self.enterContext(patch("sceneit.worker.connection"))
        connect.return_value.__enter__.return_value = self.connection
        self.oembed = self.enterContext(patch("sceneit.worker.httpx.get"))
        self.oembed.return_value = httpx.Response(200, json={"title": "Current YouTube title"})
        self.log = self.enterContext(patch("sceneit.worker.log"))
        for target in (
            "sceneit.worker.TwelveLabsClient", "sceneit.worker.upload_source",
            "sceneit.worker.private_object_path", "sceneit.worker.subprocess.run",
            "sceneit.worker.initialize", "sceneit.worker.run", "sceneit.worker.run_step",
            "sceneit.worker.publish_source", "sceneit.worker.update_proof",
            "httpx.Client.send", "httpx.AsyncClient.send",
        ):
            guard = self.enterContext(patch(
                target, side_effect=AssertionError("Metadata refresh must not ingest or access media.")
            ))
            self.addCleanup(guard.assert_not_called)

    def _conditional_update(self, sql, params):
        # Assert the actual SQL guard and server-side merge, then model its result
        # against the current row (which may have changed during the HTTP call).
        self.assertEqual(
            " ".join(sql.split()),
            "UPDATE sceneit_proofs SET media = media || %s, updated_at = now() "
            "WHERE id = %s AND youtube_id = %s RETURNING id",
        )
        patch_value, proof_id, checked_id = params
        changes = patch_value.obj
        self.assertEqual(proof_id, PROOF_ID)
        self.assertEqual(set(changes), {"youtubeMetadataVerified", "youtubeMetadataVideoId"})
        self.assertEqual(changes["youtubeMetadataVideoId"], checked_id)
        matched = bool(
            self.proof and self.proof["id"] == proof_id and self.proof["youtube_id"] == checked_id
        )
        if matched:
            self.proof["media"].update(changes)
        return Mock(**{"fetchone.return_value": {"id": proof_id} if matched else None})

    def refresh(self, expected_exit, checked_id=None):
        self.assertEqual(worker_main(["refresh-youtube-metadata"]), expected_exit)
        self.oembed.assert_called_once_with(
            "https://www.youtube.com/oembed",
            params={
                "url": f"https://www.youtube.com/watch?v={checked_id or self.YOUTUBE_ID}",
                "format": "json",
            },
            timeout=20,
        )
        self.connection.execute.assert_called_once()

    def public_checks(self):
        self.enterContext(patch("sceneit.proof.get_proof", return_value=self.proof))
        connection = self.enterContext(patch("sceneit.proof.connection"))
        connection.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = {"n": 3}
        with patch.dict(app.config, TESTING=True):
            response = app.test_client().get("/api/proof")
        self.assertEqual(response.status_code, 200)
        return {check["id"]: check["status"] for check in response.get_json()["checks"]}

    def test_command_freshly_binds_legacy_metadata_without_changing_other_evidence(self):
        before = deepcopy(self.proof)
        self.assertEqual(self.public_checks()["youtube"], "unverified")
        self.oembed.assert_not_called()
        self.connection.execute.assert_not_called()
        self.assertEqual(self.proof, before)

        self.refresh(0)

        before["media"]["youtubeMetadataVideoId"] = self.YOUTUBE_ID
        self.assertEqual(self.proof, before)
        checks = self.public_checks()
        self.assertEqual(checks["youtube"], "passed")
        self.assertEqual(checks["embed"], "unverified")
        self.assertEqual(checks["alignment"], "unverified")
        self.assertEqual(self.log.call_args.kwargs["detail"],
                         "Metadata only; playback and timeline alignment were not checked.")

    def test_replacement_link_is_checked_not_the_previous_metadata_identity(self):
        self.proof["media"]["youtubeMetadataVideoId"] = self.YOUTUBE_ID
        self.proof["youtube_id"] = self.REPLACEMENT_ID
        self.refresh(0, self.REPLACEMENT_ID)
        self.assertEqual(self.proof["media"]["youtubeMetadataVideoId"], self.REPLACEMENT_ID)

    def test_http_network_and_invalid_metadata_failures_clear_a_previous_pass(self):
        failures = (
            httpx.Response(404), httpx.Response(403), httpx.Response(429),
            httpx.Response(503), httpx.Response(302),
            httpx.TimeoutException("private diagnostic must not be logged"),
            httpx.ConnectError("private diagnostic must not be logged"),
            httpx.Response(200, content=b"not json"),
            *(httpx.Response(200, json=value) for value in (
                None, [], "unexpected", {}, {"title": None}, {"title": 1}, {"title": "  "},
            )),
        )
        original = deepcopy(self.proof)
        original["media"]["youtubeMetadataVideoId"] = self.YOUTUBE_ID
        for failure in failures:
            with self.subTest(failure=type(failure).__name__, status=getattr(failure, "status_code", None)):
                self.proof = deepcopy(original)
                self.oembed.reset_mock()
                self.connection.execute.reset_mock()
                self.log.reset_mock()
                self.oembed.side_effect = failure if isinstance(failure, Exception) else None
                self.oembed.return_value = failure
                self.refresh(1)
                expected = deepcopy(original)
                expected["media"]["youtubeMetadataVerified"] = False
                self.assertEqual(self.proof, expected)
                self.assertEqual(self.public_checks()["youtube"], "unverified")
                self.assertNotIn("private diagnostic", str(self.log.call_args_list))
                self.assertIs(self.log.call_args.kwargs["verified"], False)

    def test_new_check_is_made_every_time_including_after_success(self):
        self.refresh(0)
        self.oembed.reset_mock()
        self.connection.execute.reset_mock()
        self.oembed.return_value = httpx.Response(404)
        self.refresh(1)
        self.assertIs(self.proof["media"]["youtubeMetadataVerified"], False)
        self.oembed.reset_mock()
        self.connection.execute.reset_mock()
        self.oembed.return_value = httpx.Response(200, json={"title": "Available again"})
        self.refresh(0)
        self.assertIs(self.proof["media"]["youtubeMetadataVerified"], True)

    def test_link_change_during_success_or_failure_is_not_overwritten(self):
        original = deepcopy(self.proof)
        for result in (httpx.Response(200, json={"title": "Old link"}), httpx.Response(404),
                       httpx.TimeoutException("timed out")):
            with self.subTest(result=type(result).__name__):
                self.proof = deepcopy(original)
                self.oembed.reset_mock()
                self.connection.execute.reset_mock()
                self.log.reset_mock()
                saved = []

                def replace_link(*args, **kwargs):
                    self.proof["youtube_id"] = self.REPLACEMENT_ID
                    self.proof["media"]["youtubeMetadataVideoId"] = self.REPLACEMENT_ID
                    self.proof["media"]["playbackObservation"] = {
                        "status": "blocked", "youtubeVideoId": self.REPLACEMENT_ID,
                    }
                    saved.append(deepcopy(self.proof))
                    if isinstance(result, Exception):
                        raise result
                    return result

                self.oembed.side_effect = replace_link
                self.refresh(2)
                self.assertEqual(self.proof, saved[0])
                self.assertEqual(self.log.call_args.args[0], "youtube_metadata_refresh_skipped")

    def test_unrelated_concurrent_media_updates_are_preserved(self):
        saved = []

        def update_observation(*args, **kwargs):
            self.proof["media"]["playbackObservation"] = {
                "status": "blocked", "youtubeVideoId": self.YOUTUBE_ID,
            }
            self.proof["media"]["newEvidence"] = {"note": "concurrently saved"}
            saved.append(deepcopy(self.proof))
            return httpx.Response(200, json={"title": "Checked title"})

        self.oembed.side_effect = update_observation
        self.refresh(0)
        saved[0]["media"]["youtubeMetadataVideoId"] = self.YOUTUBE_ID
        self.assertEqual(self.proof, saved[0])
        self.assertEqual(self.public_checks()["embed"], "failed")

    def test_proof_removed_during_request_is_not_recreated(self):
        def remove_proof(*args, **kwargs):
            self.proof = None
            return httpx.Response(200, json={"title": "Checked title"})

        self.oembed.side_effect = remove_proof
        self.refresh(2)
        self.assertIsNone(self.proof)

    def test_missing_proof_or_invalid_id_fails_without_a_request_or_identity_backfill(self):
        for youtube_id in (None, "", "   ", False, [], "invalid", "x" * 12):
            with self.subTest(youtube_id=youtube_id):
                self.proof["youtube_id"] = youtube_id
                before = deepcopy(self.proof)
                with self.assertRaisesRegex(ValueError, "valid YouTube ID"):
                    worker_main(["refresh-youtube-metadata"])
                self.assertEqual(self.proof, before)
        self.proof = None
        with self.assertRaisesRegex(RuntimeError, "Initialize the proof first"):
            worker_main(["refresh-youtube-metadata"])
        self.oembed.assert_not_called()
        self.connection.execute.assert_not_called()

    def test_command_does_not_accept_a_replacement_link_or_source(self):
        for extra in (["--youtube-id", self.REPLACEMENT_ID], ["--source", "source.mp4"]):
            with self.subTest(extra=extra), patch("sys.stderr"), self.assertRaises(SystemExit) as exit:
                worker_main(["refresh-youtube-metadata", *extra])
            self.assertEqual(exit.exception.code, 2)
        self.oembed.assert_not_called()
        self.connection.execute.assert_not_called()


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
            "source_sha256": "a" * 64, "youtube_id": "vLqagjJAvU8",
            "state": "ready", "message": "Ready to search.",
            "asset_id": "uploaded-file", "provider_duration": 100.0,
            "searches_used": 1, "search_limit": 50, "updated_at": recorded_at,
            "media": {
                "duration": 100.0, "width": 1920, "height": 1080,
                "size": 123456, "hasAudio": True,
                # These passed checks must never imply cross-edit verification.
                "youtubeMetadataVerified": True,
                "youtubeMetadataVideoId": "vLqagjJAvU8",
                "playbackObservation": {
                    "status": "played", "youtubeVideoId": "vLqagjJAvU8",
                },
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
                observation = {
                    "status": "verified", "sourceSha256": "a" * 64,
                    "youtubeVideoId": "vLqagjJAvU8",
                }
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
                observation = {
                    "status": "mismatch", "sourceSha256": "a" * 64,
                    "youtubeVideoId": "vLqagjJAvU8",
                }
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
