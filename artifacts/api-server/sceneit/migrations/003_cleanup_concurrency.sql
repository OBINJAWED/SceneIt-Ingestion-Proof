BEGIN;

-- Cleanup is not a second ingestion. Multiple ready imports may expire or be
-- cancelled together, including while a newer import is processing. Keep the
-- unique ingestion fence while allowing those cleanup jobs to be queued.
DROP INDEX IF EXISTS sceneit_imports_one_active_owner_idx;
CREATE UNIQUE INDEX sceneit_imports_one_active_owner_idx
ON sceneit_imports(owner_id)
WHERE state IN ('file_required','awaiting_upload','queued','resolving','validating',
                'uploading','processing','indexing');

COMMIT;