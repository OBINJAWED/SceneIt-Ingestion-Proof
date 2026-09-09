"""Redacted request telemetry and safe, typed HTTP outage responses."""
import json
import logging
import time
import uuid

import httpx
import psycopg
from flask import g, has_request_context, jsonify, request
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException

from .proof import ProofError
from .db import DatabaseResourceExhausted
from .resources import ParticipantThrottled, ResourceExhausted

logger = logging.getLogger("sceneit")


def install_http(app):
    @app.before_request
    def begin_request():
        # Flask's normal browser JSON ceiling remains 4 KiB. Only the signed raw
        # Stripe webhook receives its documented, independently checked ceiling.
        if request.path == "/api/billing/webhook":
            request.max_content_length = 256 * 1024
        request.request_id = uuid.uuid4().hex
        request.request_started = time.monotonic()

    @app.after_request
    def finish_request(response):
        response.headers["X-Request-ID"] = getattr(request, "request_id", "")
        started = getattr(request, "request_started", time.monotonic())
        # Route templates avoid logging query strings and private identifiers.
        route = request.url_rule.rule if request.url_rule else "unmatched"
        payload = response.get_json(silent=True) if response.is_json else None
        context = _operation_context()
        if route == "/api/proof/search-operations" and isinstance(payload, list):
            logger.info(json.dumps({
                "event": "search_operations_summary",
                "request_id": getattr(request, "request_id", None),
                "active_count": sum(item.get("state") == "running" for item in payload),
                "review_required_count": sum(
                    item.get("state") == "needs_review" for item in payload
                ),
            }, separators=(",", ":")))
        logger.info(json.dumps({
            "event": "http_request",
            "request_id": getattr(request, "request_id", None),
            "method": request.method,
            "route": route,
            "status": response.status_code,
            "code": payload.get("code") if isinstance(payload, dict) else None,
            **context,
            "duration_ms": max(0, round((time.monotonic() - started) * 1000)),
        }, separators=(",", ":")))
        return response

    @app.errorhandler(ProofError)
    def expected_error(error):
        return problem_response(error.message, error.code, error.status)

    from .billing_config import BillingProblem
    from .billing_provider import BillingProviderError
    app.register_error_handler(
        BillingProblem,
        lambda error: problem_response(error.message, error.code, error.status),
    )
    app.register_error_handler(
        BillingProviderError,
        lambda _error: problem_response(
            "The billing provider is temporarily unavailable.",
            "billing_provider_unavailable", 503, retry_after=10,
        ),
    )

    @app.errorhandler(ValidationError)
    def validation_error(_error):
        return problem_response(
            "The request did not pass validation.", "invalid_request", 400
        )

    @app.errorhandler(psycopg.Error)
    def database_outage(_error):
        return _outage("database_unavailable")

    @app.errorhandler(DatabaseResourceExhausted)
    def database_capacity(_error):
        return problem_response(
            "Database capacity is temporarily busy. Please retry later.",
            "database_capacity_exhausted", 503, retry_after=10,
        )

    @app.errorhandler(ParticipantThrottled)
    def participant_throttled(_error):
        return problem_response(
            "Too many expensive requests. Please wait before retrying.",
            "participant_throttled", 429, retry_after=60,
        )

    @app.errorhandler(ResourceExhausted)
    def resource_exhausted(error):
        return problem_response(
            "Processing capacity is temporarily busy. Please retry later.",
            f"{error.resource}_capacity_exhausted", 503, retry_after=10,
        )

    for error_type in (httpx.TimeoutException, httpx.NetworkError):
        app.register_error_handler(
            error_type, lambda _error: _outage("upstream_unavailable")
        )

    @app.errorhandler(HTTPException)
    def http_error(error):
        return problem_response(
            error.description, f"http_{error.code}", error.code
        )

    @app.errorhandler(Exception)
    def unexpected_error(error):
        # Exception text can contain queries, credentials, URLs, or provider data.
        logger.error(json.dumps({
            "event": "request_failed",
            "request_id": getattr(request, "request_id", None),
            "type": type(error).__name__,
        }, separators=(",", ":")))
        return problem_response(
            "The service could not complete this request.", "internal_error", 500
        )


def _outage(code):
    logger.warning(json.dumps({
        "event": "dependency_unavailable",
        "request_id": getattr(request, "request_id", None),
        "dependency": "database" if code.startswith("database") else "upstream",
    }, separators=(",", ":")))
    return problem_response(
        "A required service is temporarily unavailable.", code, 503,
        retry_after=10,
    )


def problem_response(message, code, status, *, retry_after=None):
    """Build one contract-safe failure without exposing exception details."""
    state = _failure_state(code, status)
    payload = {"error": message, "code": code, "state": state}
    if retry_after is not None:
        payload.update(retryable=True, retryAfterSeconds=retry_after)
    response = jsonify(payload)
    response.status_code = status
    if retry_after is not None:
        response.headers["Retry-After"] = str(retry_after)
    if state == "quota_exhausted":
        _event("quota_denied", code, status)
    elif "capacity" in code or code in {"busy", "frame_busy"}:
        _event("resource_saturation", code, status)
    return response


def set_operation_context(operation_id=None, attempt_id=None):
    """Attach opaque identifiers to this request's redacted telemetry."""
    if not has_request_context():
        return
    if operation_id is not None:
        g.operation_id = str(operation_id)
    if attempt_id is not None:
        g.attempt_id = str(attempt_id)


def _failure_state(code, status):
    if status == 401:
        return "unauthorized"
    if status == 403:
        return (
            "admission_required"
            if code == "pilot_not_admitted"
            else "unauthorized"
        )
    if status == 404:
        return "not_found"
    if any(value in code for value in ("review", "uncertain", "outcome_unknown")):
        return "uncertain"
    if any(value in code for value in ("quota", "budget", "rate_limit", "throttled")):
        return "quota_exhausted"
    if code in {"search_in_progress", "index_not_ready", "frame_pending"}:
        return "processing"
    return "service_unavailable"


def _operation_context():
    view_args = request.view_args or {}
    operation_id = getattr(g, "operation_id", None) or view_args.get("search_id")
    attempt_id = getattr(g, "attempt_id", None)
    result = {}
    if operation_id is not None:
        result["operation_id"] = str(operation_id)
    if attempt_id is not None:
        result["attempt_id"] = str(attempt_id)
    return result


def _event(event, code, status):
    logger.warning(json.dumps({
        "event": event,
        "request_id": getattr(request, "request_id", None),
        "code": code,
        "status": status,
        **_operation_context(),
    }, separators=(",", ":")))