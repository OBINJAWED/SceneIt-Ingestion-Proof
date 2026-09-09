"""Small, synchronous Twelve Labs v1.3 HTTP client.

Routes and fields are from the current Twelve Labs reference:
https://docs.twelvelabs.io/api-reference/upload-files/direct-uploads/create.md
https://docs.twelvelabs.io/api-reference/manage-assets/retrieve.md
https://docs.twelvelabs.io/api-reference/index-content/create.md
https://docs.twelvelabs.io/api-reference/index-content/retrieve.md
https://docs.twelvelabs.io/api-reference/any-to-video-search/make-search-request.md
https://docs.twelvelabs.io/api-reference/indexes/create.md
https://docs.twelvelabs.io/api-reference/indexes/list.md
"""

from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import httpx

from .processes import kill_and_wait, start_guarded


BASE_URL = "https://api.twelvelabs.io/v1.3"
NORMAL_TIMEOUT = httpx.Timeout(45.0)
UPLOAD_TIMEOUT = httpx.Timeout(300.0)
MAX_RESPONSE_BYTES = 2_000_000
SEARCH_PROCESS_TIMEOUT_SECONDS = 45.0


class ProviderError(Exception):
    """A safe, structured provider failure."""

    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 ambiguous: bool = False,
                 http_status: int | None = None) -> None:
        self.code, self.message = code, message
        self.retryable, self.ambiguous = retryable, ambiguous
        self.http_status = http_status
        super().__init__(message)

    def __repr__(self) -> str:
        return (
            f"ProviderError(code={self.code!r}, message={self.message!r}, "
            f"retryable={self.retryable!r}, ambiguous={self.ambiguous!r}, "
            f"http_status={self.http_status!r})"
        )


