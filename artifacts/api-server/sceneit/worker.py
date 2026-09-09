"""A resumable operator-only ingestion command, never invoked by public routes."""
import argparse
import hashlib
import json
import logging
import subprocess
import time
from pathlib import Path

import httpx
from psycopg.types.json import Jsonb

from .db import PROOF_ID, connection, get_proof, update_proof, worker_lock
from .provider import ProviderError, TwelveLabsClient

ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger("sceneit.worker")
logging.basicConfig(level=logging.INFO, format="%(message)s")


def log(event, **fields):
    logger.info(json.dumps({"event": event, **fields}))


def initialize(source_name, youtube_id):
    import re
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
    with worker_lock():
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SceneIt one-video ingestion proof")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--source", required=True)
    init.add_argument("--youtube-id", required=True)
    runner = commands.add_parser("run")
    runner.add_argument("--max-seconds", type=int, default=1200)
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.source, args.youtube_id)
    else:
        raise SystemExit(run(max(30, min(args.max_seconds, 1800))))