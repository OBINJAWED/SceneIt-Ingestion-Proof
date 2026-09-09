"""Free, deterministic checks; these never call Twelve Labs or object storage."""
import json
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, Mock, patch

from pydantic import ValidationError

from sceneit.worker import initialize, publish_source, run_step
from sceneit.storage import parse_object_path, safe_content_range
from copy import deepcopy
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from sceneit.proof import (
    ProofError, SceneQuery, alignment_evidence, normalize_matches,
    present_search_operation,
)
from sceneit.provider import ProviderError, TwelveLabsClient
from sceneit.server import create_app


def admitted_client(test):
    """Exercise normal admission with a verified-session fixture, not a bypass."""
    app = create_app({
        "TESTING": True,
        "SESSION_SECRET": "s" * 48,
        "PILOT_ALLOWED_SUBJECTS": "pilot-subject",
        "TRUSTED_HOSTS": ["localhost", "sceneit.example"],
        "TRUST_PROXY_HOPS": 0,
        "DATABASE_CONFIGURED": True,
    })
    test.enterContext(patch(
        "sceneit.auth._session_from_cookie",
        return_value={
            "id": "session-digest", "user_id": "pilot-subject",
            "first_name": "Pilot", "csrf_token": "csrf",
        },
    ))
    return app.test_client()


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

    def test_provider_error_does_not_copy_raw_payload(self):
        client = TwelveLabsClient.__new__(TwelveLabsClient)
        client._api_key = "secret-key"
        response = Mock(
            status_code=400,
            headers={},
            json=Mock(return_value={
                "error": {
                    "code": "query echoed here",
                    "message": "secret-key and private query",
                }
            }),
        )
        error = client._safe_error(response)
        self.assertEqual("provider_http_400", error.code)
        self.assertEqual("The search provider rejected the request.", error.message)
        response.json.assert_not_called()

    def test_operation_status_omits_paid_search_text(self):
        from datetime import datetime, timedelta, timezone
        created = datetime.now(timezone.utc)
        operation = present_search_operation({
            "id": "search-id", "state": "needs_review",
            "attempt_id": "attempt-id", "created_at": created,
            "deadline_at": created + timedelta(seconds=75),
            "completed_at": created + timedelta(seconds=75),
            "error_code": "search_outcome_unknown",
            "query": "private scene description",
        })
        self.assertNotIn("query", operation)
        self.assertEqual("attempt-id", operation["attemptId"])
        self.assertEqual("needs_review", operation["state"])

    def test_search_absolute_deadline_stops_slow_response(self):
        body = json.dumps({"data": [], "page_info": {}}).encode()

        class SlowHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(.03)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        environment = {
            "TWELVE_LABS_API_KEY": "fixture-key",
            "SCENEIT_DISABLE_PROVIDER_NETWORK": "0",
            "SCENEIT_ALLOW_PROVIDER_TEST_ENDPOINT": "1",
            "SCENEIT_PROVIDER_TEST_BASE_URL":
                f"http://127.0.0.1:{server.server_port}",
        }
        try:
            with patch.dict("os.environ", environment, clear=False):
                client = TwelveLabsClient()
                started = time.monotonic()
                try:
                    with self.assertRaises(ProviderError) as raised:
                        client.search("index", "query", "visual", "asset",
                                      timeout_seconds=.15)
                finally:
                    client.close()
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertTrue(raised.exception.ambiguous)
            self.assertEqual("search_deadline_exceeded", raised.exception.code)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(1)

    def test_provider_network_disable_is_enforced_at_construction(self):
        with patch.dict(
                "os.environ",
                {"TWELVE_LABS_API_KEY": "fixture",
                 "SCENEIT_DISABLE_PROVIDER_NETWORK": "1"},
                clear=False):
            with self.assertRaises(ProviderError) as raised:
                TwelveLabsClient()
        self.assertEqual("provider_network_disabled", raised.exception.code)

    def test_database_failure_prevents_provider_construction(self):
        provider = Mock()
        with patch("sceneit.proof.connection",
                   side_effect=RuntimeError("database unavailable")):
            with self.assertRaises(RuntimeError):
                from sceneit.proof import search_scenes
                search_scenes(
                    {"query": "door", "modality": "visual"},
                    client_factory=provider)
        provider.assert_not_called()

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
        client = admitted_client(self)
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
        self.client = admitted_client(self)
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
        self.client = admitted_client(self)
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
        app = create_app({
            "TESTING": True,
            "SESSION_SECRET": "s" * 48,
            "PILOT_ALLOWED_SUBJECTS": "pilot-subject",
            "TRUSTED_HOSTS": ["sceneit.example"],
            "TRUST_PROXY_HOPS": 0,
            "DATABASE_CONFIGURED": True,
        })
        self.session = patch(
            "sceneit.auth._session_from_cookie",
            return_value={
                "id": "session-digest",
                "user_id": "pilot-subject",
                "first_name": "Pilot",
                "csrf_token": "csrf",
            },
        )
        self.session.start()
        self.addCleanup(self.session.stop)
        self.client = app.test_client()

    def test_streaming_boundary_requires_confirmed_permission(self):
        with patch("sceneit.proof_media.connection",
                   lambda: self.proof_connection(self.playback(False))):
            response = self.client.get(
                "/api/proof/source", base_url="https://sceneit.example"
            )
        self.assertEqual(response.status_code, 404)

    def test_upstream_unsatisfied_range_is_propagated(self):
        upstream = Mock(status_code=416, headers={"Content-Range": "bytes */100"})
        storage_client = Mock()
        storage_client.send.return_value = upstream
        with patch("sceneit.proof_media.connection",
                   lambda: self.proof_connection(self.playback())), \
                patch("sceneit.proof_media.signed_url",
                      return_value="https://storage.invalid/source"), \
                patch("sceneit.proof_media.httpx.Client",
                      return_value=storage_client):
            response = self.client.get(
                "/api/proof/source", headers={"Range": "bytes=200-300"},
                base_url="https://sceneit.example",
            )
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["Content-Range"], "bytes */100")
        storage_client.close.assert_called_once()

    def test_range_request_never_falls_back_to_full_body(self):
        upstream = Mock(status_code=200, headers={})
        storage_client = Mock()
        storage_client.send.return_value = upstream
        with patch("sceneit.proof_media.connection",
                   lambda: self.proof_connection(self.playback())), \
                patch("sceneit.proof_media.signed_url",
                      return_value="https://storage.invalid/source"), \
                patch("sceneit.proof_media.httpx.Client",
                      return_value=storage_client):
            response = self.client.get(
                "/api/proof/source", headers={"Range": "bytes=0-99"},
                base_url="https://sceneit.example",
            )
        self.assertEqual(response.status_code, 503)
        upstream.close.assert_called_once()
        storage_client.close.assert_called_once()

if __name__ == "__main__":
    unittest.main()