class TwelveLabsClient:
    """One-video proof client; credentials are read only when instantiated."""

    def __init__(self) -> None:
        if os.environ.get("SCENEIT_DISABLE_PROVIDER_NETWORK", "").lower() in (
                "1", "true", "yes", "on"):
            raise ProviderError(
                "provider_network_disabled",
                "Provider network access is disabled for this process.")
        api_key = os.environ.get("TWELVE_LABS_API_KEY")
        if not api_key:
            raise ProviderError("missing_credentials",
                                "TWELVE_LABS_API_KEY is not configured.")
        self._api_key = api_key
        base_url = BASE_URL
        if os.environ.get("SCENEIT_ALLOW_PROVIDER_TEST_ENDPOINT") == "1":
            base_url = os.environ.get(
                "SCENEIT_PROVIDER_TEST_BASE_URL", BASE_URL)
        self._client = httpx.Client(
            base_url=base_url,
            headers={"x-api-key": api_key, "Accept": "application/json"},
        )

    def __repr__(self) -> str:
        return "TwelveLabsClient(base_url='https://api.twelvelabs.io/v1.3')"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TwelveLabsClient:
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    @staticmethod
    def _retryable_status(status: int) -> bool:
        return status == 429 or 500 <= status <= 599

    @staticmethod
    def _wait_seconds(response: httpx.Response) -> float:
        value = response.headers.get("retry-after")
        if value:
            try:
                return min(5.0, max(0.0, float(value)))
            except ValueError:
                pass
        return 0.5

    def _safe_error(self, response: httpx.Response) -> ProviderError:
        # Provider payloads can echo submitted text or credentials.  They are
        # intentionally neither parsed nor copied into exceptions/logs.
        code = f"provider_http_{response.status_code}"
        message = "The search provider rejected the request."
        return ProviderError(
            code, message, retryable=self._retryable_status(response.status_code),
            http_status=response.status_code)

    def _request(self, method: str, path: str, *,
                 timeout: httpx.Timeout = NORMAL_TIMEOUT,
                 mutation: bool = False, **kwargs: Any) -> dict[str, Any]:
        attempts = 1 if mutation or method != "GET" else 2
        for attempt in range(attempts):
            timeout_values = (
                timeout.connect, timeout.read, timeout.write, timeout.pool)
            budget = max(
                value for value in timeout_values if value is not None)
            deadline = time.monotonic() + budget
            try:
                request = self._client.build_request(
                    method, path, timeout=timeout, **kwargs)
                response = self._client.send(request, stream=True)
            except httpx.RequestError:
                if attempt + 1 < attempts:
                    time.sleep(0.5)
                    continue
                raise ProviderError(
                    "network_error", "Could not complete the Twelve Labs request.",
                    retryable=True, ambiguous=mutation) from None

            try:
                if response.is_success:
                    if time.monotonic() >= deadline:
                        raise ProviderError(
                            "provider_deadline_exceeded",
                            "The provider operation exceeded its deadline.",
                            retryable=True, ambiguous=mutation)
                    if response.status_code == 204:
                        return {}
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() >= deadline:
                            raise ProviderError(
                                "provider_deadline_exceeded",
                                "The provider operation exceeded its deadline.",
                                retryable=True, ambiguous=mutation)
                        content.extend(chunk)
                        if len(content) > MAX_RESPONSE_BYTES:
                            raise ProviderError(
                                "response_too_large",
                                "The provider response exceeded the safe limit.",
                                ambiguous=mutation,
                                http_status=response.status_code)
                    if not content:
                        return {}
                    try:
                        body = json.loads(content)
                    except ValueError:
                        raise ProviderError(
                            "invalid_response",
                            "Twelve Labs returned a non-JSON success response.",
                            ambiguous=mutation,
                            http_status=response.status_code) from None
                    if not isinstance(body, dict):
                        raise ProviderError(
                            "invalid_response",
                            "Twelve Labs returned an unexpected response shape.",
                            ambiguous=mutation,
                            http_status=response.status_code)
                    return body

                if (attempt + 1 < attempts and
                        self._retryable_status(response.status_code)):
                    time.sleep(self._wait_seconds(response))
                    continue
                raise self._safe_error(response)
            finally:
                response.close()

        raise AssertionError("request attempt loop exhausted")

    @staticmethod
    def _metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(metadata, dict):
            raise ProviderError("invalid_argument",
                                "metadata must be a dictionary.")
        try:
            json.dumps(metadata)
        except (TypeError, ValueError):
            raise ProviderError("invalid_argument",
                                "metadata must be JSON serializable.") from None
        return metadata

    @staticmethod
    def _identified(body: dict[str, Any], *, status: bool = False,
                    uncertain: bool = False
                    ) -> dict[str, Any]:
        if not isinstance(body.get("_id"), str) or (
            status and not isinstance(body.get("status"), str)
        ):
            raise ProviderError(
                "invalid_response",
                "Twelve Labs omitted required response fields.",
                ambiguous=uncertain)
        return body

    def list_indexes(self) -> list[dict[str, Any]]:
        indexes: list[dict[str, Any]] = []
        for page in range(1, 11):
            body = self._request("GET", "/indexes",
                                 params={"page": page, "page_limit": 50})
            data = body.get("data")
            page_info = body.get("page_info")
            if not isinstance(data, list) or not all(
                    isinstance(item, dict) for item in data):
                raise ProviderError("invalid_response",
                                    "Twelve Labs returned an invalid index list.")
            if not isinstance(page_info, dict):
                raise ProviderError(
                    "invalid_response",
                    "Twelve Labs omitted index pagination information.")
            indexes.extend(data)
            total_page = page_info.get("total_page")
            if isinstance(total_page, int) and page >= total_page:
                break
            elif len(data) < 50:
                break
        return indexes

    def create_index(self, name: str, has_audio: bool = True) -> dict[str, Any]:
        options = ["visual", "audio"] if has_audio else ["visual"]
        return self._identified(self._request(
            "POST",
            "/indexes",
            mutation=True,
            json={
                "index_name": name,
                "models": [{"model_name": "marengo3.0",
                            "model_options": options}],
            },
        ), uncertain=True)

    def upload_asset(self, path: str | os.PathLike[str],
                     metadata: dict[str, Any]) -> dict[str, Any]:
        meta = self._metadata(metadata)
        file_path = Path(path)
        try:
            stream = file_path.open("rb")
        except OSError:
            raise ProviderError("file_error",
                                "The local asset file could not be opened.") from None
        media_type = mimetypes.guess_type(file_path.name)[0]
        with stream:
            return self._identified(
                self._request(
                    "POST",
                    "/assets",
                    timeout=UPLOAD_TIMEOUT,
                    mutation=True,
                    data={"method": "direct",
                          "user_metadata": json.dumps(meta)},
                    files={"file": (file_path.name, stream, media_type or
                                    "application/octet-stream")},
                ),
                status=True, uncertain=True,
            )

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        return self._identified(
            self._request("GET", f"/assets/{asset_id}"), status=True)

    def index_asset(self, index_id: str, asset_id: str,
                    metadata: dict[str, Any]) -> dict[str, Any]:
        return self._identified(self._request(
            "POST",
            f"/indexes/{index_id}/indexed-assets",
            mutation=True,
            json={"asset_id": asset_id,
                  "user_metadata": self._metadata(metadata)},
        ), uncertain=True)

    def get_indexed_asset(self, index_id: str,
                          indexed_id: str) -> dict[str, Any]:
        return self._identified(
            self._request(
                "GET", f"/indexes/{index_id}/indexed-assets/{indexed_id}"),
            status=True,
        )

    def get_index(self, index_id: str) -> dict[str, Any]:
        return self._identified(self._request("GET", f"/indexes/{index_id}"))

    def delete_index(self, index_id: str) -> dict[str, Any]:
        try:
            return self._request("DELETE", f"/indexes/{index_id}", mutation=True)
        except ProviderError as exc:
            if exc.http_status == 404:
                return {}
            raise

    def delete_asset(self, asset_id: str) -> dict[str, Any]:
        try:
            return self._request("DELETE", f"/assets/{asset_id}", mutation=True)
        except ProviderError as exc:
            if exc.http_status == 404:
                return {}
            raise

    def delete_indexed_asset(self, index_id: str, indexed_id: str) -> dict[str, Any]:
        try:
            return self._request(
                "DELETE", f"/indexes/{index_id}/indexed-assets/{indexed_id}",
                mutation=True)
        except ProviderError as exc:
            if exc.http_status == 404:
                return {}
            raise

    def search(self, index_id: str, query: str, modality: str,
               indexed_id: str, *, timeout_seconds: float | None = None
               ) -> dict[str, Any]:
        options = {
            "both": ("visual", "audio"),
            "visual": ("visual",),
            "audio": ("audio",),
        }.get(modality)
        if options is None:
            raise ProviderError("invalid_argument",
                                "modality must be one of: both, visual, audio.")
        # Search filter `id` is the indexed video's ID; it is the `_id` returned
        # by POST/GET indexed-assets, not the reusable source asset's asset_id.
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ProviderError(
                "search_deadline_exceeded",
                "The search deadline elapsed before submission completed.",
                ambiguous=True,
            )
        request = {
            "indexId": index_id,
            "query": query,
            "modality": modality,
            "indexedId": indexed_id,
        }
        timeout = min(
            SEARCH_PROCESS_TIMEOUT_SECONDS,
            timeout_seconds if timeout_seconds is not None
            else SEARCH_PROCESS_TIMEOUT_SECONDS)
        body = _bounded_search_subprocess(request, timeout)
        if not isinstance(body.get("data"), list) or not isinstance(
            body.get("page_info"), dict
        ):
            raise ProviderError("invalid_response",
                                "The search provider returned an invalid result.",
                                ambiguous=True)
        return body


