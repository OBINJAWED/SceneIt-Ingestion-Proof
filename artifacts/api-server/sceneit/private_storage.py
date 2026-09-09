"""Private, generation-aware Replit App Storage helpers.

Server operations use GCS through Replit's local external-account sidecar.
There are deliberately no public ACL helpers or public serving routes here.
"""
import base64
import logging
import mimetypes
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from flask import (
    Response, current_app, has_request_context, jsonify, request,
    stream_with_context,
)

SIDECAR = "http://127.0.0.1:1106"
DEFAULT_MAX_BYTES = 200_000_000
UPLOAD_RESERVATION_SECONDS = 15 * 60

# httpx logs complete request URLs at INFO. Resumable upload URLs are bearer
# credentials, so storage requests must never inherit the application's INFO
# root logger. This applies to both convenience APIs and Client instances.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _client():
    from google.auth.identity_pool import Credentials
    from google.cloud.storage import Client

    credentials = Credentials.from_info(
        {
            "audience": "replit",
            "subject_token_type": "access_token",
            "token_url": f"{SIDECAR}/token",
            "type": "external_account",
            "credential_source": {
                "url": f"{SIDECAR}/credential",
                "format": {
                    "type": "json",
                    "subject_token_field_name": "access_token",
                },
            },
        }
    )
    return Client(project="", credentials=credentials)


def _private_root():
    root = os.environ["PRIVATE_OBJECT_DIR"].strip("/")
    if "/" not in root:
        raise RuntimeError("PRIVATE_OBJECT_DIR must include a bucket and prefix")
    return root


def _parts(path):
    value = path.strip("/")
    root = _private_root()
    if not value.startswith(root + "/"):
        raise ValueError("Object path is outside private storage")
    bucket, name = value.split("/", 1)
    if not name or any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError("Invalid private object path")
    return bucket, name


def _blob(path, generation=None):
    bucket, name = _parts(path)
    blob = _client().bucket(bucket).blob(name, generation=generation)
    return blob


def reserve_upload(object_path, expected_bytes):
    """Create an exact-size, origin-bound, create-only resumable upload.

    The returned ``expiresAt`` is the application's 15-minute acceptance
    deadline.  GCS controls the bearer session's longer provider lifetime, so
    callers must persist ``sessionReference`` privately and cancel it when the
    reservation expires, is cancelled, or is replaced.  Completion code must
    reject uploads after ``expiresAt`` and validate the immutable generation.
    """
    _parts(object_path)
    if (
        isinstance(expected_bytes, bool)
        or not isinstance(expected_bytes, int)
        or expected_bytes < 1
        or expected_bytes > DEFAULT_MAX_BYTES
    ):
        raise ValueError("Upload size must be between 1 and 200000000 bytes")
    if not has_request_context():
        raise RuntimeError("Upload reservations require a request origin")
    origin = request.headers.get("Origin") or request.url_root.rstrip("/")
    parsed_origin = urlsplit(origin)
    parsed_app = urlsplit(request.url_root)
    if (
        parsed_origin.scheme not in ("https", "http")
        or parsed_origin.netloc != parsed_app.netloc
        or parsed_origin.path not in ("", "/")
        or parsed_origin.query
        or parsed_origin.fragment
    ):
        raise ValueError("Upload origin does not match this application")
    upload_url = _blob(object_path).create_resumable_upload_session(
        content_type="video/mp4",
        size=expected_bytes,
        origin=f"{parsed_origin.scheme}://{parsed_origin.netloc}",
        if_generation_match=0,
        timeout=30,
    )
    expires = datetime.now(timezone.utc) + timedelta(
        seconds=UPLOAD_RESERVATION_SECONDS
    )
    return {
        "uploadURL": upload_url,
        "method": "PUT",
        "headers": {
            "Content-Type": "video/mp4",
            "Content-Range": (
                f"bytes 0-{expected_bytes - 1}/{expected_bytes}"
            ),
        },
        "expiresAt": expires.isoformat(),
        # Store this encrypted value server-side; never log either value.
        "sessionReference": encrypt_upload_session(upload_url),
    }


