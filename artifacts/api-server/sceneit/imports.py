"""Private import HTTP API. This blueprint never touches the legacy proof."""
import io
import os
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request, send_file
from pydantic import (BaseModel, ConfigDict, Field, StrictBool, StrictInt,
                      ValidationError, field_validator)
from typing import Literal

from .db import connection
from .import_limits import (
    APP_IMPORT_LIMIT, APP_SEARCH_LIMIT, MAX_BYTES, MAX_DURATION_SECONDS,
    MIN_DURATION_SECONDS, OWNER_IMPORT_LIMIT, OWNER_SEARCH_LIMIT,
    RETENTION_DAYS, ImportProblem, reserve_import_budget,
)
from .import_search import list_import_searches, search_import
from .storage import private_object_path

imports_bp = Blueprint("imports", __name__)
_frame_slots = threading.BoundedSemaphore(2)


class CreateImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entryMethod: Literal["upload", "link"]
    sourceUrl: str | None = Field(default=None, max_length=2048)
    analysisAuthorized: StrictBool
    playbackAuthorized: StrictBool
    idempotencyKey: uuid.UUID

    @field_validator("analysisAuthorized")
    @classmethod
    def analysis_must_be_authorized(cls, value):
        if value is not True:
            raise ValueError("Analysis authorization is required.")
        return value


class UploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fileName: str = Field(min_length=1, max_length=255)
    sizeBytes: StrictInt = Field(gt=0, le=MAX_BYTES)
    contentType: Literal["video/mp4"]


class PlaybackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authorized: StrictBool


ACTIVE_STATES = (
    "file_required", "awaiting_upload", "queued", "resolving", "validating",
    "uploading", "processing", "indexing", "cancel_requested",
)


def _auth(write=False):
    # Imported lazily so this module remains independently testable.
    from .auth import require_csrf, require_owner
    owner = require_owner()
    if write:
        require_csrf()
    return owner


def _usage(conn, owner_id):
    owner = conn.execute(
        "SELECT imports_used,searches_used FROM sceneit_import_usage WHERE owner_id=%s",
        (owner_id,)).fetchone()
    return owner or {"imports_used": 0, "searches_used": 0}


def present_import(row, usage=None):
    usage = usage or {"imports_used": 0, "searches_used": 0}
    available = bool(
        row["media_path"] and row["playback_authorized"] and row["state"] == "ready"
        and row["expires_at"] > datetime.now(row["expires_at"].tzinfo)
    )
    return {
        "id": str(row["id"]), "title": row["title"] or "Uploaded MP4",
        "entryMethod": row["entry_method"], "sourceKind": row["source_kind"],
        "sourceUrl": row["source_url"], "externalId": row["external_id"],
        "state": row["state"], "statusMessage": row["status_message"],
        "progressPercent": row["progress_percent"], "errorCode": row["error_code"],
        "durationSeconds": row["duration_seconds"],
        "fileSizeBytes": row["file_size_bytes"], "hasAudio": row["has_audio"],
        "sourcePlaybackAvailable": available,
        "sourcePlaybackUrl": f"/api/imports/{row['id']}/source" if available else None,
        "playbackAuthorized": row["playback_authorized"],
        "timelineStatus": "unverified" if row["source_url"] else "not_applicable",
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
        "expiresAt": row["expires_at"].isoformat(),
        "searchesUsed": usage["searches_used"], "searchLimit": OWNER_SEARCH_LIMIT,
        "importsUsed": usage["imports_used"], "importLimit": OWNER_IMPORT_LIMIT,
    }


def _owned(conn, owner_id, import_id, lock=False):
    suffix = " FOR UPDATE" if lock else ""
    row = conn.execute(
        f"SELECT * FROM sceneit_imports WHERE id=%s AND owner_id=%s{suffix}",
        (import_id, owner_id)).fetchone()
    if not row:
        raise ImportProblem("import_not_found", "Import not found.", 404)
    return row


@imports_bp.get("/api/imports/config")
def import_config():
    available = False
    try:
        with connection() as conn:
            row = conn.execute(
                "SELECT worker_heartbeat_at > now()-interval '90 seconds' AS available "
                "FROM sceneit_import_app_usage WHERE singleton=true").fetchone()
            available = bool(row and row["available"])
    except Exception:
        available = False
    return jsonify({
        "maxBytes": MAX_BYTES, "minDurationSeconds": MIN_DURATION_SECONDS,
        "maxDurationSeconds": MAX_DURATION_SECONDS, "retentionDays": RETENTION_DAYS,
        "ownerImportLimit": OWNER_IMPORT_LIMIT, "appImportLimit": APP_IMPORT_LIMIT,
        "ownerSearchLimit": OWNER_SEARCH_LIMIT, "appSearchLimit": APP_SEARCH_LIMIT,
        "workerAvailable": available,
    })


