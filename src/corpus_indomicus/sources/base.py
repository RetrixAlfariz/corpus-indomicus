from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import random
import time
from typing import Any, Iterable

import httpx


@dataclass(slots=True)
class DiscoveredDocument:
    provider: str
    source_id: str
    detail_url: str
    title_hint: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class HttpSourceClient:
    """Small polite HTTP client with fixed inter-request delay and retries."""

    def __init__(
        self,
        *,
        delay: float = 1.0,
        timeout: float = 30.0,
        max_retries: int = 3,
        user_agent: str = (
            "Corpus-Indomicus/0.1 "
            "(public legal archive research; contact via GitHub RetrixAlfariz/corpus-indomicus)"
        ),
    ):
        self.delay = max(0.0, delay)
        self.max_retries = max(0, max_retries)
        self._last_request_at = 0.0
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpSourceClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _pace(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.delay - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._pace()
            try:
                response = self._client.get(url, **kwargs)
                self._last_request_at = time.monotonic()

                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if attempt < self.max_retries:
                        retry_after = response.headers.get("Retry-After")
                        if retry_after and retry_after.isdigit():
                            wait = float(retry_after)
                        else:
                            wait = min(2**attempt, 20) + random.random()
                        time.sleep(wait)
                        continue

                response.raise_for_status()
                return response
            except httpx.HTTPError as exc:
                self._last_request_at = time.monotonic()
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(min(2**attempt, 20) + random.random())

        assert last_error is not None
        raise last_error


class SourceConnector(ABC):
    provider: str

    @abstractmethod
    def discover(self, **kwargs: Any) -> Iterable[DiscoveredDocument]:
        raise NotImplementedError
