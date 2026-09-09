-- Additive tiered billing history. This migration never changes usage counters,
-- lifetime trials, or account allowance anchors.
ALTER TABLE sceneit_billing_checkouts
    ADD COLUMN tier_key text,
    ADD COLUMN cadence text CHECK (cadence IN ('monthly','yearly')),
    ADD COLUMN currency text CHECK (currency ~ '^[a-z]{3}$'),
    ADD COLUMN parameters_hash text CHECK (
        parameters_hash IS NULL OR length(parameters_hash)=64);

ALTER TABLE sceneit_billing_subscriptions
    ADD COLUMN tier_key text,
    ADD COLUMN cadence text CHECK (cadence IN ('monthly','yearly')),
    ADD COLUMN currency text CHECK (currency IS NULL OR currency ~ '^[a-z]{3}$'),
    ADD COLUMN schedule_id text,
    ADD COLUMN pending_change_id uuid;

ALTER TABLE sceneit_paid_coverage
    ADD COLUMN coverage_kind text NOT NULL DEFAULT 'period'
        CHECK (coverage_kind IN ('period','upgrade')),
    ADD COLUMN funds_coverage_id text REFERENCES sceneit_paid_coverage(id),
    ADD COLUMN tier_key text,
    ADD COLUMN tier_rank integer CHECK (tier_rank IS NULL OR tier_rank >= 0),
    ADD COLUMN capabilities_snapshot jsonb,
    ADD COLUMN limits_snapshot jsonb,
    ADD COLUMN cadence text CHECK (cadence IN ('monthly','yearly')),
    ADD COLUMN currency text CHECK (currency IS NULL OR currency ~ '^[a-z]{3}$'),
    ADD COLUMN price_id text,
    ADD COLUMN subtotal bigint CHECK (subtotal IS NULL OR subtotal >= 0),
    ADD COLUMN tax bigint CHECK (tax IS NULL OR tax >= 0),
    ADD COLUMN total bigint CHECK (total IS NULL OR total >= 0),
    ADD COLUMN amount_paid bigint CHECK (amount_paid IS NULL OR amount_paid >= 0),
    ADD COLUMN tax_behavior text CHECK (
        tax_behavior IS NULL OR tax_behavior IN ('inclusive','exclusive')),
    ADD COLUMN provider_created_at timestamptz,
    ADD COLUMN provider_receipt_at timestamptz,
    ADD CONSTRAINT sceneit_coverage_funding_shape CHECK (
        (coverage_kind='period' AND funds_coverage_id IS NULL)
        OR (coverage_kind='upgrade' AND funds_coverage_id IS NOT NULL)),
    ADD CONSTRAINT sceneit_coverage_snapshot_shape CHECK (
        (tier_key IS NULL AND tier_rank IS NULL
         AND capabilities_snapshot IS NULL AND limits_snapshot IS NULL)
        OR (tier_key IS NOT NULL AND tier_rank IS NOT NULL
            AND jsonb_typeof(capabilities_snapshot)='array'
            AND jsonb_typeof(limits_snapshot)='object'));
CREATE INDEX sceneit_paid_coverage_effective_tier_idx
    ON sceneit_paid_coverage(owner_id,ends_at,tier_key) WHERE NOT reversed;

ALTER TABLE sceneit_billing_events
    DROP CONSTRAINT sceneit_billing_events_state_check;
ALTER TABLE sceneit_billing_events
    ADD CONSTRAINT sceneit_billing_events_state_check CHECK (
        state IN ('processing','completed','pending','rejected','ignored')),
    ADD COLUMN provider_created_at timestamptz,
    ADD COLUMN received_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN object_type text,
    ADD COLUMN object_id text,
    ADD COLUMN customer_id text,
    ADD COLUMN invoice_id text,
    ADD COLUMN subscription_id text,
    ADD COLUMN payment_key text,
    ADD COLUMN currency text CHECK (
        currency IS NULL OR currency ~ '^[a-z]{3}$'),
    ADD COLUMN amount bigint CHECK (amount IS NULL OR amount >= 0),
    ADD COLUMN outcome text;
CREATE INDEX sceneit_billing_events_timeline_idx
    ON sceneit_billing_events(received_at DESC,event_id DESC);
CREATE INDEX sceneit_billing_events_payment_idx
    ON sceneit_billing_events(payment_key) WHERE payment_key IS NOT NULL;

