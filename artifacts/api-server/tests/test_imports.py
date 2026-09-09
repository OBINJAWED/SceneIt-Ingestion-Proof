"""Deterministic private-import contract tests; no database or provider calls."""
import unittest

from pydantic import ValidationError

from sceneit.import_search import normalize_import_matches
from sceneit.imports import CreateImport, UploadRequest
from sceneit.import_limits import ImportProblem, MAX_BYTES


class ImportContractTests(unittest.TestCase):
    def test_standalone_upload_needs_no_link(self):
        body = CreateImport.model_validate({
            "entryMethod": "upload", "analysisAuthorized": True,
            "playbackAuthorized": False,
            "idempotencyKey": "8af72ff8-6bd3-4d86-bf23-42ee93d859c3",
        })
        self.assertIsNone(body.sourceUrl)

    def test_rights_and_mp4_boundary_are_strict(self):
        base = {
            "entryMethod": "upload", "playbackAuthorized": False,
            "idempotencyKey": "8af72ff8-6bd3-4d86-bf23-42ee93d859c3",
        }
        with self.assertRaises(ValidationError):
            CreateImport.model_validate({**base, "analysisAuthorized": False})
        with self.assertRaises(ValidationError):
            UploadRequest.model_validate({
                "fileName": "clip.mov", "sizeBytes": 1,
                "contentType": "video/quicktime",
            })
        with self.assertRaises(ValidationError):
            UploadRequest.model_validate({
                "fileName": "clip.mp4", "sizeBytes": MAX_BYTES + 1,
                "contentType": "video/mp4",
            })

    def test_matches_are_scoped_and_source_is_nullable(self):
        item = {
            "id": "f66189d5-9018-40b5-a66d-b210dfa16c47",
            "indexed_asset_id": "owned-indexed-video",
            "duration_seconds": 30, "source_url": None, "source_kind": "file",
        }
        matches, partial = normalize_import_matches({
            "data": [
                {"video_id": "other-video", "start": 1, "end": 2},
                {"video_id": "owned-indexed-video", "start": 3, "end": 5,
                 "confidence": "high"},
            ]
        }, item, "search")
        self.assertTrue(partial)
        self.assertIsNone(matches[0]["sourceUrl"])
        self.assertNotIn("score", matches[0])

    def test_all_cross_asset_results_fail_closed(self):
        item = {
            "id": "f66189d5-9018-40b5-a66d-b210dfa16c47",
            "indexed_asset_id": "owned", "duration_seconds": 30,
            "source_url": None, "source_kind": "file",
        }
        with self.assertRaises(ImportProblem):
            normalize_import_matches(
                {"data": [{"video_id": "other", "start": 1, "end": 2}]},
                item, "search")


if __name__ == "__main__":
    unittest.main()