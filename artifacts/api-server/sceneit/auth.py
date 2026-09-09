"""Replit OIDC authentication for the Flask API.

Authentication is cookie based.  The cookie contains only a random bearer
value; its hash and all session state are kept in PostgreSQL.
"""
import base64
import hashlib
import logging
import os
import re
import secrets
import ssl
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import unquote, urlencode, urlsplit

import httpx
from flask import Blueprint, abort, current_app, g, jsonify, redirect, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .db import connection

auth_bp = Blueprint("sceneit_auth", __name__)

ISSUER = os.environ.get("ISSUER_URL", "https://replit.com/oidc").rstrip("/")
# Use the runtime's verified system trust store, including Replit's test issuer
# CA. Never disable TLS verification or special-case an untrusted hostname.
TLS_CONTEXT = ssl.create_default_context()
SESSION_COOKIE = "__Host-sceneit_session"
FLOW_COOKIE = "sceneit_oidc"
FIREBASE_CHALLENGE_COOKIE = "__Host-sceneit_firebase_challenge"
SESSION_SECONDS = 7 * 24 * 60 * 60
FLOW_SECONDS = 10 * 60
FIREBASE_CHALLENGE_SECONDS = 10 * 60


def _b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _safe_return_to(value):
    if not value or not isinstance(value, str):
        return "/"
    if len(value) > 2048:
        return "/"
    decoded = value
    while True:
        if re.search(r"%(?![0-9A-Fa-f]{2})", decoded):
            return "/"
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    parsed = urlsplit(decoded)
    if (
        not decoded.startswith("/")
        or decoded.startswith("//")
        or "\\" in decoded
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded)
        or parsed.scheme
        or parsed.netloc
    ):
        return "/"
    return value


def _serializer():
    return URLSafeTimedSerializer(
        current_app.config["SESSION_SECRET"], salt="sceneit-oidc-flow-v1"
    )

def _firebase_serializer():
    return URLSafeTimedSerializer(
        current_app.config["SESSION_SECRET"], salt="sceneit-firebase-challenge-v1"
    )
@lru_cache(maxsize=1)
def _discovery():
    response = httpx.get(
        f"{ISSUER}/.well-known/openid-configuration", timeout=15, verify=TLS_CONTEXT
    )
    response.raise_for_status()
    document = response.json()
    if document.get("issuer", "").rstrip("/") != ISSUER:
        raise RuntimeError("OIDC discovery returned an unexpected issuer")
    return document


def _verified_claims(id_token, nonce):
    """Verify the ID token before returning any of its claims."""
    from authlib.jose import JsonWebToken

    metadata = _discovery()
    response = httpx.get(metadata["jwks_uri"], timeout=15, verify=TLS_CONTEXT)
    response.raise_for_status()
    verifier = JsonWebToken(["RS256", "ES256"])
    claims = verifier.decode(
        id_token,
        response.json(),
        claims_options={
            # Discovery has already been checked against the configured issuer.
            # Preserve its exact identifier, including a significant final slash.
            "iss": {"essential": True, "value": metadata["issuer"]},
            "aud": {"essential": True, "value": os.environ["REPL_ID"]},
            "exp": {"essential": True},
            "iat": {"essential": True},
            "sub": {"essential": True},
            "nonce": {"essential": True, "value": nonce},
        },
    )
    try:
        claims.validate(leeway=30)
    except Exception as exc:
        # Claim names only, never the token, values, callback code, or nonce.
        claim_name = getattr(exc, "claim_name", "")
        safe_claim = claim_name if claim_name in {"iss", "aud", "exp", "iat", "sub", "nonce"} else "other"
        logging.getLogger("sceneit").warning("OIDC token validation failed: %s", safe_claim)
        raise
    client_id = os.environ["REPL_ID"]
    audience = claims.get("aud")
    authorized_party = claims.get("azp")
    if (
        isinstance(audience, (list, tuple))
        and len(audience) > 1
        and authorized_party != client_id
    ) or (authorized_party is not None and authorized_party != client_id):
        raise ValueError("ID token authorized party is invalid")
    subject = claims.get("sub")
    if (
        not isinstance(subject, str)
        or not subject.strip()
        or len(subject) > 255
    ):
        raise ValueError("ID token subject is invalid")
    return claims


