"""Focused real-emission checks for the commercial OpenAPI contract."""
import json
import os
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sceneit.billing_config import reset_billing_settings
from sceneit.imports import present_import
from sceneit.server import create_app


ROOT = Path(__file__).resolve().parents[3]
VALIDATOR = ROOT / "scripts" / "validate-openapi-response.mjs"
SESSION = {
    "id": "session-digest",
    "user_id": "oidc-subject-1",
    "first_name": "Pilot",
    "csrf_token": "csrf-1",
}


class CommercialContractTests(unittest.TestCase):
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

    def tearDown(self):
        reset_billing_settings()

    def assert_contract_payload(self, method, path, status, payload):
        result = subprocess.run(
            ["node", str(VALIDATOR), method, path, str(status)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=ROOT,
            timeout=15,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def assert_contract_response(self, method, path, response):
        self.assert_contract_payload(
            method, path, response.status_code, response.get_json()
        )

    def test_real_disabled_status_and_config_are_contract_safe(self):
        with patch.dict(os.environ, {"SCENEIT_BILLING_ENABLED": "false"}), \
                patch("sceneit.auth._session_from_cookie", return_value=SESSION):
            reset_billing_settings()
            status = self.client.get(
                "/api/billing/status", base_url="https://sceneit.example"
            )
            config = self.client.get(
                "/api/imports/config", base_url="https://sceneit.example"
            )

        self.assertEqual(200, status.status_code)
        self.assertEqual("disabled", status.get_json()["membership"])
        self.assertIsNone(status.get_json()["usage"])
        self.assert_contract_response("GET", "/billing/status", status)

        self.assertEqual(200, config.status_code)
        self.assertEqual("lifetime", config.get_json()["quotaMode"])
        self.assert_contract_response("GET", "/imports/config", config)

    def test_enabled_status_and_config_fixtures_are_contract_safe(self):
        metric = {"limit": 10, "used": 3, "remaining": 7}
        status_payload = {
            "enabled": True,
            "environment": "test",
            "membership": "active",
            "paidThrough": "2026-06-30T12:00:00Z",
            "cancelAtPeriodEnd": False,
            "usage": {
                "windowStart": "2026-05-31T12:00:00Z",
                "windowEnd": "2026-06-30T12:00:00Z",
                "metrics": {
                    "imports": metric,
                    "upload_attempts": metric,
                    "analysis_seconds": metric,
                    "searches": metric,
                    "media_bytes": metric,
                    "frames": metric,
                },
                "storage": metric,
                "workStopped": False,
            },
        }
        billing = {
            "enabled": True,
            "limits": {"imports": 7, "searches": 11},
            "app_limits": {"imports": 70, "searches": 110},
        }
        with patch("sceneit.auth._session_from_cookie", return_value=SESSION), \
                patch("sceneit.billing_routes.status", return_value=status_payload), \
                patch("sceneit.billing_config.billing_settings", return_value=billing):
            status = self.client.get(
                "/api/billing/status", base_url="https://sceneit.example"
            )
            config = self.client.get(
                "/api/imports/config", base_url="https://sceneit.example"
            )

        self.assert_contract_response("GET", "/billing/status", status)
        self.assertEqual("monthly", config.get_json()["quotaMode"])
        self.assertEqual(7, config.get_json()["ownerImportLimit"])
        self.assertEqual(11, config.get_json()["ownerSearchLimit"])
        self.assert_contract_response("GET", "/imports/config", config)

    def test_presented_import_emits_required_quota_mode(self):
        now = datetime.now(timezone.utc)
        row = {
            "id": "4bb15290-e6aa-4c09-8755-fefdb6e73e0b",
            "title": "Authorized clip",
            "entry_method": "upload",
            "source_kind": "file",
            "source_url": None,
            "external_id": None,
            "state": "ready",
            "status_message": "Ready",
            "progress_percent": 100,
            "error_code": None,
            "duration_seconds": 12.5,
            "file_size_bytes": 1024,
            "has_audio": True,
            "media_path": "private/object.mp4",
            "playback_authorized": True,
            "created_at": now,
            "updated_at": now,
            "expires_at": now + timedelta(days=1),
        }
        usage = {
            "imports_used": 2,
            "searches_used": 4,
            "import_limit": 10,
            "search_limit": 20,
        }
        with patch(
            "sceneit.billing_config.billing_settings",
            return_value={"enabled": True},
        ):
            payload = present_import(row, usage)

        self.assertEqual("monthly", payload["quotaMode"])
        self.assert_contract_payload(
            "GET", "/imports/{importId}", 200, payload
        )


if __name__ == "__main__":
    unittest.main()