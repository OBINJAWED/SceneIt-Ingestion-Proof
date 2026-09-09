"""Private import HTTP API. This blueprint never touches the legacy proof."""
import io
import subprocess
import uuid
from datetime import datetime

from flask import Blueprint, g, has_request_context, jsonify, request, send_file
from pydantic import (BaseModel, ConfigDict, Field, StrictBool, StrictInt,
                      ValidationError, field_validator)
from typing import Literal

from .db import connection
from .import_limits import (
    APP_IMPORT_LIMIT, APP_SEARCH_LIMIT, MAX_BYTES, MAX_DURATION_SECONDS,
    MIN_DURATION_SECONDS, OWNER_IMPORT_LIMIT, OWNER_SEARCH_LIMIT,
    RETENTION_DAYS, ImportProblem, reserve_import_budget,
    reserve_import_operation, usage_values, uses_lifetime_allowance,
)
from .import_search import list_import_searches, search_import
from .storage import private_object_path

imports_bp = Blueprint("imports", __name__)


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

def _require_new_private_work():
    # Direct provider-free transaction tests have no request capability to
    # validate. Every HTTP new-work path does.
    if has_request_context():
        from .auth import require_new_private_work
        require_new_private_work()


def _usage(conn, owner_id):
    if uses_lifetime_allowance(conn, owner_id):
        usage = usage_values(conn, owner_id)
        return {
            **usage,
            "import_limit": OWNER_IMPORT_LIMIT,
            "search_limit": OWNER_SEARCH_LIMIT,
            "quota_mode": "lifetime",
        }
    from .billing_config import billing_settings
    settings = billing_settings()
    commercial = settings["enabled"] if isinstance(settings, dict) else settings.enabled
    if commercial:
        from .quota import usage_status
        status = usage_status(owner_id, conn)
        return {
            "imports_used": status["metrics"]["imports"]["used"],
            "searches_used": status["metrics"]["searches"]["used"],
            "import_limit": status["metrics"]["imports"]["limit"],
            "search_limit": status["metrics"]["searches"]["limit"],
            "quota_mode": "monthly",
        }
    return {**usage_values(conn, owner_id), "quota_mode": "lifetime"}


