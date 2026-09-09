"""Always-on, leased worker for private imports.

Provider mutation markers are written before network calls. A lost response is
never purchased again automatically; an operator must reconcile it.
"""
import os
import math
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg

from .db import connection
from .import_limits import (ABANDONED_UPLOAD_SECONDS, MAX_BYTES,
                            MAX_DURATION_SECONDS, MIN_DURATION_SECONDS)
from .provider import ProviderError, TwelveLabsClient
from .storage import private_object_path

LEASE_SECONDS = 90
PROCESSING_DEADLINE_SECONDS = 1800
_active_worker_guard = None


class WorkerLockLost(RuntimeError):
    pass


def heartbeat():
    with connection() as conn:
        conn.execute(
            "UPDATE sceneit_import_app_usage SET worker_heartbeat_at=now() "
            "WHERE singleton=true")


def claim_job():
    token = uuid.uuid4()
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM sceneit_imports WHERE state IN "
            "('queued','resolving','validating','uploading','processing','indexing',"
            "'cancel_requested') AND next_attempt_at<=now() AND "
            "(lease_expires_at IS NULL OR lease_expires_at<now()) "
            "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
        if not row:
            return None
        return conn.execute(
            "UPDATE sceneit_imports SET lease_token=%s,"
            "lease_expires_at=now()+(%s * interval '1 second'),heartbeat_at=now(),"
            "attempts=attempts+1 WHERE id=%s RETURNING *",
            (token, LEASE_SECONDS, row["id"])).fetchone()


def _update(job, **values):
    allowed = {
        "state", "status_message", "progress_percent", "error_code", "title",
        "source_url", "external_id", "duration_seconds", "file_size_bytes",
        "has_audio", "width", "height", "sha256", "video_codec", "audio_codec",
        "media_path", "media_generation", "index_id", "asset_id",
        "indexed_asset_id", "provider_write_marker", "next_attempt_at",
        "processing_started_at", "read_failures",
    }
    if not values or not set(values).issubset(allowed):
        raise ValueError("Invalid import worker update")
    setters = ",".join(f"{key}=%s" for key in values)
    with connection() as conn:
        row = conn.execute(
            f"UPDATE sceneit_imports SET {setters},updated_at=now(),heartbeat_at=now(),"
            "lease_expires_at=now()+(%s * interval '1 second') "
            "WHERE id=%s AND lease_token=%s AND "
            "lease_expires_at>now() AND "
            "(state<>'cancel_requested' OR %s) RETURNING *",
            (*values.values(), LEASE_SECONDS, job["id"], job["lease_token"],
             job["state"] == "cancel_requested")).fetchone()
    if not row:
        raise RuntimeError("Import lease was lost")
    job.update(row)
    return job


def _finish_lease(job):
    with connection() as conn:
        conn.execute(
            "UPDATE sceneit_imports SET lease_token=NULL,lease_expires_at=NULL "
            "WHERE id=%s AND lease_token=%s", (job["id"], job["lease_token"]))


def _fail(job, code, message, state="failed", preserve_marker=False):
    values = dict(state=state, error_code=code, status_message=message,
                  progress_percent=None)
    if not preserve_marker:
        values["provider_write_marker"] = None
    _update(job, **values)


def _assert_fence(job, allow_cancel=False):
    if _active_worker_guard is not None:
        _active_worker_guard.check()
    with connection() as conn:
        row = conn.execute(
            "SELECT state,lease_token,lease_expires_at>now() AS lease_live "
            "FROM sceneit_imports WHERE id=%s",
            (job["id"],)).fetchone()
    if (not row or row["lease_token"] != job["lease_token"] or
            not row["lease_live"] or
            (row["state"] == "cancel_requested" and not allow_cancel)):
        raise RuntimeError("Import lease was lost")


