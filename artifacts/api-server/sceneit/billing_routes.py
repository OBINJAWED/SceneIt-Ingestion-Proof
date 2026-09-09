"""Private billing routes and the narrowly bounded signed webhook."""
import logging
import time
from flask import Blueprint, g, jsonify, request

from .auth import require_csrf, require_owner
from .billing import (
    confirm_change, create_checkout, create_portal, preview_change,
    process_event, status, withdraw_change,
)
from .billing_config import BillingProblem, billing_settings
from .billing_provider import BillingProviderError, StripeBillingProvider
from .security import require_pilot

billing_bp = Blueprint("sceneit_billing", __name__)
logger = logging.getLogger("sceneit")


def _signal(reason):
    from .billing_notifications import redacted_webhook_signal
    logger.warning("%s", redacted_webhook_signal(reason))


def _valid_signature_timestamp(signature, now):
    timestamps = [
        part[2:] for part in signature.split(",") if part.startswith("t=")
    ]
    try:
        signed_at = int(timestamps[0]) if len(timestamps) == 1 else None
    except ValueError:
        return False
    return bool(
        signed_at is not None
        and now - signed_at <= 300
        and signed_at - now <= 30
    )


@billing_bp.get("/api/billing/status")
def billing_status():
    return jsonify(status(
        require_owner(), purchase_authorized=bool(
            getattr(g, "pilot_admitted", False)
        )
    ))


@billing_bp.post("/api/billing/checkout")
def billing_checkout():
    require_csrf()
    denied = require_pilot()
    if denied is not None:
        return denied
    from .resources import admit_participant
    admit_participant(require_owner(), action="billing_checkout")
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) != {
        "tier", "cadence", "currency", "idempotencyKey"
    }:
        raise BillingProblem("invalid_request", "Checkout request is invalid.", 400)
    return jsonify(create_checkout(
        require_owner(), body["tier"], body["cadence"], body["currency"],
        body["idempotencyKey"], purchase_authorized=True,
    ))


@billing_bp.post("/api/billing/portal")
def billing_portal():
    require_csrf()
    from .resources import admit_participant
    admit_participant(require_owner(), action="billing_portal")
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) != {"action", "idempotencyKey"}:
        raise BillingProblem("invalid_request", "Portal request is invalid.", 400)
    return jsonify(create_portal(
        require_owner(), body["action"], body["idempotencyKey"]
    ))


@billing_bp.post("/api/billing/change/preview")
def billing_change_preview():
    require_csrf()
    denied = require_pilot()
    if denied is not None:
        return denied
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) != {
        "tier", "cadence", "currency", "idempotencyKey"
    }:
        raise BillingProblem("invalid_request", "Change preview request is invalid.", 400)
    return jsonify(preview_change(
        require_owner(), body["tier"], body["cadence"], body["currency"],
        body["idempotencyKey"], purchase_authorized=True,
    ))


@billing_bp.post("/api/billing/change/confirm")
def billing_change_confirm():
    require_csrf()
    denied = require_pilot()
    if denied is not None:
        return denied
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) != {
        "previewId", "idempotencyKey"
    }:
        raise BillingProblem("invalid_request", "Change confirmation is invalid.", 400)
    return jsonify(confirm_change(
        require_owner(), body["previewId"], body["idempotencyKey"],
        purchase_authorized=True,
    ))


@billing_bp.post("/api/billing/change/withdraw")
def billing_change_withdraw():
    require_csrf()
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) != {
        "changeId", "idempotencyKey"
    }:
        raise BillingProblem("invalid_request", "Change withdrawal is invalid.", 400)
    return jsonify(withdraw_change(
        require_owner(), body["changeId"], body["idempotencyKey"]
    ))


@billing_bp.post("/api/billing/webhook")
def billing_webhook():
    settings = billing_settings()
    if not settings.enabled:
        raise BillingProblem("billing_disabled", "Billing is disabled.", 503)
    if request.content_length is not None and request.content_length > 256 * 1024:
        _signal("oversized")
        raise BillingProblem("webhook_too_large", "Webhook payload is too large.", 413)
    payload = request.get_data(cache=False)
    if len(payload) > 256 * 1024:
        _signal("oversized")
        raise BillingProblem("webhook_too_large", "Webhook payload is too large.", 413)
    signature = request.headers.get("Stripe-Signature", "")
    if not signature or len(signature) > 2048:
        _signal("invalid_signature")
        raise BillingProblem("invalid_webhook", "Webhook signature is invalid.", 400)
    now = int(time.time())
    if not _valid_signature_timestamp(signature, now):
        _signal("invalid_timestamp")
        raise BillingProblem("invalid_webhook", "Webhook timestamp is invalid.", 400)
    try:
        event = StripeBillingProvider(settings).construct_event(payload, signature)
    except BillingProviderError as exc:
        _signal("invalid_signature")
        raise BillingProblem("invalid_webhook", "Webhook signature is invalid.", 400) from exc
    try:
        process_event(dict(event))
    except BillingProblem:
        # Verified relationship failures have a durable rejected receipt.
        raise
    except Exception:
        _signal("persistence_failed")
        raise
    return jsonify(received=True), 200