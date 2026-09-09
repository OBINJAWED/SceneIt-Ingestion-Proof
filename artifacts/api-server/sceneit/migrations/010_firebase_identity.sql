BEGIN;

ALTER TABLE sceneit_auth_users
  ADD COLUMN provider text NOT NULL DEFAULT 'replit'
    CHECK (provider IN ('replit', 'firebase')),
  ADD COLUMN email text,
  ADD COLUMN email_verified boolean NOT NULL DEFAULT false;

CREATE TABLE sceneit_firebase_trial_ledgers (
  id text PRIMARY KEY CHECK (id LIKE 'firebase-email-v1:%'),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sceneit_firebase_identities (
  id uuid PRIMARY KEY,
  project_id text NOT NULL,
  issuer text NOT NULL,
  firebase_uid text NOT NULL,
  owner_id text NOT NULL UNIQUE REFERENCES sceneit_auth_users(id) ON DELETE RESTRICT,
  trial_ledger_id text REFERENCES sceneit_firebase_trial_ledgers(id) ON DELETE RESTRICT,
  email_hash text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(project_id, issuer, firebase_uid),
  CHECK (length(project_id) BETWEEN 1 AND 255),
  CHECK (length(issuer) BETWEEN 1 AND 512),
  CHECK (length(firebase_uid) BETWEEN 1 AND 128),
  CHECK (email_hash IS NULL OR length(email_hash) = 64)
);

ALTER TABLE sceneit_auth_sessions
  ADD COLUMN firebase_identity_id uuid
    REFERENCES sceneit_firebase_identities(id) ON DELETE CASCADE,
  ADD COLUMN firebase_auth_time timestamptz,
  ADD COLUMN firebase_validated_at timestamptz,
  ADD COLUMN firebase_email text,
  ADD COLUMN firebase_email_verified boolean;

CREATE INDEX sceneit_firebase_identities_trial
  ON sceneit_firebase_identities(trial_ledger_id);
CREATE INDEX sceneit_auth_sessions_firebase_identity
  ON sceneit_auth_sessions(firebase_identity_id)
  WHERE firebase_identity_id IS NOT NULL;

COMMIT;