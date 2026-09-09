"""Atomic, durable commercial reservations; no network calls in this module.

Monthly allowances follow the ACCOUNT's first paid anniversary, not subscription
IDs. All account/application changes lock one short PostgreSQL critical section.
Reservations survive timeouts, process death, cancellation and period rollover.
"""
import calendar
from datetime import datetime, timedelta, timezone

from .billing_config import BillingProblem, METRICS, billing_settings
from .billing_time import billing_now
from .config import settings
from .db import connection

UTC = timezone.utc
CAPABILITY_BY_METRIC = {
    "imports": "imports",
    "upload_attempts": "uploads",
    "analysis_seconds": "analysis",
    "searches": "searches",
    "media_bytes": "media",
    "frames": "frames",
}


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
    return billing_now(conn)


def _snapshot(row):
    """Validate the immutable entitlement facts saved with paid coverage."""
    limits = row.get("limits_snapshot")
    capabilities = row.get("capabilities_snapshot")
    rank = row.get("tier_rank")
    if (
        not isinstance(row.get("tier_key"), str)
        or type(rank) is not int or rank < 0
        or not isinstance(capabilities, list)
        or any(not isinstance(item, str) for item in capabilities)
        or len(capabilities) != len(set(capabilities))
        or not isinstance(limits, dict)
        or set(limits) != {*METRICS, "storage_bytes"}
        or any(type(value) is not int or not 0 < value <= 10**15
               for value in limits.values())
    ):
        return None
    return {
        "tier_key": row["tier_key"],
        "rank": rank,
        "capabilities": frozenset(capabilities),
        "limits": dict(limits),
        "coverage_id": row["id"],
        "owner_id": row["owner_id"],
        "subscription_id": row["subscription_id"],
        "coverage_kind": row["coverage_kind"],
        "funds_coverage_id": row["funds_coverage_id"],
        "starts_at": row["starts_at"],
        "ends_at": row["ends_at"],
        "cadence": row.get("cadence"),
        "currency": row.get("currency"),
        "price_id": row.get("price_id"),
        "subtotal": row.get("subtotal"),
        "tax": row.get("tax"),
        "total": row.get("total"),
        "amount_paid": row.get("amount_paid"),
        "tax_behavior": row.get("tax_behavior"),
        "provider_created_at": row.get("provider_created_at"),
        "provider_receipt_at": row.get("provider_receipt_at"),
        # Only a verified paid upgrade that is bound to an authenticated,
        # confirmed change may increase an already-created usage window.
        "can_raise_window": row["coverage_kind"] == "upgrade",
    }


def effective_coverage(conn, owner_id, now=None, *, subscription_id=None):
    """Resolve the highest valid coverage through its complete funding chain.

    Callers making a financial mutation must first lock the billing account.
    Read/admission callers may use the immutable returned snapshot directly.
    """
    now = billing_now(conn) if now is None else now
    rows = conn.execute(
        "SELECT c.*,EXISTS (SELECT 1 FROM sceneit_billing_changes ch "
        "WHERE ch.owner_id=c.owner_id AND ch.kind='upgrade' "
        "AND ch.state='effective' AND ch.funded_invoice_id=c.id) "
        "AS authenticated_upgrade FROM sceneit_paid_coverage c "
        "WHERE c.owner_id=%s AND NOT c.reversed AND c.starts_at<=%s AND c.ends_at>%s "
        "AND (%s::text IS NULL OR c.subscription_id=%s::text) "
        "ORDER BY c.tier_rank,c.provider_created_at NULLS FIRST,c.id",
        (owner_id, now, now, subscription_id, subscription_id),
    ).fetchall()
    # Catalog configuration accepts at most 20 tiers. A deeper active chain is
    # malformed and fails closed rather than consuming unbounded work.
    max_depth = 20
    valid = {}
    pending = []
    for row in rows:
        snapshot = _snapshot(row)
        if not snapshot:
            continue
        if row["coverage_kind"] == "period":
            valid[row["id"]] = (snapshot, 1)
        elif row["coverage_kind"] == "upgrade" and row["authenticated_upgrade"]:
            pending.append((row, snapshot))
    for _ in range(max_depth - 1):
        progressed = False
        remaining = []
        for row, snapshot in pending:
            parent = valid.get(row["funds_coverage_id"])
            if (
                parent
                and parent[1] < max_depth
                and parent[0]["owner_id"] == snapshot["owner_id"]
                and parent[0]["subscription_id"] == snapshot["subscription_id"]
                and snapshot["rank"] > parent[0]["rank"]
                and snapshot["starts_at"] >= parent[0]["starts_at"]
                and snapshot["ends_at"] <= parent[0]["ends_at"]
            ):
                valid[row["id"]] = (snapshot, parent[1] + 1)
                progressed = True
            else:
                remaining.append((row, snapshot))
        pending = remaining
        if not progressed:
            break
    if not valid:
        return None
    return max(
        (item[0] for item in valid.values()),
        key=lambda item: (item["rank"], item["ends_at"], item["coverage_id"]),
    )


def _effective_entitlement(conn, owner_id, now):
    """Compatibility wrapper for quota and existing billing callers."""
    return effective_coverage(conn, owner_id, now)


