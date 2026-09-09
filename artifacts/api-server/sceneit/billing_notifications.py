"""Durable, bounded billing dunning and webhook incident notifications.

This module never retries a charge. Provider facts and the billing recipient are
resolved afresh by an injected Stripe-backed resolver immediately before use.
"""
import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .billing_smtp import (
    SMTPDeliveryError, TLSMailer, notification_settings,
)
from .db import connection

_SAFE_REF = re.compile(r"[A-Za-z0-9_:-]{1,128}")
_ELIGIBLE = {
    "open", "past_due", "expired_card", "uncollectible", "action_required",
}


class NotificationFactUnavailable(RuntimeError):
    """The fresh provider read failed before any SMTP transaction began."""


@dataclass(frozen=True)
class InvoiceNotificationFact:
    invoice_id: str
    recipient: str
    state: str
    tier: str
    currency: str
    amount_due: int
    attempts: int
    reversed: bool = False
    obsolete: bool = False

    @property
    def eligible(self):
        return (
            self.state in _ELIGIBLE and self.amount_due > 0
            and not self.reversed and not self.obsolete
        )


def _now(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value


def _safe_ref(value, label):
    if not isinstance(value, str) or not _SAFE_REF.fullmatch(value):
        raise ValueError(f"{label} is not a safe provider reference")
    return value


def _recipient(value):
    if (not isinstance(value, str) or len(value) > 254
            or "\r" in value or "\n" in value
            or not re.fullmatch(r"[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+", value)):
        raise ValueError("provider billing recipient is invalid")
    return value


def _validate_fact(fact, invoice_id):
    if not isinstance(fact, InvoiceNotificationFact) or fact.invoice_id != invoice_id:
        raise ValueError("provider returned a different invoice")
    _recipient(fact.recipient)
    _safe_ref(fact.tier, "tier")
    if not re.fullmatch(r"[a-z]{3}", fact.currency):
        raise ValueError("provider currency is invalid")
    if (isinstance(fact.amount_due, bool) or not isinstance(fact.amount_due, int)
            or fact.amount_due < 0 or isinstance(fact.attempts, bool)
            or not isinstance(fact.attempts, int) or fact.attempts < 0):
        raise ValueError("provider monetary or attempt facts are invalid")
    return fact


def _message_id(settings, source_type, source_id, sequence, kind):
    seed = f"sceneit:{source_type}:{source_id}:{sequence}:{kind}".encode()
    return f"<{hashlib.sha256(seed).hexdigest()}@{settings.message_domain}>"


def _health_start(runner, now):
    with connection() as conn:
        conn.execute(
            "INSERT INTO sceneit_billing_notification_runner_health"
            "(runner,last_started_at,updated_at) VALUES (%s,%s,%s) "
            "ON CONFLICT (runner) DO UPDATE SET last_started_at=EXCLUDED.last_started_at,"
            "last_error_code=NULL,updated_at=EXCLUDED.updated_at",
            (runner, now, now),
        )


def _health_finish(runner, now, *, counts=None, error=None):
    counts = counts or {}
    with connection() as conn:
        conn.execute(
            "INSERT INTO sceneit_billing_notification_runner_health"
            "(runner,last_completed_at,last_success_at,last_error_code,claimed,"
            "accepted,ambiguous,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (runner) DO UPDATE SET "
            "last_completed_at=EXCLUDED.last_completed_at,"
            "last_success_at=EXCLUDED.last_success_at,"
            "last_error_code=EXCLUDED.last_error_code,claimed=EXCLUDED.claimed,"
            "accepted=EXCLUDED.accepted,ambiguous=EXCLUDED.ambiguous,"
            "updated_at=EXCLUDED.updated_at",
            (runner, now, None if error else now, error, counts.get("claimed", 0),
             counts.get("accepted", 0), counts.get("ambiguous", 0), now),
        )


def open_dunning(invoice_id, problem_id, resolver, *, environment="test", now=None):
    """Open the sole finite campaign for an invoice after a fresh Stripe read."""
    settings = notification_settings()
    if not (settings.enabled and settings.scheduler_enabled and settings.dunning_enabled):
        return None
    invoice_id = _safe_ref(invoice_id, "invoice")
    problem_id = _safe_ref(problem_id, "problem")
    if environment not in ("test", "live"):
        raise ValueError("environment is invalid")
    current = _now(now)
    fact = _validate_fact(
        resolver.resolve_invoice_notification(invoice_id), invoice_id
    )  # Stripe billing recipient only; never auth email.
    if not fact.eligible:
        return None
    with connection() as conn:
        row = conn.execute(
            "INSERT INTO sceneit_billing_notification_campaigns"
            "(environment,invoice_id,problem_id,state,next_attempt_at,last_checked_at) "
            "VALUES (%s,%s,%s,'active',%s,%s) "
            "ON CONFLICT (environment,invoice_id) DO NOTHING RETURNING id",
            (environment, invoice_id, problem_id,
             current + timedelta(hours=settings.schedule_hours[0]), current),
        ).fetchone()
    return str(row["id"]) if row else None


def enqueue_due_dunning(resolver, *, now=None, limit=25):
    """Claim campaigns with SKIP LOCKED and enqueue at most one stable step."""
    settings = notification_settings()
    if not (settings.enabled and settings.scheduler_enabled and settings.dunning_enabled):
        return 0
    current = _now(now)
    limit = max(1, min(int(limit), 100))
    _health_start("dunning", current)
    made = 0
    try:
        with connection() as conn:
            rows = conn.execute(
                "SELECT * FROM sceneit_billing_notification_campaigns "
                "WHERE state='active' AND next_attempt_at<=%s "
                "AND NOT EXISTS (SELECT 1 FROM "
                "sceneit_billing_notification_deliveries d WHERE "
                "d.source_type='dunning' AND d.source_id="
                "sceneit_billing_notification_campaigns.id AND "
                "d.state IN ('queued','leased','transient')) "
                "ORDER BY next_attempt_at LIMIT %s FOR UPDATE SKIP LOCKED",
                (current, limit),
            ).fetchall()
            for row in rows:
                fact = _validate_fact(
                    resolver.resolve_invoice_notification(row["invoice_id"]),
                    row["invoice_id"],
                )
                if not fact.eligible:
                    conn.execute(
                        "UPDATE sceneit_billing_notification_campaigns SET "
                        "state='resolved',resolved_at=%s,last_checked_at=%s,"
                        "stop_reason='provider_resolved',updated_at=%s WHERE id=%s",
                        (current, current, current, row["id"]),
                    )
                    continue
                step = row["next_step"]
                if step >= len(settings.schedule_hours):
                    conn.execute(
                        "UPDATE sceneit_billing_notification_campaigns SET "
                        "state='stopped',stop_reason='schedule_complete',"
                        "last_checked_at=%s,updated_at=%s WHERE id=%s",
                        (current, current, row["id"]),
                    )
                    continue
                conn.execute(
                    "INSERT INTO sceneit_billing_notification_deliveries"
                    "(source_type,source_id,source_sequence,message_kind,state,"
                    "message_id,next_attempt_at) VALUES "
                    "('dunning',%s,%s,'dunning','queued',%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (row["id"], step, _message_id(
                        settings, "dunning", row["id"], step, "dunning"
                    ), current),
                )
                made += 1
                next_step = step + 1
                if next_step < len(settings.schedule_hours):
                    due = row["created_at"] + timedelta(
                        hours=settings.schedule_hours[next_step]
                    )
                else:
                    # The final queued reminder still needs an active campaign
                    # for its last-moment eligibility check. Dispatch closes it.
                    due = current + timedelta(days=36600)
                conn.execute(
                    "UPDATE sceneit_billing_notification_campaigns SET "
                    "next_step=%s,next_attempt_at=%s,last_checked_at=%s,updated_at=%s "
                    "WHERE id=%s",
                    (next_step, due, current, current, row["id"]),
                )
        _health_finish("dunning", current)
        return made
    except Exception:
        _health_finish("dunning", current, error="runner_error")
        raise


