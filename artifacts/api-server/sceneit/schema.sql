-- Fresh development schema assembled from the same immutable migrations used
-- by the operator runner. Execute with psql from this directory; web and worker
-- startup never execute this file.
\set ON_ERROR_STOP on
\ir migrations/000_proof.sql
\ir migrations/001_imports.sql
\ir migrations/002_auth.sql
\ir migrations/003_cleanup_concurrency.sql
\ir migrations/005_search_attempts.sql
\ir migrations/006_resource_persistence.sql
\ir migrations/007_billing.sql
\ir migrations/008_commercial_usage.sql
\ir migrations/009_upload_attempts.sql
\ir migrations/010_firebase_identity.sql