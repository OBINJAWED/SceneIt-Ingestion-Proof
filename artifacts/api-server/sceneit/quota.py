"""Atomic, durable commercial reservations; no network calls in this module.

Monthly allowances follow the ACCOUNT's first paid anniversary, not subscription
IDs. All account/application changes lock one short PostgreSQL critical section.
Reservations survive timeouts, process death, cancellation and period rollover.
"""
import calendar
from datetime import datetime, timedelta, timezone

from .billing_config import BillingProblem, METRICS, billing_settings
from .config import settings
from .db import connection

UTC = timezone.utc


def monthly_window(anchor, now):
    """Clamp each anniversary independently, restoring the original day later."""
    anchor, now = anchor.astimezone(UTC), now.astimezone(UTC)
    if now < anchor:
        raise ValueError("Allowance anchor is in the future")
    month = (now.year - anchor.year) * 12 + now.month - anchor.month

    def anniversary(offset):
        year, index = divmod(anchor.year * 12 + anchor.month - 1 + offset, 12)
        return anchor.replace(year=year, month=index + 1,
                              day=min(anchor.day, calendar.monthrange(year, index + 1)[1]))

    if anniversary(month) > now:
        month -= 1
    return anniversary(month), anniversary(month + 1)


def _lock(conn):
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('sceneit-commercial-budgets'))")


def _now(conn):
    # Wall time AFTER acquiring locks, never transaction-start time at a rollover.
    return conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]


def _account(conn, owner_id, now, *, require_membership=True):
    if owner_id is None:
        if require_membership:
            raise BillingProblem("membership_required", "Paid membership is required.", 402)
        return None
    if require_membership and not settings().admits(owner_id):
        raise BillingProblem("pilot_not_admitted", "This account is not admitted to the pilot.", 403)
    account = conn.execute(
        "SELECT * FROM sceneit_billing_accounts WHERE owner_id=%s FOR UPDATE",
        (owner_id,)).fetchone()
    if require_membership:
        covered = account and account["environment"] == billing_settings().environment and (
            conn.execute(
                "SELECT 1 FROM sceneit_paid_coverage WHERE owner_id=%s AND NOT reversed "
                "AND starts_at<=%s AND ends_at>%s LIMIT 1", (owner_id, now, now)).fetchone())
        if not covered or not account["allowance_anchor"] or account["allowance_anchor"] > now:
            raise BillingProblem("membership_required", "Current paid coverage is required for new work.", 402)
    return account


def _membership_required(conn, owner_id, requested):
    """Keep durable Firebase trials on app-only caps outside request context."""
    if not requested or not isinstance(owner_id, str) or not owner_id.startswith(
        "firebase:"
    ):
        return requested
    from .trial_identity import usage_owner
    # usage_owner fails closed when a Firebase identity exists without its
    # durable ledger. An unknown firebase-shaped owner remains subject to paid
    # membership rather than receiving trial admission.
    return usage_owner(conn, owner_id) == owner_id


def check_work(conn, owner_id, *, require_membership=True):
    if not billing_settings().enabled:
        return
    require_membership = _membership_required(
        conn, owner_id, require_membership
    )
    _lock(conn)
    stopped = conn.execute(
        "SELECT stopped FROM sceneit_work_control WHERE singleton=true FOR UPDATE"
    ).fetchone()
    if not stopped or stopped["stopped"]:
        raise BillingProblem("service_work_stopped", "New expensive work is temporarily stopped.", 503)
    return _account(conn, owner_id, _now(conn), require_membership=require_membership)


def _window(conn, scope, start, end, metric, limit):
    conn.execute(
        "INSERT INTO sceneit_usage_windows(scope,starts_at,ends_at,metric,allowance) "
        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (scope, start, end, metric, limit))
    row = conn.execute(
        "SELECT used,allowance FROM sceneit_usage_windows "
        "WHERE scope=%s AND starts_at=%s AND metric=%s FOR UPDATE",
        (scope, start, metric)).fetchone()
    # Lowering configured limits takes effect immediately; raising them cannot
    # mint additional units in a window already established under older policy.
    return row["used"], min(row["allowance"], limit)


def reserve(conn, owner_id, operation_id, amounts, *, require_membership=True):
    if not billing_settings().enabled:
        return
    # Savepoint also protects callers which translate a denial to a response
    # inside their outer transaction rather than propagating the exception.
    with conn.transaction():
        return _reserve(conn, owner_id, operation_id, amounts,
                        require_membership=require_membership)


