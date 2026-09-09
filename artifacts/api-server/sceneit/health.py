"""Cheap liveness and bounded, provider-free readiness checks."""
from flask import Blueprint, current_app, jsonify

from .config import settings
from .db import connection
from .migrate import status as migration_status

health_bp = Blueprint("sceneit_health", __name__)


@health_bp.get("/api/healthz")
def liveness():
    return jsonify(status="ok")


@health_bp.get("/api/readyz")
def readiness():
    try:
        settings()
        config_ok = bool(
            current_app.config.get("SESSION_SECRET")
            and current_app.config.get("TRUSTED_HOSTS")
            and current_app.config.get("DATABASE_CONFIGURED")
        )
    except Exception:
        config_ok = False
    database_ok = False
    schema_ok = False
    try:
        with connection() as conn:
            timeout = int(current_app.config.get("READINESS_TIMEOUT_MS", 1500))
            conn.execute(
                "SELECT set_config('statement_timeout',%s,true)",
                (str(timeout),),
            )
            database_ok = True
            # The bounded catalog fingerprint prevents a stamped-but-damaged
            # schema (missing table, column, index, or constraint) from serving.
            schema_ok = all(
                item["status"] == "applied"
                for item in migration_status(conn=conn)
            )
    except Exception:
        # Probe responses and logs must not expose driver messages or credentials.
        pass
    ready = config_ok and database_ok and schema_ok
    response = jsonify(
        status="ready" if ready else "not_ready",
        checks={
            "config": "ok" if config_ok else "invalid",
            "database": "ok" if database_ok else "unavailable",
            "schema": "ok" if schema_ok else "incompatible",
        },
        external={"provider": "not_probed"},
    )
    response.status_code = 200 if ready else 503
    return response