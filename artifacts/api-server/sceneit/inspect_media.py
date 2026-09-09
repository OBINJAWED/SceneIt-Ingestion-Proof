"""Conservative, bounded MP4 validation before provider expenditure."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import resource
import subprocess


MAX_BYTES = 200_000_000
MIN_DURATION = 4.0
MAX_DURATION = 4 * 60 * 60
# Current Marengo 3.0 requirements (retrieved 2026-09-07):
# https://docs.twelvelabs.io/v1.3/docs/concepts/models/marengo/marengo-3-0#video-file-requirements
# Duration 4 sec–4 hours; resolution 360x360–5184x2160; aspect ratio
# between 1:2.4 and 2.4:1. This app's direct-upload ceiling is stricter.


class MediaInspectionError(ValueError):
    """A safe media-validation failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code, self.message = code, message
        super().__init__(message)


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
    two_gib = 2 * 1024 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (two_gib, two_gib))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024,
                                               16 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=timeout, check=False, env={"PATH": os.environ.get("PATH", "")},
            preexec_fn=_limits if os.name == "posix" else None,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise MediaInspectionError(
            "invalid_media", "The MP4 could not be validated within safe limits.") from None


def _number(value, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise MediaInspectionError("invalid_media", f"The MP4 has no valid {name}.") from None
    if not math.isfinite(result):
        raise MediaInspectionError("invalid_media", f"The MP4 has an invalid {name}.")
    return result


def _fps(value) -> float:
    if not isinstance(value, str) or "/" not in value:
        return _number(value, "frame rate")
    numerator, denominator = value.split("/", 1)
    den = _number(denominator, "frame rate")
    if den == 0:
        raise MediaInspectionError("invalid_media", "The MP4 has an invalid frame rate.")
    return _number(numerator, "frame rate") / den


def _observed_duration(progress: bytes) -> float:
    """Return the greatest timeline position emitted by a complete decode."""
    greatest = -1.0
    try:
        lines = progress.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise MediaInspectionError("invalid_media", "The MP4 timeline is malformed.") from None
    for line in lines:
        if line.startswith("out_time_us="):
            greatest = max(greatest, _number(line.partition("=")[2], "timeline") / 1_000_000)
        elif line.startswith("out_time="):
            value = line.partition("=")[2]
            parts = value.split(":")
            if len(parts) == 3:
                greatest = max(
                    greatest,
                    _number(parts[0], "timeline") * 3600
                    + _number(parts[1], "timeline") * 60
                    + _number(parts[2], "timeline"))
    if greatest < 0:
        raise MediaInspectionError(
            "invalid_media", "The MP4 has no independently observed media timeline.")
    return greatest


def inspect_mp4(path: str | os.PathLike[str]) -> dict:
    file_path = Path(path)
    try:
        size = file_path.stat().st_size
        with file_path.open("rb") as stream:
            header = stream.read(64)
    except OSError:
        raise MediaInspectionError("file_unavailable", "The MP4 file is unavailable.") from None
    if size <= 0 or size > MAX_BYTES:
        raise MediaInspectionError("file_too_large" if size > MAX_BYTES else "invalid_media",
                                   "The MP4 must be non-empty and no larger than 200 MB.")
    if len(header) < 12 or header[4:8] != b"ftyp":
        raise MediaInspectionError("invalid_container", "The file is not an MP4 container.")

    probe = _run([
        "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
        "-show_format", "-show_streams", "-of", "json", str(file_path),
    ], 30)
    if probe.returncode or len(probe.stdout) > 2_000_000:
        raise MediaInspectionError("invalid_media", "The MP4 is malformed or unreadable.")
    try:
        metadata = json.loads(probe.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise MediaInspectionError("invalid_media", "The MP4 metadata is malformed.") from None
    streams = metadata.get("streams")
    file_format = metadata.get("format")
    if not isinstance(streams, list) or not isinstance(file_format, dict):
        raise MediaInspectionError("invalid_media", "The MP4 metadata is incomplete.")
    format_names = str(file_format.get("format_name", "")).split(",")
    if not {"mov", "mp4"}.intersection(format_names):
        raise MediaInspectionError("invalid_container", "The file is not an MP4 container.")
    videos = [item for item in streams if item.get("codec_type") == "video"]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    others = [item for item in streams
              if item.get("codec_type") not in {"video", "audio"}]
    if len(videos) != 1 or len(audios) > 1 or others:
        raise MediaInspectionError(
            "unsupported_streams",
            "Use one H.264 video stream with at most one AAC audio stream.")
    video = videos[0]
    if video.get("codec_name") != "h264" or video.get("pix_fmt") not in {
            "yuv420p", "yuvj420p"}:
        raise MediaInspectionError(
            "unsupported_codec", "Video must use browser-safe H.264 8-bit 4:2:0.")
    if audios and audios[0].get("codec_name") != "aac":
        raise MediaInspectionError("unsupported_codec", "Audio must use AAC, or be omitted.")
    duration = _number(file_format.get("duration"), "duration")
    if duration < MIN_DURATION or duration > MAX_DURATION:
        raise MediaInspectionError("unsupported_duration",
                                   "Video duration must be between 4 seconds and 4 hours.")
    width = int(_number(video.get("width"), "width"))
    height = int(_number(video.get("height"), "height"))
    if width < 360 or height < 360 or width > 5184 or height > 2160:
        raise MediaInspectionError(
            "unsupported_resolution",
            "Video resolution must be from 360x360 through 5184x2160.")
    aspect = width / height
    if aspect < 1 / 2.4 or aspect > 2.4:
        raise MediaInspectionError("unsupported_aspect_ratio",
                                   "Video aspect ratio must be between 1:2.4 and 2.4:1.")
    fps = _fps(video.get("avg_frame_rate"))
    if fps < 1 or fps > 120:
        raise MediaInspectionError("unsupported_frame_rate",
                                   "Video frame rate must be finite and between 1 and 120 fps.")

    validation = _run([
        "ffmpeg", "-v", "error", "-xerror", "-nostdin",
        "-threads", "1",
        "-protocol_whitelist", "file,pipe", "-i", str(file_path),
        "-map", "0:v:0", *(["-map", "0:a:0"] if audios else []),
        "-progress", "pipe:1", "-stats_period", "1",
        "-f", "null", "-",
    ], 60)
    if validation.returncode or len(validation.stdout) > 1_000_000:
        raise MediaInspectionError("invalid_media",
                                   "The MP4 contains malformed or undecodable media.")
    observed_duration = _observed_duration(validation.stdout)
    # Container duration is attacker-controlled metadata.  A complete decode's
    # output timeline independently enforces the provider bound and rejects
    # files that materially understate (or otherwise contradict) that metadata.
    if observed_duration < MIN_DURATION or observed_duration > MAX_DURATION:
        raise MediaInspectionError(
            "unsupported_duration",
            "The observed video duration must be between 4 seconds and 4 hours.")
    tolerance = max(1.0, min(duration, observed_duration) * 0.02)
    if abs(observed_duration - duration) > tolerance:
        raise MediaInspectionError(
            "invalid_media", "The MP4 metadata is inconsistent with its media timeline.")
    try:
        with file_path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        raise MediaInspectionError("file_unavailable", "The MP4 file became unavailable.") from None
    return {
        # Admission and billable units must never use a shorter container claim
        # when the independent decode observed a longer playable timeline.
        "duration": max(duration, observed_duration), "size": size, "width": width, "height": height,
        "hasAudio": bool(audios), "sha256": digest,
        "videoCodec": "h264", "audioCodec": "aac" if audios else None,
    }