def _session_from_cookie():
    token = request.cookies.get(SESSION_COOKIE)
    if (
        not token
        or len(token) > 128
        or len(token) < 20
        or re.fullmatch(r"[A-Za-z0-9_-]+", token) is None
    ):
        return None
    with connection() as conn:
        row = conn.execute(
            "SELECT s.id,s.csrf_token,u.id AS user_id,u.first_name,u.provider,"
            "COALESCE(s.firebase_email,u.email) AS email,"
            "COALESCE(s.firebase_email_verified,u.email_verified) AS email_verified,"
            "s.firebase_identity_id,"
            "s.firebase_auth_time,s.firebase_validated_at,i.firebase_uid,"
            "i.project_id AS firebase_project_id,i.issuer AS firebase_issuer "
            "FROM sceneit_auth_sessions s "
            "JOIN sceneit_auth_users u ON u.id = s.user_id "
            "LEFT JOIN sceneit_firebase_identities i "
            "ON i.id=s.firebase_identity_id "
            "WHERE s.id = %s AND s.expires_at > now()",
            (_digest(token),),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE sceneit_auth_sessions SET last_seen_at = now() WHERE id = %s",
                (row["id"],),
            )
    return row

def _invalidate_firebase_sessions(identity_id):
    with connection() as conn:
        conn.execute(
            "DELETE FROM sceneit_auth_sessions WHERE firebase_identity_id=%s",
            (identity_id,),
        )
def require_owner():
    """Return the verified session owner, or reject the request."""
    session = getattr(g, "auth_session", None)
    if not session:
        abort(401, description="Authentication required.")
    if session.get("provider", "replit") == "firebase" and not getattr(
        g, "firebase_state_valid", False
    ):
        abort(401, description="Authentication could not be verified.")
    return session["user_id"]


def require_csrf():
    """Enforce the session-bound synchronizer token on a state-changing route."""
    require_owner()
    supplied = request.headers.get("X-CSRF-Token", "")
    expected = g.auth_session["csrf_token"]
    if not supplied or not secrets.compare_digest(supplied, expected):
        abort(403, description="Invalid CSRF token.")
    origin = request.headers.get("Origin")
    if origin and urlsplit(origin).netloc != request.host:
        abort(403, description="Cross-site request rejected.")

def require_new_private_work():
    """Fail closed before a new Firebase reservation/search is purchased.

    Replay/read/completion/cleanup paths must not call this helper.
    """
    from .firebase_auth import configured, public_trial_enabled
    from .import_limits import ImportProblem

    from flask import has_request_context
    if not has_request_context():
        return None
    owner = require_owner()
    session = g.auth_session
    if session.get("provider", "replit") != "firebase":
        return owner
    if not session.get("email_verified"):
        raise ImportProblem(
            "verification_required",
            "Verify this email before starting processing.", 403,
        )
    if not public_trial_enabled(current_app.config):
        raise ImportProblem(
            "public_trial_disabled", "Public trial processing is not enabled.", 403
        )
    if not configured(current_app.config):
        raise ImportProblem(
            "firebase_not_configured",
            "Email processing is not configured.", 503,
        )
    # Resolving the ledger proves the Firebase owner has a complete durable
    # mapping. Never fall back to a fresh owner-scoped quota.
    from .trial_identity import usage_owner
    with connection() as conn:
        usage_owner(conn, owner)
    return owner
