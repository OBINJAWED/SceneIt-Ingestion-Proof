"""Controlled-pilot HTTP boundaries without database or provider calls."""
import json
import os
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from sceneit.server import _environment_config, create_app
from sceneit.db import DatabaseResourceExhausted


SESSION = {
    "id": "session-digest",
    "user_id": "oidc-subject-1",
    "first_name": "Pilot",
    "csrf_token": "csrf-1",
}


class HttpSecurityTests(unittest.TestCase):
    def app(self, **changes):
        config = {
            "TESTING": True,
            "SESSION_SECRET": "s" * 48,
            "PILOT_ALLOWED_SUBJECTS": "oidc-subject-1",
            "TRUSTED_HOSTS": ["sceneit.example"],
            "TRUST_PROXY_HOPS": 0,
            "DATABASE_CONFIGURED": True,
        }
        config.update(changes)
        return create_app(config)

    def test_health_and_minimal_auth_are_public_but_data_is_not(self):
        application = self.app()
        with patch("sceneit.auth._session_from_cookie", return_value=None) as session:
            client = application.test_client()
            self.assertEqual(
                client.get("/api/healthz", base_url="https://sceneit.example").status_code,
                200,
            )
            session.assert_not_called()
            self.assertEqual(
                client.get("/api/auth/user", base_url="https://sceneit.example").status_code,
                200,
            )
            auth_session = client.get(
                "/api/auth/session", base_url="https://sceneit.example"
            )
            self.assertEqual(auth_session.status_code, 200)
            self.assertEqual(auth_session.headers["Cache-Control"], "no-store")
            self.assertFalse(auth_session.get_json()["pilotAdmitted"])
            for path in (
                "/api/proof", "/api/proof/searches", "/api/proof/report",
                "/api/proof/source",
                "/api/proof/searches/6f30e229-bb64-46bc-aaf5-779bd96b9c11/frames/1",
                "/api/imports/config", "/api/imports/current",
            ):
                with self.subTest(path=path):
                    self.assertEqual(
                        client.get(path, base_url="https://sceneit.example").status_code,
                        401,
                    )
                    self.assertEqual(
                        client.get(path, base_url="https://sceneit.example").get_json()["state"],
                        "unauthorized",
                    )

    def test_signed_in_subject_must_match_allowlist_exactly(self):
        application = self.app(PILOT_ALLOWED_SUBJECTS="OIDC-SUBJECT-1, other")
        client = application.test_client()
        with patch("sceneit.auth._session_from_cookie", return_value=SESSION):
            response = client.get("/api/proof", base_url="https://sceneit.example")
            auth = client.get("/api/auth/user", base_url="https://sceneit.example")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "pilot_not_admitted")
        self.assertEqual(response.get_json()["state"], "admission_required")
        self.assertFalse(auth.get_json()["pilotAdmitted"])
        self.assertEqual(auth.get_json()["csrfToken"], "csrf-1")

    def test_admitted_session_is_reused_and_reported(self):
        application = self.app()
        client = application.test_client()
        with patch("sceneit.auth._session_from_cookie", return_value=SESSION), \
                patch("sceneit.server.public_proof", return_value={"state": "ready"}):
            proof = client.get("/api/proof", base_url="https://sceneit.example")
            auth = client.get("/api/auth/user", base_url="https://sceneit.example")
        self.assertEqual(proof.status_code, 200)
        self.assertTrue(auth.get_json()["pilotAdmitted"])

    def test_proof_write_requires_session_csrf_before_paid_work(self):
        application = self.app()
        client = application.test_client()
        with patch("sceneit.auth._session_from_cookie", return_value=SESSION), \
                patch("sceneit.security._is_expensive_request", return_value=False), \
                patch("sceneit.server.search_scenes") as search:
            missing = client.post(
                "/api/proof/searches", json={"query": "scene"},
                base_url="https://sceneit.example",
            )
            wrong_origin = client.post(
                "/api/proof/searches", json={"query": "scene"},
                headers={"X-CSRF-Token": "csrf-1", "Origin": "https://evil.example"},
                base_url="https://sceneit.example",
            )
        self.assertEqual(missing.status_code, 403)
        self.assertEqual(wrong_origin.status_code, 403)
        self.assertEqual(missing.get_json()["state"], "unauthorized")
        self.assertEqual(wrong_origin.get_json()["state"], "unauthorized")
        search.assert_not_called()

    def test_search_route_does_not_double_acquire_application_permit(self):
        application = self.app()
        client = application.test_client()
        result = {"id": "result", "matches": []}
        with patch("sceneit.auth._session_from_cookie", return_value=SESSION), \
                patch("sceneit.security._is_expensive_request", return_value=False), \
                patch("sceneit.server.search_scenes", return_value=result) as search, \
                patch("sceneit.resources.shared_permit") as permit:
            response = client.post(
                "/api/proof/searches", json={"query": "scene"},
                headers={"X-CSRF-Token": "csrf-1"},
                base_url="https://sceneit.example",
            )
        self.assertEqual(response.status_code, 200)
        search.assert_called_once_with({"query": "scene"})
        permit.assert_not_called()

    def test_search_operations_route_matches_proof_contract(self):
        application = self.app()
        client = application.test_client()
        operations = [{
            "id": "6f30e229-bb64-46bc-aaf5-779bd96b9c11",
            "state": "needs_review",
            "attemptId": None,
            "createdAt": "2026-01-01T00:00:00+00:00",
            "deadlineAt": None,
            "completedAt": None,
            "errorCode": "search_outcome_unknown",
        }]
        with patch("sceneit.auth._session_from_cookie", return_value=SESSION), \
                patch("sceneit.server.list_search_operations",
                      return_value=operations) as listing:
            response = client.get(
                "/api/proof/search-operations",
                base_url="https://sceneit.example",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), operations)
        listing.assert_called_once_with()

    def test_private_routes_cannot_be_shared_cached(self):
        application = self.app()
        client = application.test_client()
        with patch("sceneit.auth._session_from_cookie", return_value=SESSION), \
                patch("sceneit.server.public_proof", return_value={"state": "ready"}):
            response = client.get("/api/proof", base_url="https://sceneit.example")
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.assertIn("Cookie", response.headers["Vary"])
        self.assertNotIn("oidc-subject-1", response.headers["X-Request-ID"])

    def test_host_and_forwarded_host_policy_fail_closed(self):
        application = self.app(TRUST_PROXY_HOPS=1)
        client = application.test_client()
        direct = client.get("/api/healthz", base_url="https://evil.example")
        forwarded = client.get(
            "/api/healthz", base_url="http://internal",
            headers={
                "X-Forwarded-Host": "sceneit.example",
                "X-Forwarded-Proto": "https",
            },
        )
        self.assertEqual(direct.status_code, 400)
        self.assertEqual(forwarded.status_code, 200)

    def test_workspace_loopback_is_trusted_without_weakening_deployment(self):
        workspace = {
            "REPLIT_DEV_DOMAIN": "preview.replit.dev",
            "REPLIT_RUN_PATH": "/home/runner/workspace",
            "REPLIT_DEPLOYMENT": "",
            "REPLIT_DOMAINS": "",
            "TRUSTED_HOSTS": "",
        }
        with patch.dict(os.environ, workspace, clear=False):
            hosts = _environment_config()["TRUSTED_HOSTS"].split(",")
        self.assertIn("preview.replit.dev", hosts)
        self.assertIn("localhost", hosts)
        self.assertIn("127.0.0.1", hosts)

        with patch.dict(
            os.environ, {**workspace, "REPLIT_DEPLOYMENT": "1"}, clear=False
        ):
            deployed_hosts = _environment_config()["TRUSTED_HOSTS"].split(",")
        self.assertEqual(deployed_hosts, ["preview.replit.dev"])

    def test_request_logging_is_correlated_and_redacted(self):
        application = self.app()
        client = application.test_client()
        with self.assertLogs("sceneit", level="INFO") as captured:
            response = client.get(
                "/api/healthz?token=do-not-log",
                headers={"Cookie": "private-cookie=do-not-log"},
                base_url="https://sceneit.example",
            )
        event = json.loads(captured.records[-1].getMessage())
        self.assertEqual(event["event"], "http_request")
        self.assertEqual(event["route"], "/api/healthz")
        self.assertEqual(event["status"], 200)
        self.assertIn("duration_ms", event)
        self.assertEqual(response.headers["X-Request-ID"], event["request_id"])
        output = "\n".join(record.getMessage() for record in captured.records)
        self.assertNotIn("do-not-log", output)

    def test_db_capacity_is_typed_retryable_and_logged_as_saturation(self):
        application = self.app()
        client = application.test_client()
        with patch("sceneit.auth._session_from_cookie", return_value=SESSION), \
                patch(
                    "sceneit.server.public_proof",
                    side_effect=DatabaseResourceExhausted("private detail"),
                ), self.assertLogs("sceneit", level="WARNING") as captured:
            response = client.get(
                "/api/proof", base_url="https://sceneit.example"
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Retry-After"], "10")
        self.assertEqual(response.get_json()["state"], "service_unavailable")
        self.assertEqual(
            response.get_json()["code"], "database_capacity_exhausted"
        )
        self.assertNotIn("private detail", response.get_data(as_text=True))
        self.assertTrue(any(
            json.loads(record.getMessage()).get("event") == "resource_saturation"
            for record in captured.records
        ))

    def test_log_carries_safe_operation_and_attempt_correlation(self):
        from sceneit.http import problem_response, set_operation_context

        application = self.app()

        @application.get("/test-operation")
        def operation_failure():
            set_operation_context("operation-1", "attempt-1")
            return problem_response("Busy.", "busy", 429, retry_after=10)

        client = application.test_client()
        with self.assertLogs("sceneit", level="INFO") as captured:
            response = client.get(
                "/test-operation", base_url="https://sceneit.example"
            )
        event = json.loads(captured.records[-1].getMessage())
        self.assertEqual(response.get_json()["state"], "service_unavailable")
        self.assertEqual(event["code"], "busy")
        self.assertEqual(event["operation_id"], "operation-1")
        self.assertEqual(event["attempt_id"], "attempt-1")

    def test_quota_and_import_failures_use_contract_state(self):
        from sceneit.http import problem_response
        from sceneit.import_limits import ImportProblem
        from sceneit.imports import import_error

        application = self.app()
        with application.test_request_context(
                "/api/imports", base_url="https://sceneit.example"):
            imported = import_error(ImportProblem(
                "import_quota_reached", "Import quota reached.", 429
            ))
            with self.assertLogs("sceneit", level="WARNING") as captured:
                proof = problem_response(
                    "Proof quota reached.", "proof_budget_reached", 429
                )
        self.assertEqual(imported.get_json()["state"], "quota_exhausted")
        self.assertEqual(proof.get_json()["state"], "quota_exhausted")
        self.assertTrue(any(
            json.loads(record.getMessage()).get("event") == "quota_denied"
            for record in captured.records
        ))

    def test_proof_readiness_route_is_protected_and_contract_shaped(self):
        application = self.app()
        client = application.test_client()
        readiness = {
            "state": "ready", "proofState": "ready",
            "searchAvailable": True, "searchesUsed": 1, "searchLimit": 50,
            "detail": None, "retryAfterSeconds": None,
        }
        with patch("sceneit.auth._session_from_cookie", return_value=SESSION), \
                patch("sceneit.server.proof_readiness",
                      return_value=readiness):
            response = client.get(
                "/api/proof/readiness", base_url="https://sceneit.example"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), readiness)


