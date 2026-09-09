"""Small HTTPS-only downloader with SSRF and DNS-rebinding defenses."""
from __future__ import annotations

from contextlib import contextmanager
import http.client
import ipaddress
import os
from pathlib import Path
import socket
import ssl
import tempfile
import time
from typing import Callable, Iterable
from urllib.parse import urljoin, urlsplit


MAX_MEDIA_BYTES = 200_000_000
DEFAULT_TIMEOUT = 20.0


class NetworkSafetyError(ValueError):
    """A remote destination or response failed a safety boundary."""


def _normal_host(host: str | None) -> str:
    if not host:
        raise NetworkSafetyError("The media URL has no host.")
    try:
        value = host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise NetworkSafetyError("The media URL host is invalid.") from None
    return value


def host_allowed(host: str, suffixes: Iterable[str]) -> bool:
    host = _normal_host(host)
    return any(host == suffix or host.endswith("." + suffix)
               for suffix in suffixes)


def validate_https_url(url: str, allowed_hosts: Iterable[str]) -> tuple[str, str]:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise NetworkSafetyError("The media URL is invalid.") from None
    if parsed.scheme.lower() != "https" or port not in (None, 443):
        raise NetworkSafetyError("Only HTTPS on port 443 is permitted.")
    if parsed.username is not None or parsed.password is not None:
        raise NetworkSafetyError("Credentials in media URLs are not permitted.")
    host = _normal_host(parsed.hostname)
    if not host_allowed(host, allowed_hosts):
        raise NetworkSafetyError("The media host is not allowed for this source.")
    return host, parsed.path or "/"