def _account(conn, owner_id, now, *, require_membership=True, capabilities=()):
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
        entitlement = (
            _effective_entitlement(conn, owner_id, now)
            if account and account["environment"] == billing_settings().environment
            else None
        )
        if not entitlement or not account["allowance_anchor"] or account["allowance_anchor"] > now:
            raise BillingProblem("membership_required", "Current paid coverage is required for new work.", 402)
        missing = set(capabilities) - entitlement["capabilities"]
        if missing:
            raise BillingProblem(
                "tier_capability_required",
                "Your current membership tier does not include this work.",
                403,
            )
        account = dict(account)
        account["entitlement"] = entitlement
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


def check_work(conn, owner_id, *, require_membership=True, capabilities=()):
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
    if isinstance(capabilities, str):
        capabilities = (capabilities,)
    if not isinstance(capabilities, (tuple, list, set, frozenset)):
        raise ValueError("Invalid capability requirement")
    return _account(
        conn, owner_id, _now(conn), require_membership=require_membership,
        capabilities=capabilities if require_membership else (),
    )


def _window(conn, scope, start, end, metric, limit, *, allow_raise=False):
    conn.execute(
        "INSERT INTO sceneit_usage_windows(scope,starts_at,ends_at,metric,allowance) "
        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (scope, start, end, metric, limit))
    row = conn.execute(
        "SELECT used,allowance FROM sceneit_usage_windows "
        "WHERE scope=%s AND starts_at=%s AND metric=%s FOR UPDATE",
        (scope, start, metric)).fetchone()
    if allow_raise and row["allowance"] < limit:
        row = conn.execute(
            "UPDATE sceneit_usage_windows SET allowance=%s "
            "WHERE scope=%s AND starts_at=%s AND metric=%s RETURNING used,allowance",
            (limit, scope, start, metric),
        ).fetchone()
    # A lower effective snapshot (for example, a downgrade/refunded upgrade)
    # restricts immediately. Only authenticated verified upgrade coverage sets
    # allow_raise; mutable legacy configuration never raises this window.
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
    capabilities = {
        CAPABILITY_BY_METRIC[metric] for metric in amounts
        if metric in CAPABILITY_BY_METRIC
    }
    account = check_work(
        conn, owner_id, require_membership=require_membership,
        capabilities=capabilities,
    )
    now = _now(conn)
    app_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    app_end = app_start + timedelta(days=1)
    # Shared proof never debits member allowance, even for a signed-in member.
    owner = owner_id if require_membership else None
    owner_start, owner_end = (
        monthly_window(account["allowance_anchor"], now) if owner else (None, None))
    entitlement = account.get("entitlement") if owner else None
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
            scopes.append((
                f"owner:{owner}", owner_start, owner_end,
                entitlement["limits"][metric],
            ))
        for scope, start, end, limit in scopes:
            used, allowed = _window(
                conn, scope, start, end, metric, limit,
                allow_raise=bool(
                    owner and scope != "app" and entitlement["can_raise_window"]
                ),
            )
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


def reserve_storage(
        conn, owner_id, object_key, size_bytes, *, require_membership=True,
        capability="uploads"):
    policy = billing_settings()
    if not policy.enabled:
        return
    if (type(size_bytes) is not int or not 1 <= size_bytes <= 10**15
            or not isinstance(object_key, str) or not 1 <= len(object_key) <= 2048):
        raise ValueError("Invalid storage reservation")
    # Worker calls retain the private import owner, but durable Firebase trials
    # consume only application capacity and have no paid billing account.
    # Normalize before both admission and occupancy identity so a caller cannot
    # accidentally dereference a nonexistent paid entitlement below.
    require_membership = _membership_required(
        conn, owner_id, require_membership
    )
    account = check_work(
        conn, owner_id, require_membership=require_membership,
        capabilities=(capability,) if capability else (),
    )
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
    owner_limit = (
        account["entitlement"]["limits"]["storage_bytes"]
        if owner_id is not None else None
    )
    if owner_id is not None and _storage_used(conn, owner_id) + size_bytes > owner_limit:
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
        "SELECT allowance_anchor,environment FROM sceneit_billing_accounts WHERE owner_id=%s",
        (owner_id,)).fetchone()
    anchor = account and account["allowance_anchor"]
    start, end = monthly_window(anchor, now) if anchor and anchor <= now else (None, None)
    rows = conn.execute(
        "SELECT metric,used,allowance FROM sceneit_usage_windows WHERE scope=%s AND starts_at=%s",
        (f"owner:{owner_id}", start)).fetchall() if start else []
    by_metric = {row["metric"]: row for row in rows}
    entitlement = (
        _effective_entitlement(conn, owner_id, now)
        if account and account["environment"] == policy.environment else None
    )

    def metric_status(metric):
        row = by_metric.get(metric, {})
        snapshot_limit = (
            entitlement["limits"][metric] if entitlement
            else row.get("allowance", 0)
        )
        limit = (
            snapshot_limit if entitlement and entitlement["can_raise_window"]
            else min(snapshot_limit, row.get("allowance", snapshot_limit))
        )
        used = int(row.get("used", 0))
        return {"limit": limit, "used": used, "remaining": max(0, limit - used)}

    used = int(_storage_used(conn, owner_id))
    limit = (
        entitlement["limits"]["storage_bytes"]
        if entitlement else 0
    )
    control = conn.execute("SELECT stopped FROM sceneit_work_control WHERE singleton=true").fetchone()
    return {
        "windowStart": start.isoformat() if start else None,
        "windowEnd": end.isoformat() if end else None,
        "metrics": {metric: metric_status(metric) for metric in METRICS},
        "storage": {"limit": limit, "used": used, "remaining": max(0, limit - used)},
        "workStopped": not control or control["stopped"],
    }