"""HTTP admission, CSRF-adjacent origin checks, and response hardening."""
import secrets
from urllib.parse import urlsplit

from flask import g, jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix


PROOF_PREFIX = "/api/proof"
IMPORTS_PREFIX = "/api/imports"
PROTECTED_PREFIXES = (PROOF_PREFIX, IMPORTS_PREFIX)
PRIVATE_PREFIXES = PROTECTED_PREFIXES + (
    "/api/auth", "/api/login", "/api/callback", "/api/logout", "/api/billing",
)


def parse_allowed_subjects(value):
    """Parse exact, case-sensitive OIDC subjects; empty input denies everyone."""
    if value is None:
        return frozenset()
    if not isinstance(value, str):
        raise ValueError("PILOT_ALLOWED_SUBJECTS must be comma separated")
    subjects = [subject.strip() for subject in value.split(",")]
    if any(not subject or len(subject) > 255 for subject in subjects):
        if value.strip():
            raise ValueError("PILOT_ALLOWED_SUBJECTS contains an invalid subject")
        return frozenset()
    return frozenset(subjects)


def parse_trusted_hosts(value):
    if isinstance(value, (list, tuple)):
        hosts = [str(host).strip() for host in value]
    else:
        hosts = [host.strip() for host in str(value or "").split(",")]
    hosts = [host for host in hosts if host]
    if any("/" in host or "://" in host or len(host) > 253 for host in hosts):
        raise ValueError("TRUSTED_HOSTS contains an invalid host")
    return hosts


def is_pilot_admitted(session, allowed_subjects):
    return bool(
        session
        and session.get("provider", "replit") == "replit"
        and session.get("user_id") in allowed_subjects
        and isinstance(session.get("user_id"), str)
    )


def require_pilot():
    """Require a verified server-side session and exact pilot admission."""
    from .auth import require_owner

    require_owner()
    if not getattr(g, "pilot_admitted", False):
        from .http import problem_response

        return problem_response(
            "This account is not admitted to the controlled pilot.",
            "pilot_not_admitted", 403,
        )
    return None

def require_private_import_access():
    """Admit exact pilots or verified Firebase owners to private routes."""
    from .auth import require_owner
    from .http import problem_response

    require_owner()
    session = g.auth_session
    if session.get("provider", "replit") == "replit":
        return require_pilot()
    if not session.get("email_verified"):
        return problem_response(
            "Verify this email before using private processing.",
            "verification_required", 403,
        )
    return None
def _same_origin():
    origin = request.headers.get("Origin")
    if not origin:
        return True
    parsed = urlsplit(origin)
    expected = urlsplit(request.host_url)
    return (
        parsed.scheme in ("http", "https")
        and parsed.scheme == expected.scheme
        and parsed.netloc == expected.netloc
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
    )


def install_security(app):
    """Install validated proxy/host policy and protected-route admission."""
    hops = app.config.get("TRUST_PROXY_HOPS", 0)
    if isinstance(hops, bool) or not isinstance(hops, int) or not 0 <= hops <= 2:
        raise ValueError("TRUST_PROXY_HOPS must be an integer from 0 to 2")
    if hops:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)

    trusted_hosts = parse_trusted_hosts(app.config.get("TRUSTED_HOSTS"))
    if not trusted_hosts and not app.config.get("TESTING"):
        raise RuntimeError("TRUSTED_HOSTS must contain at least one host")
    app.config["TRUSTED_HOSTS"] = trusted_hosts or None
    app.config["PILOT_ALLOWED_SUBJECTS"] = parse_allowed_subjects(
        app.config.get("PILOT_ALLOWED_SUBJECTS", "")
    )

    @app.before_request
    def enforce_security_boundaries():
        if (
            request.path != "/api/billing/webhook"
            and request.is_json
            and request.content_length is not None
            and request.content_length > 4096
        ):
            from .http import problem_response

            return problem_response(
                "The request payload is too large.", "http_413", 413
            )
        session = getattr(g, "auth_session", None)
        g.pilot_admitted = is_pilot_admitted(
            session, app.config["PILOT_ALLOWED_SUBJECTS"]
        )
        if request.path.startswith(PROOF_PREFIX):
            denied = require_pilot()
            if denied is not None:
                return denied
            if _is_expensive_request():
                from .resources import admit_participant

                admit_participant(session["user_id"], action="expensive_http")
        elif request.path.startswith(IMPORTS_PREFIX):
            denied = require_private_import_access()
            if denied is not None:
                return denied
            if _is_expensive_request():
                from .resources import admit_participant

                admit_participant(session["user_id"], action="expensive_http")
        if (
            request.path != "/api/billing/webhook"
            and request.method in ("POST", "PUT", "PATCH", "DELETE")
            and not _same_origin()
        ):
            from .http import problem_response

            return problem_response(
                "Cross-site requests are not permitted.", "origin_rejected", 403
            )

    @app.after_request
    def apply_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.path.startswith(PRIVATE_PREFIXES):
            response.headers["Cache-Control"] = (
                "no-store"
                if request.path == "/api/auth/session"
                else "private, no-store"
            )
            response.headers["Vary"] = _vary(response.headers.get("Vary"), "Cookie")
            response.headers["Referrer-Policy"] = "no-referrer"
        elif response.mimetype == "application/json":
            response.headers.setdefault("Cache-Control", "no-store")
        return response


def _vary(existing, value):
    names = [name.strip() for name in (existing or "").split(",") if name.strip()]
    if not any(secrets.compare_digest(name.lower(), value.lower()) for name in names):
        names.append(value)
    return ", ".join(names)


def _is_expensive_request():
    if request.method != "POST":
        return False
    return request.path == "/api/proof/searches" or _is_new_public_work()

def _is_new_public_work():
    if request.method != "POST":
        return False
    parts = request.path.rstrip("/").split("/")
    return request.path == "/api/imports" or (
        len(parts) == 5
        and parts[1:3] == ["api", "imports"]
        and parts[-1] in {"upload", "searches"}
    )
