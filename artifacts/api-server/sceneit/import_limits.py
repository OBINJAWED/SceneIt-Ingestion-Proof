"""Conservative, cumulative limits for private imports."""
import os

MAX_BYTES = 200_000_000
MIN_DURATION_SECONDS = 4
MAX_DURATION_SECONDS = 1200
RETENTION_DAYS = 7
ABANDONED_UPLOAD_SECONDS = 3600
OWNER_IMPORT_LIMIT = int(os.getenv("SCENEIT_OWNER_IMPORT_LIMIT", "3"))
APP_IMPORT_LIMIT = int(os.getenv("SCENEIT_APP_IMPORT_LIMIT", "30"))
OWNER_SEARCH_LIMIT = int(os.getenv("SCENEIT_OWNER_SEARCH_LIMIT", "50"))
APP_SEARCH_LIMIT = int(os.getenv("SCENEIT_APP_SEARCH_LIMIT", "500"))


class ImportProblem(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def reserve_import_budget(conn, owner_id):
    """Reserve once, transactionally. Reservations are intentionally never refunded."""
    from .billing_config import billing_settings
    settings = billing_settings()
    enabled = settings["enabled"] if isinstance(settings, dict) else settings.enabled
    if enabled:
        raise ValueError("Commercial import reservations require an operation id")
    conn.execute(
        "INSERT INTO sceneit_import_usage(owner_id) VALUES (%s) "
        "ON CONFLICT (owner_id) DO NOTHING", (owner_id,))
    owner = conn.execute(
        "SELECT imports_used FROM sceneit_import_usage WHERE owner_id=%s FOR UPDATE",
        (owner_id,)).fetchone()
    app = conn.execute(
        "SELECT imports_used FROM sceneit_import_app_usage WHERE singleton=true FOR UPDATE"
    ).fetchone()
    if owner["imports_used"] >= OWNER_IMPORT_LIMIT:
        return False, "owner_import_limit"
    if app["imports_used"] >= APP_IMPORT_LIMIT:
        return False, "app_import_limit"
    conn.execute("UPDATE sceneit_import_usage SET imports_used=imports_used+1, "
                 "updated_at=now() WHERE owner_id=%s", (owner_id,))
    conn.execute("UPDATE sceneit_import_app_usage SET imports_used=imports_used+1 "
                 "WHERE singleton=true")
    return True, None


def reserve_search_budget(conn, owner_id):
    """Reserve a provider search before leaving the transaction."""
    from .billing_config import billing_settings
    settings = billing_settings()
    enabled = settings["enabled"] if isinstance(settings, dict) else settings.enabled
    if enabled:
        raise ValueError("Commercial search reservations require an operation id")
    conn.execute(
        "INSERT INTO sceneit_import_usage(owner_id) VALUES (%s) "
        "ON CONFLICT (owner_id) DO NOTHING", (owner_id,))
    owner = conn.execute(
        "SELECT searches_used FROM sceneit_import_usage WHERE owner_id=%s FOR UPDATE",
        (owner_id,)).fetchone()
    app = conn.execute(
        "SELECT searches_used FROM sceneit_import_app_usage WHERE singleton=true FOR UPDATE"
    ).fetchone()
    if owner["searches_used"] >= OWNER_SEARCH_LIMIT:
        return False, "owner_search_limit"
    if app["searches_used"] >= APP_SEARCH_LIMIT:
        return False, "app_search_limit"
    conn.execute("UPDATE sceneit_import_usage SET searches_used=searches_used+1, "
                 "updated_at=now() WHERE owner_id=%s", (owner_id,))
    conn.execute("UPDATE sceneit_import_app_usage SET searches_used=searches_used+1 "
                 "WHERE singleton=true")
    return True, None


def reserve_import_operation(conn, owner_id, operation_id):
    """Use recurring commercial quota, or the unchanged pilot lifetime ledger."""
    from .billing_config import billing_settings
    settings = billing_settings()
    enabled = settings["enabled"] if isinstance(settings, dict) else settings.enabled
    if not enabled:
        return reserve_import_budget(conn, owner_id)
    from .quota import reserve
    reserve(conn, owner_id, operation_id, {"imports": 1})
    return True, None


def reserve_search_operation(conn, owner_id, operation_id):
    """Reserve exactly one durable search submission."""
    from .billing_config import billing_settings
    settings = billing_settings()
    enabled = settings["enabled"] if isinstance(settings, dict) else settings.enabled
    if not enabled:
        return reserve_search_budget(conn, owner_id)
    from .quota import reserve
    reserve(conn, owner_id, operation_id, {"searches": 1})
    return True, None