"""Durable fencing and confirmed cleanup for private upload attempts."""

from .db import connection


class UploadAttemptBusy(RuntimeError):
    pass


def create_attempt(conn, import_id, owner_id, attempt_id, object_key, size_bytes):
    pending = conn.execute(
        "SELECT 1 FROM sceneit_upload_attempts WHERE import_id=%s "
        "AND state IN ('initiating','uncertain') LIMIT 1",
        (import_id,)).fetchone()
    if pending:
        raise UploadAttemptBusy("An upload initiation is pending reconciliation")
    conn.execute(
        "UPDATE sceneit_upload_attempts SET state='revoke_requested',updated_at=now() "
        "WHERE import_id=%s AND state='active'", (import_id,))
    conn.execute(
        "INSERT INTO sceneit_upload_attempts"
        "(id,import_id,owner_id,object_key,size_bytes,state) "
        "VALUES(%s,%s,%s,%s,%s,'initiating')",
        (attempt_id, import_id, owner_id, object_key, size_bytes))
    conn.execute(
        "UPDATE sceneit_imports SET upload_attempt_id=%s,upload_path=%s,"
        "upload_expected_bytes=%s,upload_generation=NULL,"
        "upload_session_reference=NULL,upload_reserved_at=NULL,"
        "upload_expires_at=NULL,budget_reserved=true,"
        "status_message='Preparing private upload reservation.',updated_at=now() "
        "WHERE id=%s",
        (attempt_id, object_key, size_bytes, import_id))


def mark_uncertain(attempt_id, connection_factory=connection):
    with connection_factory() as conn:
        conn.execute(
            "UPDATE sceneit_upload_attempts SET state='uncertain',updated_at=now() "
            "WHERE id=%s AND state IN ('initiating','revoke_requested')",
            (attempt_id,))
        conn.execute(
            "UPDATE sceneit_imports SET "
            "status_message='Private upload initiation requires reconciliation.',"
            "updated_at=now() WHERE upload_attempt_id=%s "
            "AND state<>'cancel_requested'", (attempt_id,))


def record_session(conn, attempt_id, session_reference, expires_at):
    attempt = conn.execute(
        "UPDATE sceneit_upload_attempts SET session_reference=%s,expires_at=%s,"
        "state=CASE WHEN state='initiating' THEN 'active' "
        "ELSE 'revoke_requested' END,"
        "updated_at=now() WHERE id=%s AND state IN "
        "('initiating','revoke_requested','uncertain') RETURNING *",
        (session_reference, expires_at, attempt_id)).fetchone()
    if not attempt:
        raise RuntimeError("Upload attempt fence was lost")
    return attempt


def request_revocation(conn, import_id):
    conn.execute(
        "UPDATE sceneit_upload_attempts SET "
        "state=CASE WHEN state IN ('initiating','active') "
        "THEN 'revoke_requested' ELSE state END,updated_at=now() "
        "WHERE import_id=%s AND state<>'revoked'", (import_id,))


def adopt_legacy(conn, item, attempt_id):
    """Journal a pre-009/manual fixture path before any cleanup side effect."""
    if not item.get("upload_path"):
        return None
    existing = conn.execute(
        "SELECT id FROM sceneit_upload_attempts WHERE import_id=%s LIMIT 1",
        (item["id"],)).fetchone()
    if existing:
        return existing["id"]
    state = (
        "active" if item.get("upload_session_reference")
        or item.get("upload_generation") else "uncertain")
    conn.execute(
        "INSERT INTO sceneit_upload_attempts"
        "(id,import_id,owner_id,object_key,size_bytes,state,session_reference,"
        "generation,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (attempt_id, item["id"], item["owner_id"], item["upload_path"],
         max(1, int(item.get("upload_expected_bytes") or
                    item.get("file_size_bytes") or 200_000_000)),
         state, item.get("upload_session_reference"),
         item.get("upload_generation"), item.get("upload_expires_at")))
    conn.execute(
        "UPDATE sceneit_imports SET upload_attempt_id=%s WHERE id=%s",
        (attempt_id, item["id"]))
    return attempt_id


def record_generation(conn, attempt_id, generation):
    return conn.execute(
        "UPDATE sceneit_upload_attempts SET generation=%s,updated_at=now() "
        "WHERE id=%s AND state='active' RETURNING *",
        (str(generation), attempt_id)).fetchone()


def _live_media_reference(conn, attempt):
    return conn.execute(
        "SELECT 1 FROM sceneit_imports WHERE id<>%s AND media_path=%s "
        "AND state NOT IN ('cancel_requested','cancelled','expired','failed') "
        "LIMIT 1",
        (attempt["import_id"], attempt["object_key"])).fetchone()


