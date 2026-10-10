from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from corpus_indomicus.sources.base import HttpSourceClient


@pytest.mark.parametrize("status", [429, 500, 503])
def test_http_retries_and_respects_retry_after(monkeypatch, status):
    waits = []
    monkeypatch.setattr("corpus_indomicus.sources.base.time.sleep", waits.append)
    attempts = []
    def handle(request):
        attempts.append(request)
        return httpx.Response(status if len(attempts) == 1 else 200,
            content=b"ok", headers={"Retry-After": "2"})
    with HttpSourceClient(delay=0, max_retries=1) as client:
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(handle))
        assert client.get("https://x").content == b"ok"
    assert len(attempts) == 2 and 2.0 in waits


def test_http_does_not_retry_forbidden_and_rejects_short_body(monkeypatch):
    calls = []
    monkeypatch.setattr("corpus_indomicus.sources.base.time.sleep", lambda _: None)
    def handle(request):
        calls.append(request)
        return httpx.Response(403)
    with HttpSourceClient(delay=0, max_retries=3) as client:
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(handle))
        with pytest.raises(httpx.HTTPStatusError):
            client.get("https://x")
        assert len(calls) == 1
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"short", headers={"Content-Length": "100"})))
        with pytest.raises(httpx.TransportError, match="incomplete"):
            client.get("https://x")


def test_robots_policy_blocks_disallowed_source_path(monkeypatch):
    requests = []
    def handle(request):
        requests.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nDisallow: /Search\n")
    with HttpSourceClient(delay=0) as client:
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(handle))
        with pytest.raises(RuntimeError, match="robots.txt disallows"):
            client.get("https://peraturan.bpk.go.id/Search")
    assert requests == ["/robots.txt"]
