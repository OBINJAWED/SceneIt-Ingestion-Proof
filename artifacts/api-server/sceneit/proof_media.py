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
    headers = {"Accept-Ranges": "bytes", "Content-Type": "video/mp4",
               "Cache-Control": "private, no-store"}
    for name in ("Content-Length", "Content-Range", "ETag"):
        if name in upstream.headers:
            headers[name] = upstream.headers[name]

    @stream_with_context
    def body():
        try:
            yield from upstream.iter_bytes()
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
        return open_private(row["object_path"])
    except Exception:
        raise ProofError("frame_unavailable", "The saved still is temporarily unavailable. Please refresh later.", 503) from None


def _pending():
    with connection() as conn:
        return conn.execute(
            "SELECT s.id FROM sceneit_searches s JOIN sceneit_proofs p ON p.id=s.proof_id "
            "WHERE p.id=%s AND s.state='done' AND jsonb_array_length(s.matches)>0 "
            "AND (SELECT count(*) FROM sceneit_proof_frames f WHERE f.search_id=s.id "
            "AND f.source_sha256=p.source_sha256)<jsonb_array_length(s.matches) "
            "AND p.media->'sourcePlayback'->>'permissionConfirmed'='true' "
            "AND COALESCE((p.media->'frameProcessing'->>'retryAfter')::timestamptz,"
            "'epoch'::timestamptz)<now() ORDER BY s.created_at LIMIT 1",
            (PROOF_ID,),
        ).fetchone()


def prepare_frames():
    """Prepare at most one search per tick, with a persisted failure cooldown."""
    pending = _pending()
    if not pending:
        return
    try:
        # Shared media budget includes HTTP import frames and worker inspection.
        # Child kill at 120s occurs well before this lease could expire.
        with shared_permit("media", lease_seconds=150):
            with tempfile.TemporaryDirectory(prefix="sceneit-proof-frames-") as directory:
                result = bounded_process(
                    [sys.executable, "-m", "sceneit.proof_media", str(pending["id"]), directory],
                    timeout=120,
                )
                if result:
                    raise RuntimeError("Frame preparation failed")
    except ResourceExhausted:
        return
    except Exception:
        with connection() as conn:
            conn.execute(
                "UPDATE sceneit_proofs SET media=jsonb_set(media,'{frameProcessing}',"
                "jsonb_build_object('status','degraded','retryAfter',now()+interval '5 minutes')) "
                "WHERE id=%s", (PROOF_ID,),
            )
        logger.warning(json.dumps({"event": "proof_frames_degraded", "code": "frame_unavailable"}))
    else:
        logger.info(json.dumps({"event": "proof_frames_prepared"}))


def build_frames(search_id, directory):
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
            import uuid
            object_path = private_object_path(
                f"sceneit/stills/{row['source_sha256']}/{search_id}/{rank}-{uuid.uuid4().hex}.jpg")
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
    parser.add_argument("directory")
    args = parser.parse_args()
    build_frames(args.search_id, args.directory)