def _reserve(conn, owner_id, operation_id, amounts, *, require_membership=True):
    policy = billing_settings()
    if not policy.enabled:
        return
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 512:
        raise ValueError("Invalid operation identity")
    if (not amounts or not set(amounts) <= set(METRICS)
            or any(type(n) is not int or n < 1 or n > 10**15 for n in amounts.values())):
        raise ValueError("Invalid reservation units")
    require_membership = _membership_required(
        conn, owner_id, require_membership
    )
    account = check_work(conn, owner_id, require_membership=require_membership)
    now = _now(conn)
    app_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    app_end = app_start + timedelta(days=1)
    # Shared proof never debits member allowance, even for a signed-in member.
    owner = owner_id if require_membership else None
    owner_start, owner_end = (
        monthly_window(account["allowance_anchor"], now) if owner else (None, None))
    for metric, amount in sorted(amounts.items()):
        existing = conn.execute(
            "SELECT * FROM sceneit_usage_reservations WHERE operation_id=%s AND metric=%s",
            (operation_id, metric)).fetchone()
        if existing:
            if existing["owner_id"] != owner or existing["amount"] != amount:
                raise BillingProblem("reservation_conflict", "Operation reservation does not match.", 409)
            if existing["state"] == "released":
                raise BillingProblem("reservation_released", "Released work cannot be restarted.", 409)
            # Original purchase belongs to original windows, including after restart.
            continue
        scopes = [("app", app_start, app_end, policy.app_limits[metric])]
        if owner:
            scopes.append((f"owner:{owner}", owner_start, owner_end, policy.limits[metric]))
        for scope, start, end, limit in scopes:
            used, allowed = _window(conn, scope, start, end, metric, limit)
            if used + amount > allowed:
                app = scope == "app"
                raise BillingProblem(
                    "service_capacity_exhausted" if app else "owner_quota_exhausted",
                    "Application work allowance is exhausted." if app
                    else f"Monthly {metric} allowance is exhausted.", 503 if app else 429)
        for scope, start, _end, _limit in scopes:
            conn.execute(
                "UPDATE sceneit_usage_windows SET used=used+%s "
                "WHERE scope=%s AND starts_at=%s AND metric=%s",
                (amount, scope, start, metric))
        conn.execute(
            "INSERT INTO sceneit_usage_reservations"
            "(operation_id,metric,owner_id,owner_window,app_window,amount) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (operation_id, metric, owner, owner_start, app_start, amount))


def _storage_used(conn, owner_id=None):
    # Pilot-mode objects may have been added AFTER the additive migration but
    # BEFORE commercial activation. Count every retained reference not already
    # journaled; deadlines are not evidence of physical deletion.
    clause = " WHERE owner_id=%s" if owner_id is not None else ""
    return conn.execute(
        "WITH retained AS ("
        "SELECT upload_path AS object_key,owner_id,"
        "GREATEST(COALESCE(upload_expected_bytes,200000000),1) AS size_bytes "
        "FROM sceneit_imports WHERE upload_path IS NOT NULL UNION ALL "
        "SELECT media_path,owner_id,GREATEST(COALESCE(file_size_bytes,200000000),1) "
        "FROM sceneit_imports WHERE media_path IS NOT NULL UNION ALL "
        "SELECT object_key,owner_id,size_bytes FROM sceneit_upload_attempts "
        "WHERE state<>'revoked' UNION ALL "
        "SELECT media->'sourcePlayback'->>'objectPath',NULL,"
        "CASE WHEN (media->>'size') ~ '^[0-9]{1,12}$' "
        "THEN GREATEST((media->>'size')::bigint,1) ELSE 200000000 END "
        "FROM sceneit_proofs WHERE media->'sourcePlayback'->>'objectPath' IS NOT NULL "
        "UNION ALL SELECT object_path,NULL,4000000 FROM sceneit_proof_frames), "
        "occupancy AS (SELECT owner_id,size_bytes FROM sceneit_storage_reservations "
        "WHERE state='reserved' UNION ALL SELECT r.owner_id,MAX(r.size_bytes) "
        "FROM retained r WHERE NOT EXISTS (SELECT 1 FROM sceneit_storage_reservations s "
        "WHERE s.object_key=r.object_key) GROUP BY r.object_key,r.owner_id) "
        f"SELECT COALESCE(SUM(size_bytes),0) AS used FROM occupancy{clause}",
        (owner_id,) if owner_id is not None else ()
    ).fetchone()["used"]


def reserve_storage(conn, owner_id, object_key, size_bytes, *, require_membership=True):
    policy = billing_settings()
    if not policy.enabled:
        return
    if (type(size_bytes) is not int or not 1 <= size_bytes <= 10**15
            or not isinstance(object_key, str) or not 1 <= len(object_key) <= 2048):
        raise ValueError("Invalid storage reservation")
    check_work(conn, owner_id, require_membership=require_membership)
    owner_id = owner_id if require_membership else None
    old = conn.execute("SELECT * FROM sceneit_storage_reservations WHERE object_key=%s",
                       (object_key,)).fetchone()
    if old:
        if (old["owner_id"] != owner_id or old["size_bytes"] != size_bytes
                or old["state"] != "reserved"):
            raise BillingProblem("reservation_conflict", "Storage reservation does not match.", 409)
        return
    if _storage_used(conn) + size_bytes > policy.app_limits["storage_bytes"]:
        raise BillingProblem("service_capacity_exhausted", "Application storage capacity is exhausted.", 503)
    if owner_id is not None and _storage_used(conn, owner_id) + size_bytes > policy.limits["storage_bytes"]:
        raise BillingProblem("storage_quota_exhausted", "Stored media allowance is exhausted.", 429)
    conn.execute(
        "INSERT INTO sceneit_storage_reservations(object_key,owner_id,size_bytes) VALUES(%s,%s,%s)",
        (object_key, owner_id, size_bytes))


def _audit(conn, action, reference, evidence):
    if not isinstance(evidence, str) or not 1 <= len(evidence.strip()) <= 500:
        raise ValueError("A redacted evidence reference is required")
    conn.execute("INSERT INTO sceneit_budget_audit(action,reference,evidence) VALUES(%s,%s,%s)",
                 (action, reference, evidence))


def release_storage(conn, object_key, evidence):
    """Journal proven object absence even while commercial admission is off."""
    if not isinstance(object_key, str) or not 1 <= len(object_key) <= 2048:
        raise ValueError("Invalid storage object reference")
    if not isinstance(evidence, str) or not 1 <= len(evidence.strip()) <= 500:
        raise ValueError("A redacted evidence reference is required")
    # Pre-migration pilot databases have no accounting ledger to maintain.
    # Once it exists, cleanup must never depend on billing configuration,
    # membership, pilot admission, or the operator stop.
    available = conn.execute(
        "SELECT to_regclass('sceneit_storage_reservations') IS NOT NULL AS available"
    ).fetchone()
    if not available or not available["available"]:
        return False
    _lock(conn)
    row = conn.execute(
        "INSERT INTO sceneit_storage_reservations"
        "(object_key,owner_id,size_bytes,state,released_at) "
        "SELECT %s,MIN(owner_id),GREATEST(COALESCE(MAX(GREATEST("
        "CASE WHEN upload_path=%s THEN COALESCE(upload_expected_bytes,200000000) ELSE 0 END,"
        "CASE WHEN media_path=%s THEN COALESCE(file_size_bytes,200000000) ELSE 0 END"
        ")),1),1),'released',clock_timestamp() "
        "FROM sceneit_imports WHERE upload_path=%s OR media_path=%s "
        "ON CONFLICT(object_key) DO UPDATE SET state='released',"
        "released_at=clock_timestamp() "
        "WHERE sceneit_storage_reservations.state='reserved' RETURNING object_key",
        (object_key, object_key, object_key, object_key, object_key),
    ).fetchone()
    # The insert is also a durable absence tombstone for unjournaled pilot
    # objects. Fallback inventory ignores it even if old import references
    # remain. Size 1 is only a positive sentinel when no historical size is
    # available; released rows never count toward occupancy.
    if row:
        _audit(conn, "storage_released", object_key, evidence)
    return bool(row)


def release_unused(conn, operation_id, evidence):
    """Operator-only: evidence must establish NO external work was submitted."""
    _lock(conn)
    rows = conn.execute(
        "SELECT * FROM sceneit_usage_reservations WHERE operation_id=%s AND state='reserved'",
        (operation_id,)).fetchall()
    for row in rows:
        scopes = [("app", row["app_window"])]
        if row["owner_id"]:
            scopes.append((f"owner:{row['owner_id']}", row["owner_window"]))
        for scope, window in scopes:
            conn.execute(
                "UPDATE sceneit_usage_windows SET used=used-%s "
                "WHERE scope=%s AND starts_at=%s AND metric=%s",
                (row["amount"], scope, window, row["metric"]))
    if rows:
        _audit(conn, "unused_released", operation_id, evidence)
        conn.execute(
            "UPDATE sceneit_usage_reservations SET state='released',released_at=clock_timestamp() "
            "WHERE operation_id=%s AND state='reserved'", (operation_id,))
    return len(rows)


def usage_status(owner_id, conn=None):
    policy = billing_settings()
    if not policy.enabled:
        return None
    if conn is None:
        with connection() as current:
            return usage_status(owner_id, current)
    now = _now(conn)
    # Status is usable after admission/coverage loss and never creates windows.
    account = conn.execute(
        "SELECT allowance_anchor FROM sceneit_billing_accounts WHERE owner_id=%s",
        (owner_id,)).fetchone()
    anchor = account and account["allowance_anchor"]
    start, end = monthly_window(anchor, now) if anchor and anchor <= now else (None, None)
    rows = conn.execute(
        "SELECT metric,used,allowance FROM sceneit_usage_windows WHERE scope=%s AND starts_at=%s",
        (f"owner:{owner_id}", start)).fetchall() if start else []
    by_metric = {row["metric"]: row for row in rows}

    def metric_status(metric):
        row = by_metric.get(metric, {})
        limit = min(policy.limits[metric], row.get("allowance", policy.limits[metric]))
        used = int(row.get("used", 0))
        return {"limit": limit, "used": used, "remaining": max(0, limit - used)}

    used = int(_storage_used(conn, owner_id))
    limit = policy.limits["storage_bytes"]
    control = conn.execute("SELECT stopped FROM sceneit_work_control WHERE singleton=true").fetchone()
    return {
        "windowStart": start.isoformat() if start else None,
        "windowEnd": end.isoformat() if end else None,
        "metrics": {metric: metric_status(metric) for metric in METRICS},
        "storage": {"limit": limit, "used": used, "remaining": max(0, limit - used)},
        "workStopped": not control or control["stopped"],
    }