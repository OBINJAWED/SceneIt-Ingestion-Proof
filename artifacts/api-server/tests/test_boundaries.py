"""Free, deterministic checks; these never call Twelve Labs."""
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from sceneit.proof import ProofError, SceneQuery, normalize_matches
from sceneit.worker import run_step


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


if __name__ == "__main__":
    unittest.main()