def _session_reference_key():
    """Derive a domain-separated Fernet key without retaining another secret."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    secret = (
        current_app.config.get("SESSION_SECRET")
        if has_request_context()
        else os.environ.get("SESSION_SECRET")
    )
    if not secret:
        raise RuntimeError("SESSION_SECRET is required")
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"sceneit-private-storage-v1",
        info=b"gcs-resumable-session-reference",
    ).derive(secret.encode("utf-8"))
    return base64.urlsafe_b64encode(key)


def encrypt_upload_session(upload_url):
    """Encrypt a GCS bearer session URL for private database persistence."""
    from cryptography.fernet import Fernet

    if not isinstance(upload_url, str) or not upload_url.startswith("https://"):
        raise ValueError("Invalid upload session URL")
    return Fernet(_session_reference_key()).encrypt(
        upload_url.encode("utf-8")
    ).decode("ascii")


def decrypt_upload_session(session_reference):
    """Decrypt a persisted session reference for worker cancellation only."""
    from cryptography.fernet import Fernet, InvalidToken

    try:
        value = Fernet(_session_reference_key()).decrypt(
            session_reference.encode("ascii")
        ).decode("utf-8")
    except (AttributeError, UnicodeError, ValueError, InvalidToken) as error:
        raise ValueError("Invalid upload session reference") from error
    if not value.startswith("https://"):
        raise ValueError("Invalid upload session URL")
    return value


def cancel_upload_session(url):
    """Best-effort revocation of a GCS resumable bearer session.

    The URL is intentionally excluded from all error text.
    """
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("Invalid upload session URL")
    with httpx.Client(timeout=30) as client:
        response = client.delete(url)
    if response.status_code not in (200, 204, 404, 410, 499):
        raise RuntimeError(
            f"Storage rejected upload-session cancellation "
            f"(status {response.status_code})"
        )
    return response.status_code not in (404, 410)


def object_info(path):
    blob = _blob(path)
    blob.reload(timeout=10, retry=None)
    return {
        "path": path,
        "size": int(blob.size),
        "generation": int(blob.generation),
        "contentType": blob.content_type or "application/octet-stream",
        "etag": blob.etag,
        "updatedAt": blob.updated.isoformat() if blob.updated else None,
    }


def download_object(path, destination, max_bytes=DEFAULT_MAX_BYTES, generation=None):
    if max_bytes < 0:
        raise ValueError("max_bytes must not be negative")
    info = object_info(path)
    if generation is not None and int(generation) != info["generation"]:
        raise RuntimeError("Object generation changed")
    if info["size"] > max_bytes:
        raise ValueError("Object exceeds download limit")
    blob = _blob(path, info["generation"])
    close = False
    if hasattr(destination, "write"):
        handle = destination
    else:
        handle = Path(destination).open("wb")
        close = True
    try:
        blob.download_to_file(
            handle, if_generation_match=info["generation"], timeout=120
        )
    finally:
        if close:
            handle.close()
    return info


def upload_private(source, path):
    """Upload once; an existing generation can never be overwritten."""
    blob = _blob(path)
    content_type = mimetypes.guess_type(str(getattr(source, "name", source)))[0]
    content_type = content_type or "application/octet-stream"
    if hasattr(source, "read"):
        blob.upload_from_file(
            source, rewind=True, content_type=content_type,
            if_generation_match=0, timeout=120,
        )
    else:
        blob.upload_from_filename(
            str(source), content_type=content_type,
            if_generation_match=0, timeout=120,
        )
    return object_info(path)


def delete_object(path, generation=None):
    blob = _blob(path, generation)
    options = {"if_generation_match": int(generation)} if generation else {}
    blob.delete(timeout=30, **options)


_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


def _range(value, size):
    if not value:
        return None
    match = _RANGE.fullmatch(value)
    if not match or not any(match.groups()) or size == 0:
        return False
    start_text, end_text = match.groups()
    if not start_text:
        length = int(end_text)
        if length < 1:
            return False
        start, end = max(0, size - length), size - 1
    else:
        start = int(start_text)
        end = min(int(end_text), size - 1) if end_text else size - 1
    if start >= size or start > end:
        return False
    return start, end


def open_private(path, range_header=None, generation=None):
    """Return a bounded, private Flask response, including strict Range handling."""
    info = object_info(path)
    if generation is not None and int(generation) != info["generation"]:
        from .http import problem_response
        return problem_response(
            "Object generation changed.", "object_generation_changed", 409
        )
    selected = _range(range_header, info["size"])
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "Content-Type": info["contentType"],
        "ETag": info["etag"] or "",
    }
    if selected is False:
        headers["Content-Range"] = f"bytes */{info['size']}"
        from .http import problem_response
        response = problem_response(
            "The byte range is not satisfiable.",
            "range_not_satisfiable", 416,
        )
        response.headers.update(headers)
        return response
    start, end = selected if selected else (0, info["size"] - 1)
    length = max(0, end - start + 1)
    if length > DEFAULT_MAX_BYTES:
        raise ValueError("Object response exceeds download limit")
    headers["Content-Length"] = str(length)
    status = 206 if selected else 200
    if selected:
        headers["Content-Range"] = f"bytes {start}-{end}/{info['size']}"

    @stream_with_context
    def body():
        remaining = length
        if remaining == 0:
            return
        handle = _blob(path, info["generation"]).open(
            "rb",
            chunk_size=1024 * 1024,
            if_generation_match=info["generation"],
            timeout=120,
        )
        try:
            if start:
                handle.seek(start)
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError("Storage response ended unexpectedly")
                if len(chunk) > remaining:
                    raise RuntimeError("Storage exceeded the response boundary")
                remaining -= len(chunk)
                yield chunk
        finally:
            handle.close()

    return Response(body(), status=status, headers=headers)
