CREATE TABLE sceneit_billing_accounts (
    owner_id text PRIMARY KEY REFERENCES sceneit_auth_users(id),
    customer_id text UNIQUE,
    environment text NOT NULL CHECK (environment IN ('test', 'live')),
    customer_attempt_id uuid NOT NULL DEFAULT gen_random_uuid(),
    customer_attempt_state text NOT NULL DEFAULT 'new'
        CHECK (customer_attempt_state IN ('new', 'creating', 'created', 'uncertain')),
    allowance_anchor timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sceneit_billing_checkouts (
    owner_id text NOT NULL REFERENCES sceneit_billing_accounts(owner_id),
    idempotency_key uuid NOT NULL,
    plan text NOT NULL CHECK (plan IN ('monthly', 'yearly')),
    price_id text NOT NULL,
    state text NOT NULL CHECK (state IN (
        'creating', 'created', 'uncertain', 'expired', 'completed'
    )),
    provider_session_id text,
    subscription_id text,
    hosted_url text,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, idempotency_key),
    UNIQUE (provider_session_id)
);
CREATE UNIQUE INDEX sceneit_billing_checkouts_one_open_owner_idx
    ON sceneit_billing_checkouts(owner_id)
    WHERE state IN ('creating', 'created', 'uncertain', 'completed');

CREATE TABLE sceneit_billing_subscriptions (
    subscription_id text PRIMARY KEY,
    owner_id text NOT NULL REFERENCES sceneit_billing_accounts(owner_id),
    customer_id text NOT NULL,
    environment text NOT NULL CHECK (environment IN ('test', 'live')),
    price_id text NOT NULL,
    status text NOT NULL,
    cancel_at_period_end boolean NOT NULL DEFAULT false,
    current_period_end timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sceneit_billing_subscriptions_owner_idx
    ON sceneit_billing_subscriptions(owner_id);

CREATE TABLE sceneit_paid_coverage (
    id text PRIMARY KEY,
    owner_id text NOT NULL REFERENCES sceneit_billing_accounts(owner_id),
    subscription_id text NOT NULL,
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    reversed boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (ends_at > starts_at)
);
CREATE INDEX sceneit_paid_coverage_owner_active_idx
    ON sceneit_paid_coverage(owner_id, ends_at) WHERE NOT reversed;

CREATE TABLE sceneit_billing_events (
    event_id text PRIMARY KEY,
    environment text NOT NULL CHECK (environment IN ('test', 'live')),
    event_type text NOT NULL,
    state text NOT NULL CHECK (state IN ('processing', 'completed', 'pending', 'rejected')),
    payload jsonb NOT NULL,
    attempts integer NOT NULL DEFAULT 1 CHECK (attempts > 0),
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sceneit_billing_events_pending_idx
    ON sceneit_billing_events(updated_at) WHERE state = 'pending';

CREATE TABLE sceneit_billing_audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action text NOT NULL,
    evidence text NOT NULL CHECK (length(evidence) BETWEEN 8 AND 500),
    affected integer NOT NULL CHECK (affected >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);