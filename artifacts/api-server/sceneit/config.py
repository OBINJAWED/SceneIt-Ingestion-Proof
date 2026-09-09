"""Validated process configuration with fail-closed pilot admission."""
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when required or bounded configuration is invalid."""


def _integer(env, default, *, minimum=1, maximum=300_000):
    raw = os.environ.get(env, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{env} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{env} must be between {minimum} and {maximum}")
    return value


def _boolean(env, default):
    raw = os.environ.get(env, "true" if default else "false").strip().lower()
    if raw not in {"true", "false"}:
        raise ConfigError(f"{env} must be true or false")
    return raw == "true"


@dataclass(frozen=True)
class Settings:
    database_url: str
    pilot_allowed_subjects: frozenset[str]
    db_connect_timeout_seconds: int
    db_statement_timeout_ms: int
    db_lock_timeout_ms: int
    db_idle_transaction_timeout_ms: int
    migration_lock_timeout_seconds: int
    database_permits: int
    db_slot_directory: str
    require_db_role_connection_limit: bool
    search_permits: int
    media_permits: int
    import_worker_permits: int
    permit_lease_seconds: int
    max_operation_seconds: int
    participant_requests_per_minute: int

    def admits(self, subject):
        return bool(subject) and subject in self.pilot_allowed_subjects


@lru_cache(maxsize=1)
def settings():
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ConfigError("DATABASE_URL is required")
    slot_directory = os.environ.get(
        "SCENEIT_DB_SLOT_DIRECTORY", "/tmp/sceneit-db-connection-slots"
    )
    if not Path(slot_directory).is_absolute():
        raise ConfigError("SCENEIT_DB_SLOT_DIRECTORY must be an absolute path")
    raw_subjects = os.environ.get("PILOT_ALLOWED_SUBJECTS", "")
    subjects = frozenset(value.strip() for value in raw_subjects.split(",") if value.strip())
    max_operation_seconds = _integer(
        "SCENEIT_MAX_OPERATION_SECONDS", 180, maximum=3500
    )
    permit_lease_seconds = _integer(
        "SCENEIT_PERMIT_LEASE_SECONDS", 240, maximum=3600
    )
    if permit_lease_seconds <= max_operation_seconds:
        raise ConfigError(
            "SCENEIT_PERMIT_LEASE_SECONDS must exceed SCENEIT_MAX_OPERATION_SECONDS"
        )
    return Settings(
        database_url=database_url,
        pilot_allowed_subjects=subjects,
        db_connect_timeout_seconds=_integer("SCENEIT_DB_CONNECT_TIMEOUT_SECONDS", 5, maximum=60),
        db_statement_timeout_ms=_integer("SCENEIT_DB_STATEMENT_TIMEOUT_MS", 15_000),
        db_lock_timeout_ms=_integer("SCENEIT_DB_LOCK_TIMEOUT_MS", 3_000),
        db_idle_transaction_timeout_ms=_integer(
            "SCENEIT_DB_IDLE_TRANSACTION_TIMEOUT_MS", 15_000
        ),
        migration_lock_timeout_seconds=_integer(
            "SCENEIT_MIGRATION_LOCK_TIMEOUT_SECONDS", 30, maximum=600
        ),
        database_permits=_integer("SCENEIT_DATABASE_PERMITS", 12, maximum=100),
        db_slot_directory=slot_directory,
        require_db_role_connection_limit=_boolean(
            "SCENEIT_REQUIRE_DB_ROLE_CONNECTION_LIMIT",
            bool(os.environ.get("REPLIT_DEPLOYMENT")),
        ),
        search_permits=_integer("SCENEIT_SEARCH_PERMITS", 2, maximum=100),
        media_permits=_integer("SCENEIT_MEDIA_PERMITS", 2, maximum=100),
        import_worker_permits=_integer("SCENEIT_IMPORT_WORKER_PERMITS", 1, maximum=100),
        permit_lease_seconds=permit_lease_seconds,
        max_operation_seconds=max_operation_seconds,
        participant_requests_per_minute=_integer(
            "SCENEIT_PARTICIPANT_REQUESTS_PER_MINUTE", 30, maximum=10_000
        ),
    )


def reset_settings():
    """Clear the cache for tests and explicit configuration reloads."""
    settings.cache_clear()