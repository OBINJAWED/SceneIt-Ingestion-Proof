"""Explicit ordered PostgreSQL migration status and upgrade operations."""
import argparse
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .config import settings

MIGRATION_PATTERN = re.compile(r"^(?P<version>[0-9]{3})_[a-z0-9_]+\.sql$")
LOCK_NAME = "sceneit-schema-migrations-v2"
TRACKING_TABLE = "sceneit_schema_migrations"
SCHEMA_MANIFEST = Path(__file__).with_name("schema-manifest.json")


class MigrationError(RuntimeError):
    pass


class IncompatibleSchema(MigrationError):
    pass


class MigrationLockTimeout(MigrationError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sha256: str
    sql: str


def migration_directory():
    return Path(__file__).with_name("migrations")


def load_migrations(directory=None):
    result = []
    versions = set()
    for path in sorted(Path(directory or migration_directory()).glob("*.sql")):
        match = MIGRATION_PATTERN.fullmatch(path.name)
        if not match:
            raise MigrationError(f"Invalid migration filename: {path.name}")
        version = int(match.group("version"))
        if version in versions:
            raise MigrationError(f"Duplicate migration version: {version:03d}")
        versions.add(version)
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        # The runner owns transaction boundaries, including older wrapped SQL.
        sql = "\n".join(
            line for line in text.splitlines()
            if line.strip().upper() not in {"BEGIN;", "COMMIT;"}
        )
        result.append(Migration(version, path.name, hashlib.sha256(raw).hexdigest(), sql))
    if not result:
        raise MigrationError("No migrations were found")
    return result


def connect():
    config = settings()
    return psycopg.connect(
        config.database_url,
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=config.db_connect_timeout_seconds,
    )


def _relation_exists(conn, name):
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL AS present", (name,)).fetchone()[
        "present"
    ]


def _columns(conn, table):
    return {
        row["column_name"]: row["data_type"]
        for row in conn.execute(
            "SELECT column_name,data_type FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name=%s", (table,)
        ).fetchall()
    }


def _constraint_definitions(conn, table):
    return [
        (row["constraint_type"], row["definition"])
        for row in conn.execute(
            "SELECT c.contype AS constraint_type,pg_get_constraintdef(c.oid) AS definition "
            "FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
            "JOIN pg_namespace n ON n.oid=t.relnamespace "
            "WHERE n.nspname=current_schema() AND t.relname=%s",
            (table,),
        ).fetchall()
    ]


