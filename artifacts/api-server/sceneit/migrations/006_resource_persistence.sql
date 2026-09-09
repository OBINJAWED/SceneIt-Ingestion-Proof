-- Metadata-only cached evidence and cross-process resource accounting.
ALTER TABLE sceneit_searches
  ADD CONSTRAINT sceneit_searches_state_check
    CHECK (state IN ('running','done','failed','needs_review'));

CREATE TABLE sceneit_proof_frames (
  search_id uuid NOT NULL REFERENCES sceneit_searches(id) ON DELETE CASCADE,
  rank integer NOT NULL CHECK (rank > 0),
  source_sha256 text NOT NULL,
  object_path text NOT NULL,
  PRIMARY KEY(search_id, rank)
);

CREATE TABLE sceneit_resource_leases (
  resource text NOT NULL,
  holder uuid NOT NULL,
  participant text,
  acquired_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  PRIMARY KEY(resource, holder)
);
CREATE INDEX sceneit_resource_leases_expiry_idx
  ON sceneit_resource_leases(resource, expires_at);

CREATE TABLE sceneit_participant_throttles (
  participant text NOT NULL,
  action text NOT NULL,
  window_started_at timestamptz NOT NULL,
  used integer NOT NULL CHECK (used > 0),
  PRIMARY KEY(participant, action)
);