CREATE TABLE sceneit_billing_operations (
    operation_id uuid PRIMARY KEY,
    owner_id text NOT NULL REFERENCES sceneit_billing_accounts(owner_id),
    kind text NOT NULL CHECK (kind IN (
        'portal','upgrade','schedule','withdraw','payment_recovery')),
    idempotency_key uuid NOT NULL,
    parameters_hash text NOT NULL CHECK(length(parameters_hash)=64),
    state text NOT NULL CHECK (state IN (
        'creating','created','confirmed','scheduled','completed','withdrawn',
        'expired','failed','uncertain')),
    provider_object_id text,
    hosted_url text,
    expires_at timestamptz,
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(owner_id,idempotency_key)
);
CREATE UNIQUE INDEX sceneit_billing_operations_one_mutation_idx
    ON sceneit_billing_operations(owner_id)
    WHERE kind IN ('upgrade','schedule','withdraw')
      AND state IN ('creating','uncertain');

CREATE TABLE sceneit_billing_change_previews (
    preview_id uuid PRIMARY KEY,
    owner_id text NOT NULL REFERENCES sceneit_billing_accounts(owner_id),
    idempotency_key uuid NOT NULL,
    parameters_hash text NOT NULL CHECK(length(parameters_hash)=64),
    subscription_id text NOT NULL,
    source_price_id text NOT NULL,
    target_price_id text NOT NULL,
    target_tier_key text NOT NULL,
    target_cadence text NOT NULL CHECK(target_cadence IN ('monthly','yearly')),
    currency text NOT NULL CHECK(currency ~ '^[a-z]{3}$'),
    kind text NOT NULL CHECK(kind IN ('upgrade','scheduled')),
    subtotal bigint NOT NULL CHECK(subtotal >= 0),
    tax bigint NOT NULL CHECK(tax >= 0),
    total bigint NOT NULL CHECK(total >= 0),
    proration_at timestamptz NOT NULL,
    effective_at timestamptz,
    expires_at timestamptz NOT NULL,
    state text NOT NULL DEFAULT 'open' CHECK(
        state IN ('open','confirmed','expired')),
    provider_preview_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(owner_id,idempotency_key)
);
CREATE INDEX sceneit_billing_change_previews_open_idx
    ON sceneit_billing_change_previews(owner_id,expires_at) WHERE state='open';

CREATE TABLE sceneit_billing_changes (
    change_id uuid PRIMARY KEY,
    owner_id text NOT NULL REFERENCES sceneit_billing_accounts(owner_id),
    preview_id uuid NOT NULL UNIQUE REFERENCES sceneit_billing_change_previews(preview_id),
    operation_id uuid NOT NULL UNIQUE REFERENCES sceneit_billing_operations(operation_id),
    subscription_id text NOT NULL,
    provider_schedule_id text,
    kind text NOT NULL CHECK(kind IN ('upgrade','scheduled')),
    target_tier_key text NOT NULL,
    target_cadence text NOT NULL CHECK(target_cadence IN ('monthly','yearly')),
    currency text NOT NULL CHECK(currency ~ '^[a-z]{3}$'),
    target_price_id text NOT NULL,
    state text NOT NULL CHECK(state IN (
        'confirming','payment_pending','scheduled','effective','withdrawn',
        'failed','uncertain')),
    effective_at timestamptz,
    funded_invoice_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX sceneit_billing_changes_one_pending_idx
    ON sceneit_billing_changes(owner_id)
    WHERE state IN ('confirming','payment_pending','scheduled','uncertain');

CREATE TABLE sceneit_billing_payment_problems (
    invoice_id text PRIMARY KEY,
    owner_id text NOT NULL REFERENCES sceneit_billing_accounts(owner_id),
    subscription_id text,
    provider_created_at timestamptz NOT NULL,
    tier_key text,
    cadence text CHECK(cadence IN ('monthly','yearly')),
    currency text CHECK(currency IS NULL OR currency ~ '^[a-z]{3}$'),
    amount_due bigint CHECK(amount_due IS NULL OR amount_due >= 0),
    code text NOT NULL,
    state text NOT NULL CHECK(state IN ('open','resolved','obsolete')),
    next_action text NOT NULL CHECK(
        next_action IN ('manage_billing','authenticate_payment')),
    resolved_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sceneit_billing_payment_problems_owner_open_idx
    ON sceneit_billing_payment_problems(owner_id,provider_created_at DESC)
    WHERE state='open';