def _claim_delivery(settings, current):
    lease_token = uuid.uuid4()
    with connection() as conn:
        # A lost lease may have completed SMTP DATA. It is intentionally never resent.
        conn.execute(
            "UPDATE sceneit_billing_notification_campaigns c SET "
            "state='needs_review',stop_reason='lease_expired',updated_at=%s "
            "FROM sceneit_billing_notification_deliveries d WHERE "
            "d.source_type='dunning' AND d.source_id=c.id AND d.state='leased' "
            "AND d.lease_expires_at<=%s AND c.state='active'",
            (current, current),
        )
        conn.execute(
            "UPDATE sceneit_billing_notification_deliveries SET state='ambiguous',"
            "last_error_code='lease_expired',lease_token=NULL,lease_expires_at=NULL,"
            "updated_at=%s WHERE state='leased' AND lease_expires_at<=%s",
            (current, current),
        )
        return conn.execute(
            "WITH candidate AS (SELECT id FROM "
            "sceneit_billing_notification_deliveries WHERE "
            "state IN ('queued','transient') AND next_attempt_at<=%s "
            "AND attempts<%s ORDER BY next_attempt_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
            "UPDATE sceneit_billing_notification_deliveries d SET state='leased',"
            "attempts=d.attempts+1,lease_token=%s,lease_expires_at=%s,updated_at=%s "
            "FROM candidate WHERE d.id=candidate.id RETURNING d.*",
            (current, settings.max_attempts, lease_token,
             current + timedelta(seconds=settings.lease_seconds), current),
        ).fetchone()


