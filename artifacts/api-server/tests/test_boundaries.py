"""Free, deterministic checks; these never call Twelve Labs."""
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from pydantic import ValidationError

from sceneit.proof import ProofError, SceneQuery, normalize_matches
from sceneit.worker import publish_source, run_step
from sceneit.storage import parse_object_path, safe_content_range
from sceneit.server import app


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