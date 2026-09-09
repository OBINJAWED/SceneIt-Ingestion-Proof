"""Proof evidence and actual, quota-limited semantic search."""
import math
import time
import unicodedata
import uuid
from datetime import datetime, timezone

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal

from .db import PROOF_ID, connection, get_proof
from .provider import ProviderError, TwelveLabsClient


class SceneQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500)
    modality: Literal["both", "visual", "audio"] = "both"

    @field_validator("query")
    @classmethod
    def meaningful_query(cls, value):
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("Enter a scene description.")
        return value


class ProofError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def present_search(row):
    return {
        "id": str(row["id"]),
        "query": row["query"],
        "modality": row["modality"],
        "createdAt": row["created_at"].isoformat(),
        "latencyMs": row["latency_ms"],
        "provider": "Twelve Labs",
        "partial": row["partial"],
        "matches": row["matches"],
    }


def list_searches():
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM sceneit_searches WHERE proof_id = %s AND state = 'done' "
            "ORDER BY created_at DESC LIMIT 50", (PROOF_ID,)
        ).fetchall()
    return [present_search(row) for row in rows]


def public_proof():
    row = get_proof()
    if not row:
        raise ProofError("proof_not_initialized", "The proof is not initialized yet.", 503)
    media = row["media"]
    state = row["state"]
    with connection() as conn:
        successful = conn.execute(
            "SELECT count(*) AS n FROM sceneit_searches "
            "WHERE proof_id = %s AND state = 'done'", (PROOF_ID,)
        ).fetchone()["n"]
    duration = float(media["duration"])
    provider_duration = row["provider_duration"]
    duration_agrees = provider_duration is not None and abs(provider_duration - duration) <= 1
    embed_blocked = media.get("playbackObservation", {}).get("status") == "blocked"
    failed = state in ("failed", "needs_review")
    checks = [
        {"id": "source", "label": "Original file validated", "status": "passed",
         "detail": f"H.264 video and AAC audio; {media['width']} × {media['height']}. Source fingerprint recorded."},
        {"id": "youtube", "label": "YouTube link resolves", "status": "passed" if media.get("youtubeMetadataVerified") else "unverified",
         "detail": "YouTube oEmbed metadata matches the supplied link. Playback restrictions may still apply."},
        {"id": "embed", "label": "Automated embed check", "status": "failed" if embed_blocked else "unverified",
         "detail": "YouTube refused embedded playback in the automated preview (error 150). Timestamp links remain available." if embed_blocked else "A successful metadata request does not verify embedded playback."},
        {"id": "asset", "label": "Media uploaded to Twelve Labs",
         "status": "passed" if row["asset_id"] else ("failed" if failed else "pending"),
         "detail": "Provider asset identifier saved in Postgres." if row["asset_id"] else "The authorized file is uploaded directly, not downloaded from YouTube."},
        {"id": "index", "label": "Visual + audio indexing",
         "status": "passed" if state == "ready" else ("failed" if failed else "pending"),
         "detail": "Both modalities are indexed with Marengo 3.0." if state == "ready" else row["message"]},
        {"id": "duration", "label": "Source / provider duration",
         "status": ("passed" if duration_agrees else "failed") if provider_duration is not None else "pending",
         "detail": f"Source {duration:.3f}s; provider {provider_duration:.3f}s." if provider_duration is not None else "Awaiting the provider's measured duration."},
        {"id": "search", "label": "Live semantic retrieval",
         "status": "passed" if successful else "pending",
         "detail": f"{successful} real searches saved; no mock matches or invented confidence scores."},
        {"id": "alignment", "label": "YouTube timeline alignment", "status": "unverified",
         "detail": "Compare the source still with YouTube at each result. A matching link or duration alone does not verify the edit."},
    ]
    return {
        "id": row["id"], "title": row["title"],
        "youtubeVideoId": row["youtube_id"],
        "youtubeUrl": f"https://www.youtube.com/watch?v={row['youtube_id']}",
        "durationSeconds": duration, "width": media["width"], "height": media["height"],
        "fileSizeBytes": media["size"], "hasAudio": media["hasAudio"],
        "state": state, "statusMessage": row["message"], "model": "Marengo 3.0",
        "timelineStatus": "unverified", "checks": checks,
        "searchesUsed": row["searches_used"], "searchLimit": row["search_limit"],
        "updatedAt": row["updated_at"].isoformat(),
    }


