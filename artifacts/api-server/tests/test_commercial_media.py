"""Provider-free regressions for commercially metered media boundaries."""
import json
import tempfile
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask

from sceneit.inspect_media import MediaInspectionError, inspect_mp4
from sceneit.private_storage import download_object, open_private
from sceneit import proof_media


def _completed(stdout=b"", returncode=0):
    return Mock(stdout=stdout, stderr=b"", returncode=returncode)


class ExactStorageTransferTests(unittest.TestCase):
    def test_one_byte_response_fetches_exact_upstream_range_without_retry(self):
        app = Flask(__name__)
        blob = Mock()
        blob.download_as_bytes.return_value = b"x"
        info = {
            "size": 10, "generation": 12, "contentType": "video/mp4",
            "etag": "fixture",
        }
        with app.test_request_context("/"), \
                patch("sceneit.private_storage.object_info", return_value=info), \
                patch("sceneit.private_storage._blob", return_value=blob), \
                patch("sceneit.private_storage.billing_settings",
                      create=True, return_value={"enabled": False}):
            response = open_private("/bucket/private/video", "bytes=0-0")
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.get_data(), b"x")
        kwargs = blob.download_as_bytes.call_args.kwargs
        self.assertEqual((kwargs["start"], kwargs["end"]), (0, 0))
        self.assertIsNone(kwargs["retry"])
        self.assertIsNone(kwargs["checksum"])

    def test_download_is_chunk_bounded_and_disables_sdk_retries(self):
        blob = Mock()
        blob.download_as_bytes.side_effect = [b"abc", b"de"]
        destination = Mock()
        info = {
            "size": 5, "generation": 3, "contentType": "video/mp4",
            "etag": "fixture",
        }
        with patch("sceneit.private_storage.object_info", return_value=info), \
                patch("sceneit.private_storage._blob", return_value=blob), \
                patch("sceneit.private_storage.DOWNLOAD_CHUNK_BYTES", 3):
            download_object("/bucket/private/video", destination)
        self.assertEqual(destination.write.call_args_list[0].args, (b"abc",))
        self.assertEqual(destination.write.call_args_list[1].args, (b"de",))
        self.assertEqual(
            [(call.kwargs["start"], call.kwargs["end"])
             for call in blob.download_as_bytes.call_args_list],
            [(0, 2), (3, 4)])
        self.assertTrue(all(call.kwargs["retry"] is None
                            for call in blob.download_as_bytes.call_args_list))