def _verify_proof_legacy(conn):
    proof_expected = {
        "id": "text", "title": "text", "youtube_id": "text", "source_path": "text",
        "source_sha256": "text", "media": "jsonb", "state": "text", "message": "text",
        "index_name": "text", "index_id": "text", "asset_id": "text",
        "indexed_asset_id": "text", "provider_status": "text",
        "provider_duration": "double precision", "error_code": "text",
        "searches_used": "integer", "search_limit": "integer",
        "last_search_at": "timestamp with time zone",
        "created_at": "timestamp with time zone", "updated_at": "timestamp with time zone",
    }
    search_expected = {
        "id": "uuid", "proof_id": "text", "query": "text", "query_key": "text",
        "modality": "text", "state": "text", "matches": "jsonb", "partial": "boolean",
        "latency_ms": "integer", "error_code": "text",
        "created_at": "timestamp with time zone",
        "completed_at": "timestamp with time zone",
    }
    for table, expected in (
        ("sceneit_proofs", proof_expected), ("sceneit_searches", search_expected)
    ):
        actual = _columns(conn, table)
        allowed_extra = (
            {"attempt_id", "deadline_at", "resolved_at", "resolution"}
            if table == "sceneit_searches" else set()
        )
        wrong = {name: (actual.get(name), kind) for name, kind in expected.items()
                 if actual.get(name) != kind}
        unexpected = set(actual) - set(expected) - allowed_extra
        if wrong or unexpected:
            raise IncompatibleSchema(
                f"{table} does not match the verified legacy schema"
            )
    not_null_expected = {
        "sceneit_proofs": {
            "id", "title", "youtube_id", "source_path", "source_sha256", "media",
            "state", "message", "index_name", "searches_used", "search_limit",
            "created_at", "updated_at",
        },
        "sceneit_searches": {
            "id", "proof_id", "query", "query_key", "modality", "state", "matches",
            "partial", "latency_ms", "created_at",
        },
    }
    for table, expected in not_null_expected.items():
        actual = {
            row["column_name"] for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name=%s "
                "AND is_nullable='NO'", (table,)
            ).fetchall()
        }
        if not expected <= actual:
            raise IncompatibleSchema(f"{table} has unsafe column nullability")
    expected_defaults = {
        ("sceneit_proofs", "state"): "'queued'",
        ("sceneit_proofs", "message"): "'Waiting to upload the authorized file.'",
        ("sceneit_proofs", "searches_used"): "0",
        ("sceneit_proofs", "search_limit"): "50",
        ("sceneit_proofs", "created_at"): "now()",
        ("sceneit_proofs", "updated_at"): "now()",
        ("sceneit_searches", "state"): "'running'",
        ("sceneit_searches", "matches"): "'[]'",
        ("sceneit_searches", "partial"): "false",
        ("sceneit_searches", "latency_ms"): "0",
        ("sceneit_searches", "created_at"): "now()",
    }
    for (table, column), fragment in expected_defaults.items():
        row = conn.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name=%s "
            "AND column_name=%s", (table, column)
        ).fetchone()
        if not row or fragment not in (row["column_default"] or ""):
            raise IncompatibleSchema(
                f"{table}.{column} has an unsafe or missing default"
            )

    proof_constraints = _constraint_definitions(conn, "sceneit_proofs")
    search_constraints = _constraint_definitions(conn, "sceneit_searches")
    proof_defs = [definition.lower() for _, definition in proof_constraints]
    search_defs = [definition.lower() for _, definition in search_constraints]
    required_proof = ("primary key (id)", "unique (source_sha256)")
    required_search = (
        "primary key (id)",
        "unique (proof_id, query_key, modality)",
        "foreign key (proof_id) references sceneit_proofs(id)",
    )
    if not all(any(required in definition for definition in proof_defs)
               for required in required_proof):
        raise IncompatibleSchema("Legacy proof primary/unique constraints are unsafe")
    if not all(any(required in definition for definition in search_defs)
               for required in required_search):
        raise IncompatibleSchema("Legacy search deduplication or foreign key is unsafe")
    proof_foreign_keys = [
        definition.lower() for kind, definition in search_constraints if kind == "f"
    ]
    if len(proof_foreign_keys) != 1 or "on delete" in proof_foreign_keys[0]:
        raise IncompatibleSchema("Legacy proof foreign-key delete behavior is unsafe")
    modality_checks = [
        definition.lower() for kind, definition in search_constraints if kind == "c"
    ]
    if not any(
        "modality" in definition
        and all(value in definition for value in ("both", "visual", "audio"))
        for definition in modality_checks
    ):
        raise IncompatibleSchema("Legacy search modality constraint is missing")
    bad_state = conn.execute(
        "SELECT state FROM sceneit_searches "
        "WHERE state NOT IN ('running','done','failed','needs_review') LIMIT 1"
    ).fetchone()
    if bad_state:
        raise IncompatibleSchema("sceneit_searches contains an unsupported state")
    negative = conn.execute(
        "SELECT 1 FROM sceneit_proofs WHERE searches_used < 0 OR search_limit < searches_used "
        "LIMIT 1"
    ).fetchone()
    if negative:
        raise IncompatibleSchema("proof quota counters are incompatible")


