from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import random
import time
import ssl
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx


@dataclass(slots=True)
class DiscoveredDocument:
    provider: str
    source_id: str
    detail_url: str
    title_hint: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class HttpSourceClient:
    """Polite synchronous HTTP client with pacing, retries and conditional GET support."""

    def __init__(
        self,
        *,
        delay: float = 1.0,
        timeout: float = 30.0,
        max_retries: int = 3,
        user_agent: str = (
            "Corpus-Indomicus/0.1 "
            "(public legal corpus research; github.com/RetrixAlfariz/corpus-indomicus)"
        ),
    ):
        self.delay = max(0.0, delay)
        self.max_retries = max(0, max_retries)
        self._last_request_at = 0.0
        self._robots: dict[str, RobotFileParser] = {}
        self._client = httpx.Client(
            verify=ssl.create_default_context(),
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
        remaining = self.delay - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if parts.hostname == "peraturan.bpk.go.id" and parts.path != "/robots.txt":
            if origin not in self._robots:
                policy = self.get(f"{origin}/robots.txt")
                rules = RobotFileParser(f"{origin}/robots.txt")
                rules.parse(policy.text.splitlines())
                self._robots[origin] = rules
                crawl_delay = rules.crawl_delay(self._client.headers.get("user-agent", "*"))
                if crawl_delay is not None:
                    self.delay = max(self.delay, float(crawl_delay))
            if not self._robots[origin].can_fetch(self._client.headers.get("user-agent", "*"), url):
                raise RuntimeError(f"robots.txt disallows acquisition: {url}")
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._pace()
            try:
                response = self._client.get(url, **kwargs)
                self._last_request_at = time.monotonic()
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if attempt < self.max_retries:
                        retry_after = response.headers.get("Retry-After")
                        wait = min(2**attempt, 20) + random.random()
                        if retry_after:
                            try:
                                wait = max(0.0, float(retry_after))
                            except ValueError:
                                try:
                                    wait = max(0.0, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                                except (ValueError, TypeError):
                                    pass
                        time.sleep(wait)
                        continue
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length and not response.headers.get("content-encoding") and int(length) != len(response.content):
                    raise httpx.TransportError("incomplete HTTP payload")
                return response
            except httpx.HTTPError as exc:
                self._last_request_at = time.monotonic()
                last_error = exc
                if attempt >= self.max_retries:
                    break
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code != 429 and exc.response.status_code < 500:
                    break
                time.sleep(min(2**attempt, 20) + random.random())
        assert last_error is not None
        raise last_error


class SourceConnector(ABC):
    provider: str

    @abstractmethod
    def discover(self, **kwargs: Any) -> Iterable[DiscoveredDocument]:
        raise NotImplementedError