@imports_bp.get("/api/imports/current")
def current_import():
    owner = _auth()
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM sceneit_imports WHERE owner_id=%s AND state <> 'expired' "
            "ORDER BY created_at DESC LIMIT 1", (owner,)).fetchone()
        usage = _usage(conn, owner)
    return jsonify(present_import(row, usage) if row else None)


@imports_bp.post("/api/imports")
def create_import():
    owner = _auth(True)
    try:
        body = CreateImport.model_validate(request.get_json())
    except ValidationError:
        raise ImportProblem("invalid_import", "Supply a valid import request.") from None
    if body.entryMethod == "link":
        if not body.sourceUrl:
            raise ImportProblem("source_url_required", "Paste a supported video link.")
        from .platforms import canonicalize_link
        try:
            source = canonicalize_link(body.sourceUrl)
        except Exception as exc:
            if hasattr(exc, "code"):
                raise ImportProblem(exc.code, exc.message) from None
            raise
    else:
        if body.sourceUrl is not None:
            raise ImportProblem("source_url_not_allowed",
                                "A standalone upload does not use a source link.")
        source = {"sourceKind": "file", "sourceUrl": None, "externalId": None,
                  "title": None, "message": "Select an authorized MP4 to continue."}
    import_id = uuid.uuid4()
    with connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (owner,))
        existing = conn.execute(
            "SELECT * FROM sceneit_imports WHERE owner_id=%s AND idempotency_key=%s",
            (owner, body.idempotencyKey)).fetchone()
        if existing:
            return jsonify(present_import(existing, _usage(conn, owner)))
        active = conn.execute(
            "SELECT 1 FROM sceneit_imports WHERE owner_id=%s AND state=ANY(%s) LIMIT 1",
            (owner, list(ACTIVE_STATES))).fetchone()
        if active:
            raise ImportProblem("import_in_progress",
                                "Finish or cancel the current import first.", 409)
        state = "file_required"
        message = source.get("message") or "An authorized MP4 is required."
        if body.entryMethod == "link" and source["sourceKind"] != "youtube":
            state, message = "queued", "Waiting for private link resolution."
        row = conn.execute(
            "INSERT INTO sceneit_imports"
            "(id,owner_id,idempotency_key,entry_method,source_kind,source_url,"
            "external_id,title,state,status_message,analysis_authorized,"
            "playback_authorized) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s) "
            "RETURNING *",
            (import_id, owner, body.idempotencyKey, body.entryMethod,
             source["sourceKind"], source.get("sourceUrl"), source.get("externalId"),
             source.get("title"), state, message, body.playbackAuthorized)).fetchone()
        if state == "queued":
            ok, code = reserve_import_budget(conn, owner)
            if not ok:
                raise ImportProblem(code, "The cumulative import allowance has been reached.", 429)
            row = conn.execute(
                "UPDATE sceneit_imports SET budget_reserved=true WHERE id=%s RETURNING *",
                (import_id,)).fetchone()
        usage = _usage(conn, owner)
    return jsonify(present_import(row, usage))


@imports_bp.get("/api/imports/<uuid:import_id>")
def get_import(import_id):
    owner = _auth()
    with connection() as conn:
        row = _owned(conn, owner, import_id)
        usage = _usage(conn, owner)
    return jsonify(present_import(row, usage))