def _confirmed_write(job, **values):
    """Persist a confirmed provider response without erasing a concurrent cancel."""
    allowed = {"index_id", "asset_id", "indexed_asset_id", "state",
               "status_message", "progress_percent"}
    if not values or not set(values).issubset(allowed):
        raise ValueError("Invalid confirmed provider update")
    state = values.pop("state", job["state"])
    setters = ",".join(f"{key}=%s" for key in values)
    prefix = f"{setters}," if setters else ""
    with connection() as conn:
        row = conn.execute(
            f"UPDATE sceneit_imports SET {prefix}provider_write_marker=NULL,"
            "state=CASE WHEN state='cancel_requested' THEN state ELSE %s END,"
            "updated_at=now(),heartbeat_at=now(),"
            "lease_expires_at=now()+(%s * interval '1 second') "
            "WHERE id=%s AND lease_token=%s AND lease_expires_at>now() RETURNING *",
            (*values.values(), state, LEASE_SECONDS, job["id"],
             job["lease_token"])).fetchone()
    if not row:
        raise RuntimeError("Import lease was lost")
    job.update(row)
    return job


def _renew_lease(job):
    """Retain an in-flight operation even when a cancellation arrives.

    Cancellation fences *new* operations, not recording/cleaning the result of
    an already-sent request. Only a still-live token may extend the lease.
    """
    if _active_worker_guard is not None:
        _active_worker_guard.check()
    with connection() as conn:
        result = conn.execute(
            "UPDATE sceneit_imports SET heartbeat_at=now(),"
            "lease_expires_at=now()+(%s * interval '1 second') "
            "WHERE id=%s AND lease_token=%s AND lease_expires_at>now()",
            (LEASE_SECONDS, job["id"], job["lease_token"]))
        if result.rowcount == 1:
            conn.execute(
                "UPDATE sceneit_import_app_usage SET worker_heartbeat_at=now() "
                "WHERE singleton=true")
        return result.rowcount == 1


@contextmanager
def _lease_heartbeat(job):
    stop = threading.Event()
    lost = threading.Event()

    def pulse():
        while not stop.wait(20):
            try:
                if not _renew_lease(job):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return

    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    try:
        yield
        if lost.is_set():
            raise RuntimeError("Import lease was lost")
    finally:
        stop.set()
        thread.join(timeout=1)


def _validate(job, local_path):
    from .inspect_media import inspect_mp4
    media = inspect_mp4(local_path)
    duration, size = float(media["duration"]), int(media["size"])
    if size > MAX_BYTES:
        raise ValueError("file_too_large")
    if duration < MIN_DURATION_SECONDS or duration > MAX_DURATION_SECONDS:
        raise ValueError("duration_out_of_range")
    duplicate = None
    with connection() as conn:
        ledger = conn.execute(
            "SELECT * FROM sceneit_import_fingerprints WHERE owner_id=%s AND sha256=%s",
            (job["owner_id"], media["sha256"])).fetchone()
        if ledger and ledger["status"] in ("uncertain", "deleted"):
            raise ValueError("fingerprint_not_repeatable")
        if ledger and ledger["status"] == "active":
            duplicate = ledger
        else:
            duplicate = conn.execute(
                "SELECT * FROM sceneit_imports WHERE owner_id=%s AND sha256=%s AND id<>%s "
                "AND state='ready' LIMIT 1",
                (job["owner_id"], media["sha256"], job["id"])).fetchone()
    values = {
        "duration_seconds": duration, "file_size_bytes": size,
        "has_audio": bool(media["hasAudio"]), "width": int(media["width"]),
        "height": int(media["height"]), "sha256": media["sha256"],
        "video_codec": media["videoCodec"], "audio_codec": media.get("audioCodec"),
    }
    if duplicate:
        # Reuse only identifiers already owned by this owner; no provider call.
        values.update({
            "index_id": duplicate["index_id"], "asset_id": duplicate["asset_id"],
            "indexed_asset_id": duplicate["indexed_asset_id"], "state": "ready",
            "media_path": duplicate["media_path"],
            "media_generation": duplicate["media_generation"],
            "status_message": "Ready (reused your existing analysis).",
            "progress_percent": 100,
        })
    _update(job, **values)
    return bool(duplicate)


