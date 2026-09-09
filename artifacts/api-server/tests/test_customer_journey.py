"""Connected customer-journey checks against disposable PostgreSQL.

The test uses actual Flask routes and durable rows, while Firebase, storage,
media, and search transports are fixture-backed.  It refuses DATABASE_URL and
never writes outside a randomly named schema in a sceneit_test* database.
"""
import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import psycopg
from flask import Response
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from sceneit import import_worker, migrate
from sceneit.auth import SESSION_COOKIE
from sceneit.billing_config import reset_billing_settings
from sceneit.config import reset_settings
from sceneit.firebase_auth import FirebaseConfig
from sceneit.server import create_app


TEST_URL = os.environ.get("SCENEIT_TEST_DATABASE_URL", "")
ROOT = Path(__file__).resolve().parents[3]
VALIDATOR = ROOT / "scripts" / "validate-openapi-response.mjs"
SAFE_TEST_DATABASE = (
    bool(TEST_URL)
    and TEST_URL != os.environ.get("DATABASE_URL")
    and conninfo_to_dict(TEST_URL).get("dbname", "").startswith("sceneit_test")
)


@unittest.skipUnless(
    SAFE_TEST_DATABASE,
    "SCENEIT_TEST_DATABASE_URL must name a disposable sceneit_test* database "
    "and differ from DATABASE_URL",
)
class ConnectedCustomerJourneyTests(unittest.TestCase):
    def setUp(self):
        self.schema = f"sceneit_test_{uuid.uuid4().hex}"
        self.slots = self.enterContext(
            tempfile.TemporaryDirectory(prefix="sceneit-journey-slots-")
        )
        with psycopg.connect(TEST_URL, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(
                sql.Identifier(self.schema)))
        # Register destructive cleanup immediately: migrations or app creation
        # may fail after the schema exists.
        self.addCleanup(self._drop_schema)
        # These were registered before the environment context, so unittest's
        # LIFO cleanup restores the environment before clearing cached settings.
        self.addCleanup(reset_settings)
        self.addCleanup(reset_billing_settings)

        self.enterContext(patch.dict(
            os.environ,
            {
                "DATABASE_URL": TEST_URL,
                "PRIVATE_OBJECT_DIR": "sceneit-test-bucket/private",
                "SCENEIT_BILLING_ENABLED": "false",
                "SCENEIT_DISABLE_PROVIDER_NETWORK": "1",
                "SCENEIT_DB_SLOT_DIRECTORY": self.slots,
                "SCENEIT_DATABASE_PERMITS": "4",
                "SCENEIT_SEARCH_PERMITS": "2",
                "SCENEIT_MAX_OPERATION_SECONDS": "10",
                "SCENEIT_PERMIT_LEASE_SECONDS": "120",
            },
            clear=False,
        ))
        reset_settings()
        reset_billing_settings()
        with self.conn() as conn:
            migrate.upgrade(conn=conn)

        connection_targets = (
            "sceneit.auth.connection",
            "sceneit.db.connection",
            "sceneit.imports.connection",
            "sceneit.import_search.connection",
            "sceneit.import_worker.connection",
            "sceneit.proof.connection",
            "sceneit.resources.connection",
            "sceneit.upload_attempts.connection",
        )
        for target in connection_targets:
            self.enterContext(patch(target, self.schema_connection))

        self.firebase_config = FirebaseConfig(
            "sceneit-test",
            "public-key",
            "sceneit-test.firebaseapp.com",
            "1:test:web:test",
            "fixture",
            "immutable-postgres-fixture-secret-123456",
        )
        now = int(time.time())
        self.claims = {
            "uid": "journey-firebase-user",
            "sub": "journey-firebase-user",
            "aud": "sceneit-test",
            "iss": "https://securetoken.google.com/sceneit-test",
            "email": "journey@example.com",
            "email_verified": True,
            "auth_time": now,
            "firebase": {"sign_in_provider": "password"},
        }
        self.provider_record = SimpleNamespace(
            disabled=False,
            email=self.claims["email"],
            email_verified=True,
            tokens_valid_after_timestamp=(now - 1) * 1000,
        )
        self.enterContext(patch(
            "sceneit.firebase_auth.configured", return_value=True))
        self.enterContext(patch(
            "sceneit.firebase_auth.configuration",
            return_value=self.firebase_config))
        self.enterContext(patch(
            "sceneit.firebase_auth.verify_password_token",
            return_value=self.claims))
        self.firebase_provider = self.enterContext(patch(
            "sceneit.firebase_auth.provider_user",
            return_value=self.provider_record))

        # Expected adapters above and per-operation storage/search fakes below
        # bypass these guards explicitly. Any accidental transport escape fails
        # at its lowest shared boundary during standalone execution.
        external_guards = (
            "httpx.Client.send",
            "httpx.AsyncClient.send",
            "requests.sessions.Session.request",
            "sceneit.private_storage._client",
            "sceneit.firebase_auth._admin_app",
            "sceneit.platforms._isolated_extract",
            "sceneit.provider.TwelveLabsClient",
            "sceneit.import_search.TwelveLabsClient",
            "sceneit.import_worker.TwelveLabsClient",
            "sceneit.proof.TwelveLabsClient",
        )
        self.transport_guards = []
        for target in external_guards:
            guard = self.enterContext(patch(
                target,
                side_effect=AssertionError(
                    f"Unexpected external transport reached: {target}"),
            ))
            self.transport_guards.append(guard)
            self.addCleanup(guard.assert_not_called)

        self.app = create_app({
            "TESTING": True,
            "SESSION_SECRET": "s" * 48,
            "PILOT_ALLOWED_SUBJECTS": "journey-replit-pilot",
            "TRUSTED_HOSTS": ["sceneit.example"],
            "DATABASE_CONFIGURED": True,
            "SCENEIT_PUBLIC_TRIAL_ENABLED": True,
            "FIREBASE_PROJECT_ID": "sceneit-test",
            "FIREBASE_WEB_API_KEY": "public-key",
            "FIREBASE_AUTH_DOMAIN": "sceneit-test.firebaseapp.com",
            "FIREBASE_WEB_APP_ID": "1:test:web:test",
            "FIREBASE_SERVICE_ACCOUNT_JSON": "fixture",
            "FIREBASE_TRIAL_HASH_SECRET":
                "immutable-postgres-fixture-secret-123456",
        })
        self.base_url = "https://sceneit.example"

    def _drop_schema(self):
        schema = getattr(self, "schema", "")
        if not schema.startswith("sceneit_test_"):
            raise RuntimeError("Refusing to drop a non-test schema")
        with psycopg.connect(TEST_URL, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(schema)))

    @contextmanager
    def conn(self):
        with psycopg.connect(
            TEST_URL, row_factory=dict_row, connect_timeout=10
        ) as conn:
            conn.execute(sql.SQL("SET search_path TO {}").format(
                sql.Identifier(self.schema)))
            yield conn

    @contextmanager
    def schema_connection(self):
        with self.conn() as conn:
            yield conn

    @staticmethod
    def csrf_headers(token):
        return {
            "X-CSRF-Token": token,
            "Origin": "https://sceneit.example",
        }

    def assert_contract(self, method, path, response):
        validation = subprocess.run(
            ["node", str(VALIDATOR), method, path, str(response.status_code)],
            input=json.dumps(response.get_json()),
            text=True,
            capture_output=True,
            cwd=ROOT,
            timeout=15,
        )
        self.assertEqual(0, validation.returncode, validation.stderr)
        self.assertEqual("", validation.stderr, validation.stderr)

    def exchange_firebase(self, client):
        challenge = client.get(
            "/api/auth/firebase/challenge", base_url=self.base_url)
        self.assertEqual(200, challenge.status_code, challenge.get_data(as_text=True))
        csrf = challenge.get_json()["csrfToken"]
        response = client.post(
            "/api/auth/firebase/session",
            json={"idToken": "signed-fixture-" + "x" * 200},
            headers=self.csrf_headers(csrf),
            base_url=self.base_url,
        )
        self.assertEqual(200, response.status_code, response.get_data(as_text=True))
        self.assert_contract("POST", "/auth/firebase/session", response)
        return response.get_json()

    def replit_client(self):
        raw_session = "replit-session-" + uuid.uuid4().hex
        digest = hashlib.sha256(raw_session.encode("ascii")).hexdigest()
        csrf = "replit-csrf-" + uuid.uuid4().hex
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO sceneit_auth_users"
                "(id,first_name,provider,email,email_verified) "
                "VALUES ('journey-replit-pilot','Pilot','replit',NULL,false)"
            )
            conn.execute(
                "INSERT INTO sceneit_auth_sessions"
                "(id,user_id,csrf_token,expires_at) "
                "VALUES (%s,'journey-replit-pilot',%s,now()+interval '1 hour')",
                (digest, csrf),
            )
        client = self.app.test_client()
        client.set_cookie(
            SESSION_COOKIE, raw_session, domain="sceneit.example", secure=True)
        return client, digest

    def test_connected_firebase_import_search_permissions_and_cleanup_journey(self):
        firebase = self.app.test_client()
        auth = self.exchange_firebase(firebase)
        owner = auth["user"]["id"]
        csrf = auth["csrfToken"]
        self.assertEqual("firebase", auth["user"]["provider"])
        self.assertTrue(auth["user"]["emailVerified"])
        self.assertFalse(auth["pilotAdmitted"])
        self.assertEqual(
            {"allowed": True, "reason": "ready"}, auth["privateAccess"])
        self.assertEqual(3, auth["usage"]["importsRemaining"])

        # Firebase trials use the private-import capability, not Replit pilot
        # admission.  The proof route remains pilot-only.
        denied_proof = firebase.get("/api/proof", base_url=self.base_url)
        self.assertEqual(403, denied_proof.status_code)
        self.assertEqual("pilot_not_admitted", denied_proof.get_json()["code"])
        config = firebase.get("/api/imports/config", base_url=self.base_url)
        self.assertEqual(200, config.status_code)
        self.assertEqual("lifetime", config.get_json()["quotaMode"])
        self.assert_contract("GET", "/imports/config", config)

        create_payload = {
            "entryMethod": "upload",
            "analysisAuthorized": True,
            "playbackAuthorized": True,
            "idempotencyKey": str(uuid.uuid4()),
        }
        created = firebase.post(
            "/api/imports",
            json=create_payload,
            headers=self.csrf_headers(csrf),
            base_url=self.base_url,
        )
        self.assertEqual(200, created.status_code, created.get_data(as_text=True))
        self.assert_contract("POST", "/imports", created)
        import_id = created.get_json()["id"]
        self.assertEqual("file_required", created.get_json()["state"])
        create_replay = firebase.post(
            "/api/imports",
            json=create_payload,
            headers=self.csrf_headers(csrf),
            base_url=self.base_url,
        )
        self.assertEqual(200, create_replay.status_code)
        self.assertEqual(created.get_json(), create_replay.get_json())
        self.assert_contract("POST", "/imports", create_replay)

        reservation = {
            "uploadURL": "https://upload.invalid/disposable-session",
            "method": "PUT",
            "headers": {
                "Content-Type": "video/mp4",
                "Content-Range": "bytes 0-122/123",
            },
            "expiresAt": (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
            "sessionReference": "encrypted-disposable-session",
        }
        reserve_transport = Mock(side_effect=lambda *_args: reservation.copy())
        upload_payload = {
            "fileName": "journey.mp4",
            "sizeBytes": 123,
            "contentType": "video/mp4",
        }
        with patch(
            "sceneit.private_storage.reserve_upload", reserve_transport
        ):
            reserved = firebase.post(
                f"/api/imports/{import_id}/upload",
                json=upload_payload,
                headers=self.csrf_headers(csrf),
                base_url=self.base_url,
            )
            reserve_replay = firebase.post(
                f"/api/imports/{import_id}/upload",
                json=upload_payload,
                headers=self.csrf_headers(csrf),
                base_url=self.base_url,
            )
        self.assertEqual(200, reserved.status_code, reserved.get_data(as_text=True))
        self.assertEqual("awaiting_upload", reserved.get_json()["import"]["state"])
        self.assertEqual(200, reserve_replay.status_code)
        self.assertEqual(
            reserved.get_json()["import"]["id"],
            reserve_replay.get_json()["import"]["id"],
        )
        self.assert_contract(
            "POST", "/imports/{importId}/upload", reserved)
        self.assert_contract(
            "POST", "/imports/{importId}/upload", reserve_replay)
        self.assertEqual(2, reserve_transport.call_count)

        object_info = {
            "size": 123,
            "contentType": "video/mp4",
            "generation": "77",
        }
        with patch("sceneit.private_storage.object_info",
                   return_value=object_info):
            completed = firebase.post(
                f"/api/imports/{import_id}/complete",
                json={},
                headers=self.csrf_headers(csrf),
                base_url=self.base_url,
            )
            complete_replay = firebase.post(
                f"/api/imports/{import_id}/complete",
                json={},
                headers=self.csrf_headers(csrf),
                base_url=self.base_url,
            )
        self.assertEqual(200, completed.status_code, completed.get_data(as_text=True))
        self.assertEqual("queued", completed.get_json()["state"])
        self.assertTrue(completed.get_json()["budgetReserved"])
        self.assertEqual(200, complete_replay.status_code)
        self.assertEqual(completed.get_json(), complete_replay.get_json())
        self.assert_contract(
            "POST", "/imports/{importId}/complete", completed)
        self.assert_contract(
            "POST", "/imports/{importId}/complete", complete_replay)

        # Existing worker suites own provider sequencing.  This connected route
        # journey simulates its confirmed terminal persistence without transport.
        with self.conn() as conn:
            persisted = conn.execute(
                "SELECT owner_id,state,upload_generation,budget_reserved "
                "FROM sceneit_imports WHERE id=%s", (import_id,)
            ).fetchone()
            self.assertEqual(owner, persisted["owner_id"])
            self.assertEqual("queued", persisted["state"])
            self.assertEqual("77", persisted["upload_generation"])
            self.assertTrue(persisted["budget_reserved"])
            conn.execute(
                "UPDATE sceneit_imports SET state='ready',"
                "status_message='Ready to search.',progress_percent=100,"
                "duration_seconds=20,file_size_bytes=123,has_audio=true,"
                "width=640,height=360,sha256=%s,video_codec='h264',"
                "audio_codec='aac',media_path=upload_path,media_generation=%s,"
                "index_id='journey-index',asset_id='journey-asset',"
                "indexed_asset_id='journey-indexed',updated_at=now() "
                "WHERE id=%s",
                ("a" * 64, "77", import_id),
            )

        search_transport = Mock()
        search_transport.search.return_value = {
            "data": [{
                "video_id": "journey-indexed",
                "start": 2,
                "end": 4,
                "confidence": "high",
            }],
            "page_info": {},
        }
        factory = Mock(return_value=search_transport)

        from sceneit import import_search

        def fixture_search(owner_id, item_id, payload):
            return import_search.search_import(
                owner_id, item_id, payload, client_factory=factory)

        with patch("sceneit.imports.search_import", side_effect=fixture_search):
            searched = firebase.post(
                f"/api/imports/{import_id}/searches",
                json={"query": "red door", "modality": "visual"},
                headers=self.csrf_headers(csrf),
                base_url=self.base_url,
            )
            self.assertEqual(
                200, searched.status_code, searched.get_data(as_text=True))
            self.assert_contract(
                "POST", "/imports/{importId}/searches", searched)
            result = searched.get_json()
            history = firebase.get(
                f"/api/imports/{import_id}/searches", base_url=self.base_url)
            replay = firebase.post(
                f"/api/imports/{import_id}/searches",
                json={"query": "RED DOOR", "modality": "visual"},
                headers=self.csrf_headers(csrf),
                base_url=self.base_url,
            )
        self.assertEqual(200, history.status_code)
        self.assertEqual([result], history.get_json())
        self.assert_contract(
            "GET", "/imports/{importId}/searches", history)
        self.assertEqual(200, replay.status_code)
        self.assertEqual(result, replay.get_json())
        self.assert_contract(
            "POST", "/imports/{importId}/searches", replay)
        factory.assert_called_once()
        search_transport.search.assert_called_once()
        search_transport.close.assert_called_once()
        with self.conn() as conn:
            search_row = conn.execute(
                "SELECT state,matches,resolution FROM sceneit_import_searches "
                "WHERE id=%s", (result["id"],)
            ).fetchone()
            usage = conn.execute(
                "SELECT imports_used,searches_used FROM sceneit_import_usage "
                "WHERE owner_id=(SELECT trial_ledger_id FROM "
                "sceneit_firebase_identities WHERE owner_id=%s)",
                (owner,),
            ).fetchone()
        self.assertEqual("done", search_row["state"])
        self.assertEqual("completed", search_row["resolution"])
        self.assertEqual(result["matches"], search_row["matches"])
        self.assertEqual((1, 1), (usage["imports_used"], usage["searches_used"]))

        # A fresh provider exchange for the same immutable Firebase identity
        # must reconnect to the private owner, cumulative ledger, and history.
        firebase_again = self.app.test_client()
        signed_in_again = self.exchange_firebase(firebase_again)
        self.assertEqual(owner, signed_in_again["user"]["id"])
        self.assertEqual(1, signed_in_again["usage"]["importsUsed"])
        self.assertEqual(1, signed_in_again["usage"]["searchesUsed"])
        history_again = firebase_again.get(
            f"/api/imports/{import_id}/searches", base_url=self.base_url)
        self.assertEqual(200, history_again.status_code)
        self.assertEqual([result], history_again.get_json())
        self.assert_contract(
            "GET", "/imports/{importId}/searches", history_again)

        # Saved frame/media reads use fixture bytes but traverse owner checks.
        with patch("sceneit.media.private_frame", return_value=b"jpeg-fixture"):
            frame = firebase.get(
                result["matches"][0]["frameUrl"], base_url=self.base_url)
        self.assertEqual(200, frame.status_code)
        self.assertEqual("image/jpeg", frame.mimetype)
        with patch(
            "sceneit.private_storage.open_private",
            return_value=Response(b"video-fixture", mimetype="video/mp4"),
        ) as media_transport:
            media = firebase.get(
                f"/api/imports/{import_id}/source", base_url=self.base_url)
        self.assertEqual(200, media.status_code)
        media_transport.assert_called_once()

        # A separately authenticated admitted Replit owner cannot observe any
        # Firebase-owned import, saved result, frame, or media.
        replit, replit_digest = self.replit_client()
        replit_auth = replit.get("/api/auth/session", base_url=self.base_url)
        self.assertEqual(200, replit_auth.status_code)
        self.assert_contract("GET", "/auth/session", replit_auth)
        self.assertEqual("replit", replit_auth.get_json()["user"]["provider"])
        self.assertTrue(replit_auth.get_json()["pilotAdmitted"])
        self.assertEqual(
            {"allowed": True, "reason": "ready"},
            replit_auth.get_json()["privateAccess"],
        )
        cross_owner_requests = (
            replit.get(f"/api/imports/{import_id}", base_url=self.base_url),
            replit.get(
                f"/api/imports/{import_id}/searches", base_url=self.base_url),
            replit.post(
                f"/api/imports/{import_id}/searches",
                json={"query": "red door", "modality": "visual"},
                headers=self.csrf_headers(replit_auth.get_json()["csrfToken"]),
                base_url=self.base_url,
            ),
            replit.get(
                result["matches"][0]["frameUrl"], base_url=self.base_url),
            replit.get(
                f"/api/imports/{import_id}/source", base_url=self.base_url),
        )
        self.assertEqual([404] * 5, [
            response.status_code for response in cross_owner_requests])
        self.assertTrue(all(
            response.get_json()["code"] in {"import_not_found", "frame_not_found"}
            for response in cross_owner_requests
        ))

        revoked = firebase.post(
            f"/api/imports/{import_id}/playback",
            json={"authorized": False},
            headers=self.csrf_headers(csrf),
            base_url=self.base_url,
        )
        self.assertEqual(200, revoked.status_code)
        self.assertFalse(revoked.get_json()["playbackAuthorized"])
        self.assert_contract(
            "POST", "/imports/{importId}/playback", revoked)
        unavailable = firebase.get(
            f"/api/imports/{import_id}/source", base_url=self.base_url)
        self.assertEqual(404, unavailable.status_code)
        self.assertEqual(
            "source_playback_unavailable", unavailable.get_json()["code"])

        cancelled = firebase.post(
            f"/api/imports/{import_id}/cancel",
            json={},
            headers=self.csrf_headers(csrf),
            base_url=self.base_url,
        )
        self.assertEqual(200, cancelled.status_code)
        self.assertEqual("cancel_requested", cancelled.get_json()["state"])
        self.assert_contract(
            "POST", "/imports/{importId}/cancel", cancelled)

        cleanup_transport = Mock()
        from google.api_core.exceptions import NotFound
        with patch(
            "sceneit.private_storage.decrypt_upload_session",
            return_value="https://upload.invalid/disposable-session",
        ), patch(
            "sceneit.private_storage.cancel_upload_session"
        ) as cancel_session, patch(
            "sceneit.private_storage.object_info",
            side_effect=NotFound("fixture absent"),
        ), patch("sceneit.private_storage.delete_object"):
            job = import_worker.claim_job()
            self.assertEqual(uuid.UUID(import_id), job["id"])
            import_worker.process_job(job, cleanup_transport)
        self.assertEqual(2, cancel_session.call_count)
        cleanup_transport.delete_indexed_asset.assert_called_once_with(
            "journey-index", "journey-indexed")
        cleanup_transport.delete_asset.assert_called_once_with("journey-asset")
        cleanup_transport.delete_index.assert_called_once_with("journey-index")
        with self.conn() as conn:
            cleaned = conn.execute(
                "SELECT state,playback_authorized,lease_token "
                "FROM sceneit_imports WHERE id=%s", (import_id,)
            ).fetchone()
            usage_after_cleanup = conn.execute(
                "SELECT imports_used,searches_used FROM sceneit_import_usage "
                "WHERE owner_id=%s", (usage_owner := conn.execute(
                    "SELECT trial_ledger_id FROM sceneit_firebase_identities "
                    "WHERE owner_id=%s", (owner,)
                ).fetchone()["trial_ledger_id"],)
            ).fetchone()
        self.assertEqual("cancelled", cleaned["state"])
        self.assertFalse(cleaned["playback_authorized"])
        self.assertIsNone(cleaned["lease_token"])
        self.assertEqual(
            (1, 1),
            (usage_after_cleanup["imports_used"],
             usage_after_cleanup["searches_used"]),
        )
        self.assertTrue(usage_owner.startswith("firebase-email-v1:"))

        logged_out = firebase.post(
            "/api/logout",
            json={},
            headers=self.csrf_headers(csrf),
            base_url=self.base_url,
        )
        self.assertEqual(200, logged_out.status_code)
        self.assertEqual({"success": True}, logged_out.get_json())
        self.assert_contract("POST", "/logout", logged_out)
        self.assertIsNone(firebase.get(
            "/api/auth/session", base_url=self.base_url
        ).get_json()["user"])
        logged_out_again = firebase_again.post(
            "/api/logout",
            json={},
            headers=self.csrf_headers(signed_in_again["csrfToken"]),
            base_url=self.base_url,
        )
        self.assertEqual(200, logged_out_again.status_code)
        self.assert_contract("POST", "/logout", logged_out_again)
        with self.conn() as conn:
            self.assertEqual(0, conn.execute(
                "SELECT count(*) AS n FROM sceneit_auth_sessions "
                "WHERE user_id=%s", (owner,)
            ).fetchone()["n"])

        # Expiry is independently enforced for the second provider/session.
        with self.conn() as conn:
            conn.execute(
                "UPDATE sceneit_auth_sessions SET expires_at=now()-interval '1 second' "
                "WHERE id=%s", (replit_digest,))
        expired = replit.get("/api/auth/session", base_url=self.base_url)
        self.assertEqual(200, expired.status_code)
        self.assertIsNone(expired.get_json()["user"])