def _infer_baseline(conn, migrations):
    """Verify and record only known, already-present pre-runner structures."""
    by_version = {item.version: item for item in migrations}
    proof = _relation_exists(conn, "sceneit_proofs")
    searches = _relation_exists(conn, "sceneit_searches")
    if proof != searches:
        raise IncompatibleSchema("Legacy proof tables are incomplete")
    inferred = set()
    if proof:
        _verify_proof_legacy(conn)
        inferred.add(0)

    import_tables = {
        "sceneit_import_usage", "sceneit_import_app_usage", "sceneit_imports",
        "sceneit_import_searches", "sceneit_import_fingerprints",
    }
    present_imports = {name for name in import_tables if _relation_exists(conn, name)}
    if present_imports and present_imports != import_tables:
        raise IncompatibleSchema("Legacy import tables are incomplete")
    if present_imports:
        required = {"id", "owner_id", "state", "searches_used", "imports_used"}
        observed = set(_columns(conn, "sceneit_imports")) | set(
            _columns(conn, "sceneit_import_usage")
        )
        if not required <= observed:
            raise IncompatibleSchema("Legacy import schema is incompatible")
        inferred.add(1)

    auth = [_relation_exists(conn, name) for name in
            ("sceneit_auth_users", "sceneit_auth_sessions")]
    if any(auth) and not all(auth):
        raise IncompatibleSchema("Legacy authentication tables are incomplete")
    if all(auth):
        inferred.add(2)

    if present_imports:
        predicate = conn.execute(
            "SELECT pg_get_expr(i.indpred,i.indrelid) AS predicate "
            "FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
            "WHERE c.relname='sceneit_imports_one_active_owner_idx'"
        ).fetchone()
        if not predicate:
            raise IncompatibleSchema("Legacy import concurrency index is missing")
        if "cancel_requested" not in (predicate["predicate"] or ""):
            inferred.add(3)

    attempt_columns = {"attempt_id", "deadline_at", "resolved_at", "resolution"}
    proof_search_columns = set(_columns(conn, "sceneit_searches")) if searches else set()
    import_search_columns = (
        set(_columns(conn, "sceneit_import_searches")) if present_imports else set()
    )
    any_attempts = bool(
        attempt_columns & proof_search_columns or attempt_columns & import_search_columns
    )
    complete_attempts = (
        proof and bool(present_imports)
        and attempt_columns <= proof_search_columns
        and attempt_columns <= import_search_columns
    )
    if any_attempts and not complete_attempts:
        raise IncompatibleSchema("Search-attempt migration is only partially present")
    if complete_attempts:
        inferred.add(5)

    hardening_tables = {
        "sceneit_proof_frames", "sceneit_resource_leases",
        "sceneit_participant_throttles",
    }
    hardening_present = {name for name in hardening_tables if _relation_exists(conn, name)}
    if hardening_present and hardening_present != hardening_tables:
        raise IncompatibleSchema("Resource persistence migration is only partially present")
    if hardening_present == hardening_tables:
        inferred.add(6)

    firebase_tables = {
        "sceneit_firebase_trial_ledgers", "sceneit_firebase_identities",
    }
    firebase_present = {
        name for name in firebase_tables if _relation_exists(conn, name)
    }
    if firebase_present and firebase_present != firebase_tables:
        raise IncompatibleSchema("Firebase identity migration is only partially present")
    if firebase_present == firebase_tables:
        required_identity = {
            "id", "project_id", "issuer", "firebase_uid", "owner_id",
            "trial_ledger_id", "email_hash",
        }
        required_session = {
            "firebase_identity_id", "firebase_auth_time", "firebase_validated_at",
            "firebase_email", "firebase_email_verified",
        }
        required_user = {"provider", "email", "email_verified"}
        if (
            not required_identity <= set(_columns(conn, "sceneit_firebase_identities"))
            or not required_session <= set(_columns(conn, "sceneit_auth_sessions"))
            or not required_user <= set(_columns(conn, "sceneit_auth_users"))
        ):
            raise IncompatibleSchema("Firebase identity migration is incompatible")
        inferred.add(10)

    for version in sorted(inferred):
        migration = by_version.get(version)
        if migration is None:
            raise IncompatibleSchema(f"No checked-in migration describes version {version}")
        conn.execute(
            f"INSERT INTO {TRACKING_TABLE}(version,name,sha256) VALUES (%s,%s,%s)",
            (migration.version, migration.name, migration.sha256),
        )


