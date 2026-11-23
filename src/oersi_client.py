"""Small requests-based client for the documented OERSI Elasticsearch API."""

from __future__ import annotations

from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class OERSIClient:
    def __init__(
        self,
        api_url: str,
        user_agent: str,
        timeout: float = 20.0,
        max_retries: int = 3,
        session: requests.Session | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": user_agent, "Accept": "application/json", "Content-Type": "application/json"}
        )
        retry = Retry(
            total=max_retries,
            connect=max_retries,
            read=max_retries,
            status=max_retries,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"POST"}),
            respect_retry_after_header=True,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    def search(self, query: dict[str, Any], size: int) -> dict[str, Any]:
        """Submit an Elasticsearch search body and return its JSON response."""
        response = self.session.post(
            self.api_url,
            json={"size": size, "from": 0, "query": query, "sort": [{"id": "asc"}]},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("hits"), dict):
            raise ValueError("OERSI API response does not contain a valid hits object")
        return payload
