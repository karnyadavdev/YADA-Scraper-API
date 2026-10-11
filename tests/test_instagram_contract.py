from types import SimpleNamespace

import pytest

from scraper import source_fast_paths as coverage
from scraper import source_fast_paths as sources


def _response(url, html, status=200):
    return SimpleNamespace(url=url, text=html, status_code=status)


def test_instagram_resource_identity_ignores_host_and_query_cosmetics():
    assert coverage._instagram_resource_identity(
        "https://instagram.com/openai/?hl=en"
    ) == coverage._instagram_resource_identity(
        "https://www.instagram.com/openai/"
    )
    assert coverage._instagram_resource_identity(
        "https://example.com/openai/"
    ) is None


def test_instagram_bulk_result_accepts_resource_specific_metadata():
    url = "https://www.instagram.com/openai/"
    response = _response(
        "https://instagram.com/openai/?hl=en",
        """
        <html><head>
          <meta content="OpenAI (@openai) • Instagram photos and videos" property="og:title">
          <meta property="og:description" content="4M Followers, 10 Following, 900 Posts - See Instagram photos and videos from OpenAI (@openai)">
          <meta property="og:image" content="https://cdn.example/openai.jpg">
        </head></html>
        """,
    )

    result = coverage._instagram_bulk_result(url, response, 0)

    assert result.success
    assert not result.needs_browser
    assert result.final_url == response.url
    assert f"Requested URL: {url}" in result.html
    assert "4M Followers" in result.html


@pytest.mark.parametrize(
    ("final_url", "html", "reason_fragment"),
    [
        (
            "https://www.instagram.com/openai/",
            "<html><head><title>Instagram</title></head><body></body></html>",
            "resource-specific metadata",
        ),
        (
            "https://www.instagram.com/accounts/login/?next=/openai/",
            """
            <html><head>
              <title>Login - Instagram</title>
              <meta property="og:description" content="Create an account or log in to Instagram">
            </head></html>
            """,
            "different resource",
        ),
    ],
)
def test_instagram_bulk_result_rejects_missing_or_unrelated_data(
    final_url, html, reason_fragment
):
    requested = "https://www.instagram.com/openai/"

    result = coverage._instagram_bulk_result(
        requested,
        _response(final_url, html),
        0,
    )

    assert not result.success
    assert result.needs_browser
    assert reason_fragment in result.reason
    assert result.final_url == final_url
    assert f"Requested URL: {requested}" in result.html


class _FakeBulkSession:
    def __init__(self, **kwargs):
        _FakeBulkSession.captured = dict(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_instagram_bulk_caps_session_concurrency(monkeypatch):
    async def fake_safe_get(session, url, headers=None, timeout=None):
        return _response(url, "<html><head></head><body></body></html>")

    monkeypatch.setattr(sources, "AsyncSession", _FakeBulkSession)
    monkeypatch.setattr(sources, "safe_get", fake_safe_get)

    urls = [f"https://www.instagram.com/p{i}/" for i in range(15)]
    results = await sources.scrape_instagram_bulk(urls)

    assert len(results) == 15
    assert _FakeBulkSession.captured["max_clients"] == 10


@pytest.mark.asyncio
async def test_instagram_bulk_small_list_opens_only_what_it_needs(monkeypatch):
    async def fake_safe_get(session, url, headers=None, timeout=None):
        return _response(url, "<html><head></head><body></body></html>")

    monkeypatch.setattr(sources, "AsyncSession", _FakeBulkSession)
    monkeypatch.setattr(sources, "safe_get", fake_safe_get)

    urls = [f"https://www.instagram.com/p{i}/" for i in range(4)]
    results = await sources.scrape_instagram_bulk(urls)

    assert len(results) == 4
    assert _FakeBulkSession.captured["max_clients"] == 4


@pytest.mark.asyncio
async def test_instagram_rich_extractor_is_explicit_and_auto_stays_compact(monkeypatch):
    compact = object()
    rich = object()
    compact_calls = []
    rich_calls = []

    async def fake_compact(url):
        compact_calls.append(url)
        return compact

    async def fake_rich(urls):
        rich_calls.append(list(urls))
        return [rich]

    monkeypatch.setattr(sources, "scrape_instagram", fake_compact)
    monkeypatch.setattr(sources, "scrape_instagram_bulk", fake_rich)
    url = "https://www.instagram.com/openai/"

    assert await sources.source_scrape(url) is compact
    assert await sources.source_scrape(url, structured=True) is rich
    assert compact_calls == [url]
    assert rich_calls == [[url]]
