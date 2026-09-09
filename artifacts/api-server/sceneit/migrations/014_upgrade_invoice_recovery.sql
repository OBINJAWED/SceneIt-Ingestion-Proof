BEGIN;

ALTER TABLE sceneit_billing_changes
  ADD COLUMN pending_invoice_id text;

CREATE UNIQUE INDEX sceneit_billing_changes_pending_invoice_idx
  ON sceneit_billing_changes(pending_invoice_id)
  WHERE pending_invoice_id IS NOT NULL;

ALTER TABLE sceneit_billing_changes
  DROP CONSTRAINT sceneit_billing_changes_state_check;
ALTER TABLE sceneit_billing_changes
  ADD CONSTRAINT sceneit_billing_changes_state_check CHECK(state IN (
    'confirming','payment_pending','scheduled','effective','withdrawn',
    'failed','expired','uncertain'));

COMMIT;