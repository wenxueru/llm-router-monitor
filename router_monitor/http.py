"""Pooled HTTP transport shared by each collector or maintenance policy."""

import logging

import httpx

logger = logging.getLogger(__name__)


class HttpClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None, token: str = "", origins: tuple[str, ...] = ()):
        self._token = token
        self._origins = {self._origin(url) for url in origins}
        self._client = httpx.Client(follow_redirects=True, transport=transport)

    @staticmethod
    def _origin(url: str) -> tuple:
        parsed = httpx.URL(url)
        return parsed.scheme, parsed.host, parsed.port

    def _headers(self, url: str) -> dict:
        allowed = not self._origins or self._origin(url) in self._origins
        return {"Authorization": f"Bearer {self._token}"} if self._token and allowed else {}

    def fetch(self, url: str, timeout: float = 2) -> str:
        try:
            response = self._client.get(url, timeout=timeout, headers=self._headers(url))
            response.raise_for_status()
            return response.text
        except (httpx.HTTPError, httpx.InvalidURL):
            logger.debug("Failed to fetch %s", url, exc_info=True)
            return ""

    def request_json(self, url: str, *, method: str, payload: dict | None = None, timeout: float = 10) -> dict:
        response = self._client.request(method, url, json=payload, timeout=timeout, headers=self._headers(url))
        response.raise_for_status()
        return response.json() if response.content and method != "DELETE" else {}

    def close(self) -> None:
        self._client.close()
