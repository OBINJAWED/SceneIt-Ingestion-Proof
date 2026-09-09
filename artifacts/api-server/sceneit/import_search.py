"""Owner-scoped semantic search for private imports."""
import math
import time
import unicodedata
import uuid
from datetime import datetime, timezone

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from .db import connection
from .import_limits import (ImportProblem, OWNER_SEARCH_LIMIT,
                            reserve_search_budget)
from .proof import SEARCH_DEADLINE_SECONDS, SceneQuery
from .provider import ProviderError, TwelveLabsClient
from .resources import ResourceExhausted, shared_permit


def present_search(row):
    return {
        "id": str(row["id"]), "query": row["query"],
        "modality": row["modality"], "createdAt": row["created_at"].isoformat(),
        "latencyMs": row["latency_ms"], "provider": "Twelve Labs",
        "partial": row["partial"], "matches": row["matches"],
    }


def normalize_import_matches(raw, item, search_id):
    data = raw.get("data")
    if not isinstance(data, list):
        raise ImportProblem("provider_response_invalid",
                            "The search provider returned an invalid response.", 502)
    matches, partial = [], False
    duration = float(item["duration_seconds"])
    for candidate in data:
        if not isinstance(candidate, dict):
            partial = True
            continue
        provider_id = candidate.get("video_id") or candidate.get("indexed_asset_id")
        if provider_id != item["indexed_asset_id"]:
            partial = True
            continue
        start, end = candidate.get("start"), candidate.get("end")
        if (isinstance(start, bool) or isinstance(end, bool) or
                not isinstance(start, (int, float)) or
                not isinstance(end, (int, float)) or
                not math.isfinite(start) or not math.isfinite(end) or
                start < 0 or start >= duration or start >= end or
                end > duration + .25):
            partial = True
            continue
        start, end = float(start), min(float(end), duration)
        label = candidate.get("confidence")
        rank = len(matches) + 1
        source_url = item["source_url"]
        if item["source_kind"] == "youtube" and source_url:
            separator = "&" if "?" in source_url else "?"
            source_url = f"{source_url}{separator}t={int(start)}s"
        matches.append({
            "rank": rank, "startSeconds": start, "endSeconds": end,
            "confidenceLabel": label if label in ("high", "medium", "low") else None,
            "frameUrl": f"/api/imports/{item['id']}/searches/{search_id}/frames/{rank}",
            "sourceUrl": source_url,
        })
        if len(matches) == 5:
            break
    if data and not matches:
        raise ImportProblem("provider_mapping_invalid",
                            "The returned scenes could not be safely mapped.", 502)
    return matches, partial


def list_import_searches(owner_id, import_id):
    with connection() as conn:
        exists = conn.execute(
            "SELECT expires_at FROM sceneit_imports WHERE id=%s AND owner_id=%s",
            (import_id, owner_id)).fetchone()
        if not exists:
            raise ImportProblem("import_not_found", "Import not found.", 404)
        if exists["expires_at"] <= datetime.now(exists["expires_at"].tzinfo):
            raise ImportProblem("import_expired", "Import expired.", 410)
        rows = conn.execute(
            "SELECT * FROM sceneit_import_searches WHERE import_id=%s AND "
            "owner_id=%s AND state='done' ORDER BY created_at DESC LIMIT 50",
            (import_id, owner_id)).fetchall()
    return [present_search(row) for row in rows]


def search_import(owner_id, import_id, payload, client_factory=TwelveLabsClient):
    try:
        with shared_permit(
                "search", participant=owner_id,
                lease_seconds=SEARCH_DEADLINE_SECONDS + 5):
            return _search_import(
                owner_id, import_id, payload, client_factory)
    except ResourceExhausted:
        raise ImportProblem(
            "search_busy",
            "Search capacity is busy. Please retry shortly.",
            429) from None


