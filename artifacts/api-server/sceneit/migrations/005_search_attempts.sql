BEGIN;

-- Search submissions have a 75 second recovery window. A deadline is a
-- fencing boundary, not a retry time: expired/uncertain paid work is retained
-- for operator review and its quota reservation is never returned.
ALTER TABLE sceneit_searches
  ADD COLUMN attempt_id uuid,
  ADD COLUMN deadline_at timestamptz,
  ADD COLUMN resolved_at timestamptz,
  ADD COLUMN resolution text;
UPDATE sceneit_searches
SET attempt_id = id,
    deadline_at = created_at + interval '75 seconds',
    completed_at = CASE WHEN state='running' THEN now() ELSE completed_at END,
    resolved_at = CASE WHEN state IN ('done','failed')
                       THEN COALESCE(completed_at,created_at) ELSE NULL END,
    resolution = CASE WHEN state='done' THEN 'completed'
                      WHEN state='failed' THEN 'failed' ELSE NULL END,
    error_code = CASE WHEN state='running' THEN 'search_outcome_unknown'
                      ELSE error_code END,
    state = CASE WHEN state='running' THEN 'needs_review' ELSE state END;
ALTER TABLE sceneit_searches
  ALTER COLUMN attempt_id SET NOT NULL,
  ALTER COLUMN deadline_at SET NOT NULL;
ALTER TABLE sceneit_searches
  ADD CONSTRAINT sceneit_searches_resolution_value_check
  CHECK (resolution IS NULL OR resolution IN (
    'completed','failed','legacy_running_requires_review',
    'confirmed_failed','review_retained'
  ));
CREATE INDEX IF NOT EXISTS sceneit_searches_running_deadline_idx
ON sceneit_searches(deadline_at) WHERE state = 'running';
CREATE UNIQUE INDEX sceneit_searches_attempt_idx
ON sceneit_searches(attempt_id);

ALTER TABLE sceneit_import_searches
  ADD COLUMN attempt_id uuid,
  ADD COLUMN deadline_at timestamptz,
  ADD COLUMN resolved_at timestamptz,
  ADD COLUMN resolution text;
UPDATE sceneit_import_searches
SET attempt_id = id,
    deadline_at = created_at + interval '75 seconds',
    completed_at = CASE WHEN state='running' THEN now() ELSE completed_at END,
    resolved_at = CASE WHEN state IN ('done','failed')
                       THEN COALESCE(completed_at,created_at) ELSE NULL END,
    resolution = CASE WHEN state = 'done' THEN 'completed'
                      WHEN state = 'failed' THEN 'failed'
                      ELSE NULL END,
    error_code = CASE WHEN state='running' THEN 'search_outcome_unknown'
                      ELSE error_code END,
    state = CASE WHEN state='running' THEN 'needs_review' ELSE state END;
ALTER TABLE sceneit_import_searches
  ALTER COLUMN attempt_id SET NOT NULL,
  ALTER COLUMN deadline_at SET NOT NULL;
ALTER TABLE sceneit_import_searches
  ADD CONSTRAINT sceneit_import_searches_resolution_check
  CHECK (resolution IS NULL OR resolution IN (
    'completed','failed','confirmed_failed','review_retained'
  ));
CREATE UNIQUE INDEX sceneit_import_searches_attempt_idx
ON sceneit_import_searches(attempt_id);
CREATE INDEX sceneit_import_searches_running_deadline_idx
ON sceneit_import_searches(deadline_at) WHERE state = 'running';

COMMIT;