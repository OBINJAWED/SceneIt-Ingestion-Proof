"""Disposable-PostgreSQL migration, resource, and restore rehearsals.

Set SCENEIT_TEST_DATABASE_URL to a disposable local PostgreSQL database.  The
suite refuses to use DATABASE_URL, creates isolated schemas/databases, and never
calls a media or search provider.
"""
import os
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import psycopg
from flask import Flask
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from sceneit import migrate, resources
from sceneit.config import ConfigError, reset_settings, settings
from sceneit.db import DatabaseResourceExhausted, connection
from sceneit.health import health_bp
from sceneit.firebase_auth import FirebaseConfig
from sceneit.trial_identity import trial_ledger_id, usage_owner


TEST_URL = os.environ.get("SCENEIT_TEST_DATABASE_URL", "")
SAFE_TEST_DATABASE = (
    bool(TEST_URL)
    and TEST_URL != os.environ.get("DATABASE_URL")
    and conninfo_to_dict(TEST_URL).get("dbname", "").startswith("sceneit_test")
)


def _migration_sql(version):
    path = next(migrate.migration_directory().glob(f"{version:03d}_*.sql"))
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().upper() not in {"BEGIN;", "COMMIT;"}
    )


@unittest.skipUnless(
    SAFE_TEST_DATABASE,
    "SCENEIT_TEST_DATABASE_URL must name a disposable sceneit_test* database "
    "and differ from DATABASE_URL",
)
class PersistencePostgresTests(unittest.TestCase):
    def setUp(self):
        # Capacity fixtures must not share flock slots with the running app.
        # PostgreSQL/schema isolation alone does not isolate host-local locks.
        self.slots = self.enterContext(tempfile.TemporaryDirectory(prefix="sceneit-test-slots-"))
        self.schema = f"sceneit_test_{uuid.uuid4().hex}"
        with psycopg.connect(TEST_URL, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_URL": TEST_URL,
                "PILOT_ALLOWED_SUBJECTS": "pilot-a,pilot-b",
                "SCENEIT_DATABASE_PERMITS": "2",
                "SCENEIT_DB_SLOT_DIRECTORY": self.slots,
                "SCENEIT_SEARCH_PERMITS": "2",
                "SCENEIT_MAX_OPERATION_SECONDS": "10",
                "SCENEIT_PERMIT_LEASE_SECONDS": "20",
            },
            clear=False,
        )
        self.environment.start()
        reset_settings()

    def tearDown(self):
        reset_settings()
        self.environment.stop()
        with psycopg.connect(TEST_URL, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(self.schema)
                )
            )

    @contextmanager
    def conn(self):
        with psycopg.connect(
            TEST_URL, autocommit=True, row_factory=dict_row
        ) as conn:
            conn.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema))
            )
            yield conn

    def test_fresh_repeat_checksum_and_metadata_schema(self):
        with self.conn() as conn:
            first = migrate.upgrade(conn=conn)
            self.assertEqual(len(migrate.load_migrations()), len(first))
            self.assertEqual([], migrate.upgrade(conn=conn))
            self.assertTrue(all(row["status"] == "applied"
                                for row in migrate.status(conn=conn)))
            columns = {
                row["column_name"]: row["data_type"]
                for row in conn.execute(
                    "SELECT column_name,data_type FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name='sceneit_searches'",
                    (self.schema,),
                )
            }
            self.assertEqual("uuid", columns["attempt_id"])
            self.assertEqual("timestamp with time zone", columns["deadline_at"])
            self.assertEqual("timestamp with time zone", columns["resolved_at"])
            self.assertEqual("text", columns["resolution"])
            frame_columns = {
                row["column_name"] for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name='sceneit_proof_frames'",
                    (self.schema,),
                )
            }
            self.assertEqual(
                {"search_id", "rank", "source_sha256", "object_path"}, frame_columns
            )

        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary)
            for source in migrate.migration_directory().glob("*.sql"):
                shutil.copy(source, copied / source.name)
            target = next(copied.glob("006_*.sql"))
            target.write_text(target.read_text() + "\n-- drift\n", encoding="utf-8")
            with self.conn() as conn, self.assertRaises(migrate.IncompatibleSchema):
                migrate.status(conn=conn, directory=copied)

    def test_populated_legacy_upgrade_preserves_proof_history_and_budget(self):
        search_id = uuid.uuid4()
        with self.conn() as conn:
            conn.execute(_migration_sql(0))
            conn.execute(
                "INSERT INTO sceneit_proofs"
                "(id,title,youtube_id,source_path,source_sha256,media,index_name,"
                "state,searches_used,search_limit) "
                "VALUES ('resident-evil-proof','Legacy','video','/source','sha',"
                "'{}','index','ready',37,50)"
            )
            conn.execute(
                "INSERT INTO sceneit_searches"
                "(id,proof_id,query,query_key,modality,state,matches,latency_ms,completed_at) "
                "VALUES (%s,'resident-evil-proof','door','door','visual','done',"
                "'[{\"rank\":1}]',123,now())",
                (search_id,),
            )
            applied = migrate.upgrade(conn=conn)
            self.assertNotIn("000_proof.sql", applied)
            proof = conn.execute(
                "SELECT searches_used,search_limit FROM sceneit_proofs"
            ).fetchone()
            search = conn.execute(
                "SELECT id,state,matches,attempt_id FROM sceneit_searches"
            ).fetchone()
            self.assertEqual((37, 50), (proof["searches_used"], proof["search_limit"]))
            self.assertEqual(search_id, search["id"])
            self.assertEqual(search_id, search["attempt_id"])
            self.assertEqual("done", search["state"])
            self.assertEqual([{"rank": 1}], search["matches"])

    def test_name_only_legacy_journal_is_upgraded_to_canonical_manifest(self):
        with self.conn() as conn:
            for version in (0, 1, 2, 3):
                conn.execute(_migration_sql(version))
            conn.execute(
                "CREATE TABLE sceneit_schema_migrations("
                "name text PRIMARY KEY,applied_at timestamptz NOT NULL DEFAULT now())"
            )
            for name in ("001_imports.sql", "002_auth.sql",
                         "003_cleanup_concurrency.sql"):
                conn.execute(
                    "INSERT INTO sceneit_schema_migrations(name) VALUES (%s)",
                    (name,),
                )
            migrate.upgrade(conn=conn)
            self.assertTrue(
                all(item["status"] == "applied" for item in migrate.status(conn=conn))
            )

    def test_untracked_integrated_auth_fk_rejection_is_fully_atomic(self):
        with self.conn() as conn:
            for version in (0, 1, 2, 3):
                conn.execute(_migration_sql(version))
            conn.execute(
                "ALTER TABLE sceneit_auth_sessions "
                "DROP CONSTRAINT sceneit_auth_sessions_user_id_fkey"
            )
            before = {
                row["column_name"] for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=current_schema() "
                    "AND table_name='sceneit_searches'"
                )
            }
            self.assertNotIn("attempt_id", before)
            with self.assertRaises(migrate.IncompatibleSchema):
                migrate.upgrade(conn=conn)
            self.assertFalse(
                conn.execute(
                    "SELECT to_regclass('sceneit_schema_migrations') "
                    "IS NOT NULL AS present"
                ).fetchone()["present"]
            )
            after = {
                row["column_name"] for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=current_schema() "
                    "AND table_name='sceneit_searches'"
                )
            }
            self.assertEqual(before, after)

    def test_incompatible_populated_legacy_schema_is_rejected(self):
        with self.conn() as conn:
            conn.execute("CREATE TABLE sceneit_proofs(id integer PRIMARY KEY)")
            conn.execute("CREATE TABLE sceneit_searches(id uuid PRIMARY KEY,state text)")
            with self.assertRaises(migrate.IncompatibleSchema):
                migrate.upgrade(conn=conn)
            self.assertFalse(
                conn.execute(
                    "SELECT to_regclass('sceneit_schema_migrations') IS NOT NULL AS present"
                ).fetchone()["present"]
            )

    def test_legacy_missing_dedup_constraint_and_stamped_drift_are_rejected(self):
        with self.conn() as conn:
            conn.execute(_migration_sql(0))
            conn.execute(
                "ALTER TABLE sceneit_searches "
                "DROP CONSTRAINT sceneit_searches_proof_id_query_key_modality_key"
            )
            with self.assertRaises(migrate.IncompatibleSchema):
                migrate.upgrade(conn=conn)
            self.assertFalse(
                conn.execute(
                    "SELECT to_regclass('sceneit_schema_migrations') IS NOT NULL AS present"
                ).fetchone()["present"]
            )

        # Use a second isolated schema because a rejected baseline deliberately
        # leaves only its empty migration journal behind.
        drift_schema = f"sceneit_test_{uuid.uuid4().hex}"
        with psycopg.connect(TEST_URL, autocommit=True, row_factory=dict_row) as conn:
            conn.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(drift_schema))
            )
            conn.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(drift_schema))
            )
            try:
                migrate.upgrade(conn=conn)
                conn.execute("ALTER TABLE sceneit_searches DROP CONSTRAINT "
                             "sceneit_searches_state_check")
                with self.assertRaises(migrate.IncompatibleSchema):
                    migrate.status(conn=conn)
                conn.execute(
                    "ALTER TABLE sceneit_searches "
                    "ADD CONSTRAINT sceneit_searches_state_check "
                    "CHECK (state IN ('running','done','failed','needs_review'))"
                )
                self.assertTrue(
                    all(item["status"] == "applied"
                        for item in migrate.status(conn=conn))
                )
                conn.execute(
                    "ALTER TABLE sceneit_proof_frames DROP COLUMN object_path"
                )
                with self.assertRaises(migrate.IncompatibleSchema):
                    migrate.status(conn=conn)

                @contextmanager
                def drift_connection():
                    yield conn

                app = Flask(__name__)
                app.config.update(
                    TESTING=True,
                    SESSION_SECRET="test-secret",
                    TRUSTED_HOSTS="localhost",
                    DATABASE_CONFIGURED=True,
                    READINESS_TIMEOUT_MS=1000,
                )
                app.register_blueprint(health_bp)
                with patch("sceneit.health.connection", drift_connection):
                    response = app.test_client().get(
                        "/api/readyz", headers={"Host": "localhost"}
                    )
                self.assertEqual(503, response.status_code)
                self.assertEqual(
                    "incompatible", response.get_json()["checks"]["schema"]
                )
            finally:
                conn.execute("SET search_path TO public")
                conn.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(
                        sql.Identifier(drift_schema)
                    )
                )

    def test_concurrent_upgrade_is_serialized(self):
        barrier = threading.Barrier(4)
        outcomes = []
        failures = []
        lock = threading.Lock()

        def run():
            try:
                barrier.wait()
                with self.conn() as conn:
                    result = migrate.upgrade(conn=conn, lock_timeout_seconds=15)
                with lock:
                    outcomes.append(result)
            except Exception as exc:  # asserted below with full repr
                with lock:
                    failures.append(repr(exc))

        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([], failures)
        self.assertEqual(1, sum(bool(result) for result in outcomes))
        with self.conn() as conn:
            self.assertTrue(all(row["status"] == "applied"
                                for row in migrate.status(conn=conn)))

    def test_firebase_identity_isolation_and_same_email_ledger_survive_recreation(self):
        """Qualified identities never link private owners, but share trial usage."""
        ledger = trial_ledger_id(
            "Person@Example.com", "immutable-postgres-fixture-secret-123456"
        )
        owner_a = f"firebase:{uuid.uuid4()}"
        owner_b = f"firebase:{uuid.uuid4()}"
        identity_a = uuid.uuid4()
        identity_b = uuid.uuid4()
        with self.conn() as conn:
            migrate.upgrade(conn=conn)
            conn.execute(
                "INSERT INTO sceneit_firebase_trial_ledgers(id) VALUES (%s)",
                (ledger,),
            )
            for identity, uid, owner in (
                (identity_a, "uid-before-delete", owner_a),
                (identity_b, "uid-after-recreate", owner_b),
            ):
                conn.execute(
                    "INSERT INTO sceneit_auth_users"
                    "(id,first_name,provider,email,email_verified) "
                    "VALUES (%s,'Person','firebase','person@example.com',true)",
                    (owner,),
                )
                conn.execute(
                    "INSERT INTO sceneit_firebase_identities"
                    "(id,project_id,issuer,firebase_uid,owner_id,trial_ledger_id,"
                    "email_hash) VALUES (%s,'sceneit-test',"
                    "'https://securetoken.google.com/sceneit-test',%s,%s,%s,%s)",
                    (identity, uid, owner, ledger, "a" * 64),
                )
            conn.execute(
                "INSERT INTO sceneit_import_usage"
                "(owner_id,imports_used,searches_used) VALUES (%s,3,17)",
                (ledger,),
            )
            self.assertNotEqual(owner_a, owner_b)
            self.assertEqual(ledger, usage_owner(conn, owner_a))
            self.assertEqual(ledger, usage_owner(conn, owner_b))
            usage = conn.execute(
                "SELECT imports_used,searches_used FROM sceneit_import_usage "
                "WHERE owner_id=%s", (usage_owner(conn, owner_b),)
            ).fetchone()
            self.assertEqual((3, 17), (usage["imports_used"], usage["searches_used"]))
            with self.assertRaises(psycopg.errors.UniqueViolation):
                conn.execute(
                    "INSERT INTO sceneit_firebase_identities"
                    "(id,project_id,issuer,firebase_uid,owner_id) "
                    "VALUES (%s,'sceneit-test',"
                    "'https://securetoken.google.com/sceneit-test',"
                    "'uid-after-recreate',%s)",
                    (uuid.uuid4(), owner_a),
                )

    def test_successful_firebase_exchange_persists_identity_and_emits_auth_state(self):
        with self.conn() as conn:
            migrate.upgrade(conn=conn)

        @contextmanager
        def schema_connection():
            with self.conn() as conn:
                yield conn

        app = Flask(__name__)
        app.config.update(
            TESTING=True, SESSION_SECRET="s" * 48,
            SCENEIT_PUBLIC_TRIAL_ENABLED=True,
            FIREBASE_PROJECT_ID="sceneit-test",
            FIREBASE_WEB_API_KEY="public-key",
            FIREBASE_AUTH_DOMAIN="sceneit-test.firebaseapp.com",
            FIREBASE_WEB_APP_ID="1:test:web:test",
            FIREBASE_SERVICE_ACCOUNT_JSON="fixture",
            FIREBASE_TRIAL_HASH_SECRET="immutable-postgres-fixture-secret-123456",
        )
        from sceneit.auth import auth_bp
        app.register_blueprint(auth_bp)
        now = int(__import__("time").time())
        claims = {
            "uid": "exchange-user", "sub": "exchange-user",
            "aud": "sceneit-test",
            "iss": "https://securetoken.google.com/sceneit-test",
            "email": "person@example.com", "email_verified": True,
            "auth_time": now,
            "firebase": {"sign_in_provider": "password"},
        }
        provider = SimpleNamespace(
            disabled=False, email="person@example.com", email_verified=True,
            tokens_valid_after_timestamp=(now - 1) * 1000,
        )
        config = FirebaseConfig(
            "sceneit-test", "public-key", "sceneit-test.firebaseapp.com",
            "1:test:web:test", "fixture",
            "immutable-postgres-fixture-secret-123456",
        )
        provider_emails = {"exchange-user": "person@example.com"}

        def verified_token(token, _config):
            result = dict(claims)
            if token.startswith("changed-"):
                result.update(email="changed@example.com")
            elif token.startswith("recreated-"):
                result.update(uid="recreated-user", sub="recreated-user")
            elif token.startswith("concurrent-"):
                result.update(
                    uid="concurrent-user", sub="concurrent-user",
                    email="concurrent@example.com",
                )
            return result

        def provider_user(uid, _config):
            return SimpleNamespace(
                disabled=False, email=provider_emails[uid],
                email_verified=True,
                tokens_valid_after_timestamp=(now - 1) * 1000,
            )

        def exchange(token):
            client = app.test_client()
            challenge = client.get(
                "/api/auth/firebase/challenge",
                base_url="https://sceneit.example",
            ).get_json()["csrfToken"]
            return client.post(
                "/api/auth/firebase/session", json={"idToken": token + "x" * 200},
                headers={"X-CSRF-Token": challenge},
                base_url="https://sceneit.example",
            )

        with patch("sceneit.firebase_auth.configured", return_value=True), \
                patch("sceneit.firebase_auth.configuration", return_value=config), \
                patch("sceneit.firebase_auth.verify_password_token",
                      side_effect=verified_token), \
                patch("sceneit.firebase_auth.provider_user",
                      side_effect=provider_user), \
                patch("sceneit.auth.connection", schema_connection), \
                patch("sceneit.db.connection", schema_connection), \
                patch("sceneit.resources.connection", schema_connection):
            response = exchange("signed-provider-fixture-")
            validator = Path(__file__).resolve().parents[3] / (
                "scripts/validate-openapi-response.mjs"
            )
            validation = subprocess.run(
                ["node", str(validator), "POST", "/auth/firebase/session", "200"],
                input=json.dumps(response.get_json()), text=True,
                capture_output=True, cwd=Path(__file__).resolve().parents[3],
                timeout=15,
            )
            self.assertEqual(0, validation.returncode, validation.stderr)

            with self.conn() as conn:
                original = conn.execute(
                    "SELECT owner_id,trial_ledger_id FROM "
                    "sceneit_firebase_identities WHERE firebase_uid='exchange-user'"
                ).fetchone()
                conn.execute(
                    "INSERT INTO sceneit_import_usage"
                    "(owner_id,imports_used,searches_used) VALUES (%s,2,11)",
                    (original["trial_ledger_id"],),
                )

            provider_emails["exchange-user"] = "changed@example.com"
            changed = exchange("changed-")
            self.assertEqual(409, changed.status_code)
            self.assertEqual(
                "firebase_identity_changed", changed.get_json()["code"]
            )

            provider_emails["recreated-user"] = "person@example.com"
            recreated = exchange("recreated-")
            self.assertEqual(200, recreated.status_code)
            self.assertEqual(2, recreated.get_json()["usage"]["importsUsed"])

            provider_emails["concurrent-user"] = "concurrent@example.com"
            outcomes = []
            failures = []
            barrier = threading.Barrier(2)

            def concurrent_exchange():
                try:
                    barrier.wait()
                    outcomes.append(exchange("concurrent-"))
                except Exception as exc:
                    failures.append(repr(exc))

            threads = [threading.Thread(target=concurrent_exchange) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(20)
            self.assertEqual([], failures)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual([200, 200], sorted(item.status_code for item in outcomes))
        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual(
            {
                "user", "csrfToken", "pilotAdmitted", "capabilities",
                "privateAccess", "usage",
            },
            set(payload),
        )
        self.assertEqual("firebase", payload["user"]["provider"])
        self.assertTrue(payload["user"]["emailVerified"])
        self.assertFalse(payload["pilotAdmitted"])
        self.assertEqual("ready", payload["privateAccess"]["reason"])
        self.assertEqual(3, payload["usage"]["importsRemaining"])
        with self.conn() as conn:
            identity = conn.execute(
                "SELECT owner_id,trial_ledger_id FROM sceneit_firebase_identities "
                "WHERE project_id='sceneit-test' AND firebase_uid='exchange-user'"
            ).fetchone()
            self.assertTrue(identity["owner_id"].startswith("firebase:"))
            self.assertTrue(
                identity["trial_ledger_id"].startswith("firebase-email-v1:")
            )
            self.assertEqual(
                1, conn.execute(
                    "SELECT count(*) AS n FROM sceneit_auth_sessions "
                    "WHERE firebase_email='person@example.com'"
                ).fetchone()["n"]
            )
            recreated_identity = conn.execute(
                "SELECT owner_id,trial_ledger_id FROM sceneit_firebase_identities "
                "WHERE firebase_uid='recreated-user'"
            ).fetchone()
            self.assertNotEqual(identity["owner_id"], recreated_identity["owner_id"])
            self.assertEqual(
                identity["trial_ledger_id"], recreated_identity["trial_ledger_id"]
            )
            # Material email change invalidated the original sessions; only the
            # initial session was removed before the recreated account signed in.
            self.assertEqual(
                0, conn.execute(
                    "SELECT count(*) AS n FROM sceneit_auth_sessions s JOIN "
                    "sceneit_firebase_identities i ON i.id=s.firebase_identity_id "
                    "WHERE i.firebase_uid='exchange-user'"
                ).fetchone()["n"]
            )
            concurrent = conn.execute(
                "SELECT count(*) AS identities,count(DISTINCT owner_id) AS owners "
                "FROM sceneit_firebase_identities "
                "WHERE firebase_uid='concurrent-user'"
            ).fetchone()
            self.assertEqual((1, 1), (
                concurrent["identities"], concurrent["owners"]
            ))

    def test_shared_permits_expiry_throttle_and_db_connection_cap(self):
        with self.conn() as conn:
            migrate.upgrade(conn=conn)

        @contextmanager
        def schema_connection():
            with self.conn() as conn:
                yield conn

        with patch("sceneit.resources.connection", schema_connection):
            first = resources.acquire("search", participant="pilot-a")
            second = resources.acquire("search", participant="pilot-b")
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertIsNone(resources.acquire("search", participant="pilot-c"))
            resources.release("search", first)
            replacement = resources.acquire("search", participant="pilot-c")
            self.assertIsNotNone(replacement)
            self.assertTrue(resources.renew("search", replacement))
            with self.conn() as conn:
                conn.execute(
                    "UPDATE sceneit_resource_leases SET expires_at=now()-interval '1 second'"
                )
            self.assertIsNotNone(resources.acquire("search", participant="pilot-a"))

            self.assertEqual(1, resources.admit_participant("pilot-a", limit=2))
            self.assertEqual(0, resources.admit_participant("pilot-a", limit=2))
            with self.assertRaises(resources.ParticipantThrottled):
                resources.admit_participant("pilot-a", limit=2)
            with self.assertRaises(ValueError):
                resources.acquire("media", lease_seconds=1)
            with self.assertRaises(ValueError):
                resources.renew("search", replacement, lease_seconds=1)

        # Session advisory slots are shared by all processes using this database.
        with patch.dict(os.environ, {"SCENEIT_DATABASE_PERMITS": "1"}, clear=False):
            reset_settings()
            with connection():
                with patch(
                    "sceneit.db.psycopg.connect",
                    side_effect=AssertionError("connect called above local cap"),
                ):
                    with self.assertRaises(DatabaseResourceExhausted):
                        with connection():
                            pass
        reset_settings()
        with patch.dict(
            os.environ,
            {"SCENEIT_REQUIRE_DB_ROLE_CONNECTION_LIMIT": "true"},
            clear=False,
        ):
            reset_settings()
            # The disposable cluster role deliberately has PostgreSQL's
            # unlimited default, proving production admission fails closed.
            with self.assertRaises(DatabaseResourceExhausted):
                with connection():
                    pass
        reset_settings()
        with psycopg.connect(TEST_URL, autocommit=True, row_factory=dict_row) as admin:
            role = admin.execute("SELECT current_user AS name").fetchone()["name"]
            admin.execute(
                sql.SQL("ALTER ROLE {} CONNECTION LIMIT 2").format(
                    sql.Identifier(role)
                )
            )
        try:
            with patch.dict(
                os.environ,
                {"SCENEIT_REQUIRE_DB_ROLE_CONNECTION_LIMIT": "true"},
                clear=False,
            ):
                reset_settings()
                # PostgreSQL ignores CONNECTION LIMIT for superusers.
                with self.assertRaises(DatabaseResourceExhausted):
                    with connection():
                        pass
        finally:
            with psycopg.connect(TEST_URL, autocommit=True) as admin:
                admin.execute(
                    sql.SQL("ALTER ROLE {} CONNECTION LIMIT -1").format(
                        sql.Identifier(role)
                    )
                )
            reset_settings()

    def test_backup_restore_rehearsal_preserves_records(self):
        if not shutil.which("pg_dump") or not shutil.which("pg_restore"):
            self.skipTest("PostgreSQL client tools are required")
        base = conninfo_to_dict(TEST_URL)
        source_name = f"sceneit_test_backup_{uuid.uuid4().hex}"
        restored_name = f"sceneit_test_restore_{uuid.uuid4().hex}"
        admin_url = make_conninfo(**{**base, "dbname": "postgres"})
        source_url = make_conninfo(**{**base, "dbname": source_name})
        restored_url = make_conninfo(**{**base, "dbname": restored_name})
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(source_name)))
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(restored_name)))
        try:
            with psycopg.connect(
                source_url, autocommit=True, row_factory=dict_row
            ) as source:
                migrate.upgrade(conn=source)
                source.execute(
                    "INSERT INTO sceneit_proofs"
                    "(id,title,youtube_id,source_path,source_sha256,media,index_name,"
                    "state,searches_used,search_limit) "
                    "VALUES ('resident-evil-proof','Backup','video','/source','hash',"
                    "'{}','index','ready',41,50)"
                )
            with tempfile.NamedTemporaryFile(suffix=".dump") as dump:
                subprocess.run(
                    ["pg_dump", "--format=custom", "--file", dump.name, source_url],
                    check=True, capture_output=True,
                )
                subprocess.run(
                    ["pg_restore", "--dbname", restored_url, dump.name],
                    check=True, capture_output=True,
                )
            with psycopg.connect(restored_url, row_factory=dict_row) as restored:
                row = restored.execute(
                    "SELECT title,searches_used,search_limit FROM sceneit_proofs "
                    "WHERE id='resident-evil-proof'"
                ).fetchone()
                self.assertEqual(("Backup", 41, 50),
                                 (row["title"], row["searches_used"], row["search_limit"]))
                self.assertTrue(all(item["status"] == "applied"
                                    for item in migrate.status(conn=restored)))
        finally:
            with psycopg.connect(admin_url, autocommit=True) as admin:
                for name in (source_name, restored_name):
                    admin.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname=%s", (name,)
                    )
                    admin.execute(
                        sql.SQL("DROP DATABASE IF EXISTS {}").format(
                            sql.Identifier(name)
                        )
                    )


class ConfigurationTests(unittest.TestCase):
    def tearDown(self):
        reset_settings()

    def test_allowlist_is_fail_closed_and_lease_must_exceed_operation(self):
        with patch.dict(
            os.environ,
            {"DATABASE_URL": "postgresql://invalid/test", "PILOT_ALLOWED_SUBJECTS": ""},
            clear=True,
        ):
            reset_settings()
            self.assertFalse(settings().admits("signed-in-subject"))
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://invalid/test",
                "SCENEIT_MAX_OPERATION_SECONDS": "60",
                "SCENEIT_PERMIT_LEASE_SECONDS": "60",
            },
            clear=True,
        ):
            reset_settings()
            with self.assertRaises(ConfigError):
                settings()


if __name__ == "__main__":
    unittest.main()