import pytest

from scraper import safe_http
from scraper import source_fast_paths
from scraper.safe_http import ResponseTooLargeError, TooManyRedirectsError, safe_request


class _Quit:
    def __init__(self):
        self.was_set = False

    def set(self):
        self.was_set = True


class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=(), url=""):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = list(chunks)
        self.url = url
        self.content = b""
        self.history = []
        self.quit_now = _Quit()
        self.closed = False

    async def aiter_content(self):
        for chunk in self._chunks:
            yield chunk
        await self.aclose()

    async def aclose(self):
        self.closed = True

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.fixture
def allow_test_urls(monkeypatch):
    seen = []

    async def normalize(url):
        seen.append(url)
        if "127.0.0.1" in url:
            raise ValueError("private")
        return url

    monkeypatch.setattr(safe_http, "normalize_public_url", normalize)
    return seen


@pytest.mark.asyncio
async def test_redirects_are_manually_validated_and_sensitive_headers_are_stripped(
    allow_test_urls,
):
    first = FakeResponse(
        302,
        {"location": "https://other.example/final"},
        url="https://start.example/path",
    )
    final = FakeResponse(200, chunks=[b"safe"], url="https://other.example/final")
    session = FakeSession([first, final])

    response = await safe_request(
        session,
        "GET",
        "https://start.example/path",
        headers={"Authorization": "secret", "Cookie": "a=b", "Accept": "text/html"},
        cookies={"session": "secret"},
        auth=("user", "pass"),
    )

    assert allow_test_urls == [
        "https://start.example/path",
        "https://other.example/final",
    ]
    assert response.content == b"safe"
    assert len(response.history) == 1
    _, _, second_kwargs = session.calls[1]
    assert second_kwargs["headers"] == {"Accept": "text/html"}
    assert "cookies" not in second_kwargs
    assert "auth" not in second_kwargs
    assert all(call[2]["allow_redirects"] is False for call in session.calls)
    assert all(call[2]["stream"] is True for call in session.calls)


@pytest.mark.asyncio
async def test_redirect_to_private_target_is_rejected_before_second_request(allow_test_urls):
    response = FakeResponse(302, {"location": "http://127.0.0.1/admin"})
    session = FakeSession([response])

    with pytest.raises(ValueError, match="private"):
        await safe_request(session, "GET", "https://public.example")

    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_rejected_redirect_releases_the_stream(allow_test_urls):
    response = FakeResponse(302, {"location": "http://127.0.0.1/admin"})
    session = FakeSession([response])

    with pytest.raises(ValueError, match="private"):
        await safe_request(session, "GET", "https://public.example")

    assert response.closed is True


@pytest.mark.asyncio
async def test_mid_stream_failure_releases_the_stream(allow_test_urls):
    class _BoomResponse(FakeResponse):
        async def aiter_content(self):
            yield b"partial"
            raise RuntimeError("connection reset")

    response = _BoomResponse(200, {"content-type": "text/html"})
    session = FakeSession([response])

    with pytest.raises(RuntimeError, match="connection reset"):
        await safe_request(session, "GET", "https://public.example")

    assert response.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "scrape"),
    [
        (source_fast_paths, lambda: source_fast_paths.scrape_http_meta_page(
            "https://public.example/article", "medium"
        )),
        (source_fast_paths, lambda: source_fast_paths.scrape_medium(
            "https://public.example/article"
        )),
    ],
)
async def test_named_source_transports_reject_private_redirects_before_following(
    monkeypatch, allow_test_urls, module, scrape
):
    session = FakeSession([
        FakeResponse(302, {"location": "http://127.0.0.1/admin"})
    ])
    monkeypatch.setattr(module, "AsyncSession", lambda *args, **kwargs: session)

    result = await scrape()

    assert result.success is False
    assert len(session.calls) == 1
    assert session.calls[0][2]["allow_redirects"] is False


@pytest.mark.asyncio
async def test_303_rewrites_post_to_get_but_307_preserves_method_and_body(allow_test_urls):
    session = FakeSession(
        [
            FakeResponse(307, {"location": "/still-post"}),
            FakeResponse(303, {"location": "/now-get"}),
            FakeResponse(200, chunks=[b"ok"]),
        ]
    )

    await safe_request(session, "POST", "https://public.example/start", data=b"body")

    assert [call[0] for call in session.calls] == ["POST", "POST", "GET"]
    assert session.calls[1][2]["data"] == b"body"
    assert "data" not in session.calls[2][2]


@pytest.mark.asyncio
async def test_decoded_body_is_stopped_at_limit(allow_test_urls):
    response = FakeResponse(200, chunks=[b"1234", b"5678"])
    session = FakeSession([response])

    with pytest.raises(ResponseTooLargeError, match="Decoded response body"):
        await safe_request(
            session, "GET", "https://public.example", response_limit=7
        )

    assert response.quit_now.was_set
    assert response.closed


@pytest.mark.asyncio
async def test_declared_oversize_body_is_rejected_without_reading(allow_test_urls):
    response = FakeResponse(200, {"content-length": "99"}, chunks=[b"unused"])
    session = FakeSession([response])

    with pytest.raises(ResponseTooLargeError, match="Content-Length"):
        await safe_request(
            session, "GET", "https://public.example", response_limit=10
        )

    assert response.quit_now.was_set


@pytest.mark.asyncio
async def test_redirect_limit_is_enforced(allow_test_urls):
    responses = [FakeResponse(302, {"location": f"/hop-{index}"}) for index in range(3)]
    session = FakeSession(responses)

    with pytest.raises(TooManyRedirectsError):
        await safe_request(
            session, "GET", "https://public.example/start", max_redirects=2
        )

    assert len(session.calls) == 3