def _prepare_media(job):
    from .private_storage import download_object, object_info, upload_private
    from google.api_core.exceptions import NotFound
    temp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    temp.close()
    try:
        # A direct-link upload journals its create-only destination before GCS
        # mutation. On restart, reconcile a completed generation or safely retry
        # the absent object at the same intended path.
        if job["media_path"] and not job["media_generation"]:
            intended_path = job["media_path"]
            try:
                pending = object_info(intended_path)
            except NotFound:
                pending = None
            if pending:
                download_object(
                    intended_path, temp.name, max_bytes=MAX_BYTES,
                    generation=pending["generation"])
                if _validate(job, temp.name):
                    from .private_storage import delete_object
                    delete_object(
                        intended_path, generation=pending["generation"])
                    return
                _update(
                    job, media_generation=str(pending["generation"]),
                    state="processing",
                    status_message="Recovered validated private media.",
                    progress_percent=None)
                return
        if job["upload_path"]:
            _update(job, state="validating", status_message="Validating uploaded MP4.",
                    progress_percent=None)
            download_object(job["upload_path"], temp.name, max_bytes=MAX_BYTES,
                            generation=job["upload_generation"])
            if _validate(job, temp.name):
                if not job["media_path"]:
                    _update(job, media_path=job["upload_path"],
                            media_generation=job["upload_generation"])
                return
            _update(job, media_path=job["upload_path"],
                    media_generation=job["upload_generation"], state="processing",
                    status_message="Validated; preparing private analysis.",
                    progress_percent=None)
        else:
            _update(job, state="resolving", status_message="Resolving permitted video media.",
                    progress_percent=None)
            from .platforms import resolve_link
            resolved = resolve_link(
                {"sourceKind": job["source_kind"], "sourceUrl": job["source_url"],
                 "externalId": job["external_id"], "title": job["title"]},
                temp.name,
                progress=lambda done, total=None: _update(
                    job, progress_percent=(float(done) / float(total) * 100)
                    if total else None))
            _update(job, title=resolved.get("title") or job["title"],
                    source_url=resolved.get("sourceUrl") or job["source_url"],
                    external_id=resolved.get("externalId") or job["external_id"],
                    state="validating", status_message="Validating resolved MP4.",
                    progress_percent=None)
            if _validate(job, temp.name):
                return
            path = private_object_path(f"imports/{job['id']}/source.mp4")
            _update(
                job, media_path=path, media_generation=None,
                status_message="Saving validated private media.",
                progress_percent=None)
            info = upload_private(temp.name, path)
            _update(job, media_generation=str(info["generation"]),
                    state="processing", status_message="Validated; preparing private analysis.",
                    progress_percent=None)
    finally:
        try:
            os.unlink(temp.name)
        except OSError:
            pass


