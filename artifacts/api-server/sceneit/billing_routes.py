"""Private billing routes and the narrowly bounded signed webhook."""
import time
from flask import Blueprint, jsonify, request

from .auth import require_csrf, require_owner
from .billing import create_checkout, create_portal, process_event, status
from .billing_config import BillingProblem, billing_settings
from .billing_provider import BillingProviderError, StripeBillingProvider
from .security import require_pilot

billing_bp = Blueprint("sceneit_billing", __name__)


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
    return jsonify(status(require_owner()))


@billing_bp.post("/api/billing/checkout")
def billing_checkout():
    require_csrf()
    denied = require_pilot()
    if denied is not None:
        return denied
    from .resources import admit_participant
    admit_participant(require_owner(), action="billing_checkout")
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) != {"plan", "idempotencyKey"}:
        raise BillingProblem("invalid_request", "Checkout request is invalid.", 400)
    return jsonify(create_checkout(
        require_owner(), body["plan"], body["idempotencyKey"]
    ))


@billing_bp.post("/api/billing/portal")
def billing_portal():
    require_csrf()
    from .resources import admit_participant
    admit_participant(require_owner(), action="billing_portal")
    body = request.get_json(silent=True)
    if body != {}:
        raise BillingProblem("invalid_request", "Portal request must be empty.", 400)
    return jsonify(create_portal(require_owner()))


@billing_bp.post("/api/billing/webhook")
def billing_webhook():
    settings = billing_settings()
    if not settings.enabled:
        raise BillingProblem("billing_disabled", "Billing is disabled.", 503)
    if request.content_length is not None and request.content_length > 256 * 1024:
        raise BillingProblem("webhook_too_large", "Webhook payload is too large.", 413)
    payload = request.get_data(cache=False)
    if len(payload) > 256 * 1024:
        raise BillingProblem("webhook_too_large", "Webhook payload is too large.", 413)
    signature = request.headers.get("Stripe-Signature", "")
    if not signature or len(signature) > 2048:
        raise BillingProblem("invalid_webhook", "Webhook signature is invalid.", 400)
    now = int(time.time())
    if not _valid_signature_timestamp(signature, now):
        raise BillingProblem("invalid_webhook", "Webhook timestamp is invalid.", 400)
    try:
        event = StripeBillingProvider(settings).construct_event(payload, signature)
    except BillingProviderError as exc:
        raise BillingProblem("invalid_webhook", "Webhook signature is invalid.", 400) from exc
    process_event(dict(event))
    return jsonify(received=True), 200