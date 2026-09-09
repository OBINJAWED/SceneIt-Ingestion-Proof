"""Real isolated PostgreSQL upload fencing and queue-recovery fixtures.

Storage and analysis providers are always patched; only PostgreSQL is real.
"""
import os
import threading
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import psycopg
from google.api_core.exceptions import NotFound
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from sceneit import import_worker, migrate, quota, upload_attempts
from sceneit.billing_config import BillingProblem, reset_billing_settings
from sceneit.config import reset_settings
from test_quota_policy import commercial_environment


TEST_URL = os.environ.get("SCENEIT_TEST_DATABASE_URL", "")
try:
    TEST_DB = conninfo_to_dict(TEST_URL).get("dbname", "") if TEST_URL else ""
except psycopg.Error:
    TEST_DB = ""
SAFE = bool(TEST_URL) and TEST_URL != os.environ.get("DATABASE_URL") and (
    TEST_DB.startswith("sceneit_test"))


@unittest.skipUnless(
    SAFE, "A separate disposable sceneit_test* PostgreSQL database is required")
class UploadAttemptsPostgresTests(unittest.TestCase):
    owner = "owner-a"

    def setUp(self):
        self.schema = "sceneit_test_" + uuid.uuid4().hex
        with psycopg.connect(TEST_URL, autocommit=True) as conn:
            conn.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.env = commercial_environment() | {
            "DATABASE_URL": TEST_URL,
            "PILOT_ALLOWED_SUBJECTS": self.owner,
            "SCENEIT_MEMBER_MEDIA_BYTES": "100",
            "SCENEIT_APP_MEDIA_BYTES": "100",
            "SCENEIT_MEMBER_STORAGE_BYTES": "100",
            "SCENEIT_APP_STORAGE_BYTES": "100",
            "SCENEIT_DISABLE_PROVIDER_NETWORK": "1",
        }
        self.enterContext(patch.dict(os.environ, self.env))
        reset_settings()
        reset_billing_settings()
        with self.conn() as conn:
            for migration in migrate.load_migrations():
                conn.execute(migration.sql)
            conn.execute(
                "INSERT INTO sceneit_auth_users(id,first_name) "
                "VALUES(%s,'Fixture')", (self.owner,))
            conn.execute(
                "INSERT INTO sceneit_billing_accounts"
                "(owner_id,environment,allowance_anchor) "
                "VALUES(%s,'test','2024-01-31T10:30:00Z')", (self.owner,))
            conn.execute(
                "INSERT INTO sceneit_paid_coverage"
                "(id,owner_id,subscription_id,starts_at,ends_at,tier_key,tier_rank,"
                "capabilities_snapshot,limits_snapshot) "
                "VALUES('in_fixture',%s,'sub_fixture',"
                "'2024-01-31T10:30:00Z','2030-01-31T10:30:00Z',"
                "'fixture_basic',10,"
                "'[\"imports\",\"uploads\",\"analysis\",\"searches\",\"frames\",\"media\"]',"
                "'{\"imports\":100,\"upload_attempts\":100,\"analysis_seconds\":100,"
                "\"searches\":100,\"media_bytes\":100,\"frames\":100,"
                "\"storage_bytes\":100}')",
                (self.owner,))
        self.addCleanup(self.drop)

    def drop(self):
        reset_settings()
        reset_billing_settings()
        with psycopg.connect(TEST_URL, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(
                    sql.Identifier(self.schema)))

    @contextmanager
    def conn(self):
        with psycopg.connect(TEST_URL, row_factory=dict_row) as conn:
            conn.execute(
                sql.SQL("SET LOCAL search_path TO {}").format(
                    sql.Identifier(self.schema)))
            yield conn

    def new_import(self, state="file_required"):
        import_id = uuid.uuid4()
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO sceneit_imports"
                "(id,owner_id,idempotency_key,entry_method,source_kind,state,"
                "status_message,analysis_authorized,playback_authorized) "
                "VALUES(%s,%s,%s,'upload','file',%s,'fixture',true,false)",
                (import_id, self.owner, uuid.uuid4(), state))
        return import_id

    def begin(self, import_id, attempt_id=None, path=None, size=10):
        attempt_id = attempt_id or uuid.uuid4()
        path = path or f"bucket/private/{attempt_id}.mp4"
        with self.conn() as conn:
            conn.execute(
                "SELECT id FROM sceneit_imports WHERE id=%s FOR UPDATE",
                (import_id,)).fetchone()
            quota.reserve_storage(conn, self.owner, path, size)
            upload_attempts.create_attempt(
                conn, import_id, self.owner, attempt_id, path, size)
        return attempt_id, path

    def test_concurrent_attempt_begin_allows_one_initiation(self):
        import_id = self.new_import()
        barrier = threading.Barrier(2)
        results = []

        def contender():
            barrier.wait()
            try:
                self.begin(import_id)
                results.append("started")
            except upload_attempts.UploadAttemptBusy:
                results.append("busy")

        threads = [threading.Thread(target=contender) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertCountEqual(results, ["started", "busy"])
        with self.conn() as conn:
            self.assertEqual(1, conn.execute(
                "SELECT count(*) AS n FROM sceneit_upload_attempts"
            ).fetchone()["n"])
            self.assertEqual(1, conn.execute(
                "SELECT count(*) AS n FROM sceneit_storage_reservations "
                "WHERE state='reserved'").fetchone()["n"])

    def test_cancel_during_initiation_persists_late_session_without_resurrection(self):
        import_id = self.new_import()
        attempt_id, _path = self.begin(import_id)
        initiation_in_flight = threading.Event()
        provider_returns = threading.Event()
        errors = []

        def late_result():
            initiation_in_flight.set()
            provider_returns.wait(10)
            try:
                with self.conn() as conn:
                    upload_attempts.record_session(
                        conn, attempt_id, "encrypted-late-session",
                        datetime.now(timezone.utc) + timedelta(minutes=15))
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=late_result)
        thread.start()
        self.assertTrue(initiation_in_flight.wait(5))
        with self.conn() as conn:
            conn.execute(
                "UPDATE sceneit_imports SET state='cancel_requested' WHERE id=%s",
                (import_id,))
            upload_attempts.request_revocation(conn, import_id)
        provider_returns.set()
        thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        with self.conn() as conn:
            attempt = conn.execute(
                "SELECT state,session_reference FROM sceneit_upload_attempts "
                "WHERE id=%s", (attempt_id,)).fetchone()
            item = conn.execute(
                "SELECT state FROM sceneit_imports WHERE id=%s",
                (import_id,)).fetchone()
        self.assertEqual("revoke_requested", attempt["state"])
        self.assertEqual("encrypted-late-session", attempt["session_reference"])
        self.assertEqual("cancel_requested", item["state"])

    def test_uncertain_without_url_never_cleans_or_releases(self):
        import_id = self.new_import()
        attempt_id, path = self.begin(import_id)
        upload_attempts.mark_uncertain(attempt_id, self.conn)
        with patch("sceneit.private_storage.cancel_upload_session") as cancel, \
                patch("sceneit.private_storage.object_info") as info:
            self.assertFalse(
                upload_attempts.cleanup_attempt(attempt_id, self.conn))
        cancel.assert_not_called()
        info.assert_not_called()
        with self.conn() as conn:
            self.assertEqual("uncertain", conn.execute(
                "SELECT state FROM sceneit_upload_attempts WHERE id=%s",
                (attempt_id,)).fetchone()["state"])
            self.assertEqual("reserved", conn.execute(
                "SELECT state FROM sceneit_storage_reservations "
                "WHERE object_key=%s", (path,)).fetchone()["state"])
            self.assertEqual(0, conn.execute(
                "SELECT count(*) AS n FROM sceneit_budget_audit"
            ).fetchone()["n"])

    def test_confirmed_absent_attempt_releases_exactly_once(self):
        import_id = self.new_import()
        attempt_id, path = self.begin(import_id)
        with self.conn() as conn:
            upload_attempts.record_session(
                conn, attempt_id, "encrypted-session",
                datetime.now(timezone.utc) + timedelta(minutes=15))
            upload_attempts.request_revocation(conn, import_id)
        with patch("sceneit.private_storage.decrypt_upload_session",
                   return_value="https://storage.invalid/session"), \
                patch("sceneit.private_storage.cancel_upload_session") as cancel, \
                patch("sceneit.private_storage.object_info",
                      side_effect=NotFound("fixture absent")):
            self.assertTrue(
                upload_attempts.cleanup_attempt(attempt_id, self.conn))
            self.assertTrue(
                upload_attempts.cleanup_attempt(attempt_id, self.conn))
        cancel.assert_called_once()
        with self.conn() as conn:
            self.assertEqual("revoked", conn.execute(
                "SELECT state FROM sceneit_upload_attempts WHERE id=%s",
                (attempt_id,)).fetchone()["state"])
            self.assertEqual("released", conn.execute(
                "SELECT state FROM sceneit_storage_reservations "
                "WHERE object_key=%s", (path,)).fetchone()["state"])
            self.assertEqual(1, conn.execute(
                "SELECT count(*) AS n FROM sceneit_budget_audit "
                "WHERE action='storage_released'").fetchone()["n"])

    def test_original_attempt_retains_media_shared_by_ready_duplicate(self):
        original = self.new_import("ready")
        duplicate = self.new_import("ready")
        attempt_id, path = self.begin(original)
        with self.conn() as conn:
            upload_attempts.record_session(
                conn, attempt_id, "encrypted-session",
                datetime.now(timezone.utc) + timedelta(minutes=15))
            upload_attempts.record_generation(conn, attempt_id, "7")
            conn.execute(
                "UPDATE sceneit_imports SET media_path=%s,media_generation='7',"
                "upload_generation='7',state='cancel_requested' WHERE id=%s",
                (path, original))
            conn.execute(
                "UPDATE sceneit_imports SET media_path=%s,media_generation='7',"
                "state='ready' WHERE id=%s", (path, duplicate))
            upload_attempts.request_revocation(conn, original)
        with patch("sceneit.private_storage.decrypt_upload_session",
                   return_value="https://storage.invalid/session"), \
                patch("sceneit.private_storage.cancel_upload_session") as cancel, \
                patch("sceneit.private_storage.object_info") as info, \
                patch("sceneit.private_storage.delete_object") as delete:
            self.assertTrue(
                upload_attempts.cleanup_attempt(attempt_id, self.conn))
        cancel.assert_called_once()
        info.assert_not_called()
        delete.assert_not_called()
        with self.conn() as conn:
            retained = conn.execute(
                "SELECT state,session_reference,generation "
                "FROM sceneit_upload_attempts WHERE id=%s",
                (attempt_id,)).fetchone()
            self.assertEqual("revoke_requested", retained["state"])
            self.assertIsNone(retained["session_reference"])
            self.assertEqual("7", retained["generation"])
            self.assertEqual("reserved", conn.execute(
                "SELECT state FROM sceneit_storage_reservations "
                "WHERE object_key=%s", (path,)).fetchone()["state"])
            conn.execute(
                "UPDATE sceneit_imports SET state='cancel_requested' WHERE id=%s",
                (duplicate,))
        with patch("sceneit.private_storage.object_info",
                   side_effect=NotFound("fixture absent")):
            self.assertTrue(
                upload_attempts.cleanup_attempt(attempt_id, self.conn))
        with self.conn() as conn:
            self.assertEqual("revoked", conn.execute(
                "SELECT state FROM sceneit_upload_attempts WHERE id=%s",
                (attempt_id,)).fetchone()["state"])
            self.assertEqual("released", conn.execute(
                "SELECT state FROM sceneit_storage_reservations "
                "WHERE object_key=%s", (path,)).fetchone()["state"])

    def test_pilot_replacement_cleanup_survives_commercial_reenable(self):
        import_id = self.new_import()
        first_id, second_id = uuid.uuid4(), uuid.uuid4()
        first_path = f"bucket/private/{first_id}.mp4"
        second_path = f"bucket/private/{second_id}.mp4"
        with patch.dict(os.environ, {"SCENEIT_BILLING_ENABLED": "false"}):
            reset_billing_settings()
            with self.conn() as conn:
                upload_attempts.create_attempt(
                    conn, import_id, self.owner, first_id, first_path, 10)
                upload_attempts.record_session(
                    conn, first_id, "encrypted-pilot-session",
                    datetime.now(timezone.utc) + timedelta(minutes=15))
                upload_attempts.create_attempt(
                    conn, import_id, self.owner, second_id, second_path, 10)
            with patch("sceneit.private_storage.decrypt_upload_session",
                       return_value="https://storage.invalid/session"), \
                    patch("sceneit.private_storage.cancel_upload_session") as cancel, \
                    patch("sceneit.private_storage.object_info",
                          side_effect=NotFound("fixture absent")):
                upload_attempts.cleanup_requested(
                    connection_factory=self.conn)
            cancel.assert_called_once()
        reset_billing_settings()
        with self.conn() as conn:
            states = {
                row["id"]: row["state"] for row in conn.execute(
                    "SELECT id,state FROM sceneit_upload_attempts "
                    "WHERE import_id=%s", (import_id,)).fetchall()
            }
        self.assertEqual("revoked", states[first_id])
        self.assertEqual("initiating", states[second_id])

    def test_media_denial_makes_zero_storage_or_provider_calls(self):
        with self.conn() as conn:
            quota.reserve(
                conn, self.owner, "fill-media", {"media_bytes": 100})
            quota.reserve(
                conn, self.owner, "fill-analysis", {"analysis_seconds": 100})
        job = {
            "id": str(uuid.uuid4()), "owner_id": self.owner,
            "state": "validating", "upload_path": "bucket/private/source.mp4",
            "upload_generation": "1", "upload_expected_bytes": 10,
            "media_path": None,
        }
        with patch("sceneit.import_worker.connection", self.conn), \
                patch("sceneit.import_worker._update",
                      side_effect=lambda item, **values: item.update(values) or item), \
                patch("sceneit.private_storage.download_object") as download, \
                self.assertRaises(BillingProblem) as denied:
            import_worker._prepare_media(job)
        download.assert_not_called()
        self.assertEqual("service_capacity_exhausted", denied.exception.code)
        provider = Mock()
        provider_job = {
            "id": str(uuid.uuid4()), "owner_id": self.owner,
            "state": "processing", "provider_write_marker": None,
            "index_id": None, "asset_id": None, "indexed_asset_id": None,
            "duration_seconds": 10.0, "sha256": "a" * 64,
            "has_audio": True,
        }
        with patch("sceneit.import_worker.connection", self.conn), \
                patch("sceneit.import_worker._assert_fence"), \
                self.assertRaises(BillingProblem):
            import_worker._provider_step(provider_job, provider)
        provider.create_index.assert_not_called()
        provider.upload_asset.assert_not_called()
        provider.index_asset.assert_not_called()

    def test_stopped_queue_is_deferred_without_deadline_or_provider(self):
        with self.conn() as conn:
            conn.execute("UPDATE sceneit_work_control SET stopped=true")
        job = {
            "id": str(uuid.uuid4()), "owner_id": self.owner,
            "state": "queued", "provider_write_marker": None,
            "processing_started_at": None, "expires_at": datetime.now(timezone.utc),
            "lease_token": str(uuid.uuid4()), "index_id": None,
            "asset_id": None, "indexed_asset_id": None,
        }
        updates = []

        def update(item, **values):
            updates.append(values)
            item.update(values)
            return item

        provider = Mock()
        with patch("sceneit.import_worker.connection", self.conn), \
                patch("sceneit.import_worker._update", side_effect=update), \
                patch("sceneit.import_worker._finish_lease"):
            import_worker.process_job(job, provider)
        provider.create_index.assert_not_called()
        self.assertEqual("queued", job["state"])
        self.assertIsNone(updates[-1]["processing_started_at"])
        self.assertEqual("service_work_stopped", updates[-1]["error_code"])


if __name__ == "__main__":
    unittest.main()