def _provider_step(job, client):
    # Any persisted marker seen on re-entry represents an interrupted mutation.
    if job["provider_write_marker"]:
        _fail(job, "provider_write_uncertain",
              "A provider write may have completed; operator reconciliation is required.",
              "needs_review", preserve_marker=True)
        return
    if not job["index_id"]:
        _assert_fence(job)
        with connection() as conn:
            conn.execute(
                "INSERT INTO sceneit_import_fingerprints(owner_id,sha256,status) "
                "VALUES (%s,%s,'uncertain') ON CONFLICT(owner_id,sha256) "
                "DO UPDATE SET status='uncertain',updated_at=now()",
                (job["owner_id"], job["sha256"]))
        _update(job, state="processing", provider_write_marker="create_index",
                status_message="Creating private analysis index.", progress_percent=None)
        _assert_fence(job)
        result = client.create_index(f"sceneit-{job['id']}", has_audio=job["has_audio"])
        _confirmed_write(
            job, index_id=result["_id"], state="processing",
            status_message="Uploading authorized media for analysis.",
            progress_percent=None)
        return
    if not job["asset_id"]:
        from .private_storage import download_object
        temp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        temp.close()
        try:
            download_object(job["media_path"], temp.name, max_bytes=MAX_BYTES,
                            generation=job["media_generation"])
            _update(job, state="uploading", provider_write_marker="upload_asset",
                    status_message="Uploading authorized media for analysis.",
                    progress_percent=None)
            _assert_fence(job)
            result = client.upload_asset(temp.name, {
                "sceneit_import_id": str(job["id"]),
            })
            _confirmed_write(
                job, asset_id=result["_id"], state="processing",
                status_message="Provider is processing the media.",
                progress_percent=None)
        finally:
            try:
                os.unlink(temp.name)
            except OSError:
                pass
        return
    if not job["indexed_asset_id"]:
        _assert_fence(job)
        asset = client.get_asset(job["asset_id"])
        status = str(asset.get("status", "")).lower()
        if status in ("failed", "error"):
            _fail(job, "provider_asset_failed", "The provider could not process this MP4.")
            return
        if status not in ("ready", "completed"):
            _update(job, state="processing",
                    status_message="Provider is processing the media.",
                    next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=15))
            return
        _update(job, state="indexing", provider_write_marker="index_asset",
                status_message="Indexing searchable scenes.", progress_percent=None)
        _assert_fence(job)
        result = client.index_asset(job["index_id"], job["asset_id"], {
            "sceneit_import_id": str(job["id"])})
        _confirmed_write(
            job, indexed_asset_id=result["_id"], state="indexing",
            status_message="Indexing searchable scenes.", progress_percent=None)
        return
    _assert_fence(job)
    indexed = client.get_indexed_asset(job["index_id"], job["indexed_asset_id"])
    status = str(indexed.get("status", "")).lower()
    if status in ("failed", "error"):
        _fail(job, "provider_index_failed", "The provider could not index this MP4.")
    elif status in ("ready", "completed", "indexed"):
        _assert_fence(job)
        asset = client.get_asset(job["asset_id"])
        mapped_asset = (indexed.get("asset_id") or indexed.get("source_asset_id"))
        if mapped_asset is not None and mapped_asset != job["asset_id"]:
            _fail(job, "provider_identity_mismatch",
                  "The indexed media identity did not match the uploaded asset.")
            return
        def duration_of(value):
            for metadata in (value.get("system_metadata"), value.get("video_metadata"),
                             value.get("metadata"), value):
                candidate = metadata.get("duration") if isinstance(metadata, dict) else None
                if (isinstance(candidate, (int, float)) and not isinstance(candidate, bool)
                        and math.isfinite(candidate) and candidate > 0):
                    return float(candidate)
            return None
        provider_duration = duration_of(indexed) or duration_of(asset)
        if provider_duration is None or abs(
                provider_duration - float(job["duration_seconds"])) > 1:
            _fail(job, "provider_duration_mismatch",
                  "The provider duration could not be verified against the source.")
            return
        _update(job, state="ready", status_message="Ready to search.",
                progress_percent=100, error_code=None)
        with connection() as conn:
            conn.execute(
                "INSERT INTO sceneit_import_fingerprints"
                "(owner_id,sha256,status,index_id,asset_id,indexed_asset_id,"
                "media_path,media_generation) VALUES "
                "(%s,%s,'active',%s,%s,%s,%s,%s) "
                "ON CONFLICT(owner_id,sha256) DO UPDATE SET status='active',"
                "index_id=excluded.index_id,asset_id=excluded.asset_id,"
                "indexed_asset_id=excluded.indexed_asset_id,"
                "media_path=excluded.media_path,media_generation=excluded.media_generation,"
                "updated_at=now()",
                (job["owner_id"], job["sha256"], job["index_id"], job["asset_id"],
                 job["indexed_asset_id"], job["media_path"], job["media_generation"]))
    else:
        _update(job, state="indexing", status_message="Indexing searchable scenes.",
                progress_percent=None,
                next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=15))


