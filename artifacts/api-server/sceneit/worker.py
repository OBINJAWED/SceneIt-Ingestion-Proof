"""Operator-only demo ingestion and metadata commands, never invoked by public routes."""
import argparse
import hashlib
import json
import logging
import re
import subprocess
import time
from pathlib import Path

import httpx
from psycopg.types.json import Jsonb

from .db import PROOF_ID, connection, get_proof, update_proof, worker_lock
from .provider import ProviderError, TwelveLabsClient
from .storage import private_object_path, upload_source

ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger("sceneit.worker")
logging.basicConfig(level=logging.INFO, format="%(message)s")
from .resources import ResourceExhausted, shared_permit


def log(event, **fields):
    logger.info(json.dumps({"event": event, **fields}))


def initialize(source_name, youtube_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", youtube_id):
        raise ValueError("Invalid YouTube ID")
    source = (ROOT / source_name).resolve()
    if not source.is_relative_to(ROOT / "attached_assets") or not source.is_file():
        raise ValueError("Source must be an uploaded file in attached_assets")
    metadata = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)],
        check=True, capture_output=True, timeout=30,
    ).stdout)
    video = next(s for s in metadata["streams"] if s["codec_type"] == "video")
    media = {
        "duration": float(metadata["format"]["duration"]),
        "size": source.stat().st_size, "width": video["width"], "height": video["height"],
        "hasAudio": any(s["codec_type"] == "audio" for s in metadata["streams"]),
        "youtubeMetadataVerified": False,
    }
    if media["size"] > 200_000_000:
        raise ValueError("This proof uses the direct-upload limit of 200 MB")
    if not media["hasAudio"] or media["duration"] <= 0:
        raise ValueError("This proof requires valid video and audio")
    with source.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    title = "Authorized video"
    response = httpx.get(
        "https://www.youtube.com/oembed",
        params={"url": f"https://www.youtube.com/watch?v={youtube_id}", "format": "json"},
        timeout=20,
    )
    if response.is_success:
        title = response.json().get("title", title)
        media["youtubeMetadataVerified"] = True
        media["youtubeMetadataVideoId"] = youtube_id
    with connection() as conn:
        existing = conn.execute(
            "SELECT source_sha256, youtube_id FROM sceneit_proofs WHERE id = %s",
            (PROOF_ID,),
        ).fetchone()
        if existing:
            if existing["source_sha256"] != digest or existing["youtube_id"] != youtube_id:
                raise ValueError("This proof already belongs to a different source; refusing to overwrite it")
        else:
            conn.execute(
                "INSERT INTO sceneit_proofs(id,title,youtube_id,source_path,source_sha256,media,index_name) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (PROOF_ID, title, youtube_id, str(source.relative_to(ROOT)), digest, Jsonb(media),
                 f"SceneIt_proof_{youtube_id}_{digest[:8]}"),
            )
    log("proof_initialized", duration_seconds=media["duration"], bytes=media["size"])


def refresh_youtube_metadata():
    """Recheck only the preserved demo's link, not playback or timeline alignment."""
    proof = get_proof()
    if not proof:
        raise RuntimeError("Initialize the proof first")
    youtube_id = proof.get("youtube_id")
    if not isinstance(youtube_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", youtube_id):
        raise ValueError("The proof must have a valid YouTube ID before refreshing metadata")

    verified, reason = False, "http_status"
    try:
        response = httpx.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={youtube_id}", "format": "json"},
            timeout=20,
        )
        if response.is_success:
            metadata = response.json()
            verified = (
                isinstance(metadata, dict)
                and isinstance(metadata.get("title"), str)
                and bool(metadata["title"].strip())
            )
            reason = "resolved" if verified else "invalid_metadata"
    except httpx.HTTPError:
        reason = "request_failed"
    except ValueError:
        reason = "invalid_metadata"

    # Merge into the current row, not the pre-request snapshot: preserve other
    # observations and atomically reject a link replacement while oEmbed ran.
    with connection() as conn:
        updated = conn.execute(
            "UPDATE sceneit_proofs SET media = media || %s, updated_at = now() "
            "WHERE id = %s AND youtube_id = %s RETURNING id",
            (Jsonb({
                "youtubeMetadataVerified": verified,
                "youtubeMetadataVideoId": youtube_id,
            }), PROOF_ID, youtube_id),
        ).fetchone()
    if not updated:
        log("youtube_metadata_refresh_skipped", reason="proof_link_changed_or_removed")
        return 2
    log(
        "youtube_metadata_refreshed", youtube_id=youtube_id, verified=verified, reason=reason,
        detail="Metadata only; playback and timeline alignment were not checked.",
    )
    return 0 if verified else 1


