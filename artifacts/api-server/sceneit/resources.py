"""Cross-process resource leases and participant throttles backed by Postgres.

Leases are acquired and released in short transactions. Callers must perform
network/media work only after the acquisition transaction has closed.
"""
import uuid
from contextlib import contextmanager

from .config import settings
from .db import connection


class ResourceExhausted(RuntimeError):
    def __init__(self, resource):
        self.resource = resource
        super().__init__(f"{resource} capacity is currently exhausted")


class ParticipantThrottled(RuntimeError):
    pass


def configured_limit(resource):
    config = settings()
    limits = {
        "search": config.search_permits,
        "media": config.media_permits,
        "import_worker": config.import_worker_permits,
    }
    try:
        return limits[resource]
    except KeyError as exc:
        raise ValueError("Unknown shared resource") from exc


def _lease_duration(lease_seconds):
    if lease_seconds is None:
        # The configured default is validated against the configured maximum.
        return settings().permit_lease_seconds
    elif lease_seconds < 5:
        # Explicit leases are used by deadline-aware callers (for example, the
        # 75-second proof search requests an 80-second lease). Reject token
        # durations that cannot safely cover useful work and cleanup.
        raise ValueError("Explicit resource leases must be at least 5 seconds")
    return lease_seconds


def acquire(resource, *, participant=None, holder=None, lease_seconds=None):
    """Acquire a renewable crash-expiring lease, returning its UUID or None."""
    limit = configured_limit(resource)
    lease_seconds = _lease_duration(lease_seconds)
    holder = holder or uuid.uuid4()
    with connection() as conn:
        # Serialize contenders for only this resource, across all processes.
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext('sceneit-resource:' || %s))",
            (resource,),
        )
        conn.execute(
            "DELETE FROM sceneit_resource_leases "
            "WHERE resource=%s AND expires_at <= now()", (resource,)
        )
        used = conn.execute(
            "SELECT count(*) AS n FROM sceneit_resource_leases WHERE resource=%s",
            (resource,),
        ).fetchone()["n"]
        if used >= limit:
            return None
        conn.execute(
            "INSERT INTO sceneit_resource_leases"
            "(resource,holder,participant,expires_at) "
            "VALUES (%s,%s,%s,now()+(%s * interval '1 second'))",
            (resource, holder, participant, lease_seconds),
        )
    return holder


def renew(resource, holder, *, lease_seconds=None):
    lease_seconds = _lease_duration(lease_seconds)
    with connection() as conn:
        row = conn.execute(
            "UPDATE sceneit_resource_leases "
            "SET expires_at=now()+(%s * interval '1 second') "
            "WHERE resource=%s AND holder=%s AND expires_at > now() RETURNING holder",
            (lease_seconds, resource, holder),
        ).fetchone()
    return bool(row)


def release(resource, holder):
    with connection() as conn:
        conn.execute(
            "DELETE FROM sceneit_resource_leases WHERE resource=%s AND holder=%s",
            (resource, holder),
        )


@contextmanager
def shared_permit(resource, *, participant=None, lease_seconds=None):
    holder = acquire(
        resource, participant=participant, lease_seconds=lease_seconds
    )
    if holder is None:
        raise ResourceExhausted(resource)
    try:
        yield holder
    finally:
        release(resource, holder)


def admit_participant(participant, action="request", *, limit=None, window_seconds=60):
    """Consume one shared fixed-window allowance; fail closed at the limit."""
    if not participant:
        raise ValueError("participant is required")
    limit = limit or settings().participant_requests_per_minute
    with connection() as conn:
        row = conn.execute(
            "INSERT INTO sceneit_participant_throttles"
            "(participant,action,window_started_at,used) VALUES (%s,%s,now(),1) "
            "ON CONFLICT (participant,action) DO UPDATE SET "
            "window_started_at=CASE WHEN "
            "sceneit_participant_throttles.window_started_at <= "
            "now()-(%s * interval '1 second') THEN now() "
            "ELSE sceneit_participant_throttles.window_started_at END,"
            "used=CASE WHEN sceneit_participant_throttles.window_started_at <= "
            "now()-(%s * interval '1 second') THEN 1 "
            "ELSE sceneit_participant_throttles.used+1 END RETURNING used",
            (participant, action, window_seconds, window_seconds),
        ).fetchone()
    if row["used"] > limit:
        raise ParticipantThrottled("Participant request limit reached")
    return limit - row["used"]