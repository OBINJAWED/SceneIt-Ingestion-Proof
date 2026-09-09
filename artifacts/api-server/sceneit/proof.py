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
from .resources import ResourceExhausted, shared_permit


# The provider call and fenced persistence must finish within this window.
# 75 seconds leaves headroom under the production Gunicorn 90 second timeout.
SEARCH_DEADLINE_SECONDS = 75


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


def alignment_evidence(media, *, source_sha256, youtube_id):
    """Present an operator-recorded observation only for the current video pair."""
    observation = media.get("alignmentObservation")
    if not isinstance(observation, dict):
        observation = {}
    status = observation.get("status")
    identities_match = (
        isinstance(source_sha256, str) and bool(source_sha256.strip())
        and isinstance(youtube_id, str) and bool(youtube_id.strip())
        and observation.get("sourceSha256") == source_sha256
        and observation.get("youtubeVideoId") == youtube_id
    )
    if status in ("verified", "mismatch") and not identities_match:
        return "unverified", "unverified", (
            "The saved playback observation does not identify the current source file and YouTube video. "
            "Compare paired playback again to verify this video pair."
        )
    if status == "verified":
        sample_count = observation.get("sampleCount")
        count = f"{sample_count} representative saved scenes" if sample_count else "Representative saved scenes"
        return "verified", "passed", (
            f"{count} matched the YouTube edit at their retained timestamps during paired playback. "
            "No stable offset or edit divergence was observed; verification covers the sampled moments."
        )
    if status == "mismatch":
        return "mismatch", "failed", (
            observation.get("summary")
            or "Paired playback showed that the indexed source and YouTube edit do not share one timeline."
        )
    return "unverified", "unverified", (
        "Compare paired source and YouTube playback at saved timestamps. "
        "A matching link, still, or duration alone does not verify the edit."
    )


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

