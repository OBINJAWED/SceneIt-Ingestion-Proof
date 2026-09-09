"""Operator metadata refresh regressions; network and persistence are fixtures."""
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import Mock, patch
import httpx
from sceneit.db import PROOF_ID
from sceneit.worker import main as worker_main
from test_boundaries import admitted_client

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
        response = admitted_client(self).get("/api/proof")
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



if __name__ == "__main__":
    unittest.main()