def identifier(raw):
    value = raw.get("_id") or raw.get("id")
    if not isinstance(value, str) or not value:
        raise ValueError("Provider omitted the identifier")
    return value


def measured_duration(raw):
    for container in (raw.get("system_metadata"), raw.get("metadata"), raw):
        if isinstance(container, dict):
            value = container.get("duration")
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
    return None


def run_step(client):
    proof = get_proof()
    if not proof:
        raise RuntimeError("Initialize the proof first")
    if proof["state"] in ("ready", "failed", "needs_review"):
        return True
    if not proof["index_id"]:
        # Creation is reconciled by a deterministic name, never "the first index".
        candidates = [i for i in client.list_indexes() if i.get("index_name") == proof["index_name"]]
        if len(candidates) > 1:
            update_proof(state="needs_review", message="More than one proof index exists; operator review is required.")
            return True
        if candidates:
            index = candidates[0]
        else:
            update_proof(message="Creating a dedicated visual + audio proof index.")
            index = client.create_index(proof["index_name"])
        update_proof(index_id=identifier(index), message="Proof index selected. Preparing the authorized file.")
        log("index_selected")
        return False
    if not proof["asset_id"]:
        if proof["state"] == "uploading":
            update_proof(state="needs_review", message="An upload may already have been accepted. Reconcile it before resubmitting.")
            return True
        update_proof(state="uploading", message="Uploading the original file directly to Twelve Labs.")
        log("upload_started", bytes=proof["media"]["size"])
        raw = client.upload_asset(ROOT / proof["source_path"], {
            "sceneit_proof": PROOF_ID, "source_sha256": proof["source_sha256"],
        })
        update_proof(asset_id=identifier(raw), state="processing",
                     provider_status=raw.get("status"), message="Upload accepted. Twelve Labs is processing the media asset.")
        log("upload_accepted")
        return False
    if not proof["indexed_asset_id"]:
        if proof["state"] == "indexing":
            update_proof(state="needs_review", message="Indexing may already have been accepted. Reconcile its identifier before resubmitting.")
            return True
        raw = client.get_asset(proof["asset_id"])
        status = raw.get("status")
        duration = measured_duration(raw)
        update_proof(provider_status=status, **({"provider_duration": duration} if duration else {}))
        if status == "failed":
            update_proof(state="failed", message="Twelve Labs could not process the source file.", error_code="asset_failed")
            return True
        if status != "ready":
            log("asset_processing", status=status)
            return False
        update_proof(state="indexing", message="Media is ready. Indexing visual and audio content.")
        raw = client.index_asset(proof["index_id"], proof["asset_id"], {
            "sceneit_proof": PROOF_ID, "youtube_id": proof["youtube_id"],
            "source_sha256": proof["source_sha256"],
        })
        update_proof(indexed_asset_id=identifier(raw), message="Indexing accepted. Waiting for searchable scenes.")
        log("indexing_accepted")
        return False
    raw = client.get_indexed_asset(proof["index_id"], proof["indexed_asset_id"])
    status = raw.get("status")
    duration = measured_duration(raw)
    update_proof(provider_status=status, **({"provider_duration": duration} if duration else {}))
    if status in ("ready", "indexed"):
        if duration and abs(duration - proof["media"]["duration"]) > 1:
            update_proof(state="needs_review", message="The provider and source durations differ. Review timeline alignment before searching.")
        else:
            update_proof(state="ready", message="Ready to search. Results come from the indexed original; YouTube alignment still needs comparison.",
                         error_code=None)
        log("index_ready", provider_duration=duration)
        return True
    if status in ("failed", "error"):
        update_proof(state="failed", message="Twelve Labs could not finish indexing this video.", error_code="indexing_failed")
        return True
    log("index_processing", status=status)
    return False


def run(max_seconds):
    try:
        with shared_permit(
                # A provider upload can begin on the final loop iteration.
                # Keep the crash lease beyond both its 300 second operation
                # budget and a final bounded socket read.
                "import_worker", lease_seconds=max_seconds + 630):
            with worker_lock():
                return _run_with_client(max_seconds)
    except ResourceExhausted:
        log("worker_capacity_exhausted")
        return 2

