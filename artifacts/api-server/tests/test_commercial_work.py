"""Provider-free checks for commercial work admission and durable identities."""
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from sceneit.billing_config import BillingProblem
from sceneit import import_worker
from sceneit.upload_attempts import record_session


class CommercialWorkerTests(unittest.TestCase):
    def job(self):
        return {
            "id": "f66189d5-9018-40b5-a66d-b210dfa16c47",
            "owner_id": "owner", "state": "processing",
            "provider_write_marker": None, "index_id": None,
            "asset_id": None, "indexed_asset_id": None,
            "lease_token": "lease", "sha256": "a" * 64,
            "has_audio": True, "duration_seconds": 10.01,
        }

    @staticmethod
    @contextmanager
    def connection():
        yield Mock()

    def test_quota_denial_happens_before_provider_mutation(self):
        client = Mock()
        denial = BillingProblem(
            "owner_quota_exhausted", "Monthly allowance exhausted.", 429)
        with patch("sceneit.import_worker._commercial_enabled", return_value=True), \
                patch("sceneit.import_worker._assert_fence"), \
                patch("sceneit.import_worker.connection", self.connection), \
                patch("sceneit.quota.check_work"), \
                patch("sceneit.quota.reserve", side_effect=denial), \
                self.assertRaises(BillingProblem):
            import_worker._provider_step(self.job(), client)
        client.create_index.assert_not_called()
        client.upload_asset.assert_not_called()
        client.index_asset.assert_not_called()

    def test_analysis_reservation_identity_survives_retry(self):
        client = Mock()
        client.create_index.side_effect = RuntimeError("provider not contacted")
        operations = []

        def remember(_conn, _owner, operation_id, amounts):
            operations.append((operation_id, amounts))

        with patch("sceneit.import_worker._commercial_enabled", return_value=True), \
                patch("sceneit.import_worker._assert_fence"), \
                patch("sceneit.import_worker._update",
                      side_effect=lambda job, **values: job.update(values) or job), \
                patch("sceneit.import_worker.connection", self.connection), \
                patch("sceneit.quota.check_work"), \
                patch("sceneit.quota.reserve", side_effect=remember):
            for _ in range(2):
                job = self.job()
                with self.assertRaises(RuntimeError):
                    import_worker._provider_step(job, client)

        self.assertEqual(
            operations,
            [
                ("import:f66189d5-9018-40b5-a66d-b210dfa16c47:analysis",
                 {"analysis_seconds": 11}),
                ("import:f66189d5-9018-40b5-a66d-b210dfa16c47:analysis",
                 {"analysis_seconds": 11}),
            ],
        )

    def test_private_download_denial_precedes_storage_read(self):
        job = self.job()
        job.update({
            "upload_path": "bucket/private/upload.mp4",
            "upload_generation": "1",
            "upload_expected_bytes": 123,
            "media_path": None,
        })
        denial = BillingProblem(
            "owner_quota_exhausted", "Monthly media allowance exhausted.", 429)
        with patch("sceneit.import_worker._commercial_enabled", return_value=True), \
                patch("sceneit.import_worker.connection", self.connection), \
                patch("sceneit.import_worker._update",
                      side_effect=lambda item, **values: item.update(values) or item), \
                patch("sceneit.quota.reserve", side_effect=denial), \
                patch("sceneit.private_storage.download_object") as download, \
                self.assertRaises(BillingProblem):
            import_worker._prepare_media(job)
        download.assert_not_called()

    def test_each_download_retry_has_a_distinct_reservation(self):
        job = self.job()
        operations = []

        def remember(_conn, _owner, operation_id, amounts):
            operations.append((operation_id, amounts))

        with patch("sceneit.import_worker._commercial_enabled", return_value=True), \
                patch("sceneit.import_worker.connection", self.connection), \
                patch("sceneit.quota.reserve", side_effect=remember):
            import_worker._reserve_media_read(job, 123, "validation")
            import_worker._reserve_media_read(job, 123, "validation")
        self.assertEqual([item[1] for item in operations],
                         [{"media_bytes": 123}, {"media_bytes": 123}])
        self.assertNotEqual(operations[0][0], operations[1][0])

    @patch("sceneit.import_worker._finish_lease")
    @patch("sceneit.import_worker._fail")
    def test_global_stop_defers_without_starting_deadline(self, fail, finish):
        job = self.job()
        job.update({
            "processing_started_at": None,
            "expires_at": datetime.now(timezone.utc),
        })
        client = Mock()
        updates = []

        def update(item, **values):
            updates.append(values)
            item.update(values)
            return item

        with patch("sceneit.import_worker._check_commercial_work",
                   side_effect=BillingProblem(
                       "service_work_stopped", "Work stopped.", 503)), \
                patch("sceneit.import_worker._update", side_effect=update):
            import_worker.process_job(job, client)
        fail.assert_not_called()
        client.create_index.assert_not_called()
        self.assertIsNone(updates[-1]["processing_started_at"])
        self.assertEqual(updates[-1]["error_code"], "service_work_stopped")


class UploadAttemptFenceTests(unittest.TestCase):
    def test_late_session_is_persisted_for_revocation(self):
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = {
            "state": "revoke_requested",
            "session_reference": "encrypted-reference",
        }
        row = record_session(
            conn, "attempt", "encrypted-reference",
            datetime.now(timezone.utc))
        statement = conn.execute.call_args.args[0]
        self.assertIn("'uncertain'", statement)
        self.assertIn("'revoke_requested'", statement)
        self.assertEqual(row["session_reference"], "encrypted-reference")


if __name__ == "__main__":
    unittest.main()