def _content(row, settings, resolver, current):
    if row["source_type"] == "dunning":
        with connection() as conn:
            campaign = conn.execute(
                "SELECT * FROM sceneit_billing_notification_campaigns WHERE id=%s",
                (row["source_id"],),
            ).fetchone()
        fact = _validate_fact(
            resolver.resolve_invoice_notification(campaign["invoice_id"]),
            campaign["invoice_id"],
        )
        recipient = fact.recipient
        if campaign["state"] != "active" or not fact.eligible:
            return None
        subject = "Action needed for your SceneIt billing"
        text = (
            f"Invoice {fact.invoice_id} remains unresolved. "
            f"Tier: {fact.tier}. Amount due: {fact.amount_due} "
            f"{fact.currency.upper()} minor units. Provider attempts: {fact.attempts}. "
            f"SceneIt will not retry your card. Manage billing securely: "
            f"{settings.billing_action_url}"
        )
        return recipient, subject, text
    with connection() as conn:
        incident = conn.execute(
            "SELECT * FROM sceneit_billing_notification_incidents WHERE id=%s",
            (row["source_id"],),
        ).fetchone()
    age = max(0, int((current - incident["first_seen_at"]).total_seconds()))
    resolved = row["message_kind"] == "incident_resolved"
    subject = (
        "Resolved: SceneIt billing webhook incident"
        if resolved else "SceneIt billing webhook incident"
    )
    text = (
        f"Event {incident['event_id']}; type {incident['incident_type']}; "
        f"status {'resolved' if resolved else incident['state']}; "
        f"age {age} seconds; attempts {incident['attempts']}; "
        f"reason {incident['reason']}. Recovery action: inspect the bounded "
        "billing event timeline and run operator-approved reconciliation."
    )
    return settings.operator_recipient, subject, text


