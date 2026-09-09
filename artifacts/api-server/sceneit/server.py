"""Flask application entry point for the controlled SceneIt pilot."""
import logging
import os

from flask import Flask, jsonify, request

from .proof import (
    list_search_operations, list_searches, proof_readiness, public_proof, report,
    search_scenes,
)

logger = logging.getLogger("sceneit")
logging.basicConfig(level=logging.INFO, format="%(message)s")
# Standard access logs include the complete callback query (an OIDC authorization
# code). Keep application event/error logs, not raw request lines or bearer URLs.
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("gunicorn.access").disabled = True


def _environment_config():
    domains = list(filter(None, (
        os.environ.get("TRUSTED_HOSTS", ""),
        os.environ.get("REPLIT_DOMAINS", ""),
        os.environ.get("REPLIT_DEV_DOMAIN", ""),
    )))
    # The workspace artifact router performs screenshot/health requests through
    # localhost. Admit loopback only in an interactive Replit workspace, never
    # in a deployment where the externally configured host policy stays exact.
    if (
        os.environ.get("REPLIT_DEV_DOMAIN")
        and not os.environ.get("REPLIT_DEPLOYMENT")
    ):
        domains.extend(("localhost", "127.0.0.1"))
    try:
        proxy_hops = int(os.environ.get("TRUST_PROXY_HOPS", "1"))
    except ValueError as exc:
        raise RuntimeError("TRUST_PROXY_HOPS must be an integer") from exc
    try:
        readiness_timeout = int(os.environ.get("READINESS_TIMEOUT_MS", "1500"))
    except ValueError as exc:
        raise RuntimeError("READINESS_TIMEOUT_MS must be an integer") from exc
    return {
        "SESSION_SECRET": os.environ.get("SESSION_SECRET"),
        "PILOT_ALLOWED_SUBJECTS": os.environ.get("PILOT_ALLOWED_SUBJECTS", ""),
        "TRUSTED_HOSTS": ",".join(domains),
        "TRUST_PROXY_HOPS": proxy_hops,
        "READINESS_TIMEOUT_MS": readiness_timeout,
        "DATABASE_CONFIGURED": bool(os.environ.get("DATABASE_URL")),
        "MAX_CONTENT_LENGTH": 4096,
    }


def create_app(config=None):
    """Create the app without migrations, database access, or provider calls."""
    from .auth import init_auth, require_csrf
    from .billing_config import billing_settings
    from .billing_routes import billing_bp
    from .health import health_bp
    from .http import install_http
    from .imports import imports_bp
    from .proof_media import proof_media_bp
    from .security import install_security

    application = Flask(__name__)
    if config is None:
        application.config.from_mapping(_environment_config())
    else:
        # Explicit factory configuration is isolated from malformed or incomplete
        # process environment, making route tests deterministic.
        application.config.from_mapping({
            "MAX_CONTENT_LENGTH": 4096,
            "TRUST_PROXY_HOPS": 0,
            "PILOT_ALLOWED_SUBJECTS": "",
            "TRUSTED_HOSTS": (),
            "DATABASE_CONFIGURED": False,
        })
        application.config.from_mapping(config)

    # Validate the complete opt-in commercial policy at startup. Disabled mode
    # deliberately requires no Stripe credentials and performs no I/O.
    billing_settings()
    install_http(application)
    init_auth(application)
    install_security(application)
    application.register_blueprint(health_bp)
    application.register_blueprint(billing_bp)
    application.register_blueprint(imports_bp)
    application.register_blueprint(proof_media_bp)

    @application.get("/api/proof")
    def proof_status():
        return jsonify(public_proof())

    @application.get("/api/proof/searches")
    def history():
        return jsonify(list_searches())

    @application.post("/api/proof/searches")
    def semantic_search():
        require_csrf()
        return jsonify(search_scenes(request.get_json()))

    @application.get("/api/proof/search-operations")
    def search_operations():
        return jsonify(list_search_operations())

    @application.get("/api/proof/readiness")
    def current_proof_readiness():
        return jsonify(proof_readiness())

    @application.get("/api/proof/report")
    def evidence():
        return jsonify(report())

    return application


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ["PORT"]), debug=False)