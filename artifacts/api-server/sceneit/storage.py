"""Minimal Replit App Storage access through the local sidecar."""
import os
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

SIDECAR = "http://127.0.0.1:1106"
logging.getLogger("httpx").setLevel(logging.WARNING)


def private_object_path(object_name):
    base = os.environ["PRIVATE_OBJECT_DIR"].rstrip("/")
    return f"{base}/{object_name.lstrip('/')}"


def parse_object_path(path):
    parts = path.strip("/").split("/", 1)
    if len(parts) != 2 or not all(parts):
        raise ValueError("Invalid App Storage object path")
    return parts[0], parts[1]


def signed_url(path, method, ttl_seconds=300):
    bucket, object_name = parse_object_path(path)
    response = httpx.post(
        f"{SIDECAR}/object-storage/signed-object-url",
        json={
            "bucket_name": bucket,
            "object_name": object_name,
            "method": method,
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(),
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["signed_url"]


def upload_source(source, object_path):
    """Copy the authorized existing source once; this never touches Twelve Labs."""
    head = httpx.head(signed_url(object_path, "HEAD"), timeout=30)
    if head.status_code == 200:
        if int(head.headers.get("content-length", -1)) != source.stat().st_size:
            raise RuntimeError("Stored source exists with an unexpected size")
        return False
    if head.status_code != 404:
        head.raise_for_status()
    with source.open("rb") as handle:
        response = httpx.put(
            signed_url(object_path, "PUT", 900),
            content=handle,
            headers={"Content-Type": "video/mp4"},
            timeout=300,
        )
    response.raise_for_status()
    return True


def safe_content_range(value):
    if not value:
        return None
    # A single byte range is enough for native HTML video seeking.
    import re
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if not match or not any(match.groups()):
        return None
    start, end = match.groups()
    if start and end and int(start) > int(end):
        return None
    return value