def dispatch(resolver, *, mailer=None, now=None, clock=None, limit=25):
    """Deliver bounded work. Acceptance means SMTP acceptance, not inbox delivery."""
    settings = notification_settings()
    if not (settings.enabled and settings.scheduler_enabled):
        return {"claimed": 0, "accepted": 0, "ambiguous": 0}
    def current_time():
        if clock is not None:
            return _now(clock())
        return _now(now)

    current = current_time()
    limit = max(1, min(int(limit), 100))
    mailer = mailer or TLSMailer(settings)
    counts = {"claimed": 0, "accepted": 0, "ambiguous": 0}
    _health_start("delivery", current)
    try:
        for _ in range(limit):
            current = current_time()
            row = _claim_delivery(settings, current)
            if not row:
                break
            counts["claimed"] += 1
            try:
                content = _content(row, settings, resolver, current)
                if content is None:
                    state, code = "suppressed", "obsolete"
                else:
                    lease_remaining = (
                        row["lease_expires_at"] - current
                    ).total_seconds()
                    if lease_remaining <= 0:
                        state, code = "ambiguous", "lease_expired"
                    else:
                        mailer.send(
                            *content, row["message_id"],
                            deadline=time.monotonic() + min(
                                lease_remaining, settings.timeout_seconds
                            ),
                        )
                        state, code = "accepted", None
            except SMTPDeliveryError as exc:
                state, code = exc.disposition, exc.code
            except NotificationFactUnavailable:
                state, code = "transient", "current_fact_unavailable"
            except (ValueError, LookupError):
                state, code = "permanent", "invalid_current_fact"
            finished = current_time()
            if finished >= row["lease_expires_at"]:
                state, code = "ambiguous", "lease_expired"
            if state == "accepted":
                counts["accepted"] += 1
            elif state == "ambiguous":
                counts["ambiguous"] += 1
            delay = min(3600, 30 * (2 ** max(0, row["attempts"] - 1)))
            if state == "transient" and row["attempts"] >= settings.max_attempts:
                state, code = "permanent", "retry_limit"
            with connection() as conn:
                conn.execute(
                    "UPDATE sceneit_billing_notification_deliveries SET state=%s,"
                    "last_error_code=%s,next_attempt_at=%s,accepted_at=%s,"
                    "lease_token=NULL,lease_expires_at=NULL,updated_at=%s "
                    "WHERE id=%s AND state='leased' AND lease_token=%s",
                    (state, code, finished + timedelta(seconds=delay),
                     finished if state == "accepted" else None, finished,
                     row["id"], row["lease_token"]),
                )
                if row["source_type"] == "dunning" and state in (
                    "permanent", "ambiguous"
                ):
                    conn.execute(
                        "UPDATE sceneit_billing_notification_campaigns SET state=%s,"
                        "stop_reason=%s,updated_at=%s WHERE id=%s AND state='active'",
                        ("needs_review" if state == "ambiguous" else "stopped",
                         code, finished, row["source_id"]),
                    )
                elif row["source_type"] == "dunning" and state == "accepted":
                    conn.execute(
                        "UPDATE sceneit_billing_notification_campaigns SET "
                        "state='stopped',stop_reason='schedule_complete',updated_at=%s "
                        "WHERE id=%s AND state='active' AND next_step>=%s",
                        (finished, row["source_id"], len(settings.schedule_hours)),
                    )
        current = current_time()
        _health_finish("delivery", current, counts=counts)
        return counts
    except Exception:
        _health_finish("delivery", current, counts=counts, error="runner_error")
        raise


