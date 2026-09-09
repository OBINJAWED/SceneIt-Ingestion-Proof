BEGIN;

ALTER TABLE sceneit_billing_events
  ADD COLUMN delivery_attempts bigint NOT NULL DEFAULT 1
    CHECK (delivery_attempts BETWEEN 1 AND 9223372036854775807);

COMMIT;