@auth_bp.get("/api/auth/session")
@auth_bp.get("/api/auth/user")
def current_user():
    from .firebase_auth import browser_capabilities, public_trial_enabled

    session = getattr(g, "auth_session", None)
    capabilities = browser_capabilities(current_app.config)
    if not session:
        return jsonify(
            user=None, csrfToken=None, pilotAdmitted=False,
            capabilities=capabilities,
            privateAccess={
                "allowed": False, "reason": "authentication_required",
            },
            usage=None,
        )
    provider = session.get("provider", "replit")
    firebase_valid = provider != "firebase" or getattr(
        g, "firebase_state_valid", False
    )
    verified = bool(session.get("email_verified")) if provider == "firebase" else True
    if not firebase_valid:
        reason = "identity_unavailable"
    elif provider == "firebase" and not verified:
        reason = "verification_required"
    elif provider == "firebase" and not public_trial_enabled(current_app.config):
        reason = "public_trial_disabled"
    elif provider == "firebase" and not capabilities["firebaseConfig"]:
        reason = "firebase_not_configured"
    elif provider == "replit" and not getattr(g, "pilot_admitted", False):
        reason = "pilot_not_admitted"
    else:
        reason = "ready"
    usage = None
    if firebase_valid and verified and reason in {
        "ready", "public_trial_disabled"
    }:
        from .import_limits import usage_snapshot
        usage = usage_snapshot(session["user_id"])
    return jsonify(
        user={
            "id": session["user_id"], "firstName": session["first_name"],
            "provider": provider, "email": session.get("email"),
            "emailVerified": verified,
        },
        csrfToken=session["csrf_token"],
        pilotAdmitted=bool(getattr(g, "pilot_admitted", False)),
        capabilities=capabilities,
        privateAccess={"allowed": reason == "ready", "reason": reason},
        usage=usage,
    )

@auth_bp.get("/api/auth/firebase/challenge")
def firebase_challenge():
    from .firebase_auth import configured, public_trial_enabled
    from .http import problem_response
    if not public_trial_enabled(current_app.config):
        return problem_response(
            "Public trial authentication is not enabled.",
            "public_trial_disabled", 403,
        )
    if not configured(current_app.config):
        return problem_response(
            "Email authentication is not configured.",
            "firebase_not_configured", 503,
        )
    csrf = secrets.token_urlsafe(32)
    challenge = _firebase_serializer().dumps({"csrf": csrf})
    response = jsonify(csrfToken=csrf)
    response.set_cookie(
        FIREBASE_CHALLENGE_COOKIE, challenge,
        max_age=FIREBASE_CHALLENGE_SECONDS, secure=True, httponly=True,
        samesite="Strict", path="/",
    )
    return response