def present_search_operation(row):
    """Safe operation metadata; search text is deliberately omitted."""
    return {
        "id": str(row["id"]),
        "state": row["state"],
        "attemptId": str(row["attempt_id"]),
        "createdAt": row["created_at"].isoformat(),
        "deadlineAt": row["deadline_at"].isoformat(),
        "completedAt": (
            row["completed_at"].isoformat() if row["completed_at"] else None
        ),
        "errorCode": row["error_code"],
    }
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
    youtube_id = row.get("youtube_id")
    has_youtube_id = isinstance(youtube_id, str) and bool(youtube_id.strip())
    metadata_verified = (
        has_youtube_id
        and media.get("youtubeMetadataVideoId") == youtube_id
        and media.get("youtubeMetadataVerified") is True
    )
    playback_observation = media.get("playbackObservation")
    if not isinstance(playback_observation, dict):
        playback_observation = {}
    embed_status = (
        playback_observation.get("status")
        if has_youtube_id and playback_observation.get("youtubeVideoId") == youtube_id
        else None
    )
    embed_blocked = embed_status == "blocked"
    embed_played = embed_status == "played"
    timeline_status, alignment_status, alignment_detail = alignment_evidence(
        media, source_sha256=row.get("source_sha256"), youtube_id=row.get("youtube_id"),
    )
    failed = state in ("failed", "needs_review")
    checks = [
        {"id": "source", "label": "Original file validated", "status": "passed",
         "detail": f"H.264 video and AAC audio; {media['width']} × {media['height']}. Source fingerprint recorded."},
        {"id": "youtube", "label": "YouTube link resolves", "status": "passed" if metadata_verified else "unverified",
         "detail": (
             "YouTube oEmbed metadata matches the supplied link. Playback restrictions may still apply."
             if metadata_verified else
             "YouTube link metadata has not been verified for the current video. Playback restrictions may still apply."
         )},
        {"id": "embed", "label": "Automated embed check",
          "status": "passed" if embed_played else ("failed" if embed_blocked else "unverified"),
          "detail": (
              "YouTube footage rendered and the player clock advanced during the latest automated preview."
              if embed_played else
              ("YouTube refused embedded playback in the automated preview (error 150). Timestamp links remain available."
               if embed_blocked else
               "A successful metadata request does not verify embedded playback.")
          )},
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
        {"id": "alignment", "label": "YouTube timeline alignment", "status": alignment_status,
          "detail": alignment_detail},
    ]
    source_playback = media.get("sourcePlayback", {})
    source_playback_available = (
        source_playback.get("permissionConfirmed") is True
        and source_playback.get("rightsPolicy") == "public-app-viewers"
    )
    checks.insert(3, {
        "id": "source-playback", "label": "First-party source playback",
        "status": "passed" if source_playback_available else "unverified",
        "detail": (
            "The owner permits streaming to anyone with app access; the original is served from controlled persistent storage."
            if source_playback_available else
            "Source playback remains disabled until streaming permission and persistent storage are configured."
        ),
    })
    return {
        "id": row["id"], "title": row["title"],
        "youtubeVideoId": youtube_id,
        "youtubeUrl": f"https://www.youtube.com/watch?v={youtube_id}",
        "durationSeconds": duration, "width": media["width"], "height": media["height"],
        "fileSizeBytes": media["size"], "hasAudio": media["hasAudio"],
        "state": state, "statusMessage": row["message"], "model": "Marengo 3.0",
        "timelineStatus": timeline_status, "checks": checks,
        "sourcePlaybackAvailable": source_playback_available,
        "sourcePlaybackUrl": "/api/proof/source" if source_playback_available else None,
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


def search_scenes(payload, client_factory=TwelveLabsClient):
    """Run one paid submission under a cross-process, crash-expiring permit."""
    query = SceneQuery.model_validate(payload)
    query_key = unicodedata.normalize("NFKC", query.query).casefold()
    # Cached and non-repeatable outcomes are reads, not scarce provider work.
    # This preflight is advisory; the locked reservation below remains the
    # authoritative deduplication check for races.
    reconcile_search_operations()
    with connection() as conn:
        existing = conn.execute(
            "SELECT * FROM sceneit_searches WHERE proof_id=%s AND query_key=%s "
            "AND modality=%s",
            (PROOF_ID, query_key, query.modality),
        ).fetchone()
    if existing:
        if existing["state"] == "done":
            return present_search(existing)
        code = ("search_in_progress" if existing["state"] == "running"
                else "search_needs_review" if existing["state"] == "needs_review"
                else "search_failed")
        raise ProofError(
            code,
            "That paid search will not be automatically resubmitted.",
            409,
        )
    try:
        with shared_permit(
                "search", lease_seconds=SEARCH_DEADLINE_SECONDS + 5):
            return _search_scenes(payload, client_factory)
    except ResourceExhausted:
        raise ProofError(
            "busy", "Search capacity is busy. Please retry shortly.", 429
        ) from None

def _search_scenes(payload, client_factory):
    query = SceneQuery.model_validate(payload)
    query_key = unicodedata.normalize("NFKC", query.query).casefold()
    search_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    from .http import set_operation_context
    set_operation_context(search_id, attempt_id)
    with connection() as conn:
        proof = conn.execute(
            "SELECT * FROM sceneit_proofs WHERE id = %s FOR UPDATE", (PROOF_ID,)
        ).fetchone()
        if not proof or proof["state"] != "ready":
            raise ProofError("index_not_ready", "The video must finish indexing before you can search it.", 409)
        conn.execute(
            "UPDATE sceneit_searches SET state='needs_review',"
            "error_code='search_outcome_unknown',completed_at=now() "
            "WHERE proof_id=%s AND state='running' AND deadline_at<=now()",
            (PROOF_ID,),
        )
        existing = conn.execute(
            "SELECT * FROM sceneit_searches WHERE proof_id = %s AND query_key = %s AND modality = %s",
            (PROOF_ID, query_key, query.modality),
        ).fetchone()
        if existing:
            if existing["state"] == "done":
                return present_search(existing)
            if existing["state"] == "running":
                raise ProofError("search_in_progress", "This search was submitted and is still pending. It will not be automatically resubmitted.", 409)
            if existing["state"] == "needs_review":
                raise ProofError("search_needs_review", "That search has an uncertain outcome and requires operator review. It will not be resubmitted.", 409)
            raise ProofError("search_failed", "That search failed. Try a different description; the failed request will not be automatically repeated.", 409)
        now = datetime.now(timezone.utc)
        if proof["last_search_at"] and (now - proof["last_search_at"]).total_seconds() < 3:
            raise ProofError("rate_limited", "Please wait a few seconds before another search.", 429)
        running = conn.execute(
            "SELECT count(*) AS n FROM sceneit_searches WHERE proof_id = %s "
            "AND state = 'running' AND deadline_at > now()", (PROOF_ID,)
        ).fetchone()["n"]
        if running >= 2:
            raise ProofError("busy", "Two searches are already running. Please wait.", 429)
        if proof["searches_used"] >= proof["search_limit"]:
            raise ProofError("proof_budget_reached", "The 50-request proof budget has been reached. Saved searches remain available.", 429)
        from .billing_config import billing_settings
        settings = billing_settings()
        commercial = settings["enabled"] if isinstance(settings, dict) else settings.enabled
        if commercial:
            from .quota import check_work, reserve
            # Shared proof activity spends only the application allowance.
            check_work(conn, None, require_membership=False)
            reserve(
                conn, None, f"proof-search:{search_id}", {"searches": 1},
                require_membership=False)
        conn.execute(
            "UPDATE sceneit_proofs SET searches_used = searches_used + 1, last_search_at = now() WHERE id = %s",
            (PROOF_ID,),
        )
        operation = conn.execute(
            "INSERT INTO sceneit_searches"
            "(id,proof_id,query,query_key,modality,attempt_id,deadline_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,now()+(%s * interval '1 second')) "
            "RETURNING deadline_at",
            (search_id, PROOF_ID, query.query, query_key, query.modality,
             attempt_id, SEARCH_DEADLINE_SECONDS),
        ).fetchone()
    started = time.monotonic()
    client = None
    try:
        remaining = (
            operation["deadline_at"] - datetime.now(timezone.utc)
        ).total_seconds()
        client = client_factory()
        raw = client.search(
            proof["index_id"], query.query, query.modality,
            proof["indexed_asset_id"], timeout_seconds=remaining,
        )
        matches, partial = normalize_matches(raw, proof, search_id)
        latency = int((time.monotonic() - started) * 1000)
        with connection() as conn:
            row = conn.execute(
                "UPDATE sceneit_searches SET state='done',matches=%s,partial=%s,"
                "latency_ms=%s,completed_at=now(),resolved_at=now(),"
                "resolution='completed' WHERE id=%s "
                "AND attempt_id=%s AND state='running' AND deadline_at>now() "
                "RETURNING *",
                (Jsonb(matches), partial, latency, search_id, attempt_id),
            ).fetchone()
        if not row:
            reconcile_search_operations()
            raise ProofError(
                "search_needs_review",
                "The search response arrived after its safe completion window and requires review.",
                409,
            )
        return present_search(row)
    except (ProviderError, ProofError) as exc:
        uncertain = (
            isinstance(exc, ProviderError) and exc.ambiguous
        ) or isinstance(exc, ProofError)
        state = "needs_review" if uncertain else "failed"
        code = "search_outcome_unknown" if uncertain else exc.code
        with connection() as conn:
            conn.execute(
                "UPDATE sceneit_searches SET state=%s,error_code=%s,"
                "completed_at=now(),"
                "resolved_at=CASE WHEN %s='failed' THEN now() ELSE NULL END,"
                "resolution=CASE WHEN %s='failed' THEN 'failed' ELSE NULL END "
                "WHERE id=%s AND attempt_id=%s "
                "AND state='running' AND deadline_at>now()",
                (state, code, state, state, search_id, attempt_id),
            )
        reconcile_search_operations()
        if isinstance(exc, ProviderError):
            raise ProofError(
                exc.code, exc.message, 503 if uncertain else 502
            ) from None
        raise
    except Exception:
        with connection() as conn:
            conn.execute(
                "UPDATE sceneit_searches SET state='needs_review',"
                "error_code='search_outcome_unknown',completed_at=now() "
                "WHERE id=%s AND attempt_id=%s AND state='running' "
                "AND deadline_at>now()",
                (search_id, attempt_id),
            )
        reconcile_search_operations()
        raise
    finally:
        if client is not None:
            client.close()
def report():
    proof = public_proof()
    if proof["timelineStatus"] == "verified":
        alignment_limit = (
            "Timeline verification covers representative saved scenes, not every frame of either edit."
        )
    elif proof["timelineStatus"] == "mismatch":
        alignment_limit = (
            "Paired playback found a YouTube edit/timeline mismatch; retained timestamps "
            "may not match the indexed source."
        )
    else:
        alignment_limit = "YouTube edit/timeline alignment is not independently verified."
    return {
        "proof": proof, "searches": list_searches(),
        "limitations": [
            "This proof covers only the supplied video, not a general video library.",
            alignment_limit,
            "First-party playback shows the indexed original; cross-edit claims require paired playback with YouTube.",
            "A source still is extracted at the midpoint of each returned segment.",
            "Confidence labels, when present, are provider categories, not probabilities.",
            "YouTube looping is approximate and subject to availability and browser policies.",
            "The shared proof saves search descriptions; do not enter private information.",
            "A hard cap of 50 submitted provider searches limits proof usage.",
        ] + [check["detail"] for check in proof["checks"] if check["id"] == "embed" and check["status"] == "failed"],
    }

def get_search_operation(search_id):
    reconcile_search_operations()
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM sceneit_searches WHERE proof_id=%s AND id=%s",
            (PROOF_ID, search_id),
        ).fetchone()
    if not row:
        raise ProofError("search_not_found", "Search operation not found.", 404)
    return present_search_operation(row)

