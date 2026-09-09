"""Real isolated PostgreSQL races, durable accounting and rollover fixtures."""
import os
import json
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

from sceneit import import_limits, import_worker, migrate, quota
from sceneit.billing_config import BillingProblem, reset_billing_settings
from sceneit.billing_time import isolated_verification_time
from sceneit.config import reset_settings
from test_quota_policy import commercial_environment

TEST_URL = os.environ.get("SCENEIT_TEST_DATABASE_URL", "")
SAFE = (bool(TEST_URL) and TEST_URL != os.environ.get("DATABASE_URL")
        and conninfo_to_dict(TEST_URL).get("dbname", "").startswith("sceneit_test"))
UTC = timezone.utc


@unittest.skipUnless(SAFE, "A separate disposable sceneit_test* PostgreSQL database is required")
class QuotaPostgresTests(unittest.TestCase):
    def setUp(self):
        self.schema = "billing_clock_" + uuid.uuid4().hex
        with psycopg.connect(TEST_URL, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.env = commercial_environment() | {
            "DATABASE_URL": TEST_URL, "PILOT_ALLOWED_SUBJECTS": "owner-a,owner-b",
            # This legacy ceiling is intentionally below the paid upgrade
            # snapshot. Fully verified tier snapshots, not mutable old config,
            # define member entitlement.
            "SCENEIT_MEMBER_SEARCHES": "3", "SCENEIT_APP_SEARCHES": "4",
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
                    "INSERT INTO sceneit_paid_coverage"
                    "(id,owner_id,subscription_id,starts_at,ends_at,tier_key,tier_rank,"
                    "capabilities_snapshot,limits_snapshot) "
                    "VALUES (%s,%s,'sub_fixture','2024-01-31T10:30:00Z',"
                    "'2030-01-31T10:30:00Z','fixture_basic',10,%s::jsonb,%s::jsonb)",
                    (
                        "in_" + owner, owner,
                        json.dumps([
                            "imports", "uploads", "analysis", "searches",
                            "frames", "media",
                        ]),
                        json.dumps({
                            metric: (
                                2 if metric == "searches"
                                else int(self.env[f"SCENEIT_MEMBER_{metric.upper()}"])
                            )
                            for metric in (*quota.METRICS, "storage_bytes")
                        }),
                    ))
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

    @staticmethod
    def _capabilities():
        return ["imports", "uploads", "analysis", "searches", "frames", "media"]

    def _add_paid_upgrade(
            self, conn, owner="owner-a", search_limit=4, *,
            funds_coverage_id=None, rank=20, tier_key="fixture_plus"):
        operation_id, preview_id, change_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        invoice_id = f"in_upgrade_{uuid.uuid4().hex}"
        funds_coverage_id = funds_coverage_id or "in_" + owner
        limits = {metric: 100 for metric in (*quota.METRICS, "storage_bytes")}
        limits["searches"] = search_limit
        conn.execute(
            "INSERT INTO sceneit_billing_operations"
            "(operation_id,owner_id,kind,idempotency_key,parameters_hash,state) "
            "VALUES(%s,%s,'upgrade',%s,%s,'completed')",
            (operation_id, owner, uuid.uuid4(), "a" * 64),
        )
        conn.execute(
            "INSERT INTO sceneit_billing_change_previews"
            "(preview_id,owner_id,idempotency_key,parameters_hash,subscription_id,"
            "source_price_id,target_price_id,target_tier_key,target_cadence,currency,"
            "kind,subtotal,tax,total,proration_at,expires_at,state) VALUES "
            "(%s,%s,%s,%s,'sub_fixture','price_fixtureBasicMonthly',"
            "'price_fixturePlusMonthly',%s,'monthly','usd','upgrade',"
            "1000,0,1000,'2025-02-01','2030-01-01','confirmed')",
            (preview_id, owner, uuid.uuid4(), "b" * 64, tier_key),
        )
        conn.execute(
            "INSERT INTO sceneit_billing_changes"
            "(change_id,owner_id,preview_id,operation_id,subscription_id,kind,"
            "target_tier_key,target_cadence,currency,target_price_id,state,"
            "effective_at,funded_invoice_id) VALUES "
            "(%s,%s,%s,%s,'sub_fixture','upgrade',%s,'monthly','usd',"
            "'price_fixturePlusMonthly','effective','2025-02-01',%s)",
            (change_id, owner, preview_id, operation_id, tier_key, invoice_id),
        )
        conn.execute(
            "INSERT INTO sceneit_paid_coverage"
            "(id,owner_id,subscription_id,starts_at,ends_at,coverage_kind,"
            "funds_coverage_id,tier_key,tier_rank,capabilities_snapshot,limits_snapshot,"
            "provider_created_at) VALUES "
            "(%s,%s,'sub_fixture','2025-02-01','2030-01-01','upgrade',%s,"
            "%s,%s,%s::jsonb,%s::jsonb,'2025-02-01')",
            (
                invoice_id, owner, funds_coverage_id, tier_key, rank,
                json.dumps(self._capabilities()), json.dumps(limits),
            ),
        )
        return invoice_id

    def test_owner_and_application_races_are_atomic(self):
        results = self.race(["owner-a"] * 6)
        self.assertEqual(2, results.count("allowed"))
        self.assertEqual(4, results.count("owner_quota_exhausted"))
        results = self.race(["owner-b"] * 4)
        self.assertEqual(2, results.count("allowed"))
        self.assertEqual(2, results.count("service_capacity_exhausted"))

    def test_verified_upgrade_raises_ceiling_without_resetting_usage_or_anchor(self):
        now = datetime(2025, 2, 15, tzinfo=UTC)
        with patch("sceneit.quota._now", return_value=now), self.conn() as conn:
            quota.reserve(conn, "owner-a", "before-upgrade", {"searches": 2})
            anchor = conn.execute(
                "SELECT allowance_anchor FROM sceneit_billing_accounts "
                "WHERE owner_id='owner-a'"
            ).fetchone()["allowance_anchor"]
            self._add_paid_upgrade(conn)
            quota.reserve(conn, "owner-a", "after-upgrade", {"searches": 1})
            status = quota.usage_status("owner-a", conn)
            self.assertEqual((4, 3, 1), (
                status["metrics"]["searches"]["limit"],
                status["metrics"]["searches"]["used"],
                status["metrics"]["searches"]["remaining"],
            ))
            self.assertEqual(anchor, conn.execute(
                "SELECT allowance_anchor FROM sceneit_billing_accounts "
                "WHERE owner_id='owner-a'"
            ).fetchone()["allowance_anchor"])

    def test_refunded_upgrade_restores_base_ceiling_without_refunding_usage(self):
        now = datetime(2025, 2, 15, tzinfo=UTC)
        with patch("sceneit.quota._now", return_value=now), self.conn() as conn:
            quota.reserve(conn, "owner-a", "base-use", {"searches": 2})
            invoice_id = self._add_paid_upgrade(conn)
            quota.reserve(conn, "owner-a", "upgraded-use", {"searches": 1})
            conn.execute(
                "UPDATE sceneit_paid_coverage SET reversed=true WHERE id=%s",
                (invoice_id,),
            )
            with self.assertRaises(BillingProblem) as error:
                quota.reserve(conn, "owner-a", "after-refund", {"searches": 1})
            self.assertEqual("owner_quota_exhausted", error.exception.code)
            status = quota.usage_status("owner-a", conn)
            self.assertEqual((2, 3, 0), (
                status["metrics"]["searches"]["limit"],
                status["metrics"]["searches"]["used"],
                status["metrics"]["searches"]["remaining"],
            ))

    def test_three_tier_chain_refunds_ancestor_and_never_replenishes_usage(self):
        now = datetime(2025, 2, 15, tzinfo=UTC)
        with patch("sceneit.quota._now", return_value=now), self.conn() as conn:
            anchor = conn.execute(
                "SELECT allowance_anchor FROM sceneit_billing_accounts "
                "WHERE owner_id='owner-a'"
            ).fetchone()["allowance_anchor"]
            quota.reserve(conn, "owner-a", "chain-base-use", {"searches": 1})
            middle = self._add_paid_upgrade(conn, search_limit=4)
            quota.reserve(conn, "owner-a", "chain-middle-use", {"searches": 1})
            top = self._add_paid_upgrade(
                conn, search_limit=6, funds_coverage_id=middle,
                rank=30, tier_key="fixture_max",
            )
            quota.reserve(conn, "owner-a", "chain-top-use", {"searches": 1})
            # Replays do not debit the newly raised ceiling a second time.
            quota.reserve(conn, "owner-a", "chain-middle-use", {"searches": 1})
            quota.reserve(conn, "owner-a", "chain-top-use", {"searches": 1})
            self.assertEqual((6, 3), (
                quota.usage_status("owner-a", conn)["metrics"]["searches"]["limit"],
                quota.usage_status("owner-a", conn)["metrics"]["searches"]["used"],
            ))
            # Refunding C preserves valid B and does not replenish any usage.
            conn.execute(
                "UPDATE sceneit_paid_coverage SET reversed=true WHERE id=%s", (top,)
            )
            self.assertEqual((4, 3), (
                quota.usage_status("owner-a", conn)["metrics"]["searches"]["limit"],
                quota.usage_status("owner-a", conn)["metrics"]["searches"]["used"],
            ))
            # A replacement C still depends on B. Refunding B invalidates that
            # descendant and falls back to independently paid A.
            self._add_paid_upgrade(
                conn, search_limit=6, funds_coverage_id=middle,
                rank=30, tier_key="fixture_max",
            )
            conn.execute(
                "UPDATE sceneit_paid_coverage SET reversed=true WHERE id=%s",
                (middle,),
            )
            status = quota.usage_status("owner-a", conn)
            self.assertEqual((2, 3, 0), (
                status["metrics"]["searches"]["limit"],
                status["metrics"]["searches"]["used"],
                status["metrics"]["searches"]["remaining"],
            ))
            with self.assertRaises(BillingProblem) as error:
                quota.reserve(conn, "owner-a", "chain-after-refund", {"searches": 1})
            self.assertEqual("owner_quota_exhausted", error.exception.code)
            self.assertEqual(anchor, conn.execute(
                "SELECT allowance_anchor FROM sceneit_billing_accounts "
                "WHERE owner_id='owner-a'"
            ).fetchone()["allowance_anchor"])

    def test_malformed_upgrade_ancestor_cannot_fund_valid_descendant(self):
        now = datetime(2025, 2, 15, tzinfo=UTC)
        with patch("sceneit.quota._now", return_value=now), self.conn() as conn:
            middle = self._add_paid_upgrade(conn, search_limit=4)
            self._add_paid_upgrade(
                conn, search_limit=6, funds_coverage_id=middle,
                rank=30, tier_key="fixture_max",
            )
            conn.execute(
                "UPDATE sceneit_paid_coverage SET tier_key=NULL,tier_rank=NULL,"
                "capabilities_snapshot=NULL,limits_snapshot=NULL WHERE id=%s",
                (middle,),
            )
            selected = quota.effective_coverage(conn, "owner-a", now)
            self.assertEqual("in_owner-a", selected["coverage_id"])
            self.assertEqual(10, selected["rank"])

    def test_ancestor_refund_race_cannot_leave_descendant_entitled(self):
        now = datetime(2025, 2, 15, tzinfo=UTC)
        with patch("sceneit.quota._now", return_value=now), self.conn() as conn:
            quota.reserve(conn, "owner-a", "race-base-use", {"searches": 1})
            middle = self._add_paid_upgrade(conn, search_limit=4)
            self._add_paid_upgrade(
                conn, search_limit=6, funds_coverage_id=middle,
                rank=30, tier_key="fixture_max",
            )
        barrier = threading.Barrier(2)
        outcomes = []

        def reverse_ancestor():
            barrier.wait()
            with self.conn() as conn:
                conn.execute(
                    "SELECT owner_id FROM sceneit_billing_accounts "
                    "WHERE owner_id='owner-a' FOR UPDATE"
                )
                conn.execute(
                    "UPDATE sceneit_paid_coverage SET reversed=true WHERE id=%s",
                    (middle,),
                )
            outcomes.append("reversed")

        def reserve_descendant():
            barrier.wait()
            try:
                with self.conn() as conn:
                    quota.reserve(
                        conn, "owner-a", "race-descendant-use", {"searches": 1}
                    )
                outcomes.append("reserved")
            except BillingProblem as exc:
                outcomes.append(exc.code)

        with patch("sceneit.quota._now", return_value=now):
            threads = [
                threading.Thread(target=reverse_ancestor),
                threading.Thread(target=reserve_descendant),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(15)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            with self.conn() as conn:
                selected = quota.effective_coverage(conn, "owner-a", now)
                self.assertEqual("in_owner-a", selected["coverage_id"])
                used = quota.usage_status(
                    "owner-a", conn
                )["metrics"]["searches"]["used"]
                if used < 2:
                    quota.reserve(
                        conn, "owner-a", "race-fill-base", {"searches": 2 - used}
                    )
                with self.assertRaises(BillingProblem) as error:
                    quota.reserve(
                        conn, "owner-a", "race-after-refund", {"searches": 1}
                    )
                self.assertEqual("owner_quota_exhausted", error.exception.code)
        self.assertIn("reversed", outcomes)
        self.assertTrue(
            "reserved" in outcomes or "owner_quota_exhausted" in outcomes
        )

    def test_downgrade_blocks_new_work_and_retains_over_limit_media(self):
        now = datetime(2025, 2, 15, tzinfo=UTC)
        high = {metric: 100 for metric in (*quota.METRICS, "storage_bytes")}
        high["searches"] = 4
        low = dict(high, searches=2, storage_bytes=5)
        with patch("sceneit.quota._now", return_value=now), self.conn() as conn:
            # Establish this fixture as previously funded high-tier coverage
            # before any usage is admitted.
            conn.execute(
                "UPDATE sceneit_paid_coverage SET tier_key='fixture_plus',tier_rank=20,"
                "limits_snapshot=%s::jsonb WHERE id='in_owner-a'",
                (json.dumps(high),),
            )
            quota.reserve(conn, "owner-a", "high-tier-use", {"searches": 3})
            quota.reserve_storage(conn, "owner-a", "/retained-after-downgrade", 10)
            conn.execute(
                "UPDATE sceneit_paid_coverage SET ends_at=%s WHERE id='in_owner-a'",
                (now - timedelta(seconds=1),),
            )
            conn.execute(
                "INSERT INTO sceneit_paid_coverage"
                "(id,owner_id,subscription_id,starts_at,ends_at,tier_key,tier_rank,"
                "capabilities_snapshot,limits_snapshot) VALUES "
                "('in_downgrade','owner-a','sub_fixture',%s,'2030-01-01',"
                "'fixture_basic',10,%s::jsonb,%s::jsonb)",
                (
                    now - timedelta(seconds=1),
                    json.dumps(self._capabilities()),
                    json.dumps(low),
                ),
            )
            with self.assertRaises(BillingProblem) as error:
                quota.reserve(conn, "owner-a", "low-tier-new-work", {"searches": 1})
            self.assertEqual("owner_quota_exhausted", error.exception.code)
            status = quota.usage_status("owner-a", conn)
            self.assertEqual(
                {"limit": 5, "used": 10, "remaining": 0}, status["storage"]
            )
            self.assertIsNotNone(conn.execute(
                "SELECT object_key FROM sceneit_storage_reservations "
                "WHERE object_key='/retained-after-downgrade' AND state='reserved'"
            ).fetchone())

    def test_missing_capability_denies_before_debit(self):
        now = datetime(2025, 2, 15, tzinfo=UTC)
        with patch("sceneit.quota._now", return_value=now), self.conn() as conn:
            conn.execute(
                "UPDATE sceneit_paid_coverage SET capabilities_snapshot=%s::jsonb "
                "WHERE id='in_owner-a'",
                (json.dumps(["imports"]),),
            )
            with self.assertRaises(BillingProblem) as error:
                quota.reserve(conn, "owner-a", "search-without-feature", {"searches": 1})
            self.assertEqual("tier_capability_required", error.exception.code)
            self.assertEqual(0, conn.execute(
                "SELECT count(*) AS n FROM sceneit_usage_reservations"
            ).fetchone()["n"])

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

    def test_isolated_billing_time_rolls_annual_allowance_monthly(self):
        first = datetime(2025, 2, 28, 10, 30, tzinfo=UTC)
        second = datetime(2025, 3, 31, 10, 30, tzinfo=UTC)
        clock_url = make_conninfo(
            TEST_URL, options=f"-c search_path={self.schema}"
        )
        clock_env = {
            "SCENEIT_STRIPE_TEST_CLOCK_APPROVED": "true",
            "SCENEIT_TEST_CLOCK_DATABASE_URL": clock_url,
            "DATABASE_URL": "",
        }
        with self.conn() as conn:
            with isolated_verification_time(
                    conn, first, self.schema, environ=clock_env):
                quota.reserve(conn, "owner-a", "clock-first", {"searches": 2})
        with self.conn() as conn:
            with isolated_verification_time(
                    conn, second, self.schema, environ=clock_env):
                quota.reserve(conn, "owner-a", "clock-first", {"searches": 2})
                quota.reserve(conn, "owner-a", "clock-second", {"searches": 2})
                status = quota.usage_status("owner-a", conn)
        self.assertEqual(2, status["metrics"]["searches"]["used"])
        self.assertEqual("2025-03-31T10:30:00+00:00", status["windowStart"])
        self.assertEqual("2025-04-30T10:30:00+00:00", status["windowEnd"])

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

    def test_firebase_link_worker_storage_is_application_only(self):
        ledger = "firebase-email-v1:" + ("c" * 64)
        owner = "firebase:link-worker"
        import_id = uuid.uuid4()
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO sceneit_firebase_trial_ledgers(id) VALUES (%s)",
                (ledger,),
            )
            conn.execute(
                "INSERT INTO sceneit_auth_users"
                "(id,first_name,provider,email,email_verified) "
                "VALUES (%s,'Fixture','firebase','worker@example.com',true)",
                (owner,),
            )
            conn.execute(
                "INSERT INTO sceneit_firebase_identities"
                "(id,project_id,issuer,firebase_uid,owner_id,trial_ledger_id,email_hash) "
                "VALUES (%s,'fixture-project','https://fixture.invalid','worker-uid',"
                "%s,%s,%s)",
                (uuid.uuid4(), owner, ledger, "d" * 64),
            )
            conn.execute(
                "INSERT INTO sceneit_import_usage(owner_id,imports_used,searches_used) "
                "VALUES (%s,1,1)",
                (ledger,),
            )
            conn.execute(
                "INSERT INTO sceneit_imports"
                "(id,owner_id,idempotency_key,entry_method,source_kind,source_url,"
                "state,status_message,analysis_authorized) VALUES "
                "(%s,%s,%s,'link','vimeo','https://vimeo.com/123','queued',"
                "'Fixture',true)",
                (import_id, owner, uuid.uuid4()),
            )

        job = {
            "id": import_id,
            "owner_id": owner,
            "upload_path": None,
            "media_path": None,
            "media_generation": None,
            "source_kind": "vimeo",
            "source_url": "https://vimeo.com/123",
            "external_id": "123",
            "title": "Fixture",
            "file_size_bytes": None,
        }
        path = f"/fixture-private/imports/{import_id}/source.mp4"

        def resolve(_source, destination, progress):
            Path(destination).write_bytes(b"verified-media")
            progress(len(b"verified-media"), len(b"verified-media"))
            return {}

        def validate(current, _destination):
            current["file_size_bytes"] = len(b"verified-media")
            return False

        def update(current, **values):
            current.update(values)
            return current

        # The bounded resolver read is deliberately larger than the tiny
        # default test application cap. Raise only that application-wide cap;
        # this Firebase owner still has no billing account or paid limits.
        with patch.dict(os.environ, {
                "SCENEIT_APP_MEDIA_BYTES": str(import_limits.MAX_BYTES),
                "SCENEIT_DISABLE_PROVIDER_NETWORK": "1",
        }), patch("sceneit.import_worker.connection", self.conn), \
                patch("sceneit.import_worker.private_object_path", return_value=path), \
                patch("sceneit.import_worker._validate", side_effect=validate), \
                patch("sceneit.import_worker._update", side_effect=update), \
                patch("sceneit.platforms.resolve_link", side_effect=resolve), \
                patch("sceneit.private_storage.upload_private",
                      return_value={"generation": 7}):
            reset_billing_settings()
            try:
                import_worker._prepare_media(job)
            finally:
                reset_billing_settings()

        with self.conn() as conn:
            reservation = conn.execute(
                "SELECT owner_id,size_bytes,state FROM sceneit_storage_reservations "
                "WHERE object_key=%s",
                (path,),
            ).fetchone()
            self.assertEqual(
                (None, len(b"verified-media"), "reserved"),
                (reservation["owner_id"], reservation["size_bytes"], reservation["state"]),
            )
            self.assertEqual(
                owner,
                conn.execute(
                    "SELECT owner_id FROM sceneit_imports WHERE id=%s", (import_id,)
                ).fetchone()["owner_id"],
            )
            usage = conn.execute(
                "SELECT imports_used,searches_used FROM sceneit_import_usage "
                "WHERE owner_id=%s",
                (ledger,),
            ).fetchone()
            self.assertEqual((1, 1), (usage["imports_used"], usage["searches_used"]))
            self.assertEqual(0, quota._storage_used(conn, owner))
            self.assertEqual(len(b"verified-media"), quota._storage_used(conn))
            self.assertTrue(quota.release_storage(
                conn, path, "fixture: confirmed Firebase link cleanup"
            ))
            self.assertEqual(0, quota._storage_used(conn))
            self.assertEqual(
                owner,
                conn.execute(
                    "SELECT owner_id FROM sceneit_imports WHERE id=%s", (import_id,)
                ).fetchone()["owner_id"],
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