def scan_webhook_incidents(*, now=None, limit=100):
    """Create cooldown-deduped alerts for verified persisted event receipts."""
    settings = notification_settings()
    if not (settings.enabled and settings.scheduler_enabled and settings.alerts_enabled):
        return 0
    current = _now(now)
    limit = max(1, min(int(limit), 250))
    _health_start("incidents", current)
    changed = 0
    try:
        with connection() as conn:
            rows = conn.execute(
                "SELECT event_id,state,attempts,last_error_code,updated_at FROM "
                "sceneit_billing_events WHERE state IN "
                "('rejected','failed','pending','processing') "
                "ORDER BY updated_at LIMIT %s",
                (limit,),
            ).fetchall()
            for row in rows:
                age = max(0, int((current - row["updated_at"]).total_seconds()))
                if row["state"] in ("rejected", "failed"):
                    kind = row["state"]
                elif row["state"] == "pending" and age >= settings.pending_age_seconds:
                    kind = "aging_pending"
                elif row["state"] == "processing" and age >= settings.stalled_age_seconds:
                    kind = "stalled_processing"
                else:
                    continue
                event_id = _safe_ref(row["event_id"], "event")
                key = f"{kind}:{event_id}"
                reason = row["last_error_code"] or kind
                if not isinstance(reason, str) or not _SAFE_REF.fullmatch(reason):
                    reason = "redacted_error"
                incident = conn.execute(
                    "INSERT INTO sceneit_billing_notification_incidents"
                    "(incident_key,event_id,incident_type,state,reason,attempts,"
                    "first_seen_at,last_seen_at) VALUES "
                    "(%s,%s,%s,'active',%s,%s,%s,%s) "
                    "ON CONFLICT (incident_key) DO UPDATE SET last_seen_at=%s,"
                    "attempts=EXCLUDED.attempts,reason=EXCLUDED.reason,updated_at=%s "
                    "RETURNING *",
                    (key, event_id, kind, reason,
                     row["attempts"], row["updated_at"], current, current, current),
                ).fetchone()
                if (incident["last_alert_at"] is None
                        or incident["last_alert_at"] <= current - timedelta(
                            seconds=settings.alert_cooldown_seconds
                        )):
                    sequence = incident["alert_sequence"] + 1
                    message_kind = (
                        "incident_open" if sequence == 1 else "incident_reminder"
                    )
                    conn.execute(
                        "INSERT INTO sceneit_billing_notification_deliveries"
                        "(source_type,source_id,source_sequence,message_kind,state,"
                        "message_id,next_attempt_at) VALUES "
                        "('incident',%s,%s,%s,'queued',%s,%s) ON CONFLICT DO NOTHING",
                        (incident["id"], sequence, message_kind,
                         _message_id(settings, "incident", incident["id"],
                                     sequence, message_kind), current),
                    )
                    conn.execute(
                        "UPDATE sceneit_billing_notification_incidents SET "
                        "last_alert_at=%s,alert_sequence=%s,updated_at=%s WHERE id=%s",
                        (current, sequence, current, incident["id"]),
                    )
                    changed += 1
            active = conn.execute(
                "SELECT * FROM sceneit_billing_notification_incidents "
                "WHERE state='active' ORDER BY last_seen_at LIMIT %s "
                "FOR UPDATE SKIP LOCKED", (limit,),
            ).fetchall()
            for incident in active:
                event = conn.execute(
                    "SELECT state,updated_at FROM sceneit_billing_events "
                    "WHERE event_id=%s", (incident["event_id"],),
                ).fetchone()
                still_active = False
                if event:
                    event_age = max(
                        0, int((current - event["updated_at"]).total_seconds())
                    )
                    still_active = (
                        (incident["incident_type"] in ("rejected", "failed")
                         and event["state"] == incident["incident_type"])
                        or (incident["incident_type"] == "aging_pending"
                            and event["state"] == "pending"
                            and event_age >= settings.pending_age_seconds)
                        or (incident["incident_type"] == "stalled_processing"
                            and event["state"] == "processing"
                            and event_age >= settings.stalled_age_seconds)
                    )
                if still_active:
                    continue
                sequence = incident["alert_sequence"] + 1
                conn.execute(
                    "UPDATE sceneit_billing_notification_incidents SET "
                    "state='resolved',resolved_at=%s,alert_sequence=%s,updated_at=%s "
                    "WHERE id=%s", (current, sequence, current, incident["id"]),
                )
                conn.execute(
                    "INSERT INTO sceneit_billing_notification_deliveries"
                    "(source_type,source_id,source_sequence,message_kind,state,"
                    "message_id,next_attempt_at) VALUES "
                    "('incident',%s,%s,'incident_resolved','queued',%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (incident["id"], sequence,
                     _message_id(settings, "incident", incident["id"], sequence,
                                 "incident_resolved"), current),
                )
                changed += 1
        _health_finish("incidents", current)
        return changed
    except Exception:
        _health_finish("incidents", current, error="runner_error")
        raise


def notification_health():
    """Return outbox health only; this function never creates an alert."""
    with connection() as conn:
        runners = conn.execute(
            "SELECT * FROM sceneit_billing_notification_runner_health ORDER BY runner"
        ).fetchall()
        counts = conn.execute(
            "SELECT state,count(*) AS count FROM "
            "sceneit_billing_notification_deliveries GROUP BY state ORDER BY state"
        ).fetchall()
    return {"runners": runners, "deliveries": counts}


def redacted_webhook_signal(reason, *, received_at=None):
    """Build the only allowed pre-persistence signal; callers must emit externally."""
    allowed = {
        "invalid_signature", "invalid_timestamp", "oversized", "database_unavailable",
        "persistence_failed",
    }
    if reason not in allowed:
        reason = "persistence_failed"
    return {
        "component": "billing_webhook",
        "verified": False,
        "reason": reason,
        "receivedAt": _now(received_at).isoformat(),
    }
