"""Protected proof media and persistent stills; HTTP never launches ffmpeg.

The existing import worker calls prepare_frames(). A bounded child process does
storage and extraction work. This keeps a wedged media tool or SDK outside web
processes and below the shared permit's lifetime.
"""
import argparse
import hashlib
import json
import logging
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import httpx
from flask import Blueprint, Response, jsonify, request, stream_with_context

from .db import PROOF_ID, connection
from .media import bounded_process
from .private_storage import download_object, open_private, upload_private
from .proof import ProofError
from .resources import ResourceExhausted, shared_permit
from .storage import private_object_path, safe_content_range, signed_url

proof_media_bp = Blueprint("proof_media", __name__)
logger = logging.getLogger("sceneit")
MAX_FRAME_GENERATION_ATTEMPTS = 3


@proof_media_bp.get("/api/proof/source")
def source_video():
    with connection() as conn:
        row = conn.execute(
            "SELECT media FROM sceneit_proofs WHERE id=%s", (PROOF_ID,)
        ).fetchone()
    playback = row and row["media"].get("sourcePlayback")
    if not playback or playback.get("permissionConfirmed") is not True or (
        playback.get("rightsPolicy") != "public-app-viewers"
    ):
        raise ProofError("source_playback_unavailable", "Source playback is not available.", 404)
    requested_range = request.headers.get("Range")
    byte_range = safe_content_range(requested_range)
    if requested_range and not byte_range:
        raise ProofError("invalid_range", "Only one valid byte range may be requested.", 416)
    from .billing_config import billing_settings
    settings = billing_settings()
    commercial = settings["enabled"] if isinstance(settings, dict) else settings.enabled
    operation_id = f"proof-media:{uuid.uuid4()}"
    source_size = row["media"].get("size")
    verified_size = (
        not isinstance(source_size, bool) and isinstance(source_size, int)
        and 1 <= source_size <= 200_000_000)
    if commercial and not verified_size:
        raise ProofError(
            "source_playback_unavailable",
            "A bounded source response could not be verified.", 503)
    from .private_storage import _range
    selected = _range(requested_range, source_size) if verified_size else None
    if verified_size and selected is False:
        from .http import problem_response
        response = problem_response(
            "The byte range is not satisfiable.",
            "range_not_satisfiable", 416)
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["Content-Range"] = f"bytes */{source_size}"
        return response
    expected_length = (
        (selected[1] - selected[0] + 1) if selected
        else source_size if verified_size else None)
    if commercial:
        from .quota import check_work, reserve
        with connection() as conn:
            check_work(conn, None, require_membership=False)
            reserve(
                conn, None, operation_id, {"media_bytes": expected_length},
                require_membership=False)
    client = httpx.Client(timeout=httpx.Timeout(20, connect=5))
    try:
        upstream = client.send(
            httpx.Request("GET", signed_url(playback["objectPath"], "GET"),
                          headers={"Range": byte_range} if byte_range else {}),
            stream=True,
        )
    except Exception:
        client.close()
        raise ProofError("source_playback_unavailable", "Source playback is temporarily unavailable.", 503) from None
    if upstream.status_code == 416:
        content_range = upstream.headers.get("Content-Range")
        upstream.close()
        client.close()
        from .http import problem_response
        response = problem_response(
            "The byte range is not satisfiable.",
            "range_not_satisfiable", 416,
        )
        response.headers["Accept-Ranges"] = "bytes"
        if content_range:
            response.headers["Content-Range"] = content_range
        return response
    if upstream.status_code != (206 if byte_range else 200):
        upstream.close()
        client.close()
        raise ProofError("source_playback_unavailable", "Source playback is temporarily unavailable.", 503)
    length = upstream.headers.get("Content-Length")
    if (not length or not length.isdigit()
            or (expected_length is not None and int(length) != expected_length)
            or int(length) > 200_000_000):
        upstream.close()
        client.close()
        raise ProofError(
            "source_playback_unavailable",
            "A bounded source response could not be verified.", 503)
    headers = {"Accept-Ranges": "bytes", "Content-Type": "video/mp4",
               "Cache-Control": "private, no-store"}
    for name in ("Content-Length", "Content-Range", "ETag"):
        if name in upstream.headers:
            headers[name] = upstream.headers[name]

    @stream_with_context
    def body():
        remaining = int(length)
        try:
            for chunk in upstream.iter_raw(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                if len(chunk) > remaining:
                    raise RuntimeError("Storage exceeded the proof response boundary")
                remaining -= len(chunk)
                yield chunk
            if remaining:
                raise RuntimeError("Storage ended the proof response unexpectedly")
        finally:
            upstream.close()
            client.close()

    response = Response(body(), status=upstream.status_code, headers=headers)
    # Also close resources if the server closes a HEAD or unconsumed response.
    response.call_on_close(upstream.close)
    response.call_on_close(client.close)
    return response


@proof_media_bp.get("/api/proof/searches/<uuid:search_id>/frames/<int:rank>")
def source_frame(search_id, rank):
    with connection() as conn:
        row = conn.execute(
            "SELECT s.matches,f.object_path FROM sceneit_searches s "
            "JOIN sceneit_proofs p ON p.id=s.proof_id "
            "LEFT JOIN sceneit_proof_frames f ON f.search_id=s.id AND f.rank=%s "
            "AND f.source_sha256=p.source_sha256 "
            "WHERE s.id=%s AND p.id=%s AND s.state='done'",
            (rank, search_id, PROOF_ID),
        ).fetchone()
    if not row or rank < 1 or rank > len(row["matches"]):
        raise ProofError("frame_not_found", "Source frame not found.", 404)
    if not row["object_path"]:
        raise ProofError(
            "frame_pending", "This still is being prepared by the media worker. Refresh it later.", 503)
    try:
        return open_private(
            row["object_path"], owner_id=None,
            operation_id=f"proof-frame-media:{uuid.uuid4()}",
            require_membership=False)
    except Exception as exc:
        from .billing_config import BillingProblem
        if isinstance(exc, BillingProblem):
            raise
        raise ProofError("frame_unavailable", "The saved still is temporarily unavailable. Please refresh later.", 503) from None


def _pending():
    with connection() as conn:
        return conn.execute(
            "SELECT s.id FROM sceneit_searches s JOIN sceneit_proofs p ON p.id=s.proof_id "
            "WHERE p.id=%s AND s.state='done' AND jsonb_array_length(s.matches)>0 "
            "AND (SELECT count(*) FROM sceneit_proof_frames f WHERE f.search_id=s.id "
            "AND f.source_sha256=p.source_sha256)<jsonb_array_length(s.matches) "
            "AND p.media->'sourcePlayback'->>'permissionConfirmed'='true' "
            "AND (SELECT count(*) FROM jsonb_array_elements(COALESCE("
            "p.media->'frameProcessing'->'attempts','[]'::jsonb)) a "
            "WHERE a->>'searchId'=s.id::text)<3 "
            "AND (p.media->'frameProcessing'->>'searchId' IS DISTINCT FROM s.id::text "
            "OR COALESCE((p.media->'frameProcessing'->>'retryAfter')::timestamptz,"
            "'epoch'::timestamptz)<now()) ORDER BY s.created_at LIMIT 1",
            (PROOF_ID,),
        ).fetchone()


def _commercial_enabled():
    from .billing_config import billing_settings
    settings = billing_settings()
    return settings["enabled"] if isinstance(settings, dict) else settings.enabled


def _check_shared_work():
    """Fence stopped shared work before permits, child processes, or storage."""
    if not _commercial_enabled():
        return
    from .quota import check_work
    with connection() as conn:
        check_work(conn, None, require_membership=False)


def _begin_frame_attempt(search_id):
    """Persist and meter one real child attempt immediately before it starts."""
    attempt_id = uuid.uuid4().hex
    with connection() as conn:
        row = conn.execute(
            "SELECT s.matches,p.media,p.source_sha256,"
            "(SELECT count(*) FROM sceneit_proof_frames f WHERE f.search_id=s.id "
            "AND f.source_sha256=p.source_sha256) AS completed "
            "FROM sceneit_searches s JOIN sceneit_proofs p ON p.id=s.proof_id "
            "WHERE s.id=%s AND p.id=%s AND s.state='done' FOR UPDATE OF p",
            (search_id, PROOF_ID),
        ).fetchone()
        if not row:
            return None
        processing = dict(row["media"].get("frameProcessing") or {})
        attempts = list(processing.get("attempts") or [])
        # A parent crash after commit and before/while spawning cannot prove no
        # child/storage work happened. Keep that purchase as uncertain.
        for old in attempts:
            if old.get("status") == "running":
                old["status"] = "uncertain"
        search_attempts = [
            attempt for attempt in attempts
            if attempt.get("searchId") == str(search_id)
        ]
        if len(search_attempts) >= MAX_FRAME_GENERATION_ATTEMPTS:
            processing.update({
                "status": "needs_review",
                "retryAfter": "9999-12-31T23:59:59+00:00",
                "attempts": attempts,
            })
            conn.execute(
                "UPDATE sceneit_proofs SET media=jsonb_set(media,'{frameProcessing}',"
                "%s::jsonb) WHERE id=%s", (json.dumps(processing), PROOF_ID))
            return None
        missing = len(row["matches"]) - int(row["completed"])
        if missing <= 0:
            return None
        source_size = row["media"].get("size")
        if (isinstance(source_size, bool) or not isinstance(source_size, int)
                or not 1 <= source_size <= 200_000_000):
            raise RuntimeError("Persistent source size is invalid")
        operation_id = f"proof-frames:{search_id}:{attempt_id}"
        if _commercial_enabled():
            from .quota import reserve
            # This is app-only shared-proof work. Each retry buys its own frame
            # generation and complete source-object download before any cost.
            # There is no analysis-provider call in still production, hence no
            # analysis_seconds reservation. These units do not claim to cap
            # baseline hosting or network costs outside application control.
            reserve(
                conn, None, operation_id,
                {"frames": missing, "media_bytes": source_size},
                require_membership=False)
        attempts.append({
            "id": attempt_id,
            "searchId": str(search_id),
            "operationId": operation_id,
            "status": "running",
        })
        processing.update({
            "status": "running", "searchId": str(search_id),
            "retryAfter": None, "attempts": attempts,
        })
        conn.execute(
            "UPDATE sceneit_proofs SET media=jsonb_set(media,'{frameProcessing}',"
            "%s::jsonb) WHERE id=%s", (json.dumps(processing), PROOF_ID))
    return attempt_id


def _finish_frame_attempt(attempt_id, succeeded):
    with connection() as conn:
        row = conn.execute(
            "SELECT media FROM sceneit_proofs WHERE id=%s FOR UPDATE",
            (PROOF_ID,)).fetchone()
        if not row:
            return
        processing = dict(row["media"].get("frameProcessing") or {})
        attempts = list(processing.get("attempts") or [])
        search_id = next((a.get("searchId") for a in attempts if a.get("id") == attempt_id), None)
        for attempt in attempts:
            if attempt.get("id") == attempt_id:
                attempt["status"] = "succeeded" if succeeded else "uncertain"
        exhausted = sum(a.get("searchId") == search_id for a in attempts) >= MAX_FRAME_GENERATION_ATTEMPTS
        processing.update({
            "status": (
                "complete" if succeeded else
                "needs_review" if exhausted else "degraded"),
            "retryAfter": (
                None if succeeded else
                "9999-12-31T23:59:59+00:00" if exhausted else
                # PostgreSQL supplies the authoritative retry time below.
                None),
            "attempts": attempts,
        })
        if not succeeded and not exhausted:
            conn.execute(
                "UPDATE sceneit_proofs SET media=jsonb_set("
                "jsonb_set(media,'{frameProcessing}',%s::jsonb),"
                "'{frameProcessing,retryAfter}',to_jsonb(now()+interval '5 minutes')) "
                "WHERE id=%s", (json.dumps(processing), PROOF_ID))
        else:
            conn.execute(
                "UPDATE sceneit_proofs SET media=jsonb_set(media,'{frameProcessing}',"
                "%s::jsonb) WHERE id=%s", (json.dumps(processing), PROOF_ID))


def prepare_frames():
    """Prepare at most one search per tick, with a persisted failure cooldown."""
    pending = _pending()
    if not pending:
        return
    attempt_id = None
    try:
        _check_shared_work()
        # Shared media budget includes HTTP import frames and worker inspection.
        # Child kill at 120s occurs well before this lease could expire.
        with shared_permit("media", lease_seconds=150):
            attempt_id = _begin_frame_attempt(pending["id"])
            if attempt_id is None:
                return
            with tempfile.TemporaryDirectory(prefix="sceneit-proof-frames-") as directory:
                result = bounded_process(
                    [sys.executable, "-m", "sceneit.proof_media",
                     str(pending["id"]), attempt_id, directory],
                    timeout=120,
                )
                if result:
                    raise RuntimeError("Frame preparation failed")
    except ResourceExhausted:
        return
    except Exception:
        if attempt_id:
            _finish_frame_attempt(attempt_id, False)
        logger.warning(json.dumps({"event": "proof_frames_degraded", "code": "frame_unavailable"}))
    else:
        if attempt_id:
            _finish_frame_attempt(attempt_id, True)
        logger.info(json.dumps({"event": "proof_frames_prepared"}))


def build_frames(search_id, attempt_id, directory):
    """Worker-only persistent generation. No Twelve Labs calls or workspace file."""
    with connection() as conn:
        row = conn.execute(
            "SELECT s.matches,p.source_sha256,p.media FROM sceneit_searches s "
            "JOIN sceneit_proofs p ON p.id=s.proof_id "
            "WHERE s.id=%s AND p.id=%s AND s.state='done'", (search_id, PROOF_ID),
        ).fetchone()
        existing = conn.execute(
            "SELECT rank FROM sceneit_proof_frames WHERE search_id=%s "
            "AND source_sha256=%s", (search_id, row["source_sha256"] if row else ""),
        ).fetchall()
    if not row:
        return
    playback = row["media"].get("sourcePlayback", {})
    if playback.get("permissionConfirmed") is not True or playback.get("rightsPolicy") != "public-app-viewers":
        raise RuntimeError("Persistent source permission is required")
    attempts = (row["media"].get("frameProcessing") or {}).get("attempts") or []
    if not any(item.get("id") == attempt_id and item.get("status") == "running"
               for item in attempts):
        raise RuntimeError("Frame generation attempt is not authorized")
    done = {item["rank"] for item in existing}
    try:
        source = Path(directory) / "source.mp4"
        download_object(playback["objectPath"], source, max_bytes=200_000_000)
        with source.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != row["source_sha256"]:
                raise RuntimeError("Persistent source fingerprint mismatch")
        for rank, match in enumerate(row["matches"], 1):
            if rank in done:
                continue
            frame = Path(directory) / f"{rank}.jpg"
            subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-ss",
                 str((match["startSeconds"] + match["endSeconds"]) / 2),
                 "-i", str(source), "-frames:v", "1", "-vf", "scale=640:-2",
                 "-threads", "1", str(frame)],
                timeout=20, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            # Unique paths avoid overwriting previous evidence generations even
            # when a worker was killed after an upload but before DB persistence.
            object_path = private_object_path(
                f"sceneit/stills/{row['source_sha256']}/{search_id}/{rank}-{uuid.uuid4().hex}.jpg")
            if _commercial_enabled():
                from .quota import reserve_storage
                with connection() as conn:
                    # This durable object-key ledger is also the recovery
                    # journal. It commits before upload, so a kill between GCS
                    # success and proof-frame mapping cannot create an
                    # untracked orphan. Operators reconcile/delete by this key
                    # before invoking confirmed storage release.
                    reserve_storage(
                        conn, None, object_path, frame.stat().st_size,
                        require_membership=False)
            upload_private(frame, object_path)
            with connection() as conn:
                conn.execute(
                    "INSERT INTO sceneit_proof_frames(search_id,rank,source_sha256,object_path) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT(search_id,rank) DO UPDATE "
                    "SET source_sha256=excluded.source_sha256,object_path=excluded.object_path",
                    (search_id, rank, row["source_sha256"], object_path),
                )
    finally:
        # The supervising parent owns and removes the directory even on SIGKILL.
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Internal bounded proof still worker")
    parser.add_argument("search_id", type=__import__("uuid").UUID)
    parser.add_argument("attempt_id")
    parser.add_argument("directory")
    args = parser.parse_args()
    build_frames(args.search_id, args.attempt_id, args.directory)