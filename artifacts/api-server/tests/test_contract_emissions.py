"""Validate actual Flask JSON emissions against the checked-in OpenAPI schemas."""
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from sceneit.server import create_app


ROOT = Path(__file__).resolve().parents[3]
VALIDATOR = ROOT / "scripts" / "validate-openapi-response.mjs"
SESSION = {
    "id": "session-digest",
    "user_id": "oidc-subject-1",
    "first_name": "Pilot",
    "csrf_token": "csrf-1",
}


class ContractEmissionTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({
            "TESTING": True,
            "SESSION_SECRET": "s" * 48,
            "PILOT_ALLOWED_SUBJECTS": "oidc-subject-1",
            "TRUSTED_HOSTS": ["sceneit.example"],
            "TRUST_PROXY_HOPS": 0,
            "DATABASE_CONFIGURED": True,
        })
        self.client = self.app.test_client()

    def assert_contract(self, method, path, response):
        result = subprocess.run(
            ["node", str(VALIDATOR), method, path, str(response.status_code)],
            input=json.dumps(response.get_json()),
            text=True,
            capture_output=True,
            cwd=ROOT,
            timeout=15,
        )
        self.assertEqual("", result.stderr, result.stderr)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_public_health_and_auth_emissions(self):
        with patch("sceneit.auth._session_from_cookie", return_value=None):
            health = self.client.get(
                "/api/healthz", base_url="https://sceneit.example"
            )
            auth = self.client.get(
                "/api/auth/user", base_url="https://sceneit.example"
            )
            auth_session = self.client.get(
                "/api/auth/session", base_url="https://sceneit.example"
            )
        self.assert_contract("GET", "/healthz", health)
        self.assert_contract("GET", "/auth/user", auth)
        self.assertEqual(auth_session.status_code, 200)
        self.assert_contract("GET", "/auth/session", auth_session)

    def test_proof_readiness_emission(self):
        readiness = {
            "state": "ready",
            "proofState": "ready",
            "searchAvailable": True,
            "searchesUsed": 3,
            "searchLimit": 50,
            "detail": None,
            "retryAfterSeconds": None,
        }
        with patch("sceneit.auth._session_from_cookie", return_value=SESSION), \
                patch("sceneit.server.proof_readiness",
                      return_value=readiness):
            emitted = self.client.get(
                "/api/proof/readiness", base_url="https://sceneit.example"
            )
        self.assertEqual(emitted.status_code, 200)
        self.assert_contract("GET", "/proof/readiness", emitted)

    def test_lifetime_usage_emission_without_current_import(self):
        usage = {
            "importsUsed": 3, "importLimit": 3, "importsRemaining": 0,
            "searchesUsed": 50, "searchLimit": 50, "searchesRemaining": 0,
            "lifetime": True,
        }
        with patch("sceneit.auth._session_from_cookie", return_value=SESSION), \
                patch("sceneit.import_limits.usage_snapshot", return_value=usage):
            emitted = self.client.get(
                "/api/auth/session", base_url="https://sceneit.example"
            )
        self.assertEqual(200, emitted.status_code)
        self.assertEqual(usage, emitted.get_json()["usage"])
        self.assertTrue(emitted.get_json()["privateAccess"]["allowed"])
        self.assert_contract("GET", "/auth/session", emitted)

    def test_trial_and_capacity_failures_are_distinct_typed_emissions(self):
        from sceneit.http import problem_response
        for code, state in (
            ("owner_import_limit", "trial_exhausted"),
            ("owner_search_limit", "trial_exhausted"),
            ("app_import_limit", "capacity_exhausted"),
            ("app_search_limit", "capacity_exhausted"),
        ):
            with self.subTest(code=code), self.app.test_request_context(
                "/api/imports", base_url="https://sceneit.example"
            ):
                emitted = problem_response("Allowance reached.", code, 429)
                self.assertEqual(state, emitted.get_json()["state"])
                self.assert_contract("POST", "/imports", emitted)

    def test_protected_failure_and_search_operation_emissions(self):
        with patch("sceneit.auth._session_from_cookie", return_value=None):
            denied = self.client.get(
                "/api/proof", base_url="https://sceneit.example"
            )
        self.assert_contract("GET", "/proof", denied)

        operations = [{
            "id": "6f30e229-bb64-46bc-aaf5-779bd96b9c11",
            "state": "needs_review",
            "attemptId": "1b3270d2-23f1-4e20-87b8-68c7057fd487",
            "createdAt": "2026-01-01T00:00:00+00:00",
            "deadlineAt": "2026-01-01T00:01:15+00:00",
            "completedAt": None,
            "errorCode": "search_outcome_unknown",
        }]
        with patch("sceneit.auth._session_from_cookie", return_value=SESSION), \
                patch("sceneit.server.list_search_operations",
                      return_value=operations):
            emitted = self.client.get(
                "/api/proof/search-operations",
                base_url="https://sceneit.example",
            )
        self.assert_contract("GET", "/proof/search-operations", emitted)