"""Firebase Admin verification and provider-state lifecycle checks."""
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit


FIREBASE_ISSUER_PREFIX = "https://securetoken.google.com/"
REVALIDATE_SECONDS = 60
MAX_AUTH_AGE_SECONDS = 5 * 60
_lock = threading.Lock()
_apps = {}


class FirebaseUnavailable(RuntimeError):
    pass


class FirebaseCredentialRejected(ValueError):
    pass


class FirebaseIdentityInvalid(FirebaseCredentialRejected):
    pass


@dataclass(frozen=True)
class FirebaseConfig:
    project_id: str
    api_key: str
    auth_domain: str
    app_id: str
    service_account_json: str
    trial_hash_secret: str

    @property
    def issuer(self):
        return FIREBASE_ISSUER_PREFIX + self.project_id


def public_trial_enabled(config=None):
    raw = (
        config.get("SCENEIT_PUBLIC_TRIAL_ENABLED")
        if config is not None
        else os.environ.get("SCENEIT_PUBLIC_TRIAL_ENABLED", "false")
    )
    if isinstance(raw, bool):
        return raw
    return str(raw or "false").strip().lower() == "true"


def configured(config=None):
    source = config if config is not None else os.environ
    names = (
        "FIREBASE_PROJECT_ID", "FIREBASE_WEB_API_KEY", "FIREBASE_AUTH_DOMAIN",
        "FIREBASE_WEB_APP_ID", "FIREBASE_SERVICE_ACCOUNT_JSON",
        "FIREBASE_TRIAL_HASH_SECRET",
    )
    if not all(isinstance(source.get(name), str) and source[name] for name in names):
        return False
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    try:
        service_account = json.loads(source["FIREBASE_SERVICE_ACCOUNT_JSON"])
        if not isinstance(service_account, dict):
            return False
        private_key = service_account.get("private_key")
        if not isinstance(private_key, str):
            return False
        if not isinstance(load_pem_private_key(private_key.encode(), password=None), RSAPrivateKey):
            return False
        auth_domain = urlsplit("//" + source["FIREBASE_AUTH_DOMAIN"]).hostname
    except (TypeError, ValueError, UnsupportedAlgorithm):
        return False
    project = source["FIREBASE_PROJECT_ID"]
    emulator = source.get("FIREBASE_AUTH_EMULATOR_HOST")
    testing = bool(source.get("TESTING"))
    return bool(
        isinstance(project, str) and 1 <= len(project) <= 255
        and isinstance(service_account, dict)
        and service_account.get("type") == "service_account"
        and service_account.get("project_id") == project
        and isinstance(service_account.get("client_email"), str)
        and service_account["client_email"].endswith(
            f"@{project}.iam.gserviceaccount.com"
        )
        and isinstance(source["FIREBASE_TRIAL_HASH_SECRET"], str)
        and len(source["FIREBASE_TRIAL_HASH_SECRET"]) >= 32
        and auth_domain == source["FIREBASE_AUTH_DOMAIN"]
        and not (emulator and not testing)
    )


def configuration(config=None):
    source = config if config is not None else os.environ
    if not configured(source):
        raise FirebaseUnavailable("Firebase authentication is not configured")
    secret = source["FIREBASE_TRIAL_HASH_SECRET"]
    if len(secret) < 32:
        raise FirebaseUnavailable("Firebase trial identity configuration is invalid")
    return FirebaseConfig(
        source["FIREBASE_PROJECT_ID"], source["FIREBASE_WEB_API_KEY"],
        source["FIREBASE_AUTH_DOMAIN"], source["FIREBASE_WEB_APP_ID"],
        source["FIREBASE_SERVICE_ACCOUNT_JSON"], secret,
    )


def browser_capabilities(config):
    enabled = public_trial_enabled(config)
    ready = configured(config)
    unavailable = (
        None if enabled and ready
        else "public_trial_disabled" if not enabled
        else "firebase_not_configured"
    )
    firebase = None
    if ready:
        firebase = {
            "apiKey": config["FIREBASE_WEB_API_KEY"],
            "authDomain": config["FIREBASE_AUTH_DOMAIN"],
            "projectId": config["FIREBASE_PROJECT_ID"],
            "appId": config["FIREBASE_WEB_APP_ID"],
        }
    return {
        "replit": True,
        "emailPassword": bool(enabled and ready),
        "publicTrialEnabled": enabled,
        "firebaseConfig": firebase,
        "unavailableReason": unavailable,
    }