def _run_with_client(max_seconds):
    client = None
    try:
        client = TwelveLabsClient()
        deadline = time.monotonic() + max_seconds
        failures, delay = 0, 3
        while time.monotonic() < deadline:
            try:
                if run_step(client):
                    proof = get_proof()
                    log("worker_finished", state=proof["state"], error_code=proof["error_code"])
                    return 0 if proof["state"] == "ready" else 1
                failures = 0
            except ProviderError as error:
                log("provider_error", code=error.code, ambiguous=error.ambiguous)
                if error.ambiguous:
                    update_proof(state="needs_review", error_code=error.code, message=error.message)
                    return 1
                proof = get_proof()
                # Retry only polling reads. Failed or uncertain writes need explicit review.
                polling = bool(proof["asset_id"]) and (
                    proof["state"] == "processing" or bool(proof["indexed_asset_id"])
                )
                if not error.retryable or not polling or failures >= 4:
                    update_proof(state="needs_review" if polling else "failed",
                                 error_code=error.code, message=error.message)
                    return 1
                failures += 1
            except Exception as error:
                # Persist a safe failure, not provider payloads, URLs or exception reprs.
                log("worker_error", type=type(error).__name__)
                update_proof(state="needs_review", error_code="worker_error",
                             message="Ingestion stopped safely. An operator must inspect the recorded identifiers before retrying.")
                return 1
            time.sleep(delay)
            delay = min(20, delay * 1.4)
        update_proof(message="Polling paused at its time limit. The saved provider job can be resumed without re-uploading.")
        log("polling_deadline_reached")
        return 2
    finally:
        if client is not None:
            client.close()
def publish_source(permission_confirmed, rights_policy):
    """Persist the already-indexed original for rights-approved first-party playback."""
    if not permission_confirmed or rights_policy != "public-app-viewers":
        raise RuntimeError(
            "Explicit owner permission for public app viewers is required to publish source playback"
        )
    proof = get_proof()
    if not proof:
        raise RuntimeError("Initialize the proof first")
    source = (ROOT / proof["source_path"]).resolve()
    if not source.is_relative_to(ROOT / "attached_assets") or not source.is_file():
        raise RuntimeError("The authorized source file is unavailable")
    object_path = private_object_path(f"sceneit/{proof['source_sha256']}.mp4")
    created = upload_source(source, object_path)
    media = dict(proof["media"])
    media["sourcePlayback"] = {
        "objectPath": object_path,
        "contentType": "video/mp4",
        "rightsPolicy": rights_policy,
        "permissionConfirmed": permission_confirmed,
    }
    with connection() as conn:
        conn.execute(
            "UPDATE sceneit_proofs SET media = %s, updated_at = now() WHERE id = %s",
            (Jsonb(media), PROOF_ID),
        )
    log("source_playback_published", created=created, bytes=source.stat().st_size)


def main(argv=None):
    parser = argparse.ArgumentParser(description="SceneIt operator-only preserved demo commands")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--source", required=True)
    init.add_argument("--youtube-id", required=True)
    runner = commands.add_parser("run")
    runner.add_argument("--max-seconds", type=int, default=1200)
    commands.add_parser(
        "refresh-youtube-metadata",
        help="Fresh oEmbed check of the demo's current link; no ingestion or playback check",
        description=(
            "Refresh only the preserved demo's YouTube metadata. Does not verify playback "
            "or timeline alignment, upload media, or reindex. Exit 0: metadata resolved; "
            "1: failed check saved as unverified; 2: link changed or proof removed, nothing saved."
        ),
    )
    publisher = commands.add_parser("publish-source")
    publisher.add_argument("--permission-confirmed", action="store_true", required=True)
    publisher.add_argument("--rights-policy", choices=["public-app-viewers"], required=True)
    searches = commands.add_parser("search-operations")
    searches.add_argument("action", choices=["status", "reconcile", "resolve"])
    searches.add_argument("--id")
    searches.add_argument(
        "--resolution", choices=["confirmed_failed", "review_retained"])
    args = parser.parse_args(argv)
    if args.command == "init":
        initialize(args.source, args.youtube_id)
    elif args.command == "run":
        return run(max(30, min(args.max_seconds, 1800)))
    elif args.command == "refresh-youtube-metadata":
        return refresh_youtube_metadata()
    elif args.command == "publish-source":
        publish_source(args.permission_confirmed, args.rights_policy)
    else:
        from .proof import (
            get_search_operation, list_search_operations,
            reconcile_search_operations, resolve_search_operation,
        )
        if args.action == "status":
            result = (get_search_operation(args.id) if args.id
                      else list_search_operations())
        elif args.action == "reconcile":
            result = reconcile_search_operations()
        else:
            if not args.id or not args.resolution:
                parser.error("resolve requires --id and --resolution")
            result = resolve_search_operation(args.id, args.resolution)
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
