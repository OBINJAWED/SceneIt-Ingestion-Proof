"""Real isolated PostgreSQL races, durable accounting and rollover fixtures."""
import os
import subprocess
import sys
import threading
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from sceneit import import_limits, migrate, quota
from sceneit.billing_config import BillingProblem, reset_billing_settings
from sceneit.config import reset_settings
from test_quota_policy import commercial_environment

TEST_URL = os.environ.get("SCENEIT_TEST_DATABASE_URL", "")
SAFE = (bool(TEST_URL) and TEST_URL != os.environ.get("DATABASE_URL")
        and conninfo_to_dict(TEST_URL).get("dbname", "").startswith("sceneit_test"))
UTC = timezone.utc


@unittest.skipUnless(SAFE, "A separate disposable sceneit_test* PostgreSQL database is required")
class QuotaPostgresTests(unittest.TestCase):
    def setUp(self):
        self.schema = "sceneit_test_" + uuid.uuid4().hex
        with psycopg.connect(TEST_URL, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.env = commercial_environment() | {
            "DATABASE_URL": TEST_URL, "PILOT_ALLOWED_SUBJECTS": "owner-a,owner-b",
            "SCENEIT_MEMBER_SEARCHES": "2", "SCENEIT_APP_SEARCHES": "3",
        }
        self.enterContext(patch.dict(os.environ, self.env))
        reset_settings()
        reset_billing_settings()
        with self.conn() as conn:
            for migration in migrate.load_migrations():
                conn.execute(migration.sql)
            for owner in ("owner-a", "owner-b", "not-admitted"):
                conn.execute("INSERT INTO sceneit_auth_users(id,first_name) VALUES (%s,'Fixture')",
                             (owner,))
                conn.execute(
                    "INSERT INTO sceneit_billing_accounts(owner_id,environment,allowance_anchor) "
                    "VALUES (%s,'test','2024-01-31T10:30:00Z')", (owner,))
                conn.execute(
                    "INSERT INTO sceneit_paid_coverage(id,owner_id,subscription_id,starts_at,ends_at) "
                    "VALUES (%s,%s,'sub_fixture','2024-01-31T10:30:00Z','2030-01-31T10:30:00Z')",
                    ("in_" + owner, owner))
        self.addCleanup(self.drop)

    def drop(self):
        reset_settings()
        reset_billing_settings()
        with psycopg.connect(TEST_URL, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    @contextmanager
    def conn(self):
        with psycopg.connect(TEST_URL, row_factory=dict_row) as conn:
            conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(self.schema)))
            yield conn

    def race(self, owners):
        barrier = threading.Barrier(len(owners))
        results = []

        def request(owner):
            barrier.wait()
            try:
                with self.conn() as conn:
                    quota.reserve(conn, owner, uuid.uuid4().hex, {"searches": 1})
                results.append("allowed")
            except BillingProblem as exc:
                results.append(exc.code)
        threads = [threading.Thread(target=request, args=(owner,)) for owner in owners]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
        self.assertTrue(all(not t.is_alive() for t in threads))
        return results

    def test_owner_and_application_races_are_atomic(self):
        results = self.race(["owner-a"] * 6)
        self.assertEqual(2, results.count("allowed"))
        self.assertEqual(4, results.count("owner_quota_exhausted"))
        results = self.race(["owner-b"] * 4)
        self.assertEqual(1, results.count("allowed"))
        self.assertEqual(3, results.count("service_capacity_exhausted"))

    def test_annual_monthly_refresh_no_rollover_or_identity_reset(self):
        first = datetime(2025, 2, 28, 10, 30, tzinfo=UTC)
        next_month = datetime(2025, 3, 31, 10, 30, tzinfo=UTC)
        with patch("sceneit.quota._now", return_value=first), self.conn() as conn:
            quota.reserve(conn, "owner-a", "original", {"searches": 2})
        with patch("sceneit.quota._now", return_value=next_month), self.conn() as conn:
            quota.reserve(conn, "owner-a", "original", {"searches": 2})
            quota.reserve(conn, "owner-a", "new-month", {"searches": 2})
            current = quota.usage_status("owner-a", conn)
            self.assertEqual(2, current["metrics"]["searches"]["used"])
            self.assertEqual("2025-04-30T10:30:00+00:00", current["windowEnd"])
            self.assertEqual(2, conn.execute(
                "SELECT count(*) AS n FROM sceneit_usage_reservations").fetchone()["n"])
        with patch("sceneit.quota._now", return_value=next_month), self.conn() as conn:
            conn.execute("UPDATE sceneit_paid_coverage SET subscription_id='sub_replacement'")
            with self.assertRaisesRegex(BillingProblem, "Monthly"):
                quota.reserve(conn, "owner-a", "replacement", {"searches": 1})

    def test_current_coverage_admission_and_stop_checked_on_replay(self):
        with self.conn() as conn:
            quota.reserve(conn, "owner-a", "queued", {"searches": 1})
        for mutation, code in (
            ("UPDATE sceneit_work_control SET stopped=true", "service_work_stopped"),
            ("UPDATE sceneit_paid_coverage SET reversed=true", "membership_required"),
            ("UPDATE sceneit_paid_coverage SET ends_at='2024-02-01'", "membership_required"),
        ):
            with self.subTest(code=code), self.conn() as conn:
                conn.execute(mutation)
                with self.assertRaises(BillingProblem) as error:
                    quota.reserve(conn, "owner-a", "queued", {"searches": 1})
                self.assertEqual(code, error.exception.code)
                conn.rollback()
        with self.conn() as conn, self.assertRaises(BillingProblem) as error:
            quota.reserve(conn, "not-admitted", "bad-owner", {"searches": 1})
        self.assertEqual("pilot_not_admitted", error.exception.code)

    def test_storage_occupancy_persists_until_confirmed_release(self):
        with self.conn() as conn:
            quota.reserve_storage(conn, "owner-a", "/pending", 80)
        with self.conn() as conn:
            quota.reserve_storage(conn, "owner-a", "/pending", 80)
            with self.assertRaises(BillingProblem) as error:
                quota.reserve_storage(conn, "owner-b", "/other", 21)
            self.assertEqual("service_capacity_exhausted", error.exception.code)
            self.assertEqual(80, quota.usage_status("owner-a", conn)["storage"]["used"])
        with self.conn() as conn:
            conn.execute("UPDATE sceneit_work_control SET stopped=true")
            quota.release_storage(conn, "/pending", "fixture: confirmed revoked and generation deleted")
            quota.release_storage(conn, "/pending", "fixture: duplicate cleanup")
            self.assertEqual(0, quota.usage_status("owner-a", conn)["storage"]["used"])
            self.assertEqual(1, conn.execute(
                "SELECT count(*) AS n FROM sceneit_budget_audit").fetchone()["n"])

    def test_multi_metric_denial_rolls_back_even_when_caught(self):
        with self.conn() as conn:
            with self.assertRaises(BillingProblem):
                quota.reserve(conn, "owner-a", "too-many", {"imports": 1, "searches": 4})
            self.assertEqual(0, conn.execute(
                "SELECT count(*) AS n FROM sceneit_usage_reservations").fetchone()["n"])
            self.assertEqual(0, conn.execute(
                "SELECT COALESCE(sum(used),0) AS n FROM sceneit_usage_windows").fetchone()["n"])

    def test_shared_proof_app_only_and_audited_unused_release(self):
        with self.conn() as conn:
            quota.reserve(conn, None, "proof-search", {"searches": 1}, require_membership=False)
            self.assertEqual(0, quota.usage_status("owner-a", conn)["metrics"]["searches"]["used"])
            quota.reserve(conn, "owner-a", "never-submitted", {"searches": 1})
            self.assertEqual(1, quota.release_unused(conn, "never-submitted", "fixture:no request sent"))
            self.assertEqual(0, quota.release_unused(conn, "never-submitted", "fixture:repeat"))
            with self.assertRaises(BillingProblem) as error:
                quota.reserve(conn, "owner-a", "never-submitted", {"searches": 1})
            self.assertEqual("reservation_released", error.exception.code)

    def test_firebase_trial_worker_and_lifetime_ledgers_survive_recreation(self):
        ledger = "firebase-email-v1:" + ("a" * 64)
        owners = ("firebase:original", "firebase:recreated")
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO sceneit_firebase_trial_ledgers(id) VALUES (%s)",
                (ledger,),
            )
            for index, owner in enumerate(owners):
                conn.execute(
                    "INSERT INTO sceneit_auth_users"
                    "(id,first_name,provider,email,email_verified) "
                    "VALUES (%s,'Fixture','firebase','person@example.com',true)",
                    (owner,),
                )
                conn.execute(
                    "INSERT INTO sceneit_firebase_identities"
                    "(id,project_id,issuer,firebase_uid,owner_id,trial_ledger_id,"
                    "email_hash) VALUES (%s,'fixture-project','https://fixture.invalid',"
                    "%s,%s,%s,%s)",
                    (
                        uuid.uuid4(), f"uid-{index}", owner, ledger,
                        "b" * 64,
                    ),
                )

            # These are the same helpers called by worker entry and media reads.
            # Neither owner has a billing account, paid coverage, or pilot entry.
            quota.check_work(conn, owners[0])
            quota.reserve(
                conn, owners[0], "firebase-worker-media",
                {"media_bytes": 10},
            )
            quota.reserve(
                conn, owners[1], "firebase-worker-frame", {"frames": 1},
            )
            reservations = conn.execute(
                "SELECT operation_id,owner_id FROM sceneit_usage_reservations "
                "WHERE operation_id LIKE 'firebase-worker-%' ORDER BY operation_id"
            ).fetchall()
            self.assertEqual(
                [
                    ("firebase-worker-frame", None),
                    ("firebase-worker-media", None),
                ],
                [(row["operation_id"], row["owner_id"]) for row in reservations],
            )
            scopes = conn.execute(
                "SELECT DISTINCT scope FROM sceneit_usage_windows"
            ).fetchall()
            self.assertEqual(["app"], [row["scope"] for row in scopes])

            self.assertEqual(
                (True, None),
                import_limits.reserve_import_operation(
                    conn, owners[0], "firebase-import-original"
                ),
            )
            self.assertEqual(
                (True, None),
                import_limits.reserve_import_operation(
                    conn, owners[1], "firebase-import-recreated"
                ),
            )
            self.assertEqual(
                (True, None),
                import_limits.reserve_search_operation(
                    conn, owners[0], "firebase-search-original"
                ),
            )
            usage = import_limits.usage_values(conn, owners[1])
            self.assertEqual(
                (2, 1), (usage["imports_used"], usage["searches_used"])
            )
            ledger_usage = conn.execute(
                "SELECT imports_used,searches_used "
                "FROM sceneit_import_usage WHERE owner_id=%s",
                (ledger,),
            ).fetchone()
            self.assertEqual(
                (2, 1),
                (ledger_usage["imports_used"], ledger_usage["searches_used"]),
            )
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT count(*) AS n FROM sceneit_usage_reservations "
                    "WHERE operation_id LIKE 'firebase-import-%' "
                    "OR operation_id LIKE 'firebase-search-%'"
                ).fetchone()["n"],
            )

    def test_restart_and_same_operation_race_preserve_one_reservation(self):
        fixture_url = make_conninfo(TEST_URL, options=f"-c search_path={self.schema}")
        command = (
            "from sceneit.db import connection; from sceneit.quota import reserve\n"
            "with connection() as c: reserve(c,'owner-a','restart-operation',{'searches':1})\n"
        )
        child_env = os.environ.copy()
        child_env.update(self.env | {"DATABASE_URL": fixture_url, "SCENEIT_DISABLE_PROVIDER_NETWORK": "1"})
        for _ in range(2):
            subprocess.run([sys.executable, "-c", command], env=child_env, check=True,
                           timeout=15, capture_output=True)
        with self.conn() as conn:
            self.assertEqual(1, quota.usage_status("owner-a", conn)["metrics"]["searches"]["used"])

    def test_period_edge_parallel_operations_have_single_current_window(self):
        edge = datetime(2025, 2, 28, 10, 30, tzinfo=UTC)
        with patch("sceneit.quota._now", return_value=edge):
            self.assertEqual(2, self.race(["owner-a"] * 4).count("allowed"))
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT starts_at,used FROM sceneit_usage_windows WHERE scope='owner:owner-a' "
                "AND metric='searches'").fetchall()
            self.assertEqual([(edge, 2)], [(r["starts_at"], r["used"]) for r in rows])

    def test_pilot_objects_added_after_migration_count_at_activation(self):
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO sceneit_imports(id,owner_id,idempotency_key,entry_method,"
                "source_kind,state,status_message,analysis_authorized,upload_path,"
                "upload_expected_bytes) VALUES(%s,'owner-a',%s,'upload','file',"
                "'awaiting_upload','Fixture',true,'/late-pilot-object',80)",
                (uuid.uuid4(), uuid.uuid4()))
            self.assertEqual(80, quota.usage_status("owner-a", conn)["storage"]["used"])
            with self.assertRaises(BillingProblem) as error:
                quota.reserve_storage(conn, "owner-b", "/commercial-object", 21)
            self.assertEqual("service_capacity_exhausted", error.exception.code)

    def test_shared_proof_storage_debits_application_only(self):
        with self.conn() as conn:
            quota.reserve_storage(conn, None, "/proof-still", 80, require_membership=False)
            self.assertEqual(0, quota.usage_status("owner-a", conn)["storage"]["used"])
            with self.assertRaises(BillingProblem) as error:
                quota.reserve_storage(conn, "owner-a", "/member-object", 21)
            self.assertEqual("service_capacity_exhausted", error.exception.code)

    def _pilot_object(self, path):
        import_id = uuid.uuid4()
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO sceneit_imports(id,owner_id,idempotency_key,entry_method,"
                "source_kind,state,status_message,analysis_authorized,upload_path,"
                "upload_expected_bytes) VALUES(%s,'owner-a',%s,'upload','file',"
                "'cancel_requested','Fixture',true,%s,80)",
                (import_id, uuid.uuid4(), path))
        return import_id

    def _assert_cleanup_survives_restart(self, path):
        fixture_url = make_conninfo(TEST_URL, options=f"-c search_path={self.schema}")
        child_env = os.environ.copy()
        child_env.update(self.env | {
            "DATABASE_URL": fixture_url, "SCENEIT_DISABLE_PROVIDER_NETWORK": "1",
            "SCENEIT_BILLING_ENABLED": "false",
        })
        command = (
            "from sceneit.db import connection\n"
            "from sceneit.quota import release_storage, _storage_used\n"
            "with connection() as c:\n"
            f" assert not release_storage(c,{path!r},'fixture: repeated confirmed cleanup')\n"
            " assert _storage_used(c,'owner-a') == 0\n"
            " assert _storage_used(c) == 0\n"
        )
        subprocess.run(
            [sys.executable, "-c", command], env=child_env, check=True,
            timeout=15, capture_output=True)

    def test_migrated_storage_deleted_while_disabled_releases_exactly_once(self):
        path = "/migrated-pilot-object"
        import_id = self._pilot_object(path)
        with self.conn() as conn:
            # This is the occupancy row migration 008 creates for pilot media.
            conn.execute(
                "INSERT INTO sceneit_storage_reservations(object_key,owner_id,size_bytes) "
                "VALUES(%s,'owner-a',80)", (path,))
        with patch.dict(os.environ, {"SCENEIT_BILLING_ENABLED": "false"}):
            reset_billing_settings()
            with self.conn() as conn:
                self.assertTrue(quota.release_storage(
                    conn, path, "fixture: confirmed revoked and deleted while disabled"))
                self.assertFalse(quota.release_storage(conn, path, "fixture: duplicate cleanup"))
                self.assertEqual(0, quota._storage_used(conn, "owner-a"))
                self.assertEqual(0, quota._storage_used(conn))
                conn.execute("DELETE FROM sceneit_imports WHERE id=%s", (import_id,))
        reset_billing_settings()
        self._assert_cleanup_survives_restart(path)
        with self.conn() as conn:
            self.assertEqual(0, quota.usage_status("owner-a", conn)["storage"]["used"])
            quota.reserve_storage(conn, "owner-b", "/replacement-after-disabled", 100)
            self.assertEqual(1, conn.execute(
                "SELECT count(*) AS n FROM sceneit_budget_audit "
                "WHERE action='storage_released' AND reference=%s", (path,)).fetchone()["n"])

    def test_unjournaled_pilot_cleanup_masks_stale_references_after_activation(self):
        from sceneit.upload_attempts import confirm_object_absence
        path = "/post-migration-pilot-object"
        import_id = self._pilot_object(path)
        with patch.dict(os.environ, {"SCENEIT_BILLING_ENABLED": "false"}):
            reset_billing_settings()
            with self.conn() as conn:
                quota.reserve_storage(conn, "owner-a", path, 80)
                self.assertIsNone(conn.execute(
                    "SELECT object_key FROM sceneit_storage_reservations WHERE object_key=%s",
                    (path,)).fetchone())
        reset_billing_settings()
        with self.conn() as conn:
            self.assertEqual(80, quota.usage_status("owner-a", conn)["storage"]["used"])
            confirm_object_absence(conn, path, "fixture: confirmed post-activation deletion")
            confirm_object_absence(conn, path, "fixture: duplicate confirmed absence")
            # The import still references the object; the absence journal, not
            # deleting historical import records, determines real occupancy.
            self.assertEqual(0, quota.usage_status("owner-a", conn)["storage"]["used"])
            self.assertEqual(0, quota._storage_used(conn))
            row = conn.execute(
                "SELECT owner_id,size_bytes,state FROM sceneit_storage_reservations "
                "WHERE object_key=%s", (path,)).fetchone()
            self.assertEqual(("owner-a", 80, "released"),
                             (row["owner_id"], row["size_bytes"], row["state"]))
            conn.execute("DELETE FROM sceneit_imports WHERE id=%s", (import_id,))
        self._assert_cleanup_survives_restart(path)
        with self.conn() as conn:
            quota.reserve_storage(conn, "owner-b", "/replacement-after-activation", 100)
            self.assertEqual(1, conn.execute(
                "SELECT count(*) AS n FROM sceneit_budget_audit "
                "WHERE action='storage_released' AND reference=%s", (path,)).fetchone()["n"])