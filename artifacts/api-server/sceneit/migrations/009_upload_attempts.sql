-- Durable upload-session journal. Every external initiation is preceded by one
-- committed row and cleanup is fenced by the attempt rather than mutable import
-- convenience columns.
CREATE TABLE sceneit_upload_attempts (
    id uuid PRIMARY KEY,
    import_id uuid NOT NULL,
    owner_id text NOT NULL CHECK(length(owner_id) BETWEEN 1 AND 255),
    object_key text NOT NULL UNIQUE CHECK(length(object_key) BETWEEN 1 AND 2048),
    size_bytes bigint NOT NULL CHECK(size_bytes > 0),
    state text NOT NULL CHECK(state IN (
        'initiating','active','revoke_requested','uncertain','revoked')),
    session_reference text,
    generation text,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);
CREATE INDEX sceneit_upload_attempts_cleanup_idx
ON sceneit_upload_attempts(state, updated_at)
WHERE state IN ('revoke_requested','uncertain');
CREATE INDEX sceneit_upload_attempts_import_idx
ON sceneit_upload_attempts(import_id, created_at DESC);

ALTER TABLE sceneit_imports ADD COLUMN upload_attempt_id uuid;

-- Preserve pre-migration sessions/objects. A generation is confirmed completed;
-- a bearer reference remains revocable; a path with neither is uncertain.
INSERT INTO sceneit_upload_attempts(
    id,import_id,owner_id,object_key,size_bytes,state,session_reference,
    generation,expires_at,created_at,updated_at)
SELECT gen_random_uuid(),id,owner_id,upload_path,
       GREATEST(COALESCE(upload_expected_bytes,file_size_bytes,200000000),1),
       CASE WHEN upload_session_reference IS NULL AND upload_generation IS NULL
            THEN 'uncertain' ELSE 'active' END,
       upload_session_reference,upload_generation,upload_expires_at,
       COALESCE(upload_reserved_at,created_at),updated_at
FROM sceneit_imports
WHERE upload_path IS NOT NULL;

UPDATE sceneit_imports i SET upload_attempt_id=a.id
FROM sceneit_upload_attempts a
WHERE a.import_id=i.id AND a.object_key=i.upload_path;