def list_search_operations():
    reconcile_search_operations()
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM sceneit_searches WHERE proof_id=%s "
            "ORDER BY created_at DESC LIMIT 50",
            (PROOF_ID,),
        ).fetchall()
    return [present_search_operation(row) for row in rows]

def reconcile_search_operations():
    """Move expired submissions to review without retrying or refunding them."""
    with connection() as conn:
        rows = conn.execute(
            "UPDATE sceneit_searches SET state='needs_review',"
            "error_code='search_outcome_unknown',completed_at=now() "
            "WHERE proof_id=%s AND state='running' AND deadline_at<=now() "
            "RETURNING *",
            (PROOF_ID,),
        ).fetchall()
    return [present_search_operation(row) for row in rows]

def proof_readiness():
    """Summarize local proof dependencies without probing or exposing them."""
    proof = get_proof()
    if not proof:
        raise ProofError(
            "proof_not_initialized", "Proof readiness is unavailable.", 503
        )
    proof_state = proof["state"]
    used = proof["searches_used"]
    limit = proof["search_limit"]
    provider_configured = bool(proof["index_id"] and proof["indexed_asset_id"])
    media = proof.get("media") or {}
    playback = media.get("sourcePlayback") or {}
    media_configured = bool(
        playback.get("objectPath")
        and playback.get("permissionConfirmed") is True
    )
    playback_ready = bool(
        media_configured and playback.get("rightsPolicy") == "public-app-viewers"
    )

    if proof_state == "needs_review":
        state, detail = "uncertain", "The proof requires operator review."
    elif proof_state in {"queued", "uploading", "processing", "indexing"}:
        state, detail = "processing", "The proof is still processing."
    elif proof_state != "ready" or not provider_configured:
        state, detail = (
            "service_unavailable",
            "Semantic search configuration is unavailable.",
        )
    elif used >= limit:
        state, detail = (
            "quota_exhausted",
            "The proof search budget is exhausted; saved results remain available.",
        )
    elif not media_configured:
        state, detail = (
            "ready",
            "Semantic search is ready; proof media is not configured.",
        )
    elif not playback_ready:
        state, detail = (
            "ready",
            "Semantic search is ready; source playback is degraded.",
        )
    else:
        state, detail = "ready", None
    return {
        "state": state,
        "proofState": proof_state,
        "searchAvailable": state == "ready",
        "searchesUsed": used,
        "searchLimit": limit,
        "detail": detail,
        "retryAfterSeconds": None,
    }

def resolve_search_operation(search_id, resolution):
    """Operator resolution.  Resolution never changes cumulative quota."""
    if resolution not in ("confirmed_failed", "review_retained"):
        raise ProofError("invalid_resolution", "Invalid search resolution.", 400)
    state = "failed" if resolution == "confirmed_failed" else "needs_review"
    with connection() as conn:
        row = conn.execute(
            "UPDATE sceneit_searches SET state=%s,resolution=%s,resolved_at=now(),"
            "completed_at=COALESCE(completed_at,now()),"
            "error_code=CASE WHEN %s='failed' THEN 'operator_confirmed_failed' "
            "ELSE COALESCE(error_code,'search_outcome_unknown') END "
            "WHERE proof_id=%s AND id=%s AND state IN ('running','needs_review') "
            "RETURNING *",
            (state, resolution, state, PROOF_ID, search_id),
        ).fetchone()
    if not row:
        raise ProofError(
            "search_not_resolvable",
            "Search operation was not found or is already final.",
            409,
        )
    return present_search_operation(row)