def _cancel(job, client):
    """Delete provider resources in dependency order; failures remain reviewable."""
    try:
        if job.get("provider_write_marker"):
            _fail(job, "provider_write_uncertain",
                  "Cleanup is blocked until the in-flight provider write is reconciled.",
                  "needs_review", preserve_marker=True)
            return
        from .private_storage import (
            cancel_upload_session, decrypt_upload_session, delete_object,
            object_info,
        )
        from google.api_core.exceptions import NotFound
        if job.get("upload_session_reference"):
            cancel_upload_session(
                decrypt_upload_session(job["upload_session_reference"]))
        # Fingerprint deduplication may reuse the owner's existing provider IDs
        # and media. The last reference performs cleanup.
        with connection() as conn:
            shared = conn.execute(
                "SELECT 1 FROM sceneit_imports WHERE id<>%s AND "
                "state NOT IN ('cancelled','expired') AND "
                "((indexed_asset_id IS NOT NULL AND indexed_asset_id=%s) OR "
                "(media_path IS NOT NULL AND media_path=%s)) LIMIT 1",
                (job["id"], job["indexed_asset_id"], job["media_path"])).fetchone()
        if not shared:
            if job["indexed_asset_id"]:
                _update(
                    job,
                    provider_write_marker=f"delete_indexed_asset:{job['indexed_asset_id']}")
                _assert_fence(job, allow_cancel=True)
                client.delete_indexed_asset(job["index_id"], job["indexed_asset_id"])
                _confirmed_write(job, indexed_asset_id=None, state="cancel_requested")
            if job["asset_id"]:
                _update(job, provider_write_marker=f"delete_asset:{job['asset_id']}")
                _assert_fence(job, allow_cancel=True)
                client.delete_asset(job["asset_id"])
                _confirmed_write(job, asset_id=None, state="cancel_requested")
            if job["index_id"]:
                _update(job, provider_write_marker=f"delete_index:{job['index_id']}")
                _assert_fence(job, allow_cancel=True)
                client.delete_index(job["index_id"])
                _confirmed_write(job, index_id=None, state="cancel_requested")
            if job["media_path"]:
                try:
                    info = object_info(job["media_path"])
                    generation = job["media_generation"] or info["generation"]
                    delete_object(job["media_path"], generation=generation)
                except NotFound:
                    pass
        if job["upload_path"] and job["upload_path"] != job["media_path"]:
            try:
                info = object_info(job["upload_path"])
                generation = job["upload_generation"] or info["generation"]
                delete_object(job["upload_path"], generation=generation)
            except NotFound:
                pass
        if not shared and job.get("sha256"):
            with connection() as conn:
                conn.execute(
                    "UPDATE sceneit_import_fingerprints SET status='deleted',"
                    "media_path=NULL,media_generation=NULL,updated_at=now() "
                    "WHERE owner_id=%s AND sha256=%s",
                    (job["owner_id"], job["sha256"]))
        final_state = "expired" if job["expires_at"] <= datetime.now(timezone.utc) else "cancelled"
        _update(job, state=final_state,
                status_message=("Import expired and was cleaned up." if final_state == "expired"
                                else "Import cancelled and cleaned up."),
                progress_percent=None)
    except Exception:
        uncertain = bool(job.get("provider_write_marker"))
        _fail(job, "cleanup_needs_review",
              "Cleanup could not be confirmed; operator review is required.",
              "needs_review", preserve_marker=uncertain)


def process_job(job, client=None):
    owned_client = None
    try:
        started = job.get("processing_started_at")
        if (job["state"] != "cancel_requested" and started and
                (datetime.now(timezone.utc) - started).total_seconds() >
                PROCESSING_DEADLINE_SECONDS):
            uncertain = bool(job.get("provider_write_marker"))
            _fail(
                job,
                "provider_write_uncertain" if uncertain
                else "processing_deadline_exceeded",
                "A provider write requires reconciliation before cleanup."
                if uncertain else
                "Import processing exceeded the 30 minute deadline.",
                "needs_review" if uncertain else "failed",
                preserve_marker=uncertain)
            return
        if not started:
            _update(job, processing_started_at=datetime.now(timezone.utc))
        with _lease_heartbeat(job):
            if job["state"] == "cancel_requested":
                if client is None and any(
                        job[key] for key in
                        ("index_id", "asset_id", "indexed_asset_id")):
                    owned_client = TwelveLabsClient()
                _cancel(job, client or owned_client)
            elif job["state"] in ("queued", "resolving", "validating"):
                _prepare_media(job)
            else:
                owned_client = None if client is not None else TwelveLabsClient()
                _provider_step(job, client or owned_client)
    except ProviderError as exc:
        if exc.ambiguous:
            _fail(job, "provider_write_uncertain",
                  "A provider write may have completed; operator reconciliation is required.",
                  "needs_review", preserve_marker=True)
        elif exc.retryable and job.get("read_failures", 0) < 5:
            _update(job, status_message="Temporary provider delay; retry scheduled.",
                    error_code=exc.code,
                    read_failures=job.get("read_failures", 0) + 1,
                    next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=30))
        else:
            _fail(job, exc.code, exc.message)
    except Exception as exc:
        if isinstance(exc, WorkerLockLost):
            raise
        if isinstance(exc, RuntimeError) and str(exc) == "Import lease was lost":
            return
        code = getattr(exc, "code", None) or (
            str(exc) if str(exc) in ("file_too_large", "duration_out_of_range")
            else "import_processing_failed")
        message = getattr(exc, "message", None) or {
            "file_too_large": "The MP4 exceeds the 200 MB limit.",
            "duration_out_of_range": "The MP4 must be between 4 seconds and 20 minutes.",
        }.get(code, "The import could not be processed.")
        if code == "file_required":
            _update(job, state="file_required", error_code=code,
                    status_message=message, progress_percent=None,
                    provider_write_marker=None)
        else:
            uncertain = bool(job.get("provider_write_marker"))
            _fail(job, code if not uncertain else "provider_write_uncertain",
                  message if not uncertain else
                  "A provider write may have completed; operator reconciliation is required.",
                  "needs_review" if uncertain else "failed",
                  preserve_marker=uncertain)
    finally:
        if owned_client is not None:
            owned_client.close()
        _finish_lease(job)


