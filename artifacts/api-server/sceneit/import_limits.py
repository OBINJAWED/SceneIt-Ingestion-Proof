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

def usage_ledger_owner(conn, owner_id):
    """Resolve the quota ledger without changing resource ownership semantics."""
    # This is deliberately fail-closed. Authentication owns the durable mapping;
    # an unavailable mapping must never turn into a fresh quota under owner_id.
    from .trial_identity import usage_owner
    return usage_owner(conn, owner_id)


def uses_lifetime_allowance(conn, owner_id):
    """Return whether this owner is backed by the durable trial ledger."""
    return usage_ledger_owner(conn, owner_id) != owner_id


def reserve_import_budget(conn, owner_id):
    """Reserve once, transactionally. Reservations are intentionally never refunded."""
    ledger_owner = usage_ledger_owner(conn, owner_id)
    from .billing_config import billing_settings
    settings = billing_settings()
    enabled = settings["enabled"] if isinstance(settings, dict) else settings.enabled
    if enabled and ledger_owner == owner_id:
        raise ValueError("Commercial import reservations require an operation id")
    conn.execute(
        "INSERT INTO sceneit_import_usage(owner_id) VALUES (%s) "
        "ON CONFLICT (owner_id) DO NOTHING", (ledger_owner,))
    owner = conn.execute(
        "SELECT imports_used FROM sceneit_import_usage WHERE owner_id=%s FOR UPDATE",
        (ledger_owner,)).fetchone()
    app = conn.execute(
        "SELECT imports_used FROM sceneit_import_app_usage WHERE singleton=true FOR UPDATE"
    ).fetchone()
    if owner["imports_used"] >= OWNER_IMPORT_LIMIT:
        return False, "owner_import_limit"
    if app["imports_used"] >= APP_IMPORT_LIMIT:
        return False, "app_import_limit"
    conn.execute("UPDATE sceneit_import_usage SET imports_used=imports_used+1, "
                 "updated_at=now() WHERE owner_id=%s", (ledger_owner,))
    conn.execute("UPDATE sceneit_import_app_usage SET imports_used=imports_used+1 "
                 "WHERE singleton=true")
    return True, None


def reserve_search_budget(conn, owner_id):
    """Reserve a provider search before leaving the transaction."""
    ledger_owner = usage_ledger_owner(conn, owner_id)
    from .billing_config import billing_settings
    settings = billing_settings()
    enabled = settings["enabled"] if isinstance(settings, dict) else settings.enabled
    if enabled and ledger_owner == owner_id:
        raise ValueError("Commercial search reservations require an operation id")
    conn.execute(
        "INSERT INTO sceneit_import_usage(owner_id) VALUES (%s) "
        "ON CONFLICT (owner_id) DO NOTHING", (ledger_owner,))
    owner = conn.execute(
        "SELECT searches_used FROM sceneit_import_usage WHERE owner_id=%s FOR UPDATE",
        (ledger_owner,)).fetchone()
    app = conn.execute(
        "SELECT searches_used FROM sceneit_import_app_usage WHERE singleton=true FOR UPDATE"
    ).fetchone()
    if owner["searches_used"] >= OWNER_SEARCH_LIMIT:
        return False, "owner_search_limit"
    if app["searches_used"] >= APP_SEARCH_LIMIT:
        return False, "app_search_limit"
    conn.execute("UPDATE sceneit_import_usage SET searches_used=searches_used+1, "
                 "updated_at=now() WHERE owner_id=%s", (ledger_owner,))
    conn.execute("UPDATE sceneit_import_app_usage SET searches_used=searches_used+1 "
                 "WHERE singleton=true")
    return True, None


def reserve_import_operation(conn, owner_id, operation_id):
    """Use recurring commercial quota, or the unchanged pilot lifetime ledger."""
    from .billing_config import billing_settings
    settings = billing_settings()
    enabled = settings["enabled"] if isinstance(settings, dict) else settings.enabled
    if not enabled or uses_lifetime_allowance(conn, owner_id):
        return reserve_import_budget(conn, owner_id)
    from .quota import reserve
    reserve(conn, owner_id, operation_id, {"imports": 1})
    return True, None


def reserve_search_operation(conn, owner_id, operation_id):
    """Reserve exactly one durable search submission."""
    from .billing_config import billing_settings
    settings = billing_settings()
    enabled = settings["enabled"] if isinstance(settings, dict) else settings.enabled
    if not enabled or uses_lifetime_allowance(conn, owner_id):
        return reserve_search_budget(conn, owner_id)
    from .quota import reserve
    reserve(conn, owner_id, operation_id, {"searches": 1})
    return True, None

def usage_values(conn, owner_id):
    ledger_owner = usage_ledger_owner(conn, owner_id)
    row = conn.execute(
        "SELECT imports_used,searches_used FROM sceneit_import_usage WHERE owner_id=%s",
        (ledger_owner,)).fetchone()
    return row or {"imports_used": 0, "searches_used": 0}

def usage_snapshot(owner_id):
    """Return the authenticated owner's durable lifetime or paid allowance."""
    from .db import connection
    with connection() as conn:
        trial = uses_lifetime_allowance(conn, owner_id)
        from .billing_config import billing_settings
        settings = billing_settings()
        commercial = (
            settings["enabled"] if isinstance(settings, dict) else settings.enabled
        )
        if commercial and not trial:
            from .quota import usage_status
            status = usage_status(owner_id, conn)
            imports = status["metrics"]["imports"]
            searches = status["metrics"]["searches"]
            return {
                "importsUsed": imports["used"],
                "importLimit": imports["limit"],
                "importsRemaining": imports["remaining"],
                "searchesUsed": searches["used"],
                "searchLimit": searches["limit"],
                "searchesRemaining": searches["remaining"],
                "lifetime": False,
            }
        usage = usage_values(conn, owner_id)
    imports_used = usage["imports_used"]
    searches_used = usage["searches_used"]
    return {
        "importsUsed": imports_used,
        "importLimit": OWNER_IMPORT_LIMIT,
        "importsRemaining": max(0, OWNER_IMPORT_LIMIT - imports_used),
        "searchesUsed": searches_used,
        "searchLimit": OWNER_SEARCH_LIMIT,
        "searchesRemaining": max(0, OWNER_SEARCH_LIMIT - searches_used),
        "lifetime": True,
    }