def _bounded_search_subprocess(request, timeout):
    """Return only after the provider child has exited or been killed/reaped."""
    child = start_guarded(
        [sys.executable, "-m", "sceneit.provider", "search-child"],
        timeout=timeout,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL)
    encoded = json.dumps(request, separators=(",", ":")).encode()
    started = time.monotonic()
    try:
        # The independent guard owns the absolute deadline. This extra second
        # only gives the parent time to observe and reap its killed group.
        output, _ = child.communicate(
            input=encoded, timeout=max(.1, timeout) + 1)
    except subprocess.TimeoutExpired:
        kill_and_wait(child)
        raise ProviderError(
            "search_deadline_exceeded",
            "The search provider did not respond before the deadline.",
            retryable=True, ambiguous=True) from None
    deadline_margin = min(.2, timeout * .1)
    if (child.returncode and
            time.monotonic() - started >= timeout - deadline_margin):
        raise ProviderError(
            "search_deadline_exceeded",
            "The search provider did not respond before the deadline.",
            retryable=True, ambiguous=True)
    if len(output) > MAX_RESPONSE_BYTES or child.returncode:
        raise ProviderError(
            "search_process_failed",
            "The isolated search provider process failed.",
            ambiguous=True)
    try:
        envelope = json.loads(output)
    except (ValueError, UnicodeDecodeError):
        raise ProviderError(
            "search_process_failed",
            "The isolated search provider process returned an invalid result.",
            ambiguous=True) from None
    if not isinstance(envelope, dict) or not isinstance(envelope.get("ok"), bool):
        raise ProviderError(
            "search_process_failed",
            "The isolated search provider process returned an invalid result.",
            ambiguous=True)
    if not envelope["ok"]:
        error = envelope.get("error", {})
        raise ProviderError(
            error.get("code", "search_process_failed"),
            error.get("message", "The isolated search provider process failed."),
            retryable=error.get("retryable") is True,
            ambiguous=error.get("ambiguous") is True,
            http_status=error.get("httpStatus"))
    body = envelope.get("body")
    if not isinstance(body, dict):
        raise ProviderError(
            "search_process_failed",
            "The isolated search provider process returned an invalid result.",
            ambiguous=True)
    return body


def _run_search_child():
    try:
        raw = sys.stdin.buffer.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return 1
        request = json.loads(raw)
        with TwelveLabsClient() as client:
            options = {
                "both": ("visual", "audio"),
                "visual": ("visual",),
                "audio": ("audio",),
            }[request["modality"]]
            fields = [
                ("query_text", (None, request["query"])),
                ("index_id", (None, request["indexId"])),
                ("page_limit", (None, "10")),
                ("filter", (None, json.dumps({"id": [request["indexedId"]]}))),
            ]
            fields.extend(
                ("search_options", (None, option)) for option in options)
            body = client._request(
                "POST", "/search", files=fields, mutation=True,
                timeout=httpx.Timeout(SEARCH_PROCESS_TIMEOUT_SECONDS))
        envelope = {"ok": True, "body": body}
    except ProviderError as exc:
        envelope = {"ok": False, "error": {
            "code": exc.code, "message": exc.message,
            "retryable": exc.retryable, "ambiguous": exc.ambiguous,
            "httpStatus": exc.http_status,
        }}
    except Exception:
        envelope = {"ok": False, "error": {
            "code": "search_process_failed",
            "message": "The isolated search provider process failed.",
            "ambiguous": True,
        }}
    encoded = json.dumps(envelope, separators=(",", ":")).encode()
    if len(encoded) > MAX_RESPONSE_BYTES:
        return 1
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__" and sys.argv[1:] == ["search-child"]:
    raise SystemExit(_run_search_child())