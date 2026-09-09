"""Provider-free Firebase authentication and trial identity boundaries."""
import json
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from sceneit.auth import (
    FIREBASE_CHALLENGE_COOKIE, auth_bp, revalidate_firebase_session,
)
from sceneit.firebase_auth import (
    FirebaseCredentialRejected, FirebaseUnavailable, browser_capabilities, configured,
    provider_state_matches, verify_password_token,
)
from sceneit.trial_identity import (
    normalize_verified_email, trial_ledger_id, usage_owner,
)

_FIXTURE_PRIVATE_KEY = rsa.generate_private_key(
    public_exponent=65537, key_size=2048
).private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()


def firebase_config(**changes):
    result = {
        "TESTING": True,
        "FIREBASE_PROJECT_ID": "sceneit-test",
        "FIREBASE_WEB_API_KEY": "public-api-key",
        "FIREBASE_AUTH_DOMAIN": "sceneit-test.firebaseapp.com",
        "FIREBASE_WEB_APP_ID": "1:123:web:abc",
        "FIREBASE_SERVICE_ACCOUNT_JSON": json.dumps({
            "type": "service_account", "project_id": "sceneit-test",
            "private_key_id": "fixture", "private_key": _FIXTURE_PRIVATE_KEY,
            "client_email": "fixture@sceneit-test.iam.gserviceaccount.com",
            "token_uri": "https://oauth2.googleapis.com/token",
        }),
        "FIREBASE_TRIAL_HASH_SECRET": "immutable-fixture-secret-" + "x" * 32,
        "SCENEIT_PUBLIC_TRIAL_ENABLED": True,
    }
    result.update(changes)
    return result


