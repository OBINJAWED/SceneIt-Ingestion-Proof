"""Rehearse additive billing upgrades without touching the application database."""
import os
import unittest
import uuid
from datetime import datetime, timezone

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from sceneit import migrate


TEST_URL = os.environ.get("SCENEIT_TEST_DATABASE_URL", "")
SAFE_TEST_DATABASE = (
    bool(TEST_URL)
    and TEST_URL != os.environ.get("DATABASE_URL")
    and conninfo_to_dict(TEST_URL).get("dbname", "").startswith("sceneit_test")
)


@unittest.skipUnless(SAFE_TEST_DATABASE, "A separate disposable sceneit_test* database is required")
class BillingMigrationPreservationTests(unittest.TestCase):
    def test_populated_membership_and_lifetime_trial_survive_additive_upgrade(self):
        schema = f"sceneit_billing_migration_{uuid.uuid4().hex}"
        anchor = datetime(2026, 1, 31, tzinfo=timezone.utc)
        end = datetime(2026, 2, 28, tzinfo=timezone.utc)
        with psycopg.connect(TEST_URL, autocommit=True, row_factory=dict_row) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            try:
                conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                old = [item for item in migrate.load_migrations() if item.version <= 10]
                conn.execute(
                    "CREATE TABLE sceneit_schema_migrations("
                    "version integer PRIMARY KEY,name text NOT NULL UNIQUE,"
                    "sha256 text NOT NULL CHECK(length(sha256)=64),"
                    "applied_at timestamptz NOT NULL DEFAULT now())"
                )
                for item in old:
                    conn.execute(item.sql)
                    conn.execute(
                        "INSERT INTO sceneit_schema_migrations(version,name,sha256) VALUES(%s,%s,%s)",
                        (item.version, item.name, item.sha256),
                    )
                conn.execute(
                    "INSERT INTO sceneit_auth_users(id,first_name) "
                    "VALUES('migration-member','Fixture'),"
                    "('firebase:migration-trial','Fixture')"
                )
                conn.execute(
                    "INSERT INTO sceneit_billing_accounts(owner_id,customer_id,environment,"
                    "customer_attempt_state,allowance_anchor) "
                    "VALUES('migration-member','cus_migration','test','created',%s)", (anchor,)
                )
                conn.execute(
                    "INSERT INTO sceneit_billing_subscriptions(subscription_id,owner_id,customer_id,"
                    "environment,price_id,status,current_period_end) VALUES("
                    "'sub_migration','migration-member','cus_migration','test',"
                    "'price_historical','active',%s)", (end,)
                )
                conn.execute(
                    "INSERT INTO sceneit_paid_coverage(id,owner_id,subscription_id,"
                    "starts_at,ends_at,reversed) VALUES"
                    "('in_paid','migration-member','sub_migration',%s,%s,false),"
                    "('in_reversed','migration-member','sub_migration',%s,%s,true)",
                    (anchor, end, anchor, end),
                )
                conn.execute(
                    "INSERT INTO sceneit_usage_windows(scope,starts_at,ends_at,metric,allowance,used) "
                    "VALUES('owner:migration-member',%s,%s,'searches',100,73)", (anchor, end)
                )
                conn.execute(
                    "INSERT INTO sceneit_firebase_trial_ledgers(id) "
                    "VALUES('firebase-email-v1:migration-fixture')"
                )
                conn.execute(
                    "INSERT INTO sceneit_import_usage(owner_id,imports_used,searches_used) "
                    "VALUES('firebase-email-v1:migration-fixture',3,50)"
                )
                conn.execute(
                    "INSERT INTO sceneit_firebase_identities(id,project_id,issuer,firebase_uid,"
                    "owner_id,trial_ledger_id) VALUES(%s,'fixture','https://issuer.invalid',"
                    "'migration-uid','firebase:migration-trial','firebase-email-v1:migration-fixture')",
                    (uuid.uuid4(),),
                )
                before_coverage = conn.execute(
                    "SELECT id,owner_id,subscription_id,starts_at,ends_at,reversed "
                    "FROM sceneit_paid_coverage ORDER BY id"
                ).fetchall()
                applied = migrate.upgrade(conn=conn)
                self.assertTrue(applied)
                self.assertTrue(all(int(name[:3]) > 10 for name in applied))
                self.assertEqual([], migrate.upgrade(conn=conn))
                self.assertTrue(all(item["status"] == "applied" for item in migrate.status(conn=conn)))
                self.assertEqual(before_coverage, conn.execute(
                    "SELECT id,owner_id,subscription_id,starts_at,ends_at,reversed "
                    "FROM sceneit_paid_coverage ORDER BY id"
                ).fetchall())
                self.assertEqual(anchor, conn.execute(
                    "SELECT allowance_anchor FROM sceneit_billing_accounts"
                ).fetchone()["allowance_anchor"])
                self.assertEqual({"allowance": 100, "used": 73}, conn.execute(
                    "SELECT allowance,used FROM sceneit_usage_windows"
                ).fetchone())
                self.assertEqual({"imports_used": 3, "searches_used": 50}, conn.execute(
                    "SELECT imports_used,searches_used FROM sceneit_import_usage"
                ).fetchone())
                self.assertEqual("firebase-email-v1:migration-fixture", conn.execute(
                    "SELECT trial_ledger_id FROM sceneit_firebase_identities"
                ).fetchone()["trial_ledger_id"])
                self.assertEqual("price_historical", conn.execute(
                    "SELECT price_id FROM sceneit_billing_subscriptions"
                ).fetchone()["price_id"])
            finally:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))