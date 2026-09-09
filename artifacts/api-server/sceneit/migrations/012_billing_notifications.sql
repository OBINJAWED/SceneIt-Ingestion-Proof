CREATE TABLE sceneit_billing_notification_campaigns (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    environment text NOT NULL CHECK (environment IN ('test', 'live')),
    invoice_id text NOT NULL,
    problem_id text NOT NULL,
    state text NOT NULL CHECK (state IN (
        'active', 'resolved', 'suppressed', 'stopped', 'needs_review'
    )),
    next_step integer NOT NULL DEFAULT 0 CHECK (next_step >= 0),
    next_attempt_at timestamptz NOT NULL,
    last_checked_at timestamptz NOT NULL,
    resolved_at timestamptz,
    stop_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (environment, invoice_id)
);
CREATE INDEX sceneit_billing_notification_campaigns_due_idx
    ON sceneit_billing_notification_campaigns(next_attempt_at)
    WHERE state = 'active';

CREATE TABLE sceneit_billing_notification_incidents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_key text NOT NULL UNIQUE,
    event_id text NOT NULL,
    incident_type text NOT NULL CHECK (incident_type IN (
        'rejected', 'failed', 'aging_pending', 'stalled_processing'
    )),
    state text NOT NULL CHECK (state IN ('active', 'resolved')),
    reason text NOT NULL,
    attempts integer NOT NULL CHECK (attempts >= 0),
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    last_alert_at timestamptz,
    alert_sequence integer NOT NULL DEFAULT 0 CHECK (alert_sequence >= 0),
    resolved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sceneit_billing_notification_incidents_active_idx
    ON sceneit_billing_notification_incidents(last_seen_at)
    WHERE state = 'active';

CREATE TABLE sceneit_billing_notification_deliveries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type text NOT NULL CHECK (source_type IN ('dunning', 'incident')),
    source_id uuid NOT NULL,
    source_sequence integer NOT NULL CHECK (source_sequence >= 0),
    message_kind text NOT NULL CHECK (message_kind IN (
        'dunning', 'incident_open', 'incident_reminder', 'incident_resolved'
    )),
    state text NOT NULL CHECK (state IN (
        'queued', 'leased', 'accepted', 'transient', 'permanent',
        'ambiguous', 'suppressed'
    )),
    message_id text NOT NULL UNIQUE,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz NOT NULL,
    lease_token uuid,
    lease_expires_at timestamptz,
    accepted_at timestamptz,
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_type, source_id, source_sequence, message_kind)
);
CREATE INDEX sceneit_billing_notification_deliveries_due_idx
    ON sceneit_billing_notification_deliveries(next_attempt_at)
    WHERE state IN ('queued', 'transient');

CREATE TABLE sceneit_billing_notification_runner_health (
    runner text PRIMARY KEY CHECK (runner IN ('dunning', 'incidents', 'delivery')),
    last_started_at timestamptz,
    last_completed_at timestamptz,
    last_success_at timestamptz,
    last_error_code text,
    claimed integer NOT NULL DEFAULT 0 CHECK (claimed >= 0),
    accepted integer NOT NULL DEFAULT 0 CHECK (accepted >= 0),
    ambiguous integer NOT NULL DEFAULT 0 CHECK (ambiguous >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);