@imports_bp.post("/api/imports/<uuid:import_id>/upload")
def reserve_upload(import_id):
    owner = _auth(True)
    try:
        body = UploadRequest.model_validate(request.get_json())
    except ValidationError:
        raise ImportProblem("invalid_upload", "Choose an MP4 no larger than 200 MB.") from None
    if not body.fileName.lower().endswith(".mp4"):
        raise ImportProblem("invalid_upload", "The selected file must have an .mp4 name.")
    from .private_storage import (
        cancel_upload_session, decrypt_upload_session, delete_object,
        object_info, reserve_upload as storage_reserve,
    )
    with connection() as conn:
        row = _owned(conn, owner, import_id, True)
        if row["state"] not in ("file_required", "awaiting_upload"):
            raise ImportProblem("upload_not_allowed", "This import is not awaiting a file.", 409)
        if not row["budget_reserved"]:
            ok, code = reserve_import_budget(conn, owner)
            if not ok:
                raise ImportProblem(code, "The cumulative import allowance has been reached.", 429)
        old_path = row["upload_path"]
        path = private_object_path(
            f"imports/{import_id}/uploads/{uuid.uuid4()}.mp4")
        # Re-selection revokes the prior bearer session and removes a completed
        # but unaccepted generation before issuing another create-only session.
        try:
            if row["upload_session_reference"]:
                cancel_upload_session(
                    decrypt_upload_session(row["upload_session_reference"]))
            if old_path:
                try:
                    stale = object_info(old_path)
                except Exception as exc:
                    if type(exc).__name__ != "NotFound":
                        raise
                else:
                    delete_object(old_path, generation=stale["generation"])
            reservation = storage_reserve(path, body.sizeBytes)
        except Exception:
            conn.execute(
                "UPDATE sceneit_imports SET budget_reserved=true,"
                "status_message='Private upload reservation is temporarily unavailable.',"
                "updated_at=now() WHERE id=%s", (import_id,))
            return jsonify(
                error="A constrained private upload reservation could not be prepared.",
                code="upload_reservation_unavailable"), 503
        session_reference = reservation.pop("sessionReference")
        display_title = row["title"]
        if row["source_kind"] == "file":
            display_title = body.fileName.replace("\\", "/").rsplit("/", 1)[-1][:255]
        row = conn.execute(
            "UPDATE sceneit_imports SET state='awaiting_upload',"
            "status_message='Upload reserved; validation has not started.',"
            "upload_path=%s,upload_expected_bytes=%s,upload_reserved_at=now(),"
            "upload_expires_at=%s,upload_session_reference=%s,budget_reserved=true,"
            "title=%s,updated_at=now() "
            "WHERE id=%s RETURNING *",
            (path, body.sizeBytes, datetime.fromisoformat(reservation["expiresAt"]),
             session_reference, display_title, import_id)).fetchone()
        usage = _usage(conn, owner)
    return jsonify({"import": present_import(row, usage), **reservation})


@imports_bp.post("/api/imports/<uuid:import_id>/complete")
def complete_upload(import_id):
    owner = _auth(True)
    if request.get_json() != {}:
        raise ImportProblem("invalid_request", "No completion fields are accepted.")
    from .private_storage import object_info
    with connection() as conn:
        row = _owned(conn, owner, import_id, True)
        if (row["state"] != "awaiting_upload" and row["upload_generation"] and
                row["budget_reserved"]):
            return jsonify(present_import(row, _usage(conn, owner)))
        if row["state"] != "awaiting_upload" or not row["upload_path"]:
            raise ImportProblem("upload_not_pending", "No upload is awaiting completion.", 409)
        if not row["upload_expires_at"] or row["upload_expires_at"] <= datetime.now(
                row["upload_expires_at"].tzinfo):
            conn.execute(
                "UPDATE sceneit_imports SET state='cancel_requested',"
                "status_message='Upload reservation expired; cleanup requested.',"
                "updated_at=now() WHERE id=%s", (import_id,))
            return jsonify(
                error="This upload reservation expired. Select the MP4 again.",
                code="upload_reservation_expired"), 409
        try:
            info = object_info(row["upload_path"])
        except Exception:
            raise ImportProblem("upload_incomplete",
                                "The uploaded object is missing or unavailable.", 409) from None
        if not info or int(info["size"]) != row["upload_expected_bytes"]:
            raise ImportProblem("upload_incomplete",
                                "The uploaded object is missing or has the wrong size.", 409)
        if info.get("contentType") != "video/mp4":
            raise ImportProblem("upload_content_type_invalid",
                                "The uploaded object is not an MP4.", 409)
        if not row["budget_reserved"]:
            ok, code = reserve_import_budget(conn, owner)
            if not ok:
                raise ImportProblem(code, "The cumulative import allowance has been reached.", 429)
        row = conn.execute(
            "UPDATE sceneit_imports SET state='queued',status_message='Queued for validation.',"
            "progress_percent=NULL,upload_generation=%s,budget_reserved=true,"
            "updated_at=now(),upload_session_reference=NULL WHERE id=%s RETURNING *",
            (str(info["generation"]), import_id)).fetchone()
        usage = _usage(conn, owner)
    return jsonify(present_import(row, usage))


