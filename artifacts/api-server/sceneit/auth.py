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
SESSION_SECONDS = 7 * 24 * 60 * 60
FLOW_SECONDS = 10 * 60


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
            "SELECT s.id, s.csrf_token, u.id AS user_id, u.first_name "
            "FROM sceneit_auth_sessions s "
            "JOIN sceneit_auth_users u ON u.id = s.user_id "
            "WHERE s.id = %s AND s.expires_at > now()",
            (_digest(token),),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE sceneit_auth_sessions SET last_seen_at = now() WHERE id = %s",
                (row["id"],),
            )
    return row


def require_owner():
    """Return the verified session owner, or reject the request."""
    session = getattr(g, "auth_session", None)
    if not session:
        abort(401, description="Authentication required.")
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


@auth_bp.get("/api/auth/session")
@auth_bp.get("/api/auth/user")
def current_user():
    session = getattr(g, "auth_session", None)
    if not session:
        return jsonify(user=None, csrfToken=None, pilotAdmitted=False)
    return jsonify(
        user={"id": session["user_id"], "firstName": session["first_name"]},
        csrfToken=session["csrf_token"],
        pilotAdmitted=bool(getattr(g, "pilot_admitted", False)),
    )


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
        conn.execute(
            "INSERT INTO sceneit_auth_users (id, first_name, updated_at) "
            "VALUES (%s, %s, now()) ON CONFLICT (id) DO UPDATE SET "
            "first_name = EXCLUDED.first_name, updated_at = now()",
            (user_id, first_name),
        )
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
            or request.path.startswith(("/api/proof", "/api/imports"))
            or (
                request.path.startswith("/api/billing")
                and request.path != "/api/billing/webhook"
            )
        )
        g.auth_session = _session_from_cookie() if needs_session else None

    app.register_blueprint(auth_bp)
