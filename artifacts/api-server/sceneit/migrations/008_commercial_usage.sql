-- Additive, never resets pilot counters or paid account anniversary anchors.
CREATE TABLE sceneit_work_control (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    stopped boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO sceneit_work_control(singleton) VALUES(true);

CREATE TABLE sceneit_usage_windows (
    scope text NOT NULL,
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL CHECK(ends_at > starts_at),
    metric text NOT NULL CHECK(metric IN (
        'imports','upload_attempts','analysis_seconds','searches','media_bytes','frames')),
    used bigint NOT NULL DEFAULT 0 CHECK(used >= 0),
    allowance bigint NOT NULL CHECK(allowance > 0),
    PRIMARY KEY(scope, starts_at, metric)
);
CREATE TABLE sceneit_usage_reservations (
    operation_id text NOT NULL CHECK(length(operation_id) BETWEEN 1 AND 512),
    metric text NOT NULL CHECK(metric IN (
        'imports','upload_attempts','analysis_seconds','searches','media_bytes','frames')),
    owner_id text,
    owner_window timestamptz,
    app_window timestamptz NOT NULL,
    amount bigint NOT NULL CHECK(amount > 0),
    state text NOT NULL DEFAULT 'reserved' CHECK(state IN ('reserved','released')),
    created_at timestamptz NOT NULL DEFAULT now(),
    released_at timestamptz,
    PRIMARY KEY(operation_id, metric)
);
CREATE TABLE sceneit_storage_reservations (
    object_key text PRIMARY KEY CHECK(length(object_key) BETWEEN 1 AND 2048),
    owner_id text,
    size_bytes bigint NOT NULL CHECK(size_bytes > 0),
    state text NOT NULL DEFAULT 'reserved' CHECK(state IN ('reserved','released')),
    created_at timestamptz NOT NULL DEFAULT now(),
    released_at timestamptz
);
CREATE INDEX sceneit_storage_owner_idx ON sceneit_storage_reservations(owner_id)
    WHERE state='reserved';
CREATE TABLE sceneit_budget_audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action text NOT NULL,
    reference text NOT NULL,
    evidence text NOT NULL CHECK(length(evidence) BETWEEN 1 AND 500),
    created_at timestamptz NOT NULL DEFAULT now()
);
-- Previously accepted objects and pending sessions remain occupancy, even if
-- their application retention deadline has elapsed. No DDL runs on startup.
INSERT INTO sceneit_storage_reservations(object_key,owner_id,size_bytes)
SELECT path, owner_id, MAX(size_bytes) FROM (
    SELECT upload_path AS path,owner_id,
           GREATEST(COALESCE(upload_expected_bytes,200000000),1) AS size_bytes
    FROM sceneit_imports WHERE upload_path IS NOT NULL
    UNION ALL
    SELECT media_path AS path,owner_id,
           GREATEST(COALESCE(file_size_bytes,200000000),1) AS size_bytes
    FROM sceneit_imports WHERE media_path IS NOT NULL
) retained GROUP BY path,owner_id;

-- Shared evidence never spends member allowance but does occupy app storage.
INSERT INTO sceneit_storage_reservations(object_key,owner_id,size_bytes)
SELECT media->'sourcePlayback'->>'objectPath',NULL,
       CASE WHEN (media->>'size') ~ '^[0-9]{1,12}$'
            THEN GREATEST((media->>'size')::bigint,1) ELSE 200000000 END
FROM sceneit_proofs
WHERE media->'sourcePlayback'->>'objectPath' IS NOT NULL
ON CONFLICT DO NOTHING;
INSERT INTO sceneit_storage_reservations(object_key,owner_id,size_bytes)
SELECT object_path,NULL,4000000 FROM sceneit_proof_frames
ON CONFLICT DO NOTHING;