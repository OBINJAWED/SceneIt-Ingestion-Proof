"""Flask API. Ingestion is deliberately absent from the public HTTP surface."""
import io
import json
import logging
import os
import subprocess
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, send_file
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from .db import PROOF_ID, connection
from .proof import ProofError, list_searches, public_proof, report, search_scenes

ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger("sceneit")
logging.basicConfig(level=logging.INFO, format="%(message)s")
frame_slots = threading.BoundedSemaphore(2)
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config["MAX_CONTENT_LENGTH"] = 4096


@app.before_request
def protect_requests():
    request.request_id = uuid.uuid4().hex
    if request.method != "POST":
        return
    origin = request.headers.get("Origin")
    if origin and urlsplit(origin).netloc != request.host:
        raise ProofError("origin_rejected", "Cross-site requests are not permitted.", 403)
    if not request.is_json:
        raise ProofError("json_required", "Send a JSON scene description.", 400)


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Request-ID"] = getattr(request, "request_id", "")
    if response.mimetype == "application/json":
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.get("/api/healthz")
def health():
    return jsonify(status="ok")


@app.get("/api/readyz")
def readiness():
    with connection() as conn:
        conn.execute("SELECT 1")
    return jsonify(status="ready")


@app.get("/api/proof")
def proof_status():
    return jsonify(public_proof())


@app.get("/api/proof/searches")
def history():
    return jsonify(list_searches())


@app.post("/api/proof/searches")
def semantic_search():
    result = search_scenes(request.get_json())
    logger.info(json.dumps({"event": "search_completed", "request_id": request.request_id,
                            "matches": len(result["matches"]), "latency_ms": result["latencyMs"]}))
    return jsonify(result)


@app.get("/api/proof/report")
def evidence():
    return jsonify(report())


@app.get("/api/proof/searches/<uuid:search_id>/frames/<int:rank>")
def source_frame(search_id, rank):
    with connection() as conn:
        row = conn.execute(
            "SELECT s.matches, p.source_path FROM sceneit_searches s "
            "JOIN sceneit_proofs p ON p.id = s.proof_id "
            "WHERE s.id = %s AND p.id = %s AND s.state = 'done'",
            (search_id, PROOF_ID),
        ).fetchone()
    if not row or rank < 1 or rank > len(row["matches"]):
        raise ProofError("frame_not_found", "Source frame not found.", 404)
    source = (ROOT / row["source_path"]).resolve()
    if not source.is_relative_to(ROOT / "attached_assets") or not source.is_file():
        raise ProofError("source_unavailable", "The source file is unavailable on this server.", 404)
    match = row["matches"][rank - 1]
    midpoint = (match["startSeconds"] + match["endSeconds"]) / 2
    if not frame_slots.acquire(blocking=False):
        raise ProofError("frame_busy", "Source frame extraction is busy. Please retry.", 429)
    try:
        frame = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", str(midpoint), "-i", str(source),
             "-frames:v", "1", "-vf", "scale=640:-2", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
            capture_output=True, timeout=20, check=True,
        ).stdout
        if not frame:
            raise ProofError("frame_unavailable", "The source frame could not be extracted.", 503)
        response = send_file(io.BytesIO(frame), mimetype="image/jpeg")
        response.headers["Cache-Control"] = "private, max-age=86400"
        return response
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        raise ProofError("frame_unavailable", "The source frame could not be extracted.", 503) from None
    finally:
        frame_slots.release()


@app.errorhandler(ProofError)
def expected_error(error):
    return jsonify(error=error.message, code=error.code), error.status


@app.errorhandler(ValidationError)
def validation_error(_error):
    return jsonify(error="Enter a description of 1–500 characters and a valid search modality.", code="invalid_query"), 400


@app.errorhandler(HTTPException)
def http_error(error):
    return jsonify(error=error.description, code=f"http_{error.code}"), error.code


@app.errorhandler(Exception)
def unexpected_error(error):
    # No exception repr: it may contain network credentials or private file URLs.
    logger.error(json.dumps({"event": "request_failed", "request_id": getattr(request, "request_id", None),
                             "type": type(error).__name__}))
    return jsonify(error="The proof service could not complete this request.", code="internal_error"), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ["PORT"]), debug=False)