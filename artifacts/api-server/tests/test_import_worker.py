"""Worker safety invariants that do not purchase provider operations."""
import unittest
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from sceneit.import_worker import process_job
from sceneit import import_worker


class ImportWorkerSafetyTests(unittest.TestCase):
    def job(self, **changes):
        value = {
            "id": "f66189d5-9018-40b5-a66d-b210dfa16c47",
            "owner_id": "owner", "state": "uploading",
            "provider_write_marker": "upload_asset", "index_id": "index",
            "asset_id": None, "indexed_asset_id": None, "attempts": 2,
            "lease_token": "token", "expires_at": Mock(), "has_audio": True,
            "sha256": "a" * 64,
        }
        value.update(changes)
        return value

    @patch("sceneit.import_worker._finish_lease")
    @patch("sceneit.import_worker._fail")
    def test_restart_with_mutation_marker_never_resubmits(self, fail, finish):
        client = Mock()
        process_job(self.job(), client)
        client.upload_asset.assert_not_called()
        client.index_asset.assert_not_called()
        self.assertEqual(fail.call_args.args[-1], "needs_review")
        finish.assert_called_once()

    @patch("sceneit.import_worker._finish_lease")
    @patch("sceneit.import_worker._fail")
    def test_ambiguous_provider_mutation_needs_review(self, fail, finish):
        from sceneit.provider import ProviderError
        client = Mock()
        client.create_index.side_effect = ProviderError(
            "network_error", "safe", retryable=True, ambiguous=True)
        job = self.job(state="processing", provider_write_marker=None,
                       index_id=None)
        @contextmanager
        def fake_connection():
            connection = Mock()
            connection.execute.return_value.rowcount = 1
            yield connection
        with patch("sceneit.import_worker._update", side_effect=lambda row, **kw:
                   row.update(kw) or row), \
                patch("sceneit.import_worker._assert_fence"), \
                patch("sceneit.import_worker.connection", fake_connection):
            process_job(job, client)
        self.assertEqual(fail.call_args.args[-1], "needs_review")

    @patch("sceneit.import_worker._finish_lease")
    @patch("sceneit.import_worker._cancel")
    def test_cancellation_bypasses_processing_deadline(self, cancel, finish):
        job = self.job(
            state="cancel_requested",
            provider_write_marker=None,
            processing_started_at=(
                datetime.now(timezone.utc) - timedelta(days=8)),
            index_id=None, asset_id=None, indexed_asset_id=None,
        )
        with patch("sceneit.import_worker._lease_heartbeat",
                   return_value=nullcontext()):
            process_job(job, Mock())
        cancel.assert_called_once()

    def test_lost_advisory_guard_blocks_new_provider_side_effect(self):
        job = self.job(
            state="processing", provider_write_marker=None, index_id=None)
        guard = Mock()
        guard.check.side_effect = RuntimeError("advisory lock lost")
        client = Mock()
        previous = import_worker._active_worker_guard
        import_worker._active_worker_guard = guard
        try:
            with self.assertRaises(RuntimeError):
                import_worker._provider_step(job, client)
        finally:
            import_worker._active_worker_guard = previous
        client.create_index.assert_not_called()

    def test_indexed_system_metadata_duration_can_become_ready(self):
        job = self.job(
            state="indexing", provider_write_marker=None,
            index_id="index", asset_id="asset", indexed_asset_id="indexed",
            duration_seconds=10.0, media_path="/private/source.mp4",
            media_generation="1")
        client = Mock()
        client.get_indexed_asset.return_value = {
            "status": "indexed", "asset_id": "asset",
            "system_metadata": {"duration": 10.4},
        }
        client.get_asset.return_value = {"status": "ready"}
        @contextmanager
        def fake_connection():
            yield Mock()
        with patch("sceneit.import_worker._assert_fence"), \
                patch("sceneit.import_worker._update",
                      side_effect=lambda row, **kw: row.update(kw) or row), \
                patch("sceneit.import_worker.connection", fake_connection):
            import_worker._provider_step(job, client)
        self.assertEqual("ready", job["state"])
        self.assertEqual(100, job["progress_percent"])

    def test_idle_proof_frame_failure_is_isolated_and_redacted(self):
        with patch("sceneit.proof_media.prepare_frames",
                   side_effect=RuntimeError("private query / secret path")), \
                patch.object(import_worker.logger, "warning") as warning:
            self.assertFalse(import_worker.prepare_proof_frames())
        message = warning.call_args.args[0]
        self.assertIn("frame_preparation_failed", message)
        self.assertNotIn("private query", message)
        self.assertNotIn("secret path", message)

    def test_worker_resource_lease_loss_fences_work(self):
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = (True,)
        guard = import_worker._WorkerLockGuard(
            connection, resource_holder="resource-holder")
        with patch("sceneit.import_worker.renew", return_value=False):
            with self.assertRaises(import_worker.WorkerLockLost):
                guard.check()


if __name__ == "__main__":
    unittest.main()