def present_import(row, usage=None):
    usage = usage or {"imports_used": 0, "searches_used": 0}
    from .billing_config import billing_settings
    billing = billing_settings()
    commercial = billing["enabled"] if isinstance(billing, dict) else billing.enabled
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
        "budgetReserved": row.get("budget_reserved", False),
        "timelineStatus": "unverified" if row["source_url"] else "not_applicable",
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
        "expiresAt": row["expires_at"].isoformat(),
        "searchesUsed": usage["searches_used"],
        "searchLimit": usage.get("search_limit", OWNER_SEARCH_LIMIT),
        "importsUsed": usage["imports_used"],
        "importLimit": usage.get("import_limit", OWNER_IMPORT_LIMIT),
        "quotaMode": usage.get(
            "quota_mode", "monthly" if commercial else "lifetime"),
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
    from .billing_config import billing_settings
    billing = billing_settings()
    commercial = billing["enabled"] if isinstance(billing, dict) else billing.enabled
    # The signed-in Firebase capability is a lifetime trial even when the
    # separately controlled Replit subscription capability is enabled.
    session = getattr(g, "auth_session", None) or {}
    commercial = commercial and session.get("provider", "replit") != "firebase"
    owner_limits = billing["limits"] if isinstance(billing, dict) else billing.limits
    app_limits = billing["app_limits"] if isinstance(billing, dict) else billing.app_limits
    return jsonify({
        "maxBytes": MAX_BYTES, "minDurationSeconds": MIN_DURATION_SECONDS,
        "maxDurationSeconds": MAX_DURATION_SECONDS, "retentionDays": RETENTION_DAYS,
        "ownerImportLimit": (
            owner_limits["imports"] if commercial else OWNER_IMPORT_LIMIT),
        "appImportLimit": (
            app_limits["imports"] if commercial else APP_IMPORT_LIMIT),
        "ownerSearchLimit": (
            owner_limits["searches"] if commercial else OWNER_SEARCH_LIMIT),
        "appSearchLimit": (
            app_limits["searches"] if commercial else APP_SEARCH_LIMIT),
        "quotaMode": "monthly" if commercial else "lifetime",
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
        _require_new_private_work()
        from .billing_config import billing_settings
        if billing_settings().enabled and not uses_lifetime_allowance(conn, owner):
            from .quota import check_work
            check_work(conn, owner, capabilities=("imports",))
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
            ok, code = reserve_import_operation(
                conn, owner, f"import:{import_id}")
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
    from .private_storage import reserve_upload as storage_reserve
    from .upload_attempts import (
        UploadAttemptBusy, create_attempt, mark_uncertain, record_session)
    attempt_id = uuid.uuid4()
    path = private_object_path(
        f"imports/{import_id}/uploads/{uuid.uuid4()}.mp4")
    with connection() as conn:
        row = _owned(conn, owner, import_id, True)
        if row["state"] not in ("file_required", "awaiting_upload"):
            raise ImportProblem("upload_not_allowed", "This import is not awaiting a file.", 409)
        _require_new_private_work()
        if not row["budget_reserved"]:
            ok, code = reserve_import_operation(
                conn, owner, f"import:{import_id}")
            if not ok:
                raise ImportProblem(code, "The cumulative import allowance has been reached.", 429)
        from .billing_config import billing_settings
        settings = billing_settings()
        commercial = settings["enabled"] if isinstance(settings, dict) else settings.enabled
        if commercial:
            from .quota import reserve, reserve_storage
            trial = uses_lifetime_allowance(conn, owner)
            reserve(
                conn, owner, f"import:{import_id}:upload:{path}",
                {"upload_attempts": 1}, require_membership=not trial)
            reserve_storage(
                conn, None if trial else owner, path, body.sizeBytes,
                require_membership=not trial,
                capability="uploads" if not trial else None,
            )
        try:
            create_attempt(
                conn, import_id, owner, attempt_id, path, body.sizeBytes)
        except UploadAttemptBusy:
            raise ImportProblem(
                "upload_attempt_pending",
                "The previous upload initiation must be reconciled first.",
                409) from None
    # Quota and occupancy transactions must commit before any storage call.
    try:
        reservation = storage_reserve(path, body.sizeBytes)
    except Exception:
        # Initiation exceptions are ambiguous. Never release quota/occupancy or
        # infer safety from an absent object.
        mark_uncertain(attempt_id, connection)
        from .http import problem_response
        return problem_response(
            "A constrained private upload reservation could not be prepared.",
            "upload_reservation_unavailable", 503,
        )
    with connection() as conn:
        current = _owned(conn, owner, import_id, True)
        session_reference = reservation.pop("sessionReference")
        expires_at = datetime.fromisoformat(reservation["expiresAt"])
        record_session(conn, attempt_id, session_reference, expires_at)
        display_title = current["title"]
        if current["source_kind"] == "file":
            display_title = body.fileName.replace("\\", "/").rsplit("/", 1)[-1][:255]
        row = conn.execute(
            "UPDATE sceneit_imports SET state='awaiting_upload',"
            "status_message='Upload reserved; validation has not started.',"
            "upload_path=%s,upload_expected_bytes=%s,upload_reserved_at=now(),"
            "upload_expires_at=%s,upload_session_reference=%s,budget_reserved=true,"
            "title=%s,updated_at=now() "
            "WHERE id=%s AND upload_attempt_id=%s "
            "AND state IN ('file_required','awaiting_upload') RETURNING *",
            (path, body.sizeBytes, expires_at, session_reference, display_title,
             import_id, attempt_id)).fetchone()
        if not row:
            # record_session persisted the secret even if cancellation or a
            # newer fence won. Ensure worker cleanup is requested; never return
            # a bearer URL to a cancelled request.
            conn.execute(
                "UPDATE sceneit_upload_attempts SET state='revoke_requested',"
                "updated_at=now() WHERE id=%s AND state='active'", (attempt_id,))
            usage = None
        else:
            usage = _usage(conn, owner)
    if not row:
        from .http import problem_response
        return problem_response(
            "The import was cancelled while the upload was being prepared.",
            "upload_cancelled", 409)
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
            from .http import problem_response
            return problem_response(
                "This upload reservation expired. Select the MP4 again.",
                "upload_reservation_expired", 409,
            )
        _require_new_private_work()
        trial = uses_lifetime_allowance(conn, owner)
        from .billing_config import billing_settings
        settings = billing_settings()
        commercial = settings["enabled"] if isinstance(settings, dict) else settings.enabled
        if commercial and not trial:
            from .quota import check_work
            check_work(conn, owner, capabilities=("uploads",))
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
            ok, code = reserve_import_operation(
                conn, owner, f"import:{import_id}")
            if not ok:
                raise ImportProblem(code, "The cumulative import allowance has been reached.", 429)
        if not row.get("upload_attempt_id"):
            raise ImportProblem(
                "upload_attempt_missing",
                "The upload attempt requires reconciliation.", 409)
        from .upload_attempts import record_generation
        if not record_generation(
                conn, row["upload_attempt_id"], str(info["generation"])):
            raise ImportProblem(
                "upload_attempt_not_active",
                "The upload attempt is no longer active.", 409)
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
        if row.get("upload_attempt_id"):
            from .upload_attempts import request_revocation
            request_revocation(conn, import_id)
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
        trial = uses_lifetime_allowance(conn, owner)
    if (row["state"] != "ready" or not row["playback_authorized"] or
            not row["media_path"]):
        raise ImportProblem("source_playback_unavailable", "Source playback is unavailable.", 404)
    if row["expires_at"] <= datetime.now(row["expires_at"].tzinfo):
        raise ImportProblem("import_expired", "Import expired.", 410)
    from .private_storage import open_private
    return open_private(
        row["media_path"], request.headers.get("Range"), row["media_generation"],
        owner_id=owner, operation_id=f"media:source:{uuid.uuid4()}",
        require_membership=not trial)


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
            "SELECT i.media_path,i.media_generation,i.file_size_bytes,i.expires_at,s.matches "
            "FROM sceneit_imports i "
            "JOIN sceneit_import_searches s ON s.import_id=i.id "
            "WHERE i.id=%s AND i.owner_id=%s AND s.id=%s AND s.owner_id=%s "
            "AND i.state='ready' AND s.state='done'",
            (import_id, owner, search_id, owner)).fetchone()
        trial = uses_lifetime_allowance(conn, owner)
    if not row or rank < 1 or rank > len(row["matches"]) or not row["media_path"]:
        raise ImportProblem("frame_not_found", "Source frame not found.", 404)
    if row.get("expires_at") and row["expires_at"] <= datetime.now(
            row["expires_at"].tzinfo):
        raise ImportProblem("import_expired", "Import expired.", 410)
    try:
        from .media import MAX_FRAME_BYTES, private_frame
        match = row["matches"][rank-1]
        midpoint = (match["startSeconds"] + match["endSeconds"]) / 2
        operation_id = f"media:frame:{uuid.uuid4()}"
        from .billing_config import billing_settings
        settings = billing_settings()
        commercial = settings["enabled"] if isinstance(settings, dict) else settings.enabled
        if commercial:
            from .quota import reserve
            with connection() as conn:
                reserve(
                    conn, owner, operation_id,
                    {
                        "frames": 1,
                        # ffmpeg reads the source and can emit at most this
                        # bounded response. Admit both before extraction.
                        "media_bytes": (
                            int(row["file_size_bytes"]) + MAX_FRAME_BYTES
                        ),
                    },
                    require_membership=not trial)
        output = private_frame(row["media_path"], row["media_generation"], midpoint)
        if not output:
            raise ImportProblem("frame_unavailable", "Source frame is unavailable.", 503)
        response = send_file(io.BytesIO(output), mimetype="image/jpeg")
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, RuntimeError) as error:
        from .resources import ResourceExhausted
        if isinstance(error, ResourceExhausted):
            raise ImportProblem("frame_busy", "Frame extraction is busy. Please refresh later.", 429) from None
        raise ImportProblem("frame_unavailable", "Source frame is unavailable.", 503) from None


@imports_bp.errorhandler(ImportProblem)
def import_error(error):
    from .http import problem_response

    return problem_response(error.message, error.code, error.status)
