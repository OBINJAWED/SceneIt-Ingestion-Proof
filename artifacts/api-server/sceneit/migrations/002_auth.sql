BEGIN;

CREATE TABLE sceneit_auth_users (
  id text PRIMARY KEY,
  first_name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sceneit_auth_sessions (
  id text PRIMARY KEY,
  user_id text NOT NULL REFERENCES sceneit_auth_users(id) ON DELETE CASCADE,
  csrf_token text NOT NULL,
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX sceneit_auth_sessions_expiry
  ON sceneit_auth_sessions(expires_at);
CREATE INDEX sceneit_auth_sessions_user
  ON sceneit_auth_sessions(user_id, expires_at DESC);

COMMIT;
