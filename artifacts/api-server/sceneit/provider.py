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
import time
from typing import Any

import httpx


BASE_URL = "https://api.twelvelabs.io/v1.3"
NORMAL_TIMEOUT = httpx.Timeout(45.0)
UPLOAD_TIMEOUT = httpx.Timeout(300.0)


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
        api_key = os.environ.get("TWELVE_LABS_API_KEY")
        if not api_key:
            raise ProviderError("missing_credentials",
                                "TWELVE_LABS_API_KEY is not configured.")
        self._api_key = api_key
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"x-api-key": api_key, "Accept": "application/json"},
        )

    def __repr__(self) -> str:
        return "TwelveLabsClient(base_url='https://api.twelvelabs.io/v1.3')"

    def close(self) -> None:
        self._client.close()

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
        code = f"http_{response.status_code}"
        message = "Twelve Labs rejected the request."
        try:
            body = response.json()
            if isinstance(body, dict):
                error = body.get("error")
                source = error if isinstance(error, dict) else body
                raw_code = source.get("code")
                raw_message = source.get("message")
                if isinstance(raw_code, (str, int)):
                    code = str(raw_code)[:100]
                elif isinstance(error, str):
                    message = error[:500]
                if isinstance(raw_message, str):
                    message = raw_message[:500]
        except (ValueError, TypeError):
            pass
        # Defensive redaction if the provider unexpectedly echoes a credential.
        if self._api_key:
            message = message.replace(self._api_key, "[REDACTED]")
            code = code.replace(self._api_key, "[REDACTED]")
        return ProviderError(
            code, message, retryable=self._retryable_status(response.status_code),
            http_status=response.status_code)

    def _request(self, method: str, path: str, *,
                 timeout: httpx.Timeout = NORMAL_TIMEOUT,
                 mutation: bool = False, **kwargs: Any) -> dict[str, Any]:
        attempts = 1 if mutation or method != "GET" else 2
        for attempt in range(attempts):
            try:
                response = self._client.request(method, path, timeout=timeout,
                                                **kwargs)
            except httpx.RequestError:
                if attempt + 1 < attempts:
                    time.sleep(0.5)
                    continue
                raise ProviderError(
                    "network_error", "Could not complete the Twelve Labs request.",
                    retryable=True, ambiguous=mutation) from None

            if response.is_success:
                try:
                    body = response.json()
                except ValueError:
                    raise ProviderError(
                        "invalid_response",
                        "Twelve Labs returned a non-JSON success response.",
                        http_status=response.status_code) from None
                if not isinstance(body, dict):
                    raise ProviderError(
                        "invalid_response",
                        "Twelve Labs returned an unexpected response shape.",
                        http_status=response.status_code)
                return body

            if (attempt + 1 < attempts and
                    self._retryable_status(response.status_code)):
                time.sleep(self._wait_seconds(response))
                continue
            raise self._safe_error(response)

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
    def _identified(body: dict[str, Any], *, status: bool = False
                    ) -> dict[str, Any]:
        if not isinstance(body.get("_id"), str) or (
            status and not isinstance(body.get("status"), str)
        ):
            raise ProviderError(
                "invalid_response",
                "Twelve Labs omitted required response fields.")
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

    def create_index(self, name: str) -> dict[str, Any]:
        return self._identified(self._request(
            "POST",
            "/indexes",
            mutation=True,
            json={
                "index_name": name,
                "models": [{"model_name": "marengo3.0",
                            "model_options": ["visual", "audio"]}],
            },
        ))

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
                status=True,
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
        ))

    def get_indexed_asset(self, index_id: str,
                          indexed_id: str) -> dict[str, Any]:
        return self._identified(
            self._request(
                "GET", f"/indexes/{index_id}/indexed-assets/{indexed_id}"),
            status=True,
        )

    def search(self, index_id: str, query: str, modality: str,
               indexed_id: str) -> dict[str, Any]:
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
        fields = [
            ("query_text", (None, query)),
            ("index_id", (None, index_id)),
            ("page_limit", (None, "10")),
            ("filter", (None, json.dumps({"id": [indexed_id]}))),
        ]
        fields.extend(("search_options", (None, option)) for option in options)
        body = self._request("POST", "/search", files=fields)
        if not isinstance(body.get("data"), list) or not isinstance(
            body.get("page_info"), dict
        ):
            raise ProviderError("invalid_response",
                                "Twelve Labs returned an invalid search result.")
        return body