def normalize_matches(raw, proof, search_id):
    data = raw.get("data")
    if not isinstance(data, list):
        raise ProofError("provider_response_invalid", "The provider returned an unexpected search response.", 502)
    result, dropped = [], False
    for item in data:
        # Search is scoped to this indexed asset. Validate mapping again at the boundary.
        provider_id = item.get("video_id") or item.get("indexed_asset_id")
        if provider_id != proof["indexed_asset_id"]:
            dropped = True
            continue
        start, end = item.get("start"), item.get("end")
        if (isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or not math.isfinite(start) or not math.isfinite(end)
                or start < 0 or start >= end or end > proof["media"]["duration"] + 0.25):
            dropped = True
            continue
        end = min(end, proof["media"]["duration"])
        label = item.get("confidence")
        label = label if label in ("high", "medium", "low") else None
        rank = len(result) + 1
        result.append({
            "rank": rank, "startSeconds": float(start), "endSeconds": float(end),
            "confidenceLabel": label,
            "frameUrl": f"/api/proof/searches/{search_id}/frames/{rank}",
            "youtubeUrl": f"https://www.youtube.com/watch?v={proof['youtube_id']}&t={int(start)}s",
        })
        if len(result) == 5:
            break
    if data and not result:
        raise ProofError("provider_mapping_invalid", "The returned scenes could not be safely mapped to this video.", 502)
    return result, dropped


def search_scenes(payload):
    query = SceneQuery.model_validate(payload)
    query_key = unicodedata.normalize("NFKC", query.query).casefold()
    search_id = uuid.uuid4()
    with connection() as conn:
        proof = conn.execute(
            "SELECT * FROM sceneit_proofs WHERE id = %s FOR UPDATE", (PROOF_ID,)
        ).fetchone()
        if not proof or proof["state"] != "ready":
            raise ProofError("index_not_ready", "The video must finish indexing before you can search it.", 409)
        existing = conn.execute(
            "SELECT * FROM sceneit_searches WHERE proof_id = %s AND query_key = %s AND modality = %s",
            (PROOF_ID, query_key, query.modality),
        ).fetchone()
        if existing:
            if existing["state"] == "done":
                return present_search(existing)
            if existing["state"] == "running":
                raise ProofError("search_in_progress", "This search was submitted and is still pending. It will not be automatically resubmitted.", 409)
            raise ProofError("search_failed", "That search failed. Try a different description; the failed request will not be automatically repeated.", 409)
        now = datetime.now(timezone.utc)
        if proof["last_search_at"] and (now - proof["last_search_at"]).total_seconds() < 3:
            raise ProofError("rate_limited", "Please wait a few seconds before another search.", 429)
        running = conn.execute(
            "SELECT count(*) AS n FROM sceneit_searches WHERE proof_id = %s "
            "AND state = 'running' AND created_at > now() - interval '2 minutes'", (PROOF_ID,)
        ).fetchone()["n"]
        if running >= 2:
            raise ProofError("busy", "Two searches are already running. Please wait.", 429)
        if proof["searches_used"] >= proof["search_limit"]:
            raise ProofError("proof_budget_reached", "The 50-request proof budget has been reached. Saved searches remain available.", 429)
        conn.execute(
            "UPDATE sceneit_proofs SET searches_used = searches_used + 1, last_search_at = now() WHERE id = %s",
            (PROOF_ID,),
        )
        conn.execute(
            "INSERT INTO sceneit_searches(id, proof_id, query, query_key, modality) VALUES (%s,%s,%s,%s,%s)",
            (search_id, PROOF_ID, query.query, query_key, query.modality),
        )
    started = time.monotonic()
    try:
        raw = TwelveLabsClient().search(
            proof["index_id"], query.query, query.modality, proof["indexed_asset_id"]
        )
        matches, partial = normalize_matches(raw, proof, search_id)
        latency = int((time.monotonic() - started) * 1000)
        with connection() as conn:
            row = conn.execute(
                "UPDATE sceneit_searches SET state = 'done', matches = %s, partial = %s, "
                "latency_ms = %s, completed_at = now() WHERE id = %s RETURNING *",
                (Jsonb(matches), partial, latency, search_id),
            ).fetchone()
        return present_search(row)
    except (ProviderError, ProofError) as exc:
        with connection() as conn:
            conn.execute(
                "UPDATE sceneit_searches SET state = 'failed', error_code = %s, completed_at = now() WHERE id = %s",
                (exc.code, search_id),
            )
        if isinstance(exc, ProviderError):
            raise ProofError(exc.code, exc.message, 502) from None
        raise
    except Exception:
        with connection() as conn:
            conn.execute(
                "UPDATE sceneit_searches SET state = 'failed', error_code = 'search_internal_error', "
                "completed_at = now() WHERE id = %s", (search_id,)
            )
        raise


def report():
    proof = public_proof()
    return {
        "proof": proof, "searches": list_searches(),
        "limitations": [
            "This proof covers only the supplied video, not a general video library.",
            "YouTube edit/timeline alignment is not independently verified.",
            "A source still is extracted at the midpoint of each returned segment.",
            "Confidence labels, when present, are provider categories, not probabilities.",
            "YouTube looping is approximate and subject to availability and browser policies.",
            "The shared proof saves search descriptions; do not enter private information.",
            "A hard cap of 50 submitted provider searches limits proof usage.",
        ] + [check["detail"] for check in proof["checks"] if check["id"] == "embed" and check["status"] == "failed"],
    }