@auth_bp.get("/api/login")
def login():
    metadata = _discovery()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    callback = request.url_root.rstrip("/") + "/api/callback"
    flow = _serializer().dumps(
        {
            "state": state,
            "nonce": nonce,
            "verifier": verifier,
            "returnTo": _safe_return_to(request.args.get("returnTo")),
            "callback": callback,
        }
    )
    query = urlencode(
        {
            "client_id": os.environ["REPL_ID"],
            "redirect_uri": callback,
            "response_type": "code",
            "scope": "openid profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    response = redirect(f"{metadata['authorization_endpoint']}?{query}")
    response.set_cookie(
        FLOW_COOKIE, flow, max_age=FLOW_SECONDS, secure=True, httponly=True,
        samesite="Lax", path="/api/callback",
    )
    return response


@auth_bp.get("/api/callback")
def callback():
    try:
        flow = _serializer().loads(
            request.cookies.get(FLOW_COOKIE, ""), max_age=FLOW_SECONDS
        )
    except (BadSignature, SignatureExpired):
        abort(400, description="The login request expired.")
    if request.args.get("state") != flow["state"] or not request.args.get("code"):
        abort(400, description="Invalid login callback.")
    metadata = _discovery()
    token_response = httpx.post(
        metadata["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "client_id": os.environ["REPL_ID"],
            "code": request.args["code"],
            "redirect_uri": flow["callback"],
            "code_verifier": flow["verifier"],
        },
        timeout=15,
        verify=TLS_CONTEXT,
    )
    token_response.raise_for_status()
    claims = _verified_claims(token_response.json()["id_token"], flow["nonce"])
    user_id = str(claims["sub"])
    first_name = (
        claims.get("first_name")
        or claims.get("given_name")
        or str(claims.get("name") or "User").split()[0]
    )
    raw_session = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    with connection() as conn:
        collision = conn.execute(
            "SELECT provider FROM sceneit_auth_users WHERE id=%s", (user_id,)
        ).fetchone()
        if collision and collision["provider"] != "replit":
            abort(409, description="This identity cannot be used.")
        inserted = conn.execute(
            "INSERT INTO sceneit_auth_users (id, first_name, provider, updated_at) "
            "VALUES (%s, %s, 'replit', now()) ON CONFLICT (id) DO UPDATE SET "
            "first_name = EXCLUDED.first_name, updated_at = now() "
            "WHERE sceneit_auth_users.provider='replit' RETURNING id",
            (user_id, first_name),
        ).fetchone()
        if not inserted:
            abort(409, description="This identity cannot be used.")
        conn.execute(
            "INSERT INTO sceneit_auth_sessions "
            "(id, user_id, csrf_token, expires_at) "
            "VALUES (%s, %s, %s, now() + make_interval(secs => %s))",
            (_digest(raw_session), user_id, csrf, SESSION_SECONDS),
        )
    response = redirect(_safe_return_to(flow.get("returnTo")))
    response.delete_cookie(FLOW_COOKIE, path="/api/callback")
    response.set_cookie(
        SESSION_COOKIE, raw_session, max_age=SESSION_SECONDS, secure=True,
        httponly=True, samesite="Lax", path="/",
    )
    return response


@auth_bp.post("/api/logout")
def logout():
    if getattr(g, "auth_session", None):
        if getattr(g, "firebase_state_valid", True):
            require_csrf()
        with connection() as conn:
            conn.execute(
                "DELETE FROM sceneit_auth_sessions WHERE id = %s",
                (g.auth_session["id"],),
            )
    response = jsonify(success=True)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


def init_auth(app):
    """Install auth state loading and routes on a Flask application."""
    secret = app.config.get("SESSION_SECRET") or os.environ.get("SESSION_SECRET")
    if not secret or len(secret) < 32:
        raise RuntimeError("SESSION_SECRET must contain at least 32 characters")
    app.config["SESSION_SECRET"] = secret

    @app.before_request
    def load_auth_session():
        # Health, login and callback are public and never pay for a session lookup.
        needs_session = (
            request.path == "/api/auth/user"
            or request.path == "/api/auth/session"
            or request.path == "/api/logout"
            or request.path.startswith("/api/auth/firebase")
            or request.path.startswith(("/api/proof", "/api/imports"))
            or (
                request.path.startswith("/api/billing")
                and request.path != "/api/billing/webhook"
            )
        )
        g.auth_session = _session_from_cookie() if needs_session else None
        g.firebase_state_valid = revalidate_firebase_session(g.auth_session)

    app.register_blueprint(auth_bp)

def _exchange_throttle(value, action):
    import hmac
    from .resources import admit_participant
    key = hmac.new(
        current_app.config["SESSION_SECRET"].encode(),
        value.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    admit_participant(f"firebase:{key}", action=action, limit=10, window_seconds=60)

def revalidate_firebase_session(session, *, force=False):
    """Recheck provider state at most once per bounded trust window."""
    if not session or session.get("provider", "replit") != "firebase":
        return True
    validated = session.get("firebase_validated_at")
    now = datetime.now(timezone.utc)
    from .firebase_auth import (
        FirebaseIdentityInvalid, FirebaseUnavailable, configuration,
        provider_state_matches, provider_user,
    )
    try:
        config = configuration(current_app.config)
        if (
            session.get("firebase_project_id") != config.project_id
            or session.get("firebase_issuer") != config.issuer
        ):
            _invalidate_firebase_sessions(session["firebase_identity_id"])
            return False
        if (
            not force and validated and
            (now - validated).total_seconds() <= 60
        ):
            return True
        user = provider_user(session["firebase_uid"], config)
    except FirebaseIdentityInvalid:
        _invalidate_firebase_sessions(session["firebase_identity_id"])
        return False
    except FirebaseUnavailable:
        # Uncertainty is a denial, never an extension of provider trust.
        return False
    if not provider_state_matches(session, user):
        _invalidate_firebase_sessions(session["firebase_identity_id"])
        return False
    with connection() as conn:
        conn.execute(
            "UPDATE sceneit_auth_sessions SET firebase_validated_at=now() "
            "WHERE id=%s", (session["id"],)
        )
    session["firebase_validated_at"] = now
    return True

@auth_bp.post("/api/auth/firebase/session")
def firebase_session():
    from .firebase_auth import (
        FirebaseCredentialRejected, FirebaseUnavailable, configuration, configured,
        provider_state_matches, provider_user, public_trial_enabled,
        verify_password_token,
    )
    from .http import problem_response
    from .trial_identity import email_fingerprint, trial_ledger_id

    if not public_trial_enabled(current_app.config):
        return problem_response(
            "Public trial authentication is not enabled.",
            "public_trial_disabled", 403,
        )
    if not configured(current_app.config):
        return problem_response(
            "Email authentication is not configured.",
            "firebase_not_configured", 503,
        )
    try:
        challenge = _firebase_serializer().loads(
            request.cookies.get(FIREBASE_CHALLENGE_COOKIE, ""),
            max_age=FIREBASE_CHALLENGE_SECONDS,
        )
    except (BadSignature, SignatureExpired):
        abort(403, description="Invalid login request.")
    supplied = request.headers.get("X-CSRF-Token", "")
    if not supplied or not secrets.compare_digest(supplied, challenge.get("csrf", "")):
        abort(403, description="Invalid login request.")
    origin = request.headers.get("Origin")
    if origin and urlsplit(origin).netloc != request.host:
        abort(403, description="Invalid login request.")
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) != {"idToken"}:
        abort(400, description="Invalid login request.")
    _exchange_throttle(request.remote_addr or "unknown", "firebase_exchange_ip")
    config = configuration(current_app.config)
    try:
        claims = verify_password_token(body["idToken"], config)
        uid = claims["uid"]
        _exchange_throttle(uid, "firebase_exchange_subject")
        provider_record = provider_user(uid, config)
        snapshot = {
            "email": claims["email"],
            "email_verified": claims["email_verified"],
            "firebase_auth_time": datetime.fromtimestamp(
                claims["auth_time"], timezone.utc
            ),
        }
        if not provider_state_matches(snapshot, provider_record):
            raise FirebaseCredentialRejected("Credential rejected")
    except FirebaseCredentialRejected:
        return problem_response(
            "The credential could not be accepted.",
            "firebase_credential_invalid", 401,
        )
    except FirebaseUnavailable:
        return problem_response(
            "Email authentication is temporarily unavailable.",
            "firebase_unavailable", 503, retry_after=10,
        )

    verified = bool(claims["email_verified"])
    ledger = (
        trial_ledger_id(claims["email"], config.trial_hash_secret)
        if verified else None
    )
    fingerprint = email_fingerprint(claims["email"]) if verified else None
    first_name = claims["email"].split("@", 1)[0][:100] or "User"
    identity_id = uuid.uuid4()
    owner_id = f"firebase:{uuid.uuid4()}"
    raw_session = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    with connection() as conn, conn.transaction():
        identity_lock = f"sceneit-firebase:{config.project_id}:{config.issuer}:{uid}"
        # Own an explicit transaction even for autocommit connection adapters.
        # Keep the lock through commit without leaking session-scoped locks to
        # pooled connections or exposing an uncommitted identity to a waiter.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (identity_lock,))
        identity = conn.execute(
            "SELECT id,owner_id,trial_ledger_id,email_hash "
            "FROM sceneit_firebase_identities "
            "WHERE project_id=%s AND issuer=%s AND firebase_uid=%s FOR UPDATE",
            (config.project_id, config.issuer, uid),
        ).fetchone()
        if identity:
            identity_id, owner_id = identity["id"], identity["owner_id"]
            if verified and identity["trial_ledger_id"]:
                ledger = identity["trial_ledger_id"]
            existing_user = conn.execute(
                "SELECT email,email_verified FROM sceneit_auth_users "
                "WHERE id=%s FOR UPDATE", (owner_id,)
            ).fetchone()
            if (
                existing_user
                and existing_user["email"] != claims["email"]
            ) or (
                verified and identity["email_hash"]
                and identity["email_hash"] != fingerprint
            ):
                conn.execute(
                    "DELETE FROM sceneit_auth_sessions WHERE firebase_identity_id=%s",
                    (identity_id,),
                )
                return problem_response(
                    "This account identity changed and must be recovered.",
                    "firebase_identity_changed", 409,
                )
            if (
                existing_user
                and bool(existing_user["email_verified"]) != verified
            ):
                conn.execute(
                    "DELETE FROM sceneit_auth_sessions WHERE firebase_identity_id=%s",
                    (identity_id,),
                )
        else:
            conn.execute(
                "INSERT INTO sceneit_auth_users"
                "(id,first_name,provider,email,email_verified,updated_at) "
                "VALUES (%s,%s,'firebase',%s,%s,now())",
                (owner_id, first_name, claims["email"], verified),
            )
            conn.execute(
                "INSERT INTO sceneit_firebase_identities"
                "(id,project_id,issuer,firebase_uid,owner_id) "
                "VALUES (%s,%s,%s,%s,%s)",
                (identity_id, config.project_id, config.issuer, uid, owner_id),
            )
        if ledger:
            conn.execute(
                "INSERT INTO sceneit_firebase_trial_ledgers(id) VALUES (%s) "
                "ON CONFLICT (id) DO NOTHING", (ledger,)
            )
        conn.execute(
            "UPDATE sceneit_firebase_identities SET "
            "trial_ledger_id=COALESCE(trial_ledger_id,%s),"
            "email_hash=COALESCE(email_hash,%s),updated_at=now() WHERE id=%s",
            (ledger, fingerprint, identity_id),
        )
        conn.execute(
            "UPDATE sceneit_auth_users SET first_name=%s,email=%s,"
            "email_verified=%s,updated_at=now() WHERE id=%s AND provider='firebase'",
            (first_name, claims["email"], verified, owner_id),
        )
        conn.execute(
            "INSERT INTO sceneit_auth_sessions"
            "(id,user_id,csrf_token,expires_at,firebase_identity_id,"
            "firebase_auth_time,firebase_validated_at,firebase_email,"
            "firebase_email_verified) "
            "VALUES (%s,%s,%s,now()+make_interval(secs=>%s),%s,%s,now(),%s,%s)",
            (
                _digest(raw_session), owner_id, csrf, SESSION_SECONDS,
                identity_id, snapshot["firebase_auth_time"],
                claims["email"], verified,
            ),
        )
    g.auth_session = {
        "id": _digest(raw_session), "user_id": owner_id,
        "first_name": first_name, "provider": "firebase",
        "email": claims["email"], "email_verified": verified,
        "csrf_token": csrf, "firebase_identity_id": identity_id,
        "firebase_auth_time": snapshot["firebase_auth_time"],
        "firebase_validated_at": datetime.now(timezone.utc),
        "firebase_uid": uid, "firebase_project_id": config.project_id,
        "firebase_issuer": config.issuer,
    }
    g.firebase_state_valid = True
    g.pilot_admitted = False
    response = current_user()
    response.delete_cookie(
        FIREBASE_CHALLENGE_COOKIE, path="/"
    )
    response.set_cookie(
        SESSION_COOKIE, raw_session, max_age=SESSION_SECONDS, secure=True,
        httponly=True, samesite="Lax", path="/",
    )
    return response
