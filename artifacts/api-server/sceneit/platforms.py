"""Canonical platform links and narrowly constrained direct-media imports."""
from __future__ import annotations

import re
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

from .safe_network import (MAX_MEDIA_BYTES, NetworkSafetyError,
                           bounded_extractor_network, download_https,
                           validate_https_url)


class PlatformError(ValueError):
    """A safe structured link failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code, self.message = code, message
        super().__init__(message)


class FileRequired(PlatformError):
    """The link may be retained, but an authorized MP4 is required."""


_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_DIGITS = re.compile(r"^[0-9]{5,30}$")
_HOSTS = {
    "tiktok": ("tiktok.com", "tiktokcdn.com", "tiktokcdn-us.com",
               "tiktokv.com", "byteoversea.com", "ibytedtos.com", "musical.ly"),
    "x": ("x.com", "twitter.com", "twimg.com"),
    "vimeo": ("vimeo.com", "vimeocdn.com", "akamaized.net"),
}
_EXTRACTORS = {
    "tiktok": r"TikTok(?:VM)?$",
    "x": r"Twitter$",
    "vimeo": r"Vimeo$",
}


def _result(kind: str, url: str, external_id: str | None, title: str,
            message: str) -> dict:
    return {"sourceKind": kind, "sourceUrl": url, "externalId": external_id,
            "title": title, "message": message}


def canonicalize_link(url: str) -> dict:
    if not isinstance(url, str) or len(url) > 2048:
        raise PlatformError("invalid_url", "Enter a valid supported video link.")
    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except ValueError:
        raise PlatformError("invalid_url", "Enter a valid supported video link.") from None
    if parsed.scheme.lower() != "https" or port not in (None, 443):
        raise PlatformError("unsafe_url", "Video links must use HTTPS on port 443.")
    if parsed.username is not None or parsed.password is not None:
        raise PlatformError("unsafe_url", "Links containing credentials are not accepted.")
    host = (parsed.hostname or "").lower().rstrip(".")
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    parts = [part for part in path.split("/") if part]
    query = parse_qs(parsed.query)

    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if query.get("list"):
            raise PlatformError("collection_unsupported",
                                "YouTube playlists are not supported.")
        video_id = None
        if path == "/watch":
            values = query.get("v", [])
            video_id = values[0] if len(values) == 1 else None
        elif len(parts) == 2 and parts[0] == "shorts":
            video_id = parts[1]
        elif parts and parts[0] in {"live", "playlist", "channel", "user",
                                    "c", "embed"}:
            raise PlatformError("unsupported_video",
                                "Live, collection, channel, and embed links are not supported.")
        if not video_id or not _ID.fullmatch(video_id):
            raise PlatformError("unsupported_video", "Use a single YouTube video or Shorts link.")
        return _result("youtube", f"https://www.youtube.com/watch?v={video_id}",
                       video_id, "YouTube video",
                       "Unverified YouTube source context only; live/upcoming video is unsupported. "
                       "Upload an authorized MP4 to analyze it.")

    if host == "youtu.be":
        if query.get("list"):
            raise PlatformError("collection_unsupported",
                                "YouTube playlists are not supported.")
        if len(parts) != 1 or not _ID.fullmatch(parts[0]):
            raise PlatformError("unsupported_video", "Use a single YouTube short link.")
        return _result("youtube", f"https://www.youtube.com/watch?v={parts[0]}",
                       parts[0], "YouTube video",
                       "Unverified YouTube source context only; live/upcoming video is unsupported. "
                       "Upload an authorized MP4 to analyze it.")

    if host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com",
                "mobile.twitter.com"}:
        if len(parts) != 3 or parts[1] != "status" or not _DIGITS.fullmatch(parts[2]):
            raise PlatformError("unsupported_video", "Use a single X/Twitter post link.")
        return _result("x", f"https://x.com/{parts[0]}/status/{parts[2]}",
                       parts[2], "X/Twitter post",
                       "SceneIt will attempt one public progressive MP4; a file may be required.")

    if host in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}:
        if len(parts) == 3 and parts[0].startswith("@") and parts[1] == "video" \
                and _DIGITS.fullmatch(parts[2]):
            return _result("tiktok",
                           f"https://www.tiktok.com/{parts[0]}/video/{parts[2]}",
                           parts[2], "TikTok video",
                           "SceneIt will attempt one public progressive MP4; a file may be required.")
        if len(parts) == 2 and parts[0] == "t" and \
                re.fullmatch(r"[A-Za-z0-9]+", parts[1]):
            return _result("tiktok", f"https://www.tiktok.com/t/{parts[1]}/",
                           None, "TikTok shared video",
                           "SceneIt will verify this resolves to one public video; a file may be required.")
        raise PlatformError("unsupported_video", "Use a single TikTok video link.")
    if host in {"vm.tiktok.com", "vt.tiktok.com"}:
        if len(parts) != 1 or not re.fullmatch(r"[A-Za-z0-9]+", parts[0]):
            raise PlatformError("unsupported_video", "Use a valid TikTok share link.")
        return _result("tiktok", f"https://{host}/{parts[0]}/", None,
                       "TikTok shared video",
                       "SceneIt will verify this resolves to one public video; a file may be required.")

    if host in {"vimeo.com", "www.vimeo.com", "player.vimeo.com"}:
        if host == "player.vimeo.com":
            valid = len(parts) == 2 and parts[0] == "video"
            video_id = parts[1] if valid else None
        else:
            valid = len(parts) == 1
            video_id = parts[0] if valid else None
        if not video_id or not _DIGITS.fullmatch(video_id):
            raise PlatformError("unsupported_video",
                                "Use an ordinary single Vimeo video link, not a collection or live event.")
        return _result("vimeo", f"https://vimeo.com/{video_id}", video_id,
                       "Vimeo video",
                       "SceneIt will attempt one public progressive MP4; a file may be required.")

    raise PlatformError("unsupported_host",
                        "Supported links are YouTube, X/Twitter, TikTok, and Vimeo.")


def _single_progressive(info: dict, kind: str) -> tuple[dict, str]:
    if info.get("_type") in {"playlist", "multi_video"} or info.get("entries"):
        raise PlatformError("ambiguous_media", "The link contains multiple videos.")
    if info.get("is_live") or info.get("live_status") in {
            "is_live", "is_upcoming", "post_live"}:
        raise PlatformError("live_unsupported", "Live and upcoming video is not supported.")
    if info.get("has_drm"):
        raise PlatformError("restricted_media", "DRM-protected video is not supported.")
    if info.get("availability") in {"private", "premium_only", "subscriber_only",
                                    "needs_auth"}:
        raise PlatformError("restricted_media",
                            "Private, paid, and account-restricted video is not supported.")
    formats = info.get("formats")
    if not isinstance(formats, list):
        formats = [info] if info.get("url") else []
    if not formats:
        raise PlatformError("non_video", "The link does not contain a downloadable video.")
    choices = []
    seen_urls: set[str] = set()
    has_separate_audio = any(
        isinstance(item, dict) and item.get("vcodec") in (None, "none") and
        item.get("acodec") not in (None, "none")
        for item in formats
    )
    for item in formats:
        if not isinstance(item, dict):
            continue
        media_url = item.get("url")
        if not isinstance(media_url, str):
            continue
        if item.get("vcodec") in (None, "none"):
            continue
        if (item.get("ext") or "").lower() != "mp4":
            continue
        if item.get("protocol") not in (None, "https"):
            continue
        if any(item.get(key) for key in ("manifest_url", "fragments")):
            continue
        size = item.get("filesize") or item.get("filesize_approx")
        if isinstance(size, (int, float)) and size > MAX_MEDIA_BYTES:
            continue
        try:
            validate_https_url(media_url, _HOSTS[kind])
        except NetworkSafetyError:
            continue
        if media_url in seen_urls:
            continue
        seen_urls.add(media_url)
        choices.append(item)
    if not choices:
        raise FileRequired(
            "file_required",
            "A safe progressive MP4 was not available. Upload an authorized MP4.")
    muxed = [item for item in choices if item.get("acodec") not in (None, "none")]
    if muxed:
        choices = muxed
    elif has_separate_audio:
        raise FileRequired(
            "file_required",
            "Only split video and audio were available. Upload an authorized MP4.")
    # Multiple encodes of the same single video are not content ambiguity. Pick
    # deterministically by resolution/bitrate, allowing the bounded downloader
    # to enforce the ceiling when an extractor cannot report a byte size.
    choices.sort(key=lambda item: (
        item.get("height") if isinstance(item.get("height"), (int, float)) else 0,
        item.get("width") if isinstance(item.get("width"), (int, float)) else 0,
        item.get("tbr") if isinstance(item.get("tbr"), (int, float)) else 0,
        str(item.get("format_id", "")),
    ), reverse=True)
    return choices[0], choices[0]["url"]


def _child_limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (50, 50))
    resource.setrlimit(resource.RLIMIT_AS, (1024 ** 3, 1024 ** 3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (3_000_000, 3_000_000))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


class _QuietLogger:
    """Discard extractor text because errors may contain signed media URLs."""

    def debug(self, message):  # pragma: no cover - yt-dlp callback API
        pass

    info = warning = error = debug


def _safe_youtube_dl(options: dict):
    """Build yt-dlp with exactly the guarded stdlib urllib transport."""
    import yt_dlp
    from yt_dlp.networking._urllib import UrllibRH

    class SafeYoutubeDL(yt_dlp.YoutubeDL):
        """Force the one transport covered by bounded_extractor_network."""

        def build_request_director(self, handlers, preferences=None):
            # Do not permit optional requests, curl_cffi, websocket, plugin, or
            # future ambient handlers to bypass the pinned urllib/http.client
            # transport guarded by bounded_extractor_network.
            return super().build_request_director([UrllibRH], preferences=[])

    return SafeYoutubeDL(options)


def _extract_in_child(kind: str, url: str) -> dict:
    """Entry point used only by the credential-free isolated subprocess."""
    options = {
        "allowed_extractors": [_EXTRACTORS[kind]],
        "extract_flat": "in_playlist",
        "ignoreconfig": True,
        "noplaylist": False,
        "socket_timeout": 12,
        "retries": 0,
        "fragment_retries": 0,
        "proxy": "",
        "cookiefile": None,
        "usenetrc": False,
        "username": None,
        "password": None,
        "plugin_dirs": [],
        "external_downloader": None,
        "js_runtimes": {},
        "remote_components": set(),
        "client_certificate": None,
        "client_certificate_key": None,
        "client_certificate_password": None,
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietLogger(),
    }
    with bounded_extractor_network(_HOSTS[kind]), _safe_youtube_dl(options) as ydl:
        if set(ydl._request_director.handlers) != {"Urllib"}:
            raise PlatformError("unsafe_transport",
                                "The extractor transport could not be restricted.")
        raw = ydl.extract_info(url, download=False)
    if not isinstance(raw, dict):
        raise PlatformError("invalid_media", "The platform returned invalid media details.")
    # Return only fields used by policy. Never serialize descriptions, comments,
    # cookies, or arbitrary extractor payloads across the process boundary.
    summary = {key: raw.get(key) for key in (
        "_type", "id", "title", "is_live", "live_status", "availability",
        "has_drm", "webpage_url", "uploader_id")}
    entries = raw.get("entries")
    if entries is not None:
        summary["entryCount"] = len(entries) if isinstance(entries, list) else 1
    formats = raw.get("formats")
    if isinstance(formats, list):
        summary["formats"] = [
            {key: item.get(key) for key in (
                "url", "ext", "protocol", "vcodec", "acodec", "manifest_url",
                "fragments", "filesize", "filesize_approx", "height", "width",
                "tbr", "format_id", "http_headers")}
            for item in formats if isinstance(item, dict)
        ]
    return summary


def _isolated_extract(kind: str, url: str) -> dict:
    package_root = str(Path(__file__).resolve().parents[1])
    code = (
        "import json,sys;"
        f"sys.path.insert(0,{package_root!r});"
        "from sceneit.platforms import _extract_in_child;"
        f"print(json.dumps(_extract_in_child({kind!r},{url!r}),separators=(',',':')))"
    )
    clean_home = tempfile.mkdtemp(prefix="sceneit-extractor-")
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": clean_home,
        "XDG_CONFIG_HOME": clean_home,
        "PYTHONNOUSERSITE": "1",
        "YTDLP_NO_PLUGINS": "1",
        "NO_PROXY": "*",
        "no_proxy": "*",
    }
    try:
        with tempfile.TemporaryFile() as output:
            process = subprocess.run(
                [sys.executable, "-I", "-c", code], stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.DEVNULL, timeout=50,
                check=False, env=environment,
                preexec_fn=_child_limits if os.name == "posix" else None,
            )
            output_size = output.tell()
            output.seek(0)
            stdout = output.read(2_000_001)
    except (OSError, subprocess.TimeoutExpired):
        raise FileRequired("file_required",
                           "This link could not be imported safely. Upload an authorized MP4.") from None
    finally:
        try:
            os.rmdir(clean_home)
        except OSError:
            pass
    if process.returncode or output_size > 2_000_000:
        raise FileRequired("file_required",
                           "This link could not be imported safely. Upload an authorized MP4.")
    try:
        result = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise FileRequired("file_required",
                           "This link could not be imported safely. Upload an authorized MP4.") from None
    if not isinstance(result, dict):
        raise FileRequired("file_required",
                           "This link could not be imported safely. Upload an authorized MP4.")
    if result.pop("entryCount", 0):
        result["entries"] = [None]
    return result


def resolve_link(source_dict: dict, destination_path: str | Path,
                 progress=None) -> dict:
    if not isinstance(source_dict, dict):
        raise PlatformError("invalid_source", "The link source details are invalid.")
    canonical = canonicalize_link(source_dict.get("sourceUrl", ""))
    kind = canonical["sourceKind"]
    if kind == "youtube":
        # Deliberately before importing or constructing YoutubeDL. YouTube's
        # public oEmbed response does not expose live/upcoming state, and the
        # official Data API requires credentials. Therefore we reject explicit
        # /live forms during canonicalization and truthfully leave watch/Shorts
        # live state unverified rather than scraping or inferring from duration.
        raise FileRequired("file_required",
                           "YouTube downloads are disabled and live status is unverified. "
                           "Upload an authorized MP4 for analysis.")
    try:
        info = _isolated_extract(kind, canonical["sourceUrl"])
        resolved_tiktok = None
        if kind == "tiktok" and canonical["externalId"] is None:
            resolved = info.get("webpage_url")
            if not isinstance(resolved, str):
                raise PlatformError(
                    "invalid_media", "TikTok share link did not resolve to one video.")
            resolved_tiktok = canonicalize_link(resolved)
            if resolved_tiktok["sourceKind"] != "tiktok" or \
                    not resolved_tiktok["externalId"]:
                raise PlatformError(
                    "invalid_media", "TikTok share link did not resolve to one video.")
        selected, media_url = _single_progressive(info, kind)
        headers = selected.get("http_headers")
        download_https(media_url, destination_path, allowed_hosts=_HOSTS[kind],
                       headers=headers if isinstance(headers, dict) else None,
                       progress=progress)
    except FileRequired:
        raise
    except PlatformError:
        raise
    except Exception:
        raise FileRequired(
            "file_required",
            "This link could not be imported safely. Upload an authorized MP4.") from None
    title = info.get("title")
    if not isinstance(title, str) or not title.strip():
        title = canonical["title"]
    external_id = info.get("id")
    if not isinstance(external_id, str):
        external_id = canonical["externalId"]
    source_url = canonical["sourceUrl"]
    if kind == "tiktok" and canonical["externalId"] is None:
        source_url = resolved_tiktok["sourceUrl"]
        external_id = resolved_tiktok["externalId"]
    return {"title": title[:300], "sourceUrl": source_url,
            "externalId": external_id}