def public_addresses(host: str, *,
                     resolver: Callable[..., list] = socket.getaddrinfo) -> list[str]:
    """Resolve once and retain only globally routable addresses."""
    try:
        records = resolver(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise NetworkSafetyError("The media host could not be resolved.") from None
    addresses: list[str] = []
    for record in records:
        raw = record[4][0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            raise NetworkSafetyError("The media host resolved unexpectedly.") from None
        if not address.is_global:
            raise NetworkSafetyError(
                "Private, reserved, and local network destinations are blocked.")
        if raw not in addresses:
            addresses.append(raw)
    if not addresses:
        raise NetworkSafetyError("The media host has no public address.")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: float) -> None:
        super().__init__(host, 443, timeout=timeout,
                         context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._address, 443), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def download_https(url: str, destination: str | os.PathLike[str], *,
                   allowed_hosts: Iterable[str],
                   max_bytes: int = MAX_MEDIA_BYTES,
                   max_redirects: int = 3,
                   timeout: float = DEFAULT_TIMEOUT,
                   wall_timeout: float = 120.0,
                   headers: dict[str, str] | None = None,
                   progress: Callable[[int, int | None], None] | None = None
                   ) -> None:
    """Download to an atomic local file after pinning every redirect's IP."""
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".media-", dir=target.parent)
    os.close(fd)
    current = url
    deadline = time.monotonic() + wall_timeout
    try:
        for redirect in range(max_redirects + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NetworkSafetyError("The media download exceeded its time limit.")
            host, _ = validate_https_url(current, allowed_hosts)
            parsed = urlsplit(current)
            addresses = public_addresses(host)
            request_headers = {
                "Accept": "video/mp4,application/octet-stream;q=0.8",
                "Accept-Encoding": "identity",
                "Connection": "close",
                "Host": host,
                "User-Agent": "SceneIt-media-import/1",
            }
            for key, value in (headers or {}).items():
                lower = key.lower()
                if lower not in {"user-agent", "referer", "origin",
                                  "accept", "accept-language"}:
                    continue
                if not isinstance(value, str) or len(value) > 1000:
                    continue
                if lower in {"referer", "origin"}:
                    validate_https_url(value, allowed_hosts)
                request_headers[key] = value
            connection = _PinnedHTTPSConnection(
                host, addresses[0], min(timeout, remaining))
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            try:
                connection.request("GET", path, headers=request_headers)
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    response.read(4096)
                    if not location or redirect == max_redirects:
                        raise NetworkSafetyError("The media redirected too many times.")
                    current = urljoin(current, location)
                    validate_https_url(current, allowed_hosts)
                    continue
                if response.status != 200:
                    raise NetworkSafetyError("The platform did not provide the media file.")
                length = response.getheader("Content-Length")
                if length:
                    try:
                        if int(length) > max_bytes:
                            raise NetworkSafetyError("The media exceeds the 200 MB limit.")
                    except ValueError:
                        raise NetworkSafetyError(
                            "The platform returned an invalid media length.") from None
                total = 0
                with open(temporary, "wb") as stream:
                    while chunk := response.read(64 * 1024):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise NetworkSafetyError(
                                "The media download exceeded its time limit.")
                        if connection.sock:
                            connection.sock.settimeout(min(timeout, remaining))
                        total += len(chunk)
                        if total > max_bytes:
                            raise NetworkSafetyError("The media exceeds the 200 MB limit.")
                        stream.write(chunk)
                        if progress:
                            progress(total, int(length) if length else None)
                if total == 0:
                    raise NetworkSafetyError("The platform returned an empty media file.")
                os.replace(temporary, target)
                return
            except (OSError, http.client.HTTPException, ssl.SSLError):
                raise NetworkSafetyError("The media download failed safely.") from None
            finally:
                connection.close()
        raise NetworkSafetyError("The media redirected too many times.")
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextmanager
def pinned_dns(allowed_hosts: Iterable[str]):
    """Pin public DNS answers for an extractor's in-process HTTPS requests.

    yt-dlp is run synchronously while this process-wide guard is installed.
    Callers must serialize resolver execution.
    """
    suffixes = tuple(allowed_hosts)
    original = socket.getaddrinfo
    cache: dict[str, list] = {}

    def guarded(host, port, *args, **kwargs):
        name = _normal_host(host)
        if port not in (443, "443", "https"):
            raise NetworkSafetyError("Extractor network access is HTTPS-only.")
        if not host_allowed(name, suffixes):
            raise NetworkSafetyError("Extractor requested an unapproved host.")
        if name not in cache:
            records = original(name, 443, *args, **kwargs)
            for record in records:
                if not ipaddress.ip_address(record[4][0]).is_global:
                    raise NetworkSafetyError("Extractor resolved a non-public address.")
            if not records:
                raise NetworkSafetyError("Extractor host has no public address.")
            cache[name] = records
        return cache[name]

    socket.getaddrinfo = guarded
    try:
        yield
    finally:
        socket.getaddrinfo = original


@contextmanager
def bounded_extractor_network(allowed_hosts: Iterable[str], *,
                              max_requests: int = 24,
                              max_response_bytes: int = 12_000_000,
                              wall_timeout: float = 45.0):
    """Guard every child-extractor HTTP request, redirect, and response.

    This is process-global by design and must only be used in the dedicated
    short-lived extractor subprocess, never in the web/worker process.
    """
    original_request = http.client.HTTPConnection.request
    original_read = http.client.HTTPResponse.read
    original_readinto = http.client.HTTPResponse.readinto
    started = time.monotonic()
    request_count = 0
    response_bytes = 0

    def check_deadline():
        if time.monotonic() - started > wall_timeout:
            raise NetworkSafetyError("Extractor exceeded its time limit.")

    def guarded_request(connection, method, url, *args, **kwargs):
        nonlocal request_count
        check_deadline()
        request_count += 1
        if request_count > max_requests:
            raise NetworkSafetyError("Extractor made too many network requests.")
        if not isinstance(connection, http.client.HTTPSConnection):
            raise NetworkSafetyError("Extractor attempted non-HTTPS access.")
        host = _normal_host(connection.host)
        if connection.port not in (None, 443) or not host_allowed(host, allowed_hosts):
            raise NetworkSafetyError("Extractor requested an unapproved destination.")
        # Origin-form targets are required. Absolute-form is only for proxies.
        if not isinstance(url, str) or not url.startswith("/") or url.startswith("//"):
            raise NetworkSafetyError("Extractor attempted an unsafe request target.")
        return original_request(connection, method, url, *args, **kwargs)

    def account(data):
        nonlocal response_bytes
        check_deadline()
        amount = data if isinstance(data, int) else len(data or b"")
        response_bytes += amount
        if response_bytes > max_response_bytes:
            raise NetworkSafetyError("Extractor responses exceeded the byte limit.")

    def guarded_read(response, *args, **kwargs):
        data = original_read(response, *args, **kwargs)
        account(data)
        return data

    def guarded_readinto(response, buffer):
        amount = original_readinto(response, buffer)
        account(amount or 0)
        return amount

    with pinned_dns(allowed_hosts):
        http.client.HTTPConnection.request = guarded_request
        http.client.HTTPResponse.read = guarded_read
        http.client.HTTPResponse.readinto = guarded_readinto
        try:
            yield
        finally:
            http.client.HTTPConnection.request = original_request
            http.client.HTTPResponse.read = original_read
            http.client.HTTPResponse.readinto = original_readinto