class FirebaseConfigurationTests(unittest.TestCase):
    def test_complete_configuration_and_default_off_capability(self):
        complete = firebase_config()
        self.assertTrue(configured(complete))
        self.assertTrue(browser_capabilities(complete)["emailPassword"])
        disabled = {**complete, "SCENEIT_PUBLIC_TRIAL_ENABLED": False}
        self.assertFalse(browser_capabilities(disabled)["emailPassword"])
        self.assertEqual(
            "public_trial_disabled",
            browser_capabilities(disabled)["unavailableReason"],
        )
        for changes in (
            {"FIREBASE_TRIAL_HASH_SECRET": "short"},
            {"FIREBASE_SERVICE_ACCOUNT_JSON": "{}"},
            {"FIREBASE_SERVICE_ACCOUNT_JSON": "[]"},
            {"FIREBASE_SERVICE_ACCOUNT_JSON": "null"},
            {"FIREBASE_SERVICE_ACCOUNT_JSON": '{"private_key":null}'},
            {"FIREBASE_SERVICE_ACCOUNT_JSON": '{"private_key":123}'},
            {"FIREBASE_AUTH_DOMAIN": 123},
            {"FIREBASE_AUTH_EMULATOR_HOST": "localhost:9099", "TESTING": False},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(configured({**complete, **changes}))
                capabilities = browser_capabilities({
                    **complete, **changes, "SCENEIT_PUBLIC_TRIAL_ENABLED": False,
                })
                self.assertTrue(capabilities["replit"])
                self.assertFalse(capabilities["emailPassword"])

    def test_challenge_cookie_is_host_cookie_and_explicit_failures(self):
        app = Flask(__name__)
        app.config.update(firebase_config(), SESSION_SECRET="s" * 48)
        app.register_blueprint(auth_bp)
        client = app.test_client()
        response = client.get(
            "/api/auth/firebase/challenge", base_url="https://sceneit.example"
        )
        self.assertEqual(200, response.status_code)
        cookie = response.headers["Set-Cookie"]
        self.assertIn(FIREBASE_CHALLENGE_COOKIE + "=", cookie)
        self.assertIn("Path=/", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

        app.config["SCENEIT_PUBLIC_TRIAL_ENABLED"] = False
        denied = client.get(
            "/api/auth/firebase/challenge", base_url="https://sceneit.example"
        )
        self.assertEqual(403, denied.status_code)
        self.assertEqual("public_trial_disabled", denied.get_json()["code"])

    def test_session_exchange_requires_bound_challenge_and_types_failures(self):
        app = Flask(__name__)
        app.config.update(firebase_config(), SESSION_SECRET="s" * 48)
        app.register_blueprint(auth_bp)
        client = app.test_client()
        challenge = client.get(
            "/api/auth/firebase/challenge", base_url="https://sceneit.example"
        ).get_json()["csrfToken"]
        with patch("sceneit.auth._exchange_throttle"):
            missing = client.post(
                "/api/auth/firebase/session", json={"idToken": "x" * 200},
                base_url="https://sceneit.example",
            )
            self.assertEqual(403, missing.status_code)
            with patch(
                "sceneit.auth.verify_password_token",
                side_effect=FirebaseCredentialRejected("no"),
                create=True,
            ):
                # The function is imported locally, so patch its defining module.
                pass
            with patch(
                "sceneit.firebase_auth.verify_password_token",
                side_effect=FirebaseCredentialRejected("no"),
            ):
                invalid = client.post(
                    "/api/auth/firebase/session", json={"idToken": "x" * 200},
                    headers={"X-CSRF-Token": challenge},
                    base_url="https://sceneit.example",
                )
        self.assertEqual(401, invalid.status_code)
        self.assertEqual("firebase_credential_invalid", invalid.get_json()["code"])


class FirebaseCredentialTests(unittest.TestCase):
    def test_real_signed_firebase_jwt_with_provider_free_certificate_transport(self):
        """Exercise Google's Firebase JWT verifier with an in-memory CA response."""
        from authlib.jose import JsonWebToken
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        from google.oauth2 import id_token as google_id_token

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture")])
        now = datetime.now(timezone.utc)
        certificate = (
            x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now.replace(tzinfo=None))
            .not_valid_after(datetime.fromtimestamp(now.timestamp() + 3600))
            .sign(key, hashes.SHA256())
        )
        pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
        claims = {
            "iss": "https://securetoken.google.com/sceneit-test",
            "aud": "sceneit-test", "sub": "signed-user",
            "iat": int(now.timestamp()), "exp": int(now.timestamp()) + 300,
            "auth_time": int(now.timestamp()), "email": "p@example.com",
            "email_verified": True,
            "firebase": {"sign_in_provider": "password"},
        }
        encoded = JsonWebToken(["RS256"]).encode(
            {"alg": "RS256", "kid": "fixture-key"}, claims, key
        )

        class Response:
            status = 200
            data = json.dumps({"fixture-key": pem}).encode()

        class Request:
            def __call__(self, *_args, **_kwargs):
                return Response()

        verified = google_id_token.verify_firebase_token(
            encoded.decode(), Request(), audience="sceneit-test"
        )
        self.assertEqual("signed-user", verified["sub"])
        forged = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        forged_token = JsonWebToken(["RS256"]).encode(
            {"alg": "RS256", "kid": "fixture-key"}, claims, forged
        )
        with self.assertRaises(Exception):
            google_id_token.verify_firebase_token(
                forged_token.decode(), Request(), audience="sceneit-test"
            )

    def test_admin_claims_enforce_project_password_freshness_and_email(self):
        config = firebase_config()
        now = datetime.now(timezone.utc)
        valid = {
            "aud": "sceneit-test",
            "iss": "https://securetoken.google.com/sceneit-test",
            "sub": "firebase-user",
            "uid": "firebase-user",
            "email": "Person@Example.com",
            "email_verified": True,
            "auth_time": int(now.timestamp()),
            "firebase": {"sign_in_provider": "password"},
        }
        with patch("sceneit.firebase_auth._admin_app", return_value=object()), \
                patch("firebase_admin.auth.verify_id_token", return_value=valid):
            self.assertEqual(
                "firebase-user",
                verify_password_token("x" * 200, configuration_object(config),
                                      now=now)["uid"],
            )
        invalid = (
            {**valid, "aud": "other"},
            {**valid, "iss": "https://securetoken.google.com/other"},
            {**valid, "email_verified": "true"},
            {**valid, "auth_time": int(now.timestamp()) - 301},
            {**valid, "firebase": {"sign_in_provider": "google.com"}},
        )
        for claims in invalid:
            with self.subTest(claims=claims), \
                    patch("sceneit.firebase_auth._admin_app", return_value=object()), \
                    patch("firebase_admin.auth.verify_id_token",
                          return_value=claims), \
                    self.assertRaises(FirebaseCredentialRejected):
                verify_password_token(
                    "x" * 200, configuration_object(config), now=now
                )

    def test_sdk_millisecond_revocation_and_material_state_changes(self):
        auth_time = datetime.fromtimestamp(time.time() - 10, timezone.utc)
        session = {
            "email": "person@example.com", "email_verified": True,
            "firebase_auth_time": auth_time,
        }
        valid = SimpleNamespace(
            disabled=False, email="person@example.com", email_verified=True,
            tokens_valid_after_timestamp=int((auth_time.timestamp() - 1) * 1000),
        )
        self.assertTrue(provider_state_matches(session, valid))
        valid.tokens_valid_after_timestamp = int(
            (auth_time.timestamp() + 1) * 1000
        )
        self.assertFalse(provider_state_matches(session, valid))
        for change in (
            {"disabled": True},
            {"email": "changed@example.com"},
            {"email_verified": False},
        ):
            user = SimpleNamespace(**{
                "disabled": False, "email": "person@example.com",
                "email_verified": True,
                "tokens_valid_after_timestamp": 0, **change,
            })
            self.assertFalse(provider_state_matches(session, user))

    def test_revalidation_deletes_changed_sessions_but_not_provider_outage(self):
        app = Flask(__name__)
        app.config.update(firebase_config(), SESSION_SECRET="s" * 48)
        session = {
            "id": "digest", "provider": "firebase",
            "firebase_identity_id": "identity", "firebase_uid": "uid",
            "firebase_validated_at": None, "email": "person@example.com",
            "email_verified": True,
            "firebase_auth_time": datetime.now(timezone.utc),
            "firebase_project_id": "sceneit-test",
            "firebase_issuer": "https://securetoken.google.com/sceneit-test",
        }
        statements = []

        class Conn:
            def execute(self, statement, _params):
                statements.append(statement)
                return self

        @contextmanager
        def database():
            yield Conn()

        changed = SimpleNamespace(
            disabled=True, email="person@example.com", email_verified=True,
            tokens_valid_after_timestamp=0,
        )
        with app.test_request_context("/api/imports"), \
                patch("sceneit.auth.connection", database), \
                patch("sceneit.firebase_auth.provider_user", return_value=changed):
            self.assertFalse(revalidate_firebase_session(session, force=True))
        self.assertTrue(any("DELETE FROM sceneit_auth_sessions" in s
                            for s in statements))

        statements.clear()
        with app.test_request_context("/api/imports"), \
                patch("sceneit.auth.connection", database), \
                patch(
                    "sceneit.firebase_auth.provider_user",
                    side_effect=FirebaseUnavailable("offline"),
                ):
            self.assertFalse(revalidate_firebase_session(session, force=True))
        self.assertFalse(any("DELETE FROM sceneit_auth_sessions" in s
                             for s in statements))

    def test_revalidation_cache_is_bounded_and_never_crosses_project_switch(self):
        app = Flask(__name__)
        app.config.update(firebase_config(), SESSION_SECRET="s" * 48)
        base = {
            "id": "digest", "provider": "firebase",
            "firebase_identity_id": "identity", "firebase_uid": "uid",
            "email": "person@example.com", "email_verified": True,
            "firebase_auth_time": datetime.now(timezone.utc),
            "firebase_project_id": "sceneit-test",
            "firebase_issuer": "https://securetoken.google.com/sceneit-test",
        }
        statements = []

        class Conn:
            def execute(self, statement, _params):
                statements.append(statement)
                return self

        @contextmanager
        def database():
            yield Conn()

        with app.test_request_context("/api/imports"), \
                patch("sceneit.auth.connection", database), \
                patch("sceneit.firebase_auth.provider_user") as provider:
            cached = {
                **base, "firebase_validated_at": datetime.now(timezone.utc)
            }
            self.assertTrue(revalidate_firebase_session(cached))
            provider.assert_not_called()

            switched = {**cached, "firebase_project_id": "other-project"}
            self.assertFalse(revalidate_firebase_session(switched))
            provider.assert_not_called()
        self.assertTrue(any("DELETE FROM sceneit_auth_sessions" in s
                            for s in statements))

        valid_user = SimpleNamespace(
            disabled=False, email="person@example.com", email_verified=True,
            tokens_valid_after_timestamp=0,
        )
        stale = {
            **base,
            "firebase_validated_at": datetime.fromtimestamp(
                time.time() - 61, timezone.utc
            ),
        }
        with app.test_request_context("/api/imports"), \
                patch("sceneit.auth.connection", database), \
                patch("sceneit.firebase_auth.provider_user",
                      return_value=valid_user) as provider:
            self.assertTrue(revalidate_firebase_session(stale))
            provider.assert_called_once()


class TrialIdentityTests(unittest.TestCase):
    def test_email_ledger_is_normalized_stable_and_secret_specific(self):
        self.assertEqual("person@example.com",
                         normalize_verified_email(" Person@EXAMPLE.com "))
        first = trial_ledger_id("Person@example.com", "a" * 32)
        self.assertEqual(first, trial_ledger_id("person@EXAMPLE.COM", "a" * 32))
        self.assertNotEqual(first, trial_ledger_id("person@example.com", "b" * 32))
        self.assertNotIn("person", first)

    def test_missing_firebase_mapping_never_falls_back_to_fresh_owner(self):
        class Conn:
            def __init__(self, row):
                self.row = row

            def execute(self, *_args):
                return self

            def fetchone(self):
                return self.row

        self.assertEqual("legacy", usage_owner(Conn(None), "legacy"))
        self.assertEqual(
            "legacy", usage_owner(
                Conn({"provider": "replit", "trial_ledger_id": None}), "legacy"
            )
        )
        with self.assertRaises(RuntimeError):
            usage_owner(
                Conn({"provider": "firebase", "trial_ledger_id": None}),
                "firebase:owner",
            )


def configuration_object(config):
    from sceneit.firebase_auth import configuration
    return configuration(config)


if __name__ == "__main__":
    unittest.main()