-- Original one-video proof schema. Existing rows and the cumulative budget are preserved.
CREATE TABLE IF NOT EXISTS sceneit_proofs (
  id text PRIMARY KEY,
  title text NOT NULL,
  youtube_id text NOT NULL,
  source_path text NOT NULL,
  source_sha256 text NOT NULL UNIQUE,
  media jsonb NOT NULL,
  state text NOT NULL DEFAULT 'queued',
  message text NOT NULL DEFAULT 'Waiting to upload the authorized file.',
  index_name text NOT NULL,
  index_id text,
  asset_id text,
  indexed_asset_id text,
  provider_status text,
  provider_duration double precision,
  error_code text,
  searches_used integer NOT NULL DEFAULT 0,
  search_limit integer NOT NULL DEFAULT 50,
  last_search_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sceneit_searches (
  id uuid PRIMARY KEY,
  proof_id text NOT NULL REFERENCES sceneit_proofs(id),
  query text NOT NULL,
  query_key text NOT NULL,
  modality text NOT NULL CHECK (modality IN ('both', 'visual', 'audio')),
  state text NOT NULL DEFAULT 'running',
  matches jsonb NOT NULL DEFAULT '[]',
  partial boolean NOT NULL DEFAULT false,
  latency_ms integer NOT NULL DEFAULT 0,
  error_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  UNIQUE(proof_id, query_key, modality)
);
CREATE INDEX IF NOT EXISTS sceneit_searches_history
  ON sceneit_searches(proof_id, created_at DESC);