def cleanup_expired():
    """Queue confirmed cleanup without deleting cumulative usage counters."""
    with connection() as conn:
        conn.execute(
            "UPDATE sceneit_imports SET state='cancel_requested',"
            "status_message='Retention expired; cleanup requested.',updated_at=now() "
            "WHERE expires_at<now() AND state NOT IN "
            "('expired','cancel_requested','cancelled','needs_review')")
        conn.execute(
            "UPDATE sceneit_imports SET state='cancel_requested',"
            "status_message='Upload reservation expired; cleanup requested.',updated_at=now() "
            "WHERE state='awaiting_upload' AND upload_expires_at < now()")
        conn.execute(
            "UPDATE sceneit_imports SET state='cancel_requested',"
            "status_message='Inactive import expired; cleanup requested.',updated_at=now() "
            "WHERE state IN ('file_required','awaiting_upload') AND "
            "created_at < now()-(%s * interval '1 second')",
            (ABANDONED_UPLOAD_SECONDS,))
        conn.execute(
            "DELETE FROM sceneit_imports WHERE "
            "(state='expired' OR (state='cancelled' AND expires_at<now())) "
            "AND lease_token IS NULL")


class _WorkerLockGuard:
    def __init__(self, conn):
        self.conn = conn
        self._mutex = threading.Lock()

    def check(self):
        try:
            with self._mutex:
                held = self.conn.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE "
                    "locktype='advisory' AND pid=pg_backend_pid() AND granted "
                    "AND classid=((hashtext('sceneit-private-import-worker')::bigint "
                    ">> 32) & 4294967295)::oid AND objid="
                    "(hashtext('sceneit-private-import-worker')::bigint "
                    "& 4294967295)::oid)").fetchone()[0]
        except Exception:
            raise WorkerLockLost(
                "Private import worker advisory lock was lost") from None
        if not held:
            raise WorkerLockLost("Private import worker advisory lock was lost")


@contextmanager
def worker_lock():
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext('sceneit-private-import-worker'))"
        ).fetchone()[0]
        if not locked:
            raise RuntimeError("Another private import worker is active")
        guard = _WorkerLockGuard(conn)
        try:
            guard.check()
            yield guard
        finally:
            try:
                conn.execute(
                    "SELECT pg_advisory_unlock("
                    "hashtext('sceneit-private-import-worker'))")
            except Exception:
                pass


def run_forever():
    global _active_worker_guard
    with worker_lock() as guard:
        _active_worker_guard = guard
        try:
            while True:
                guard.check()
                heartbeat()
                cleanup_expired()
                job = claim_job()
                if job:
                    process_job(job)
                else:
                    time.sleep(5)
        finally:
            _active_worker_guard = None


if __name__ == "__main__":
    run_forever()