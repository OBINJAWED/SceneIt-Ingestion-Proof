"""Focused cancellation regressions; no storage or provider calls are made."""
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from flask import Flask, g, jsonify
from werkzeug.exceptions import HTTPException

from sceneit import import_worker, imports
from sceneit.import_limits import ImportProblem

class Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row

class ImportConnection:
    def __init__(self, row, usage):
        self.row = row
        self.usage = usage
        self.updates = 0

    def execute(self, query, parameters=()):
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT * FROM sceneit_imports"):
            import_id, owner = parameters
            return Result(
                self.row
                if self.row["id"] == import_id and self.row["owner_id"] == owner
                else None
            )
        if normalized.startswith(
                "UPDATE sceneit_imports SET state='cancel_requested'"
            ):
            self.updates += 1
            self.row["state"] = "cancel_requested"
            self.row["status_message"] = "Cancellation and cleanup requested."
            self.row["updated_at"] = datetime.now(timezone.utc)
            return Result(self.row)
        if normalized.startswith("SELECT imports_used,searches_used"):
            return Result(self.usage.copy())
        raise AssertionError(f"Unexpected database statement: {normalized}")

class ImportCancellationTests(unittest.TestCase):
    def setUp(self):
        now = datetime.now(timezone.utc)
        self.owner = "cancel-owner"
        self.row = {
            "id": uuid.uuid4(),
            "owner_id": self.owner,
            "title": None,
            "entry_method": "upload",
            "source_kind": "file",
            "source_url": None,
            "external_id": None,
            "state": "awaiting_upload",
            "status_message": "Upload reserved.",
            "progress_percent": None,
            "error_code": None,
            "duration_seconds": None,
            "file_size_bytes": None,
            "has_audio": None,
            "media_path": None,
            "playback_authorized": False,
            "created_at": now,
            "updated_at": now,
            "expires_at": now + timedelta(days=1),
            "upload_path": "private/reserved.mp4",
            "upload_generation": None,
            "upload_expires_at": now + timedelta(minutes=10),
            "budget_reserved": True,
        }
        self.usage = {"imports_used": 2, "searches_used": 7}
        self.database = ImportConnection(self.row, self.usage)

        @contextmanager
        def connection():
            yield self.database

        connection_patch = patch("sceneit.imports.connection", connection)
        connection_patch.start()
        self.addCleanup(connection_patch.stop)
        app = Flask(__name__)
        app.config["TESTING"] = True

        @app.before_request
        def session():
            owner = imports.request.headers.get("X-Test-Owner")
            g.auth_session = (
                {"user_id": owner, "csrf_token": "cancel-csrf"} if owner else None
            )

        @app.errorhandler(ImportProblem)
        def import_error(error):
            return jsonify(error=error.message, code=error.code), error.status

        @app.errorhandler(HTTPException)
        def http_error(error):
            return jsonify(error=error.description), error.code

        app.register_blueprint(imports.imports_bp)
        self.client = app.test_client()
    def headers(self, owner=None, csrf="cancel-csrf"):
        headers = {"X-Test-Owner": owner or self.owner}
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        return headers
    def cancel(self, **header_changes):
        headers = self.headers()
        headers.update(header_changes)
        return self.client.post(
            f"/api/imports/{self.row['id']}/cancel", json={}, headers=headers
        )

    def test_cancel_requires_authenticated_owner_and_csrf(self):
        path = f"/api/imports/{self.row['id']}/cancel"
        anonymous = self.client.post(path, json={})
        missing_csrf = self.client.post(
            path, json={}, headers=self.headers(csrf=None)
        )
        wrong_owner = self.client.post(
            path, json={}, headers=self.headers(owner="another-owner")
        )
        self.assertEqual(401, anonymous.status_code)
        self.assertEqual(403, missing_csrf.status_code)
        self.assertEqual(404, wrong_owner.status_code)
        self.assertEqual("awaiting_upload", self.row["state"])

    def test_awaiting_upload_and_queued_can_cancel_without_refunding_usage(self):
        for state in ("awaiting_upload", "queued"):
            with self.subTest(state=state):
                self.row["state"] = state
                response = self.cancel()
                self.assertEqual(200, response.status_code)
                body = response.get_json()
                self.assertEqual("cancel_requested", body["state"])
                self.assertEqual(2, body["importsUsed"])
                self.assertEqual(7, body["searchesUsed"])
        self.assertEqual({"imports_used": 2, "searches_used": 7}, self.usage)

    def test_completion_after_cancel_cannot_enqueue_or_touch_storage(self):
        self.assertEqual(200, self.cancel().status_code)
        with patch(
            "sceneit.private_storage.object_info",
            side_effect=AssertionError("cancelled upload reached storage"),
        ), patch(
            "sceneit.imports.reserve_import_budget",
            side_effect=AssertionError("cancelled upload reserved budget"),
        ):
            response = self.client.post(
                f"/api/imports/{self.row['id']}/complete",
                json={},
                headers=self.headers(),
            )
        self.assertEqual(409, response.status_code)
        self.assertEqual("upload_not_pending", response.get_json()["code"])
        self.assertEqual("cancel_requested", self.row["state"])

    def test_repeated_cancel_is_idempotent_and_terminal_stays_terminal(self):
        first = self.cancel()
        second = self.cancel()
        self.assertEqual("cancel_requested", first.get_json()["state"])
        self.assertEqual("cancel_requested", second.get_json()["state"])
        self.row["state"] = "cancelled"
        terminal = self.cancel()
        self.assertEqual(200, terminal.status_code)
        self.assertEqual("cancelled", terminal.get_json()["state"])
        self.assertEqual(2, self.database.updates)

    def test_worker_revokes_reserved_session_before_cleanup_and_honors_marker(self):
        events = []
        job = {
            **self.row,
            "state": "cancel_requested",
            "upload_session_reference": "encrypted",
            "provider_write_marker": None,
            "indexed_asset_id": None,
            "asset_id": None,
            "index_id": None,
            "media_generation": None,
            "sha256": None,
        }

        @contextmanager
        def worker_connection():
            events.append("cleanup-query")
            connection = Mock()
            connection.execute.return_value.fetchone.return_value = None
            yield connection

        with patch("sceneit.import_worker.connection", worker_connection), patch(
            "sceneit.private_storage.decrypt_upload_session",
            side_effect=lambda reference: events.append("decrypt") or "session-url",
        ), patch(
            "sceneit.private_storage.cancel_upload_session",
            side_effect=lambda url: events.append("revoke"),
        ), patch(
            "sceneit.private_storage.object_info",
            side_effect=lambda path: events.append("object-info") or {"generation": "1"},
        ), patch(
            "sceneit.private_storage.delete_object",
            side_effect=lambda path, **kw: events.append("delete-object"),
        ), patch("sceneit.import_worker._update", side_effect=lambda row, **kw: row.update(kw)):
            import_worker._cancel(job, Mock())
        self.assertEqual(["decrypt", "revoke", "cleanup-query"], events[:3])
        self.assertEqual(["object-info", "delete-object"], events[3:])
        self.assertEqual("cancelled", job["state"])

        guarded = {
            **job, "provider_write_marker": "upload_asset",
            "state": "cancel_requested",
        }
        with patch("sceneit.import_worker._fail") as fail, patch(
            "sceneit.private_storage.cancel_upload_session"
        ) as revoke:
            import_worker._cancel(guarded, Mock())
        fail.assert_called_once()
        revoke.assert_not_called()