def _ensure_tracking(conn, migrations):
    existed = _relation_exists(conn, TRACKING_TABLE)
    if not existed:
        conn.execute(
            f"CREATE TABLE {TRACKING_TABLE}("
            "version integer PRIMARY KEY,name text NOT NULL UNIQUE,"
            "sha256 text NOT NULL CHECK(length(sha256)=64),"
            "applied_at timestamptz NOT NULL DEFAULT now())"
        )
        _infer_baseline(conn, migrations)
        return

    columns = set(_columns(conn, TRACKING_TABLE))
    if columns == {"name", "applied_at"}:
        # Upgrade the original name-only runner without replaying its migrations.
        conn.execute(f"ALTER TABLE {TRACKING_TABLE} ADD COLUMN version integer")
        conn.execute(f"ALTER TABLE {TRACKING_TABLE} ADD COLUMN sha256 text")
        by_name = {item.name: item for item in migrations}
        for row in conn.execute(f"SELECT name FROM {TRACKING_TABLE}").fetchall():
            item = by_name.get(row["name"])
            if item is None:
                raise IncompatibleSchema(
                    f"Unknown previously applied migration: {row['name']}"
                )
            conn.execute(
                f"UPDATE {TRACKING_TABLE} SET version=%s,sha256=%s WHERE name=%s",
                (item.version, item.sha256, item.name),
            )
        conn.execute(
            "CREATE TEMP TABLE sceneit_migration_journal_upgrade "
            f"ON COMMIT DROP AS SELECT version,name,sha256,applied_at FROM {TRACKING_TABLE}"
        )
        conn.execute(f"DROP TABLE {TRACKING_TABLE}")
        conn.execute(
            f"CREATE TABLE {TRACKING_TABLE}("
            "version integer PRIMARY KEY,name text NOT NULL UNIQUE,"
            "sha256 text NOT NULL CHECK(length(sha256)=64),"
            "applied_at timestamptz NOT NULL DEFAULT now())"
        )
        conn.execute(
            f"INSERT INTO {TRACKING_TABLE}(version,name,sha256,applied_at) "
            "SELECT version,name,sha256,applied_at "
            "FROM sceneit_migration_journal_upgrade"
        )
    elif not {"version", "name", "sha256", "applied_at"} <= columns:
        raise IncompatibleSchema("Migration tracking table has an incompatible shape")


def _applied(conn, migrations):
    rows = conn.execute(
        f"SELECT version,name,sha256,applied_at FROM {TRACKING_TABLE} ORDER BY version"
    ).fetchall()
    expected = {item.version: item for item in migrations}
    for row in rows:
        item = expected.get(row["version"])
        if item is None or item.name != row["name"]:
            raise IncompatibleSchema(
                f"Applied migration version {row['version']} is not recognized"
            )
        if item.sha256 != row["sha256"]:
            raise IncompatibleSchema(f"Checksum mismatch for {item.name}")
    return {row["version"]: row for row in rows}


def schema_fingerprint(conn):
    """Canonical final-schema fingerprint (no rows, secrets, or owner content)."""
    schema = conn.execute("SELECT current_schema() AS name").fetchone()["name"]
    if isinstance(schema, bytes):
        schema = schema.decode("utf-8")
    columns = conn.execute(
        "SELECT table_name,column_name,ordinal_position,data_type,is_nullable,"
        "COALESCE(column_default,'') AS column_default "
        "FROM information_schema.columns WHERE table_schema=current_schema() "
        "AND table_name LIKE 'sceneit_%' ORDER BY table_name,ordinal_position"
    ).fetchall()
    constraints = conn.execute(
        "SELECT t.relname AS table_name,c.conname,c.contype,"
        "pg_get_constraintdef(c.oid) AS definition,c.convalidated "
        "FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
        "JOIN pg_namespace n ON n.oid=t.relnamespace "
        "WHERE n.nspname=current_schema() AND t.relname LIKE 'sceneit_%' "
        "ORDER BY t.relname,c.conname"
    ).fetchall()
    indexes = conn.execute(
        "SELECT tablename,indexname,indexdef FROM pg_indexes "
        "WHERE schemaname=current_schema() AND tablename LIKE 'sceneit_%' "
        "ORDER BY tablename,indexname"
    ).fetchall()

    def clean(value):
        if isinstance(value, bytes):
            return clean(value.decode("ascii"))
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, str):
            return value.replace(f'"{schema}".', "").replace(f"{schema}.", "")
        return value

    payload = clean({
        "columns": [dict(row) for row in columns],
        "constraints": [dict(row) for row in constraints],
        "indexes": [dict(row) for row in indexes],
    })
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _verify_schema_manifest(conn):
    if not SCHEMA_MANIFEST.exists():
        raise MigrationError("The checked-in schema manifest is missing")
    manifest = json.loads(SCHEMA_MANIFEST.read_text(encoding="utf-8"))
    actual = schema_fingerprint(conn)
    if manifest.get("sha256") != actual:
        raise IncompatibleSchema(
            "Applied migration journal does not match the live schema fingerprint"
        )