def _search_import(owner_id, import_id, payload, client_factory):
    try:
        query = SceneQuery.model_validate(payload)
    except ValidationError:
        raise ImportProblem("invalid_query",
                            "Enter a description of 1–500 characters and a valid modality.") from None
    key = unicodedata.normalize("NFKC", query.query).casefold()
    search_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    with connection() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext('sceneit-import-search-app'))")
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (owner_id,))
        item = conn.execute(
            "SELECT * FROM sceneit_imports WHERE id=%s AND owner_id=%s FOR UPDATE",
            (import_id, owner_id)).fetchone()
        if not item:
            raise ImportProblem("import_not_found", "Import not found.", 404)
        if item["expires_at"] <= datetime.now(item["expires_at"].tzinfo):
            raise ImportProblem("import_expired", "Import expired.", 410)
        if item["state"] != "ready":
            raise ImportProblem("index_not_ready",
                                "The video must finish indexing before search.", 409)
        if query.modality in ("audio", "both") and not item["has_audio"]:
            raise ImportProblem("audio_unavailable",
                                "This video has no searchable audio; use visual search.", 400)
        conn.execute(
            "UPDATE sceneit_import_searches SET state='needs_review',"
            "error_code='search_outcome_unknown',completed_at=now() "
            "WHERE state='running' AND deadline_at<=now()")
        existing = conn.execute(
            "SELECT * FROM sceneit_import_searches WHERE owner_id=%s AND import_id=%s "
            "AND query_key=%s AND modality=%s",
            (owner_id, import_id, key, query.modality)).fetchone()
        if existing:
            if existing["state"] == "done":
                return present_search(existing)
            raise ImportProblem("search_not_repeatable",
                                "That search is pending or uncertain and will not be resubmitted.", 409)
        recent = conn.execute(
            "SELECT 1 FROM sceneit_import_searches WHERE owner_id=%s AND "
            "created_at > now()-interval '3 seconds' LIMIT 1",
            (owner_id,)).fetchone()
        if recent:
            raise ImportProblem("rate_limited",
                                "Please wait before starting another search.", 429)
        owner_running = conn.execute(
            "SELECT count(*) AS n FROM sceneit_import_searches WHERE owner_id=%s "
            "AND state='running' AND created_at>now()-interval '2 minutes'",
            (owner_id,)).fetchone()["n"]
        app_running = conn.execute(
            "SELECT count(*) AS n FROM sceneit_import_searches WHERE state='running' "
            "AND created_at>now()-interval '2 minutes'").fetchone()["n"]
        if owner_running >= 2 or app_running >= 10:
            raise ImportProblem("search_busy",
                                "Search capacity is busy. Please retry shortly.", 429)
        ok, code = reserve_search_budget(conn, owner_id)
        if not ok:
            raise ImportProblem(code, "The cumulative search allowance has been reached.", 429)
        operation = conn.execute(
            "INSERT INTO sceneit_import_searches"
            "(id,import_id,owner_id,query,query_key,modality,"
            "provider_write_marker,attempt_id,deadline_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,'submitting',%s,"
            "now()+(%s * interval '1 second')) RETURNING deadline_at",
            (search_id, import_id, owner_id, query.query, key, query.modality,
             attempt_id, SEARCH_DEADLINE_SECONDS)).fetchone()
    started = time.monotonic()
    client = None
    try:
        client = client_factory()
        remaining = (
            operation["deadline_at"] - datetime.now(timezone.utc)
        ).total_seconds()
        raw = client.search(item["index_id"], query.query, query.modality,
                            item["indexed_asset_id"],
                            timeout_seconds=remaining)
        matches, partial = normalize_import_matches(raw, item, search_id)
        with connection() as conn:
            row = conn.execute(
                "UPDATE sceneit_import_searches SET state='done',matches=%s,"
                "partial=%s,latency_ms=%s,completed_at=now(),resolved_at=now(),"
                "resolution='completed',provider_write_marker=NULL "
                "WHERE id=%s AND owner_id=%s AND attempt_id=%s "
                "AND state='running' AND deadline_at>now() RETURNING *",
                (Jsonb(matches), partial, int((time.monotonic()-started)*1000),
                  search_id, owner_id, attempt_id)).fetchone()
        if not row:
            raise ImportProblem(
                "search_needs_review",
                "The response arrived after the safe completion window and requires review.",
                409)
        return present_search(row)
    except (ProviderError, ImportProblem) as exc:
        uncertain = (
            isinstance(exc, ProviderError) and exc.ambiguous
        ) or isinstance(exc, ImportProblem)
        with connection() as conn:
            conn.execute(
                "UPDATE sceneit_import_searches SET state=%s,error_code=%s,"
                "completed_at=now(),"
                "resolved_at=CASE WHEN %s='failed' THEN now() ELSE NULL END,"
                "resolution=CASE WHEN %s='failed' THEN 'failed' ELSE NULL END "
                "WHERE id=%s AND attempt_id=%s "
                "AND state='running' AND deadline_at>now()",
                ("needs_review" if uncertain else "failed",
                 "search_outcome_unknown" if uncertain else exc.code,
                 "needs_review" if uncertain else "failed",
                 "needs_review" if uncertain else "failed",
                 search_id, attempt_id))
            conn.execute(
                "UPDATE sceneit_import_searches SET state='needs_review',"
                "error_code='search_outcome_unknown',completed_at=now() "
                "WHERE id=%s AND attempt_id=%s AND state='running' "
                "AND deadline_at<=now()",
                (search_id, attempt_id))
        status = (
            503 if isinstance(exc, ProviderError) and uncertain
            else 502 if isinstance(exc, ProviderError)
            else exc.status
        )
        raise ImportProblem(exc.code, exc.message, status) from None
    except Exception:
        with connection() as conn:
            conn.execute(
                "UPDATE sceneit_import_searches SET state='needs_review',"
                "error_code='search_outcome_unknown',completed_at=now() "
                "WHERE id=%s AND attempt_id=%s AND state='running' "
                "AND deadline_at>now()",
                (search_id, attempt_id))
            conn.execute(
                "UPDATE sceneit_import_searches SET state='needs_review',"
                "error_code='search_outcome_unknown',completed_at=now() "
                "WHERE id=%s AND attempt_id=%s AND state='running' "
                "AND deadline_at<=now()",
                (search_id, attempt_id))
        raise
    finally:
        if client is not None:
            client.close()