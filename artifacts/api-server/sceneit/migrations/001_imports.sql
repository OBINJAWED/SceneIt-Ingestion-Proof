-- Private imports are deliberately separate from the legacy proof tables.
CREATE TABLE IF NOT EXISTS sceneit_import_usage (
    owner_id text PRIMARY KEY,
    imports_used integer NOT NULL DEFAULT 0 CHECK (imports_used >= 0),
    searches_used integer NOT NULL DEFAULT 0 CHECK (searches_used >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sceneit_import_app_usage (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    imports_used integer NOT NULL DEFAULT 0 CHECK (imports_used >= 0),
    searches_used integer NOT NULL DEFAULT 0 CHECK (searches_used >= 0),
    worker_heartbeat_at timestamptz
);
INSERT INTO sceneit_import_app_usage(singleton) VALUES (true)
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS sceneit_imports (
    id uuid PRIMARY KEY,
    owner_id text NOT NULL CHECK (length(owner_id) BETWEEN 1 AND 255),
    idempotency_key uuid NOT NULL,
    entry_method text NOT NULL CHECK (entry_method IN ('upload','link')),
    source_kind text NOT NULL CHECK (source_kind IN ('file','youtube','x','tiktok','vimeo')),
    source_url text,
    external_id text,
    title text,
    state text NOT NULL CHECK (state IN (
      'file_required','awaiting_upload','queued','resolving','validating',
      'uploading','processing','indexing','ready','failed','needs_review',
      'cancel_requested','cancelled','expired')),
    status_message text NOT NULL,
    progress_percent double precision,
    error_code text,
    analysis_authorized boolean NOT NULL,
    playback_authorized boolean NOT NULL DEFAULT false,
    duration_seconds double precision,
    file_size_bytes bigint,
    has_audio boolean,
    width integer,
    height integer,
    sha256 text,
    video_codec text,
    audio_codec text,
    upload_path text,
    upload_generation text,
    upload_expected_bytes bigint,
    upload_reserved_at timestamptz,
    upload_expires_at timestamptz,
    upload_session_reference text,
    processing_started_at timestamptz,
    read_failures integer NOT NULL DEFAULT 0,
    media_path text,
    media_generation text,
    index_id text,
    asset_id text,
    indexed_asset_id text,
    provider_write_marker text,
    budget_reserved boolean NOT NULL DEFAULT false,
    lease_token uuid,
    lease_expires_at timestamptz,
    heartbeat_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now() + interval '7 days',
    UNIQUE(owner_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS sceneit_imports_queue_idx
ON sceneit_imports(next_attempt_at, created_at)
WHERE state IN ('queued','resolving','validating','uploading','processing','indexing',
                'cancel_requested');
CREATE INDEX IF NOT EXISTS sceneit_imports_owner_idx
ON sceneit_imports(owner_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS sceneit_imports_one_active_owner_idx
ON sceneit_imports(owner_id)
WHERE state IN ('file_required','awaiting_upload','queued','resolving','validating',
                'uploading','processing','indexing','cancel_requested');
CREATE INDEX IF NOT EXISTS sceneit_imports_owner_fingerprint_idx
ON sceneit_imports(owner_id, sha256)
WHERE sha256 IS NOT NULL AND state NOT IN ('failed','cancelled','expired');

CREATE TABLE IF NOT EXISTS sceneit_import_searches (
    id uuid PRIMARY KEY,
    import_id uuid NOT NULL REFERENCES sceneit_imports(id) ON DELETE CASCADE,
    owner_id text NOT NULL,
    query text NOT NULL,
    query_key text NOT NULL,
    modality text NOT NULL CHECK (modality IN ('both','visual','audio')),
    state text NOT NULL DEFAULT 'running' CHECK (state IN ('running','done','failed','needs_review')),
    matches jsonb NOT NULL DEFAULT '[]'::jsonb,
    partial boolean NOT NULL DEFAULT false,
    latency_ms integer,
    error_code text,
    provider_write_marker text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE(owner_id, import_id, query_key, modality)
);
CREATE INDEX IF NOT EXISTS sceneit_import_searches_history_idx
ON sceneit_import_searches(owner_id, import_id, created_at DESC);

-- A permanent owner-scoped purchase ledger prevents delete/switch/retry from
-- purchasing the same fingerprint again after import metadata is retained or removed.
CREATE TABLE IF NOT EXISTS sceneit_import_fingerprints (
    owner_id text NOT NULL,
    sha256 text NOT NULL,
    status text NOT NULL CHECK (status IN ('active','uncertain','deleted')),
    index_id text,
    asset_id text,
    indexed_asset_id text,
    media_path text,
    media_generation text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(owner_id, sha256)
);