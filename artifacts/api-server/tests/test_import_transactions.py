"""Real-Postgres safety tests for the private import subsystem.

These tests use a randomly named schema and patch every private-import database
context to put that schema first on the search path.  They never create auth,
proof, or import records in the application's normal schema.
"""
import os
import re
import secrets
import threading
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import psycopg
from flask import Flask, g, jsonify
from psycopg import sql
from psycopg.rows import dict_row
from werkzeug.exceptions import HTTPException

from sceneit import import_limits, import_search, import_worker, imports, migrate, proof
from sceneit.provider import ProviderError
from sceneit.resources import configured_limit
from sceneit.platforms import FileRequired, resolve_link


DATABASE_AVAILABLE = bool(os.environ.get("DATABASE_URL"))
SCHEMA_PATTERN = re.compile(r"sceneit_test_[0-9a-f]{24}\Z")


@unittest.skipUnless(DATABASE_AVAILABLE, "DATABASE_URL is required for Postgres integration tests")
class PrivateImportTransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = f"sceneit_test_{secrets.token_hex(12)}"
        if not SCHEMA_PATTERN.fullmatch(cls.schema):
            raise RuntimeError("Refusing to create an invalid test schema")

        # DATABASE_URL is passed straight to psycopg and is never logged.
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))

        try:
            migration_dir = Path(__file__).parents[1] / "sceneit" / "migrations"
            with cls._raw_connection() as conn:
                migrate.upgrade(
                    conn=conn, directory=migration_dir,
                    lock_timeout_seconds=5)
        except Exception:
            cls._drop_schema()
            raise

        cls.connection_patchers = [
            patch("sceneit.imports.connection", cls._test_connection),
            patch("sceneit.import_search.connection", cls._test_connection),
            patch("sceneit.import_worker.connection", cls._test_connection),
            patch("sceneit.proof.connection", cls._test_connection),
            patch("sceneit.resources.connection", cls._test_connection),
        ]
        for patcher in cls.connection_patchers:
            patcher.start()
        cls.environment = patch.dict(
            os.environ, {"PRIVATE_OBJECT_DIR": "sceneit-test-bucket/private"}, clear=False
        )
        cls.environment.start()

        app = Flask(__name__)
        app.config.update(TESTING=True)

        @app.before_request
        def install_verified_test_session():
            owner = imports.request.headers.get("X-Test-Verified-Owner")
            g.auth_session = (
                {"id": "verified-test-session", "user_id": owner,
                 "first_name": "Test", "csrf_token": "verified-csrf"}
                if owner else None
            )

        @app.errorhandler(HTTPException)
        def http_error(error):
            return jsonify(error=error.description, code=f"http_{error.code}"), error.code

        app.register_blueprint(imports.imports_bp)
        cls.app = app

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "environment"):
            cls.environment.stop()
        for patcher in reversed(getattr(cls, "connection_patchers", [])):
            patcher.stop()
        cls._drop_schema()

    @classmethod
    @contextmanager
    def _raw_connection(cls):
        with psycopg.connect(
            os.environ["DATABASE_URL"], row_factory=dict_row, connect_timeout=10
        ) as conn:
            conn.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(cls.schema))
            )
            yield conn

    @classmethod
    @contextmanager
    def _test_connection(cls):
        with cls._raw_connection() as conn:
            yield conn

    @classmethod
    def _drop_schema(cls):
        schema = getattr(cls, "schema", "")
        if not SCHEMA_PATTERN.fullmatch(schema):
            raise RuntimeError("Refusing to drop a non-test schema")
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )

    def setUp(self):
        with self._test_connection() as conn:
            conn.execute(
                "TRUNCATE sceneit_searches,sceneit_proofs,"
                "sceneit_resource_leases,sceneit_participant_throttles,"
                "sceneit_import_searches,sceneit_import_fingerprints,"
                "sceneit_imports,sceneit_import_usage CASCADE"
            )
            conn.execute(
                "UPDATE sceneit_import_app_usage SET imports_used=0,searches_used=0,"
                "worker_heartbeat_at=NULL WHERE singleton=true"
            )
            conn.execute(
                "INSERT INTO sceneit_proofs"
                "(id,title,youtube_id,source_path,source_sha256,media,state,"
                "message,index_name,index_id,asset_id,indexed_asset_id) VALUES "
                "(%s,'Test proof','vLqagjJAvU8','attached_assets/test.mp4',"
                "'proof-test-sha','{\"duration\":100,\"width\":640,"
                "\"height\":360,\"size\":1000,\"hasAudio\":true}'::jsonb,"
                "'ready','Ready','test-index-name','test-index','test-asset',"
                "'test-indexed')",
                (proof.PROOF_ID,))

    @staticmethod
    def headers(owner, csrf=True, **extra):
        result = {"X-Test-Verified-Owner": owner}
        if csrf:
            result["X-CSRF-Token"] = "verified-csrf"
        result.update(extra)
        return result

    def insert_import(self, owner, state="file_required", **values):
        import_id = uuid.uuid4()
        with self._test_connection() as conn:
            conn.execute(
                "INSERT INTO sceneit_imports"
                "(id,owner_id,idempotency_key,entry_method,source_kind,state,"
                "status_message,analysis_authorized,playback_authorized) "
                "VALUES (%s,%s,%s,'upload','file',%s,'test',true,false)",
                (import_id, owner, uuid.uuid4(), state),
            )
            if values:
                allowed = {
                    "source_url", "source_kind", "entry_method", "media_path",
                    "media_generation", "upload_path", "upload_generation",
                    "upload_expected_bytes", "upload_expires_at",
                    "upload_session_reference", "duration_seconds", "file_size_bytes",
                    "has_audio", "width", "height", "sha256", "video_codec",
                    "audio_codec", "index_id", "asset_id", "indexed_asset_id",
                    "provider_write_marker", "budget_reserved", "lease_token",
                    "lease_expires_at", "expires_at", "attempts",
                    "playback_authorized",
                }
                if not set(values).issubset(allowed):
                    raise ValueError("Invalid test import field")
                assignments = sql.SQL(",").join(
                    sql.SQL("{}=%s").format(sql.Identifier(name)) for name in values
                )
                conn.execute(
                    sql.SQL("UPDATE sceneit_imports SET {} WHERE id=%s").format(
                        assignments
                    ),
                    (*values.values(), import_id),
                )
        return import_id

    def fetch_import(self, import_id):
        with self._test_connection() as conn:
            return conn.execute(
                "SELECT * FROM sceneit_imports WHERE id=%s", (import_id,)
            ).fetchone()

    def fetch_proof_search(self):
        with self._test_connection() as conn:
            return conn.execute(
                "SELECT * FROM sceneit_searches ORDER BY created_at DESC LIMIT 1"
            ).fetchone()

    def proof_client(self, *, response=None, error=None):
        client = Mock()
        if error is not None:
            client.search.side_effect = error
        else:
            client.search.return_value = response or {
                "data": [{
                    "video_id": "test-indexed", "start": 2, "end": 4,
                    "confidence": "high",
                }],
                "page_info": {},
            }
        return client

    def test_proof_ambiguous_and_unknown_outcomes_keep_quota_and_close(self):
        cases = (
            ProviderError("network_error", "safe", ambiguous=True),
            RuntimeError("unexpected"),
        )
        for number, error in enumerate(cases):
            with self.subTest(error=type(error).__name__):
                client = self.proof_client(error=error)
                with self.assertRaises(Exception):
                    proof.search_scenes(
                        {"query": f"uncertain {number}", "modality": "visual"},
                        client_factory=Mock(return_value=client))
                row = self.fetch_proof_search()
                self.assertEqual("needs_review", row["state"])
                self.assertEqual("search_outcome_unknown", row["error_code"])
                client.close.assert_called_once()
                retry_provider = Mock()
                with self.assertRaises(proof.ProofError):
                    proof.search_scenes(
                        {"query": f"uncertain {number}", "modality": "visual"},
                        client_factory=retry_provider)
                retry_provider.assert_not_called()
                with self._test_connection() as conn:
                    conn.execute(
                        "UPDATE sceneit_proofs SET last_search_at=NULL WHERE id=%s",
                        (proof.PROOF_ID,))
        with self._test_connection() as conn:
            used = conn.execute(
                "SELECT searches_used FROM sceneit_proofs WHERE id=%s",
                (proof.PROOF_ID,)).fetchone()["searches_used"]
        self.assertEqual(2, used)

    def test_definitive_failure_is_not_refunded_and_client_closes(self):
        client = self.proof_client(error=ProviderError(
            "provider_http_400", "safe rejection", ambiguous=False))
        with self.assertRaises(proof.ProofError):
            proof.search_scenes(
                {"query": "definitive", "modality": "visual"},
                client_factory=Mock(return_value=client))
        row = self.fetch_proof_search()
        self.assertEqual("failed", row["state"])
        self.assertEqual("failed", row["resolution"])
        client.close.assert_called_once()
        with self._test_connection() as conn:
            used = conn.execute(
                "SELECT searches_used FROM sceneit_proofs WHERE id=%s",
                (proof.PROOF_ID,)).fetchone()["searches_used"]
        self.assertEqual(1, used)

    def test_malformed_confirmed_response_needs_review_and_closes(self):
        client = self.proof_client(response={
            "data": [{"video_id": "different-asset", "start": 2, "end": 4}],
            "page_info": {},
        })
        with self.assertRaises(proof.ProofError):
            proof.search_scenes(
                {"query": "malformed mapping", "modality": "visual"},
                client_factory=Mock(return_value=client))
        row = self.fetch_proof_search()
        self.assertEqual("needs_review", row["state"])
        self.assertEqual("search_outcome_unknown", row["error_code"])
        client.close.assert_called_once()

    def test_operator_resolution_fences_late_success(self):
        entered, release_response = threading.Event(), threading.Event()
        client = self.proof_client()

        def blocked_search(*_args, **_kwargs):
            entered.set()
            self.assertTrue(release_response.wait(5))
            return {
                "data": [{"video_id": "test-indexed", "start": 2, "end": 4}],
                "page_info": {},
            }

        client.search.side_effect = blocked_search
        outcome = []

        def submit():
            try:
                proof.search_scenes(
                    {"query": "late result", "modality": "visual"},
                    client_factory=Mock(return_value=client))
            except Exception as exc:
                outcome.append(exc)

        thread = threading.Thread(target=submit)
        thread.start()
        self.assertTrue(entered.wait(5))
        running = self.fetch_proof_search()
        proof.resolve_search_operation(running["id"], "confirmed_failed")
        release_response.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(outcome)
        final = self.fetch_proof_search()
        self.assertEqual("failed", final["state"])
        self.assertEqual("confirmed_failed", final["resolution"])
        self.assertEqual(running["attempt_id"], final["attempt_id"])
        client.close.assert_called_once()
        with self._test_connection() as conn:
            used = conn.execute(
                "SELECT searches_used FROM sceneit_proofs WHERE id=%s",
                (proof.PROOF_ID,)).fetchone()["searches_used"]
        self.assertEqual(1, used)

    def test_crashed_attempt_reconciles_without_refund_or_provider(self):
        search_id, attempt_id = uuid.uuid4(), uuid.uuid4()
        with self._test_connection() as conn:
            conn.execute(
                "UPDATE sceneit_proofs SET searches_used=1 WHERE id=%s",
                (proof.PROOF_ID,))
            conn.execute(
                "INSERT INTO sceneit_searches"
                "(id,proof_id,query,query_key,modality,attempt_id,deadline_at) "
                "VALUES (%s,%s,'crashed','crashed','visual',%s,"
                "now()-interval '1 second')",
                (search_id, proof.PROOF_ID, attempt_id))
        reconciled = proof.reconcile_search_operations()
        self.assertEqual([str(search_id)], [item["id"] for item in reconciled])
        row = self.fetch_proof_search()
        self.assertEqual("needs_review", row["state"])
        self.assertEqual("search_outcome_unknown", row["error_code"])
        with self._test_connection() as conn:
            used = conn.execute(
                "SELECT searches_used FROM sceneit_proofs WHERE id=%s",
                (proof.PROOF_ID,)).fetchone()["searches_used"]
        self.assertEqual(1, used)

    def test_saturation_spends_nothing_and_cached_duplicate_needs_no_permit(self):
        first_client = self.proof_client()
        first = proof.search_scenes(
            {"query": "cached result", "modality": "visual"},
            client_factory=Mock(return_value=first_client))
        first_client.close.assert_called_once()
        with self._test_connection() as conn:
            for _ in range(configured_limit("search")):
                conn.execute(
                    "INSERT INTO sceneit_resource_leases"
                    "(resource,holder,expires_at) "
                    "VALUES ('search',%s,now()+interval '5 minutes')",
                    (uuid.uuid4(),))
        duplicate_provider = Mock()
        self.assertEqual(
            first,
            proof.search_scenes(
                {"query": "cached result", "modality": "visual"},
                client_factory=duplicate_provider))
        duplicate_provider.assert_not_called()
        new_provider = Mock()
        with self.assertRaises(proof.ProofError) as raised:
            proof.search_scenes(
                {"query": "new paid result", "modality": "visual"},
                client_factory=new_provider)
        self.assertEqual("busy", raised.exception.code)
        new_provider.assert_not_called()
        with self._test_connection() as conn:
            used = conn.execute(
                "SELECT searches_used FROM sceneit_proofs WHERE id=%s",
                (proof.PROOF_ID,)).fetchone()["searches_used"]
        self.assertEqual(1, used)

    def test_multiple_ready_imports_can_cancel_while_another_is_active(self):
        owner = "cleanup-owner"
        first = self.insert_import(owner, state="ready")
        second = self.insert_import(owner, state="ready")
        active = self.insert_import(owner, state="queued")
        with self.app.test_client() as client:
            for import_id in (first, second):
                response = client.post(
                    f"/api/imports/{import_id}/cancel", json={},
                    headers=self.headers(owner))
                self.assertEqual(200, response.status_code)
                self.assertEqual("cancel_requested", response.get_json()["state"])
        self.assertEqual("queued", self.fetch_import(active)["state"])
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.insert_import(owner, state="queued")

    def test_bulk_expiry_queues_multiple_ready_imports_for_same_owner(self):
        expired = datetime.now(timezone.utc) - timedelta(seconds=1)
        ids = [self.insert_import("expiry-owner", state="ready", expires_at=expired)
               for _ in range(2)]
        import_worker.cleanup_expired()
        for import_id in ids:
            self.assertEqual("cancel_requested", self.fetch_import(import_id)["state"])

    @staticmethod
    def create_payload(key=None, **changes):
        payload = {
            "entryMethod": "upload",
            "analysisAuthorized": True,
            "playbackAuthorized": False,
            "idempotencyKey": str(key or uuid.uuid4()),
        }
        payload.update(changes)
        return payload

    def test_concurrent_same_owner_idempotency_and_active_limit(self):
        owner = "concurrent-idempotent-owner"
        key = uuid.uuid4()
        barrier = threading.Barrier(6)
        results = []
        result_lock = threading.Lock()

        def create(payload):
            barrier.wait()
            with self.app.test_client() as client:
                response = client.post(
                    "/api/imports", json=payload, headers=self.headers(owner)
                )
            with result_lock:
                results.append((response.status_code, response.get_json()))

        threads = [
            threading.Thread(target=create, args=(self.create_payload(key),))
            for _ in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([200] * 6, sorted(status for status, _ in results))
        self.assertEqual(1, len({body["id"] for _, body in results}))
        with self._test_connection() as conn:
            count = conn.execute(
                "SELECT count(*) AS n FROM sceneit_imports WHERE owner_id=%s", (owner,)
            ).fetchone()["n"]
        self.assertEqual(1, count)

        # Different keys racing for a fresh owner still produce one active row.
        limit_owner = "concurrent-active-limit-owner"
        limit_barrier = threading.Barrier(2)
        limit_results = []

        def create_distinct():
            limit_barrier.wait()
            with self.app.test_client() as client:
                response = client.post(
                    "/api/imports", json=self.create_payload(),
                    headers=self.headers(limit_owner),
                )
            with result_lock:
                limit_results.append((response.status_code, response.get_json()))

        limit_threads = [threading.Thread(target=create_distinct) for _ in range(2)]
        for thread in limit_threads:
            thread.start()
        for thread in limit_threads:
            thread.join(10)
        self.assertTrue(all(not thread.is_alive() for thread in limit_threads))
        self.assertEqual([200, 409], sorted(status for status, _ in limit_results))
        rejected = next(body for status, body in limit_results if status == 409)
        self.assertEqual("import_in_progress", rejected["code"])
        with self._test_connection() as conn:
            count = conn.execute(
                "SELECT count(*) AS n FROM sceneit_imports WHERE owner_id=%s",
                (limit_owner,),
            ).fetchone()["n"]
        self.assertEqual(1, count)

    def test_owner_and_app_budget_reservations_are_transactional(self):
        owner = "owner-race"
        with self._test_connection() as conn:
            conn.execute(
                "INSERT INTO sceneit_import_usage(owner_id,imports_used) VALUES (%s,%s)",
                (owner, import_limits.OWNER_IMPORT_LIMIT - 1),
            )

        def race(owners):
            barrier = threading.Barrier(len(owners))
            outcomes = []
            lock = threading.Lock()

            def reserve(candidate):
                barrier.wait()
                with self._test_connection() as conn:
                    outcome = import_limits.reserve_import_budget(conn, candidate)
                with lock:
                    outcomes.append(outcome)

            threads = [threading.Thread(target=reserve, args=(item,)) for item in owners]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            return outcomes

        outcomes = race([owner] * 6)
        self.assertEqual(1, sum(ok for ok, _ in outcomes))
        self.assertEqual(
            5, sum(code == "owner_import_limit" for ok, code in outcomes if not ok)
        )
        with self._test_connection() as conn:
            used = conn.execute(
                "SELECT imports_used FROM sceneit_import_usage WHERE owner_id=%s",
                (owner,),
            ).fetchone()["imports_used"]
        self.assertEqual(import_limits.OWNER_IMPORT_LIMIT, used)

        with self._test_connection() as conn:
            conn.execute("TRUNCATE sceneit_import_usage")
            conn.execute(
                "UPDATE sceneit_import_app_usage SET imports_used=%s WHERE singleton=true",
                (import_limits.APP_IMPORT_LIMIT - 1,),
            )
        outcomes = race([f"app-race-{number}" for number in range(6)])
        self.assertEqual(1, sum(ok for ok, _ in outcomes))
        self.assertEqual(
            5, sum(code == "app_import_limit" for ok, code in outcomes if not ok)
        )
        with self._test_connection() as conn:
            used = conn.execute(
                "SELECT imports_used FROM sceneit_import_app_usage WHERE singleton=true"
            ).fetchone()["imports_used"]
        self.assertEqual(import_limits.APP_IMPORT_LIMIT, used)

    def test_cross_owner_routes_fail_closed_before_storage_or_provider(self):
        victim = "victim-owner"
        attacker = "attacker-owner"
        import_id = self.insert_import(
            victim, upload_path="sceneit-test-bucket/private/upload.mp4",
            upload_expected_bytes=9,
            upload_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            media_path="sceneit-test-bucket/private/media.mp4",
            media_generation="1",
        )
        search_id = uuid.uuid4()
        requests = [
            ("get", f"/api/imports/{import_id}", None),
            ("get", f"/api/imports/{import_id}/searches", None),
            ("get", f"/api/imports/{import_id}/source", None),
            ("get", f"/api/imports/{import_id}/searches/{search_id}/frames/1", None),
            ("post", f"/api/imports/{import_id}/searches",
             {"query": "door", "modality": "visual"}),
            ("post", f"/api/imports/{import_id}/playback", {"authorized": True}),
            ("post", f"/api/imports/{import_id}/upload",
             {"fileName": "clip.mp4", "sizeBytes": 9, "contentType": "video/mp4"}),
            ("post", f"/api/imports/{import_id}/complete", {}),
            ("post", f"/api/imports/{import_id}/cancel", {}),
        ]
        with patch(
            "sceneit.private_storage.object_info",
            side_effect=AssertionError("storage must not be reached"),
        ):
            with self.app.test_client() as client:
                for method, path, body in requests:
                    response = getattr(client, method)(
                        path, json=body,
                        headers=self.headers(attacker, **{"X-Owner-ID": victim}),
                    )
                    self.assertEqual(404, response.status_code, path)

    def test_csrf_spoofing_and_strict_rights_literals(self):
        owner = "csrf-owner"
        payload = self.create_payload()
        with self.app.test_client() as client:
            missing = client.post(
                "/api/imports", json=payload, headers=self.headers(owner, csrf=False)
            )
            wrong = client.post(
                "/api/imports", json=payload,
                headers={**self.headers(owner), "X-CSRF-Token": "wrong"},
            )
            self.assertEqual(403, missing.status_code)
            self.assertEqual(403, wrong.status_code)

            for bad_value in (False, 1, "true"):
                bad = client.post(
                    "/api/imports",
                    json=self.create_payload(analysisAuthorized=bad_value),
                    headers=self.headers(owner),
                )
                self.assertEqual(400, bad.status_code)
                self.assertEqual("invalid_import", bad.get_json()["code"])

            bad_playback = client.post(
                "/api/imports",
                json=self.create_payload(playbackAuthorized=1),
                headers=self.headers(owner),
            )
            self.assertEqual(400, bad_playback.status_code)

            created = client.post(
                "/api/imports", json=self.create_payload(),
                headers=self.headers(owner, **{"X-Owner-ID": "spoofed-owner"}),
            )
        self.assertEqual(200, created.status_code)
        row = self.fetch_import(uuid.UUID(created.get_json()["id"]))
        self.assertEqual(owner, row["owner_id"])

    def test_standalone_has_no_link_and_youtube_never_invokes_downloader(self):
        with self.app.test_client() as client:
            standalone = client.post(
                "/api/imports", json=self.create_payload(),
                headers=self.headers("standalone-owner"),
            )
        self.assertEqual(200, standalone.status_code)
        self.assertIsNone(standalone.get_json()["sourceUrl"])
        self.assertEqual("file_required", standalone.get_json()["state"])

        extractor = Mock(side_effect=AssertionError("YouTube extractor invoked"))
        youtube_payload = self.create_payload(
            entryMethod="link",
            sourceUrl="https://youtu.be/dQw4w9WgXcQ",
        )
        with patch("sceneit.platforms._isolated_extract", extractor):
            with self.app.test_client() as client:
                youtube = client.post(
                    "/api/imports", json=youtube_payload,
                    headers=self.headers("youtube-owner"),
                )
            self.assertEqual(200, youtube.status_code)
            self.assertEqual("file_required", youtube.get_json()["state"])
            with self.assertRaises(FileRequired) as raised:
                resolve_link(
                    {"sourceKind": "youtube",
                     "sourceUrl": "https://youtu.be/dQw4w9WgXcQ"},
                    "/unused",
                )
        self.assertEqual("file_required", raised.exception.code)
        extractor.assert_not_called()

    def test_repeated_upload_completion_is_idempotent(self):
        owner = "upload-owner"
        with self.app.test_client() as client:
            created = client.post(
                "/api/imports", json=self.create_payload(), headers=self.headers(owner)
            ).get_json()
            import_id = created["id"]
            reservation = {
                "uploadUrl": "https://upload.invalid/session",
                "expiresAt": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                "sessionReference": "encrypted-session",
            }
            with patch("sceneit.private_storage.reserve_upload", return_value=reservation):
                reserved = client.post(
                    f"/api/imports/{import_id}/upload",
                    json={"fileName": "clip.mp4", "sizeBytes": 123,
                          "contentType": "video/mp4"},
                    headers=self.headers(owner),
                )
            self.assertEqual(200, reserved.status_code)
            info = {"size": 123, "contentType": "video/mp4", "generation": "77"}
            with patch("sceneit.private_storage.object_info", return_value=info):
                first = client.post(
                    f"/api/imports/{import_id}/complete", json={},
                    headers=self.headers(owner),
                )
                second = client.post(
                    f"/api/imports/{import_id}/complete", json={},
                    headers=self.headers(owner),
                )
        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual(first.get_json()["id"], second.get_json()["id"])
        self.assertEqual("queued", second.get_json()["state"])
        with self._test_connection() as conn:
            usage = conn.execute(
                "SELECT imports_used FROM sceneit_import_usage WHERE owner_id=%s",
                (owner,),
            ).fetchone()["imports_used"]
        self.assertEqual(1, usage)

    def test_cancellation_during_provider_response_is_fenced(self):
        owner = "cancel-race-owner"
        token = uuid.uuid4()
        import_id = self.insert_import(
            owner, state="processing", lease_token=token,
            lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=90),
            attempts=1, duration_seconds=10, has_audio=True, sha256="a" * 64,
            index_id="owned-index", asset_id="owned-asset",
            media_path="sceneit-test-bucket/private/media.mp4", media_generation="4",
        )
        job = self.fetch_import(import_id)
        entered = threading.Event()
        release = threading.Event()
        client = Mock()
        client.get_asset.return_value = {"status": "ready", "duration": 10}

        def index_asset(*_args, **_kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return {"_id": "response-after-cancel"}

        client.index_asset.side_effect = index_asset
        thread = threading.Thread(target=import_worker.process_job, args=(job, client))
        thread.start()
        self.assertTrue(entered.wait(5))
        with self._test_connection() as conn:
            conn.execute(
                "UPDATE sceneit_imports SET state='cancel_requested' WHERE id=%s",
                (import_id,),
            )
        release.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        row = self.fetch_import(import_id)
        self.assertEqual("cancel_requested", row["state"])
        self.assertEqual("owned-index", row["index_id"])
        self.assertEqual("owned-asset", row["asset_id"])
        # The response was confirmed, so its ID is retained even though the
        # monotonic cancellation state wins. Cleanup can now delete it safely.
        self.assertEqual("response-after-cancel", row["indexed_asset_id"])
        self.assertIsNone(row["lease_token"])
        cleanup_token = uuid.uuid4()
        with self._test_connection() as conn:
            conn.execute(
                "UPDATE sceneit_imports SET lease_token=%s,"
                "lease_expires_at=now()+interval '90 seconds' WHERE id=%s",
                (cleanup_token, import_id),
            )
        cleanup_client = Mock()
        from google.api_core.exceptions import NotFound
        with patch("sceneit.private_storage.object_info",
                   side_effect=NotFound("absent")):
            import_worker.process_job(
                self.fetch_import(import_id), cleanup_client)
        cleaned = self.fetch_import(import_id)
        self.assertEqual("cancelled", cleaned["state"])
        cleanup_client.delete_indexed_asset.assert_called_once_with(
            "owned-index", "response-after-cancel")
        cleanup_client.delete_asset.assert_called_once_with("owned-asset")
        cleanup_client.delete_index.assert_called_once_with("owned-index")

    def test_long_inflight_cancel_keeps_live_lease_for_confirmed_response(self):
        token = uuid.uuid4()
        import_id = self.insert_import(
            "long-inflight-owner", state="uploading", lease_token=token,
            lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=90),
            attempts=1, provider_write_marker="upload_asset",
            index_id="owned-index", has_audio=True,
        )
        job = self.fetch_import(import_id)
        self.assertEqual("uploading", job["state"])
        with self._test_connection() as conn:
            conn.execute(
                "UPDATE sceneit_imports SET state='cancel_requested' WHERE id=%s",
                (import_id,),
            )

        # Six virtual heartbeat ticks stand in for an upload lasting longer than
        # the original 90-second lease. Cancellation must not stop renewal of
        # the already-sent operation merely because the job snapshot is stale.
        for _ in range(6):
            with self._test_connection() as conn:
                conn.execute(
                    "UPDATE sceneit_imports SET lease_expires_at=now()+"
                    "interval '1 second' WHERE id=%s", (import_id,),
                )
            self.assertEqual("uploading", job["state"])
            self.assertTrue(import_worker._renew_lease(job))
            with self._test_connection() as conn:
                extension = conn.execute(
                    "SELECT lease_expires_at >= now()+interval '89 seconds' AS enough,"
                    "lease_expires_at <= now()+interval '91 seconds' AS bounded "
                    "FROM sceneit_imports WHERE id=%s", (import_id,),
                ).fetchone()
            self.assertTrue(extension["enough"])
            self.assertTrue(extension["bounded"])

        import_worker._confirmed_write(
            job, asset_id="asset-confirmed-after-long-cancel", state="processing",
            status_message="Provider response recorded.", progress_percent=None,
        )
        row = self.fetch_import(import_id)
        self.assertEqual(import_id, row["id"])
        self.assertEqual("cancel_requested", row["state"])
        self.assertEqual("asset-confirmed-after-long-cancel", row["asset_id"])
        self.assertIsNone(row["provider_write_marker"])

        # An expired lease and a live lease whose token was stolen are both
        # immutable to the old in-memory job.
        with self._test_connection() as conn:
            expired = conn.execute(
                "UPDATE sceneit_imports SET lease_expires_at=now()-interval '1 second' "
                "WHERE id=%s RETURNING lease_expires_at", (import_id,),
            ).fetchone()["lease_expires_at"]
        self.assertFalse(import_worker._renew_lease(job))
        self.assertEqual(expired, self.fetch_import(import_id)["lease_expires_at"])

        stolen_token = uuid.uuid4()
        with self._test_connection() as conn:
            stolen = conn.execute(
                "UPDATE sceneit_imports SET lease_token=%s,"
                "lease_expires_at=now()+interval '1 second' WHERE id=%s "
                "RETURNING lease_token,lease_expires_at",
                (stolen_token, import_id),
            ).fetchone()
        self.assertFalse(import_worker._renew_lease(job))
        unchanged = self.fetch_import(import_id)
        self.assertEqual(stolen["lease_token"], unchanged["lease_token"])
        self.assertEqual(stolen["lease_expires_at"], unchanged["lease_expires_at"])

    def test_restart_marker_becomes_needs_review_without_retry(self):
        token = uuid.uuid4()
        import_id = self.insert_import(
            "marker-owner", state="uploading", lease_token=token,
            lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=90),
            attempts=2, provider_write_marker="upload_asset",
            index_id="owned-index", has_audio=True,
        )
        client = Mock()
        import_worker.process_job(self.fetch_import(import_id), client)
        row = self.fetch_import(import_id)
        self.assertEqual("needs_review", row["state"])
        self.assertEqual("provider_write_uncertain", row["error_code"])
        self.assertEqual("upload_asset", row["provider_write_marker"])
        client.create_index.assert_not_called()
        client.upload_asset.assert_not_called()
        client.index_asset.assert_not_called()

    def test_expired_import_denies_source_and_search_immediately(self):
        owner = "expired-owner"
        import_id = self.insert_import(
            owner, state="ready",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            media_path="sceneit-test-bucket/private/media.mp4", media_generation="1",
            duration_seconds=10, has_audio=True, index_id="index",
            asset_id="asset", indexed_asset_id="indexed", playback_authorized=True,
        )
        provider = Mock(side_effect=AssertionError("expired import reached provider"))

        def search_with_fake(owner_id, item_id, payload):
            return import_search.search_import(
                owner_id, item_id, payload, client_factory=provider
            )

        with patch("sceneit.imports.search_import", search_with_fake):
            with self.app.test_client() as client:
                source = client.get(
                    f"/api/imports/{import_id}/source", headers=self.headers(owner)
                )
                search = client.post(
                    f"/api/imports/{import_id}/searches",
                    json={"query": "door", "modality": "visual"},
                    headers=self.headers(owner),
                )
        self.assertEqual(410, source.status_code)
        self.assertEqual("import_expired", source.get_json()["code"])
        self.assertEqual(410, search.status_code)
        self.assertEqual("import_expired", search.get_json()["code"])
        provider.assert_not_called()

    def test_cumulative_allowance_survives_cancelled_cleanup(self):
        owner = "cumulative-owner"
        with self._test_connection() as conn:
            ok, code = import_limits.reserve_import_budget(conn, owner)
        self.assertTrue(ok)
        self.assertIsNone(code)
        import_id = self.insert_import(
            owner, state="cancelled",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            budget_reserved=True,
        )
        import_worker.cleanup_expired()
        with self._test_connection() as conn:
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM sceneit_imports WHERE id=%s", (import_id,)
                ).fetchone()
            )
            owner_used = conn.execute(
                "SELECT imports_used FROM sceneit_import_usage WHERE owner_id=%s",
                (owner,),
            ).fetchone()["imports_used"]
            app_used = conn.execute(
                "SELECT imports_used FROM sceneit_import_app_usage WHERE singleton=true"
            ).fetchone()["imports_used"]
        self.assertEqual(1, owner_used)
        self.assertEqual(1, app_used)

    def test_silent_media_rejects_audio_modalities_but_allows_visual(self):
        owner = "silent-owner"
        import_id = self.insert_import(
            owner, state="ready", duration_seconds=20, has_audio=False,
            index_id="index", asset_id="asset", indexed_asset_id="indexed",
        )
        provider = Mock()
        provider.return_value.search.return_value = {
            "data": [{"video_id": "indexed", "start": 1, "end": 3,
                      "confidence": "high"}]
        }

        def search_with_fake(owner_id, item_id, payload):
            return import_search.search_import(
                owner_id, item_id, payload, client_factory=provider
            )

        with patch("sceneit.imports.search_import", search_with_fake):
            with self.app.test_client() as client:
                for modality in ("audio", "both"):
                    response = client.post(
                        f"/api/imports/{import_id}/searches",
                        json={"query": f"silent {modality}", "modality": modality},
                        headers=self.headers(owner),
                    )
                    self.assertEqual(400, response.status_code)
                    self.assertEqual("audio_unavailable", response.get_json()["code"])
                visual = client.post(
                    f"/api/imports/{import_id}/searches",
                    json={"query": "visible door", "modality": "visual"},
                    headers=self.headers(owner),
                )
        self.assertEqual(200, visual.status_code)
        self.assertEqual("visual", visual.get_json()["modality"])
        self.assertEqual(1, provider.call_count)
        provider.return_value.search.assert_called_once()

    def test_fingerprint_uncertainty_blocks_and_active_ready_mapping_reuses_ids(self):
        media = {
            "duration": 12, "size": 100, "hasAudio": True, "width": 640,
            "height": 360, "sha256": "f" * 64, "videoCodec": "h264",
            "audioCodec": "aac",
        }
        owner = "fingerprint-owner"
        uncertain_id = self.insert_import(
            owner, state="validating", lease_token=uuid.uuid4(),
            lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=90),
        )
        with self._test_connection() as conn:
            conn.execute(
                "INSERT INTO sceneit_import_fingerprints(owner_id,sha256,status) "
                "VALUES (%s,%s,'uncertain')", (owner, media["sha256"])
            )
        with patch("sceneit.inspect_media.inspect_mp4", return_value=media):
            with self.assertRaisesRegex(ValueError, "fingerprint_not_repeatable"):
                import_worker._validate(self.fetch_import(uncertain_id), "/unused.mp4")

        with self._test_connection() as conn:
            conn.execute(
                "UPDATE sceneit_imports SET state='failed' WHERE id=%s",
                (uncertain_id,),
            )
            conn.execute(
                "UPDATE sceneit_import_fingerprints SET status='active',"
                "index_id='reused-index',asset_id='reused-asset',"
                "indexed_asset_id='reused-indexed',"
                "media_path='sceneit-test-bucket/private/reused.mp4',"
                "media_generation='8' WHERE owner_id=%s AND sha256=%s",
                (owner, media["sha256"]),
            )
        ready_id = self.insert_import(
            owner, state="validating", lease_token=uuid.uuid4(),
            lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=90),
        )
        with patch("sceneit.inspect_media.inspect_mp4", return_value=media):
            duplicate = import_worker._validate(
                self.fetch_import(ready_id), "/unused.mp4"
            )
        self.assertTrue(duplicate)
        row = self.fetch_import(ready_id)
        self.assertEqual("ready", row["state"])
        self.assertEqual("reused-index", row["index_id"])
        self.assertEqual("reused-asset", row["asset_id"])
        self.assertEqual("reused-indexed", row["indexed_asset_id"])
        self.assertEqual("sceneit-test-bucket/private/reused.mp4", row["media_path"])


if __name__ == "__main__":
    unittest.main()