def _lock(conn, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while True:
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS locked", (LOCK_NAME,)
        ).fetchone()["locked"]
        if locked:
            return
        if time.monotonic() >= deadline:
            raise MigrationLockTimeout("Timed out waiting for the migration lock")
        time.sleep(0.1)


def status(conn=None, directory=None):
    migrations = load_migrations(directory)
    owned = conn is None
    conn = conn or connect()
    try:
        if not _relation_exists(conn, TRACKING_TABLE):
            return [{"version": item.version, "name": item.name, "status": "pending"}
                    for item in migrations]
        applied = _applied(conn, migrations)
        if len(applied) == len(migrations):
            _verify_schema_manifest(conn)
        return [
            {"version": item.version, "name": item.name,
             "status": "applied" if item.version in applied else "pending"}
            for item in migrations
        ]
    finally:
        if owned:
            conn.close()


def upgrade(conn=None, directory=None, lock_timeout_seconds=None):
    migrations = load_migrations(directory)
    config = settings()
    owned = conn is None
    conn = conn or connect()
    applied_now = []
    _lock(conn, lock_timeout_seconds or config.migration_lock_timeout_seconds)
    try:
        untracked = not _relation_exists(conn, TRACKING_TABLE)

        def apply_pending():
            _ensure_tracking(conn, migrations)
            applied = _applied(conn, migrations)
            for item in migrations:
                if item.version in applied:
                    continue
                # Nested transaction() is a savepoint when an untracked
                # baseline is being upgraded under the outer atomic wrapper.
                with conn.transaction():
                    conn.execute(item.sql)
                    conn.execute(
                        f"INSERT INTO {TRACKING_TABLE}(version,name,sha256) "
                        "VALUES (%s,%s,%s)",
                        (item.version, item.name, item.sha256),
                    )
                applied_now.append(item.name)
            _verify_schema_manifest(conn)

        if untracked:
            # Existing integrated schemas are either accepted and upgraded in
            # full, or left byte-for-byte structurally unchanged with no
            # journal. This includes failures found only by the final manifest.
            with conn.transaction():
                apply_pending()
            return applied_now

        # Baseline validation and journal creation are one atomic decision. A
        # rejected/terminated baseline cannot leave an empty ledger that would
        # bypass validation on the next invocation.
        with conn.transaction():
            _ensure_tracking(conn, migrations)
        apply_pending()
        return applied_now
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_NAME,))
        finally:
            if owned:
                conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Explicit SceneIt schema status and operator-approved upgrade"
    )
    operation = parser.add_subparsers(dest="operation", required=True)
    operation.add_parser("status")
    upgrade_parser = operation.add_parser("upgrade")
    upgrade_parser.add_argument(
        "--operator-approved",
        action="store_true",
        help="confirm backup/restore rehearsal and release approval are recorded",
    )
    args = parser.parse_args(argv)
    if args.operation == "status":
        for item in status():
            print(f"{item['version']:03d} {item['status']:7s} {item['name']}")
        return 0
    if not args.operator_approved:
        parser.error("upgrade requires --operator-approved")
    applied = upgrade()
    print("Schema is current." if not applied else "Applied: " + ", ".join(applied))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())