def _admin_app(config):
    try:
        import firebase_admin
        from firebase_admin import credentials
    except ImportError as exc:
        raise FirebaseUnavailable("Firebase Admin SDK is unavailable") from exc
    key = config.project_id
    with _lock:
        if key in _apps:
            return _apps[key]
        try:
            payload = json.loads(config.service_account_json)
            app = firebase_admin.initialize_app(
                credentials.Certificate(payload),
                {"projectId": config.project_id},
                name=f"sceneit-{config.project_id}",
            )
        except Exception as exc:
            raise FirebaseUnavailable("Firebase Admin configuration is invalid") from exc
        _apps[key] = app
        return app


def verify_password_token(id_token, config=None, now=None):
    """Verify a fresh password ID token without retaining the raw bearer."""
    if not isinstance(id_token, str) or not 100 <= len(id_token) <= 16_384:
        raise FirebaseCredentialRejected("Credential rejected")
    config = config or configuration()
    now = now or datetime.now(timezone.utc)
    from firebase_admin import auth
    # Local SDK/credential initialization failure is configuration/outage, not
    # evidence that the caller supplied an invalid token.
    app = _admin_app(config)
    try:
        claims = auth.verify_id_token(
            id_token, app=app, check_revoked=True, clock_skew_seconds=30
        )
    except Exception as exc:
        if type(exc).__name__ in {
            "InvalidIdTokenError", "ExpiredIdTokenError", "RevokedIdTokenError",
            "UserDisabledError", "CertificateFetchError",
        }:
            # CertificateFetchError is an upstream outage despite its location
            # in the token verifier.
            if type(exc).__name__ == "CertificateFetchError":
                raise FirebaseUnavailable(
                    "Firebase token certificates are unavailable"
                ) from exc
            raise FirebaseCredentialRejected("Credential rejected") from exc
        raise FirebaseUnavailable("Firebase token verification is unavailable") from exc
    provider = (claims.get("firebase") or {}).get("sign_in_provider")
    auth_time = claims.get("auth_time")
    email = claims.get("email")
    if (
        claims.get("aud") != config.project_id
        or claims.get("iss") != config.issuer
        or provider != "password"
        or not isinstance(auth_time, (int, float))
        or now.timestamp() - auth_time < -30
        or now.timestamp() - auth_time > MAX_AUTH_AGE_SECONDS
        or not isinstance(claims.get("uid") or claims.get("sub"), str)
        or not email
        or not isinstance(claims.get("email_verified"), bool)
    ):
        raise FirebaseCredentialRejected("Credential rejected")
    claims["uid"] = claims.get("uid") or claims["sub"]
    return claims


def provider_user(uid, config=None):
    config = config or configuration()
    try:
        from firebase_admin import auth
        return auth.get_user(uid, app=_admin_app(config))
    except Exception as exc:
        if type(exc).__name__ in {
            "UserNotFoundError", "UserDisabledError", "RevokedIdTokenError"
        }:
            raise FirebaseIdentityInvalid("Firebase identity is invalid") from exc
        raise FirebaseUnavailable("Firebase user state could not be verified") from exc


def provider_state_matches(session, user):
    if getattr(user, "disabled", True):
        return False
    email = getattr(user, "email", None)
    if not email or email != session.get("email"):
        return False
    if bool(getattr(user, "email_verified", False)) != bool(
        session.get("email_verified")
    ):
        return False
    valid_after = getattr(user, "tokens_valid_after_timestamp", None)
    auth_time = session.get("firebase_auth_time")
    if valid_after and auth_time:
        # firebase-admin exposes this as integer milliseconds. Accommodate a
        # datetime too so tests/adapters cannot accidentally weaken comparison.
        if isinstance(valid_after, (int, float)):
            valid_after = datetime.fromtimestamp(valid_after / 1000, timezone.utc)
        elif not isinstance(valid_after, datetime):
            return False
        elif valid_after.tzinfo is None:
            valid_after = valid_after.replace(tzinfo=timezone.utc)
        if auth_time.tzinfo is None:
            auth_time = auth_time.replace(tzinfo=timezone.utc)
        if valid_after > auth_time:
            return False
    return True