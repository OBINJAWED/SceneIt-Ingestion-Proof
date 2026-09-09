"""Provider-free HTTP security checks for the private billing boundary."""
import os
import time
import unittest
from unittest.mock import Mock, patch

from sceneit.billing_config import (
    BillingSettings,
    reset_billing_settings,
)
from sceneit.billing_provider import BillingProviderError
from sceneit.server import create_app


SESSION = {
    "id": "session-digest",
    "user_id": "oidc-subject-1",
    "first_name": "Pilot",
    "csrf_token": "csrf-1",
}
BASE_URL = "https://sceneit.example"
CHECKOUT = {
    "tier": "fixture_basic",
    "cadence": "monthly",
    "currency": "usd",
    "idempotencyKey": "6f30e229-bb64-46bc-aaf5-779bd96b9c11",
}
PORTAL = {
    "action": "manage",
    "idempotencyKey": "24e7b94a-dbc9-4fa7-a614-bbb35165d2b5",
}


class BillingSecurityTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(
            os.environ, {"SCENEIT_BILLING_ENABLED": "false"}, clear=False
        ):
            reset_billing_settings()
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

    def test_unauthenticated_billing_owner_routes_are_401(self):
        with patch("sceneit.auth._session_from_cookie", return_value=None), \
                patch("sceneit.billing_routes.status") as status, \
                patch("sceneit.billing_routes.create_checkout") as checkout, \
                patch("sceneit.billing_routes.create_portal") as portal:
            responses = (
                self.client.get("/api/billing/status", base_url=BASE_URL),
                self.client.post(
                    "/api/billing/checkout", json=CHECKOUT, base_url=BASE_URL
                ),
                self.client.post(
                    "/api/billing/portal", json=PORTAL, base_url=BASE_URL
                ),
            )

        self.assertEqual([401, 401, 401], [item.status_code for item in responses])
        self.assertTrue(all(item.get_json()["state"] == "unauthorized"
                            for item in responses))
        status.assert_not_called()
        checkout.assert_not_called()
        portal.assert_not_called()

    def test_admitted_checkout_without_csrf_is_denied_before_provider(self):
        with patch("sceneit.auth._session_from_cookie", return_value=SESSION), \
                patch("sceneit.billing_routes.create_checkout") as checkout, \
                patch("sceneit.resources.admit_participant") as throttle:
            response = self.client.post(
                "/api/billing/checkout", json=CHECKOUT, base_url=BASE_URL
            )

        self.assertEqual(403, response.status_code)
        self.assertEqual("http_403", response.get_json()["code"])
        checkout.assert_not_called()
        throttle.assert_not_called()

    def test_status_and_portal_survive_admission_loss_but_checkout_does_not(self):
        former_member = {**SESSION, "user_id": "former-pilot-subject"}
        inactive = {
            "enabled": True,
            "environment": "test",
            "membership": "inactive",
            "paidThrough": None,
            "cancelAtPeriodEnd": False,
            "usage": None,
        }
        hosted = {
            "url": "https://billing.stripe.com/p/session",
            "expiresAt": None,
        }
        with patch("sceneit.auth._session_from_cookie", return_value=former_member), \
                patch("sceneit.billing_routes.status", return_value=inactive) as status, \
                patch(
                    "sceneit.billing_routes.create_portal", return_value=hosted
                ) as portal, \
                patch("sceneit.billing_routes.create_checkout") as checkout, \
                patch("sceneit.resources.admit_participant"):
            status_response = self.client.get(
                "/api/billing/status", base_url=BASE_URL
            )
            portal_response = self.client.post(
                "/api/billing/portal",
                json=PORTAL,
                headers={"X-CSRF-Token": "csrf-1"},
                base_url=BASE_URL,
            )
            checkout_response = self.client.post(
                "/api/billing/checkout",
                json=CHECKOUT,
                headers={"X-CSRF-Token": "csrf-1"},
                base_url=BASE_URL,
            )

        self.assertEqual(200, status_response.status_code)
        self.assertEqual(200, portal_response.status_code)
        self.assertEqual(403, checkout_response.status_code)
        self.assertEqual("pilot_not_admitted", checkout_response.get_json()["code"])
        status.assert_called_once_with(
            "former-pilot-subject", purchase_authorized=False
        )
        portal.assert_called_once_with(
            "former-pilot-subject", "manage", PORTAL["idempotencyKey"]
        )
        checkout.assert_not_called()

    def test_checkout_strict_keys_reject_server_authority_injection(self):
        injected = {
            "price": "price_attacker",
            "customer": "cus_other",
            "returnUrl": "https://evil.example/paid",
        }
        with patch("sceneit.auth._session_from_cookie", return_value=SESSION), \
                patch("sceneit.billing_routes.create_checkout") as checkout, \
                patch("sceneit.resources.admit_participant"):
            for key, value in injected.items():
                with self.subTest(key=key):
                    response = self.client.post(
                        "/api/billing/checkout",
                        json={**CHECKOUT, key: value},
                        headers={"X-CSRF-Token": "csrf-1"},
                        base_url=BASE_URL,
                    )
                    self.assertEqual(400, response.status_code)
                    self.assertEqual("invalid_request", response.get_json()["code"])

        checkout.assert_not_called()

    def test_forged_redirect_parameters_do_not_create_entitlement(self):
        inactive = {
            "enabled": True,
            "environment": "test",
            "membership": "inactive",
            "paidThrough": None,
            "cancelAtPeriodEnd": False,
            "usage": None,
        }
        with patch("sceneit.auth._session_from_cookie", return_value=SESSION), \
                patch(
                    "sceneit.billing_routes.status", return_value=inactive
                ) as status, \
                patch("sceneit.billing_routes.create_checkout") as checkout:
            response = self.client.get(
                "/api/billing/status"
                "?success=true&customer=cus_forged&subscription=sub_forged",
                base_url=BASE_URL,
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual("inactive", response.get_json()["membership"])
        status.assert_called_once_with(
            "oidc-subject-1", purchase_authorized=True
        )
        checkout.assert_not_called()

    def test_webhook_has_narrow_size_and_signature_boundary(self):
        settings = BillingSettings(enabled=True, environment="test")
        provider = Mock()
        provider.construct_event.return_value = {
            "id": "evt_fixture",
            "type": "irrelevant.fixture",
            "livemode": False,
        }
        provider_type = Mock(return_value=provider)
        five_kib = b'{"padding":"' + (b"x" * 5000) + b'"}'
        signature = f"t={int(time.time())},v1=fixture-signature"

        with patch("sceneit.billing_routes.billing_settings",
                   return_value=settings), \
                patch("sceneit.billing_routes.StripeBillingProvider",
                      provider_type), \
                patch("sceneit.billing_routes.process_event") as process:
            missing = self.client.post(
                "/api/billing/webhook", data=b"{}", content_type="application/json",
                base_url=BASE_URL,
            )
            accepted = self.client.post(
                "/api/billing/webhook", data=five_kib,
                content_type="application/json",
                headers={
                    "Stripe-Signature": signature,
                    "Origin": "https://evil.example",
                },
                base_url=BASE_URL,
            )
            oversized = self.client.post(
                "/api/billing/webhook", data=b"x" * (256 * 1024 + 1),
                content_type="application/json",
                headers={"Stripe-Signature": signature},
                base_url=BASE_URL,
            )

        self.assertEqual(400, missing.status_code)
        self.assertEqual("invalid_webhook", missing.get_json()["code"])
        self.assertEqual(200, accepted.status_code)
        self.assertEqual({"received": True}, accepted.get_json())
        self.assertEqual(413, oversized.status_code)
        self.assertEqual("webhook_too_large", oversized.get_json()["code"])
        process.assert_called_once()

    def test_invalid_signature_and_neighboring_origin_are_rejected(self):
        settings = BillingSettings(enabled=True, environment="test")
        provider = Mock()
        provider.construct_event.side_effect = BillingProviderError(
            "invalid_webhook"
        )
        signature = f"t={int(time.time())},v1=forged"
        with patch("sceneit.billing_routes.billing_settings",
                   return_value=settings), \
                patch("sceneit.billing_routes.StripeBillingProvider",
                      return_value=provider), \
                patch("sceneit.billing_routes.process_event") as process:
            invalid = self.client.post(
                "/api/billing/webhook", data=b"{}",
                content_type="application/json",
                headers={"Stripe-Signature": signature},
                base_url=BASE_URL,
            )
            neighbor = self.client.post(
                "/api/billing/webhook-neighbor", data=b"{}",
                content_type="application/json",
                headers={"Origin": "https://evil.example"},
                base_url=BASE_URL,
            )

        self.assertEqual(400, invalid.status_code)
        self.assertEqual("invalid_webhook", invalid.get_json()["code"])
        self.assertEqual(403, neighbor.status_code)
        self.assertEqual("origin_rejected", neighbor.get_json()["code"])
        process.assert_not_called()

    def test_ordinary_json_ceiling_remains_4096(self):
        with patch("sceneit.auth._session_from_cookie", return_value=SESSION), \
                patch("sceneit.billing_routes.create_portal") as portal, \
                patch("sceneit.resources.admit_participant"):
            response = self.client.post(
                "/api/billing/portal",
                data=b'{"padding":"' + (b"x" * 5000) + b'"}',
                content_type="application/json",
                headers={"X-CSRF-Token": "csrf-1"},
                base_url=BASE_URL,
            )

        self.assertEqual(413, response.status_code)
        self.assertEqual("http_413", response.get_json()["code"])
        portal.assert_not_called()

    def test_disabled_mode_is_readable_but_mutations_stay_503(self):
        with patch("sceneit.auth._session_from_cookie", return_value=SESSION), \
                patch("sceneit.resources.admit_participant"):
            status = self.client.get("/api/billing/status", base_url=BASE_URL)
            checkout = self.client.post(
                "/api/billing/checkout",
                json=CHECKOUT,
                headers={"X-CSRF-Token": "csrf-1"},
                base_url=BASE_URL,
            )
            portal = self.client.post(
                "/api/billing/portal",
                json=PORTAL,
                headers={"X-CSRF-Token": "csrf-1"},
                base_url=BASE_URL,
            )
            webhook = self.client.post(
                "/api/billing/webhook",
                data=b"{}",
                content_type="application/json",
                base_url=BASE_URL,
            )

        self.assertEqual(200, status.status_code)
        self.assertEqual("disabled", status.get_json()["membership"])
        self.assertEqual([503, 503, 503], [
            checkout.status_code, portal.status_code, webhook.status_code
        ])
        self.assertTrue(all(
            response.get_json()["code"] == "billing_disabled"
            for response in (checkout, portal, webhook)
        ))


if __name__ == "__main__":
    unittest.main()