class ReadinessTests(unittest.TestCase):
    @staticmethod
    @contextmanager
    def ready_connection():
        conn = Mock()
        conn.execute.return_value.fetchall.return_value = [{
            "version": 1, "name": "001_test.sql", "sha256": "a" * 64,
        }]
        yield conn

    def test_readiness_separates_external_without_provider_probe(self):
        application = HttpSecurityTests().app()
        client = application.test_client()
        with patch("sceneit.health.settings"), \
                patch("sceneit.health.connection", self.ready_connection), \
                patch("sceneit.health.migration_status", return_value=[
                    {"status": "applied"}
                ]):
            response = client.get("/api/readyz", base_url="https://sceneit.example")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["external"]["provider"], "not_probed")

    def test_database_or_schema_outage_is_safe_and_not_ready(self):
        application = HttpSecurityTests().app()
        client = application.test_client()
        with patch("sceneit.health.settings"), \
                patch("sceneit.health.connection", side_effect=RuntimeError("secret-url")):
            response = client.get("/api/readyz", base_url="https://sceneit.example")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["checks"]["database"], "unavailable")
        self.assertNotIn("secret-url", response.get_data(as_text=True))

    def test_pending_or_incompatible_schema_is_not_ready(self):
        application = HttpSecurityTests().app()
        client = application.test_client()
        with patch("sceneit.health.settings"), \
                patch("sceneit.health.connection", self.ready_connection), \
                patch("sceneit.health.migration_status", return_value=[
                    {"status": "applied"}, {"status": "pending"},
                ]):
            response = client.get("/api/readyz", base_url="https://sceneit.example")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["checks"], {
            "config": "ok", "database": "ok", "schema": "incompatible",
        })


if __name__ == "__main__":
    unittest.main()