@imports_bp.post("/api/imports/<uuid:import_id>/cancel")
def cancel_import(import_id):
    owner = _auth(True)
    with connection() as conn:
        row = _owned(conn, owner, import_id, True)
        if row["state"] in ("cancelled", "expired"):
            pass
        else:
            row = conn.execute(
                "UPDATE sceneit_imports SET state='cancel_requested',"
                "status_message='Cancellation and cleanup requested.',updated_at=now() "
                "WHERE id=%s RETURNING *", (import_id,)).fetchone()
        usage = _usage(conn, owner)
    return jsonify(present_import(row, usage))


@imports_bp.post("/api/imports/<uuid:import_id>/playback")
def authorize_playback(import_id):
    owner = _auth(True)
    try:
        body = PlaybackRequest.model_validate(request.get_json())
    except ValidationError:
        raise ImportProblem("invalid_playback", "Supply an authorized boolean.") from None
    with connection() as conn:
        _owned(conn, owner, import_id, True)
        row = conn.execute(
            "UPDATE sceneit_imports SET playback_authorized=%s,updated_at=now() "
            "WHERE id=%s RETURNING *", (body.authorized, import_id)).fetchone()
        usage = _usage(conn, owner)
    return jsonify(present_import(row, usage))


@imports_bp.get("/api/imports/<uuid:import_id>/source")
def import_source(import_id):
    owner = _auth()
    with connection() as conn:
        row = _owned(conn, owner, import_id)
    if (row["state"] != "ready" or not row["playback_authorized"] or
            not row["media_path"]):
        raise ImportProblem("source_playback_unavailable", "Source playback is unavailable.", 404)
    if row["expires_at"] <= datetime.now(row["expires_at"].tzinfo):
        raise ImportProblem("import_expired", "Import expired.", 410)
    from .private_storage import open_private
    return open_private(row["media_path"], request.headers.get("Range"),
                        row["media_generation"])


@imports_bp.get("/api/imports/<uuid:import_id>/searches")
def import_search_history(import_id):
    return jsonify(list_import_searches(_auth(), import_id))


@imports_bp.post("/api/imports/<uuid:import_id>/searches")
def submit_import_search(import_id):
    return jsonify(search_import(_auth(True), import_id, request.get_json()))


@imports_bp.get("/api/imports/<uuid:import_id>/searches/<uuid:search_id>/frames/<int:rank>")
def import_frame(import_id, search_id, rank):
    owner = _auth()
    with connection() as conn:
        row = conn.execute(
            "SELECT i.media_path,i.media_generation,i.expires_at,s.matches "
            "FROM sceneit_imports i "
            "JOIN sceneit_import_searches s ON s.import_id=i.id "
            "WHERE i.id=%s AND i.owner_id=%s AND s.id=%s AND s.owner_id=%s "
            "AND i.state='ready' AND s.state='done'",
            (import_id, owner, search_id, owner)).fetchone()
    if not row or rank < 1 or rank > len(row["matches"]) or not row["media_path"]:
        raise ImportProblem("frame_not_found", "Source frame not found.", 404)
    if row.get("expires_at") and row["expires_at"] <= datetime.now(
            row["expires_at"].tzinfo):
        raise ImportProblem("import_expired", "Import expired.", 410)
    if not _frame_slots.acquire(False):
        raise ImportProblem("frame_busy", "Frame extraction is busy.", 429)
    path = None
    try:
        from .private_storage import download_object
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as temp:
            path = temp.name
        download_object(row["media_path"], path, max_bytes=MAX_BYTES,
                        generation=row["media_generation"])
        match = row["matches"][rank-1]
        midpoint = (match["startSeconds"] + match["endSeconds"]) / 2
        output = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", str(midpoint), "-i", path,
             "-frames:v", "1", "-vf", "scale=640:-2", "-f", "image2pipe",
             "-vcodec", "mjpeg", "pipe:1"], capture_output=True, timeout=20,
            check=True).stdout
        if not output:
            raise ImportProblem("frame_unavailable", "Source frame is unavailable.", 503)
        response = send_file(io.BytesIO(output), mimetype="image/jpeg")
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        raise ImportProblem("frame_unavailable", "Source frame is unavailable.", 503) from None
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
        _frame_slots.release()


@imports_bp.errorhandler(ImportProblem)
def import_error(error):
    return jsonify(error=error.message, code=error.code), error.status