class ProofGenerationMeteringTests(unittest.TestCase):
    @staticmethod
    @contextmanager
    def _connection(connection):
        yield connection

    def test_stopped_shared_work_launches_no_generation(self):
        stopped = RuntimeError("stopped")
        with patch.object(proof_media, "_pending", return_value={"id": "search"}), \
                patch.object(proof_media, "_check_shared_work", side_effect=stopped), \
                patch.object(proof_media, "shared_permit") as permit, \
                patch.object(proof_media, "bounded_process") as process:
            proof_media.prepare_frames()
        permit.assert_not_called()
        process.assert_not_called()

    def test_failed_retries_receive_distinct_operation_reservations(self):
        attempts = []
        reserve = Mock()

        def run_attempt():
            connection = Mock()
            connection.execute.return_value.fetchone.return_value = {
                "matches": [{"startSeconds": 1, "endSeconds": 2}],
                "media": {"size": 10, "frameProcessing": {"attempts": []}},
                "source_sha256": "a" * 64,
                "completed": 0,
            }
            with patch.object(proof_media, "connection",
                              lambda: self._connection(connection)), \
                    patch.object(proof_media, "_commercial_enabled",
                                 return_value=True), \
                    patch("sceneit.quota.reserve", reserve):
                attempts.append(proof_media._begin_frame_attempt("search"))

        run_attempt()
        run_attempt()
        self.assertNotEqual(attempts[0], attempts[1])
        operation_ids = [call.args[2] for call in reserve.call_args_list]
        self.assertEqual(len(set(operation_ids)), 2)

    def test_still_storage_is_reserved_before_upload(self):
        order = []
        frame_row = {
            "matches": [{"startSeconds": 1, "endSeconds": 2}],
            "source_sha256": "a" * 64,
            "media": {
                "size": 10,
                "frameProcessing": {
                    "attempts": [{"id": "attempt", "status": "running"}],
                },
                "sourcePlayback": {
                    "permissionConfirmed": True,
                    "rightsPolicy": "public-app-viewers",
                    "objectPath": "/bucket/private/source",
                },
            },
        }
        query = Mock()
        query.execute.return_value.fetchone.return_value = frame_row
        query.execute.return_value.fetchall.return_value = []
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)

        def fake_download(_path, destination, **_kwargs):
            Path(destination).write_bytes(b"source")

        def fake_run(*_args, **_kwargs):
            Path(directory.name, "1.jpg").write_bytes(b"still")

        with patch.object(proof_media, "connection",
                          lambda: self._connection(query)), \
                patch.object(proof_media, "_commercial_enabled",
                             return_value=True), \
                patch.object(proof_media, "download_object",
                             side_effect=fake_download), \
                patch("sceneit.proof_media.hashlib.file_digest",
                      return_value=Mock(hexdigest=lambda: "a" * 64)), \
                patch.object(proof_media.subprocess, "run",
                             side_effect=fake_run), \
                patch("sceneit.quota.reserve_storage",
                      side_effect=lambda *_a, **_k: order.append("reserve")), \
                patch.object(proof_media, "upload_private",
                             side_effect=lambda *_a, **_k: order.append("upload")):
            proof_media.build_frames("search", "attempt", directory.name)
        self.assertEqual(order[:2], ["reserve", "upload"])

    def test_retry_limit_is_per_saved_search_not_global(self):
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = {
            "matches": [{"startSeconds": 1, "endSeconds": 2}],
            "media": {"size": 10, "frameProcessing": {"attempts": [
                {"id": str(i), "searchId": "previous", "status": "uncertain"}
                for i in range(3)
            ]}},
            "source_sha256": "a" * 64,
            "completed": 0,
        }
        with patch.object(proof_media, "connection", lambda: self._connection(conn)), \
                patch.object(proof_media, "_commercial_enabled", return_value=True), \
                patch("sceneit.quota.reserve") as reserve:
            self.assertIsNotNone(proof_media._begin_frame_attempt("next-search"))
        reserve.assert_called_once()


class ObservedDurationTests(unittest.TestCase):
    def test_small_duration_tolerance_does_not_underreserve_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.mp4"
            path.write_bytes(b"\0\0\0\20ftypmp42" + b"x" * 20)
            metadata = {
                "format": {"format_name": "mov,mp4", "duration": "5"},
                "streams": [{
                    "codec_type": "video", "codec_name": "h264",
                    "pix_fmt": "yuv420p", "width": 360, "height": 360,
                    "avg_frame_rate": "24/1",
                }],
            }
            with patch("sceneit.inspect_media._run", side_effect=[
                    _completed(json.dumps(metadata).encode()),
                    _completed(b"out_time_us=5200000\nprogress=end\n")]):
                self.assertEqual(5.2, inspect_mp4(path)["duration"])

    def test_understated_container_duration_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.mp4"
            path.write_bytes(b"\0\0\0\20ftypmp42" + b"x" * 20)
            metadata = {
                "format": {"format_name": "mov,mp4", "duration": "5"},
                "streams": [{
                    "codec_type": "video", "codec_name": "h264",
                    "pix_fmt": "yuv420p", "width": 360, "height": 360,
                    "avg_frame_rate": "24/1",
                }],
            }
            progress = b"out_time_us=10000000\nprogress=end\n"
            with patch("sceneit.inspect_media._run", side_effect=[
                    _completed(json.dumps(metadata).encode()),
                    _completed(progress)]), self.assertRaises(
                        MediaInspectionError) as raised:
                inspect_mp4(path)
        self.assertEqual(raised.exception.code, "invalid_media")


if __name__ == "__main__":
    unittest.main()