def cleanup_attempt(attempt_id, connection_factory=connection):
    """Revoke a bearer, retaining shared media until its final live reference."""
    from google.api_core.exceptions import NotFound
    from .private_storage import (
        cancel_upload_session, decrypt_upload_session, delete_object, object_info)

    # Snapshot without holding database locks across session revocation.
    with connection_factory() as conn:
        attempt = conn.execute(
            "SELECT * FROM sceneit_upload_attempts WHERE id=%s FOR UPDATE",
            (attempt_id,)).fetchone()
        if not attempt or attempt["state"] == "revoked":
            return True
        if attempt["state"] != "revoke_requested":
            return False
        if (not attempt["session_reference"] and not attempt["generation"]
                and not attempt["revoked_at"]):
            conn.execute(
                "UPDATE sceneit_upload_attempts SET state='uncertain',"
                "updated_at=now() WHERE id=%s", (attempt_id,))
            return False
    if attempt["session_reference"]:
        cancel_upload_session(
            decrypt_upload_session(attempt["session_reference"]))

    # Lock imports before the attempt, matching the HTTP import->attempt order.
    # No quota/global lock is acquired in this transaction.
    with connection_factory() as conn:
        conn.execute(
            "SELECT id FROM sceneit_imports WHERE media_path=%s "
            "AND state NOT IN ('cancel_requested','cancelled','expired','failed') "
            "FOR SHARE", (attempt["object_key"],)).fetchall()
        current = conn.execute(
            "SELECT * FROM sceneit_upload_attempts WHERE id=%s FOR UPDATE",
            (attempt_id,)).fetchone()
        if not current or current["state"] == "revoked":
            return True
        if current["state"] != "revoke_requested":
            return False
        if current["session_reference"] != attempt["session_reference"]:
            return False
        if _live_media_reference(conn, current):
            # Session revocation is complete, but this immutable generation and
            # its occupancy belong to another live deduplicated import.
            conn.execute(
                "UPDATE sceneit_upload_attempts SET session_reference=NULL,"
                "updated_at=now() WHERE id=%s", (attempt_id,))
            return True
        try:
            info = object_info(current["object_key"])
            generation = current["generation"] or str(info["generation"])
            delete_object(current["object_key"], generation=generation)
        except NotFound:
            pass
        # revoked_at journals confirmed object absence before the independent
        # quota transaction. A crash can safely finish release on the next tick.
        conn.execute(
            "UPDATE sceneit_upload_attempts SET session_reference=NULL,"
            "generation=NULL,revoked_at=now(),updated_at=now() WHERE id=%s",
            (attempt_id,))

    # Global quota lock precedes the attempt row lock, the same order used by
    # reserve_storage followed by create_attempt.
    with connection_factory() as conn:
        from .quota import release_storage
        release_storage(
            conn, attempt["object_key"],
            f"confirmed upload attempt cleanup {attempt_id}")
        conn.execute(
            "UPDATE sceneit_upload_attempts SET state='revoked',"
            "revoked_at=COALESCE(revoked_at,now()),"
            "updated_at=now() WHERE id=%s", (attempt_id,))
        return True


def cleanup_requested(limit=10, connection_factory=connection):
    with connection_factory() as conn:
        rows = conn.execute(
            "SELECT id FROM sceneit_upload_attempts "
            "WHERE state='revoke_requested' ORDER BY updated_at LIMIT %s",
            (limit,)).fetchall()
    for row in rows:
        try:
            cleanup_attempt(row["id"], connection_factory)
        except Exception:
            # A timeout is not proof of revocation; the durable row remains.
            continue


def confirm_object_absence(conn, object_key, evidence):
    """Finalize occupancy after another fenced cleanup deleted the object."""
    from .quota import release_storage
    release_storage(conn, object_key, evidence)
    available = conn.execute(
        "SELECT to_regclass('sceneit_upload_attempts') IS NOT NULL AS available"
    ).fetchone()
    if not available or not available["available"]:
        return
    conn.execute(
        "UPDATE sceneit_upload_attempts SET state='revoked',"
        "revoked_at=COALESCE(revoked_at,now()),updated_at=now() "
        "WHERE object_key=%s AND state='revoke_requested' "
        "AND session_reference IS NULL",
        (object_key,))


def all_revoked(import_id, connection_factory=connection):
    with connection_factory() as conn:
        return not bool(conn.execute(
            "SELECT 1 FROM sceneit_upload_attempts WHERE import_id=%s "
            "AND state<>'revoked' LIMIT 1", (import_id,)).fetchone())