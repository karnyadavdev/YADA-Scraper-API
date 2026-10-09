import pytest

from scraper import http_scraping
from scraper import source_fast_paths


@pytest.mark.asyncio
async def test_reddit_bulk_dispatch_uses_warmup_capable_route(monkeypatch):
    calls = []

    async def coverage_reddit(urls, timeout=15.0, cookies=None):
        calls.append((list(urls), cookies))
        return ["warmup-capable"]

    monkeypatch.setattr(source_fast_paths, "scrape_reddit_bulk", coverage_reddit)

    cookies = [{"name": "session", "value": "saved"}]
    result = await source_fast_paths.source_bulk_scrape(
        ["https://www.reddit.com/r/python/new/"],
        cookies=cookies,
    )

    assert result == ["warmup-capable"]
    assert calls == [
        (["https://www.reddit.com/r/python/new/"], cookies)
    ]


@pytest.mark.asyncio
async def test_quick_scrape_cookie_domain_excludes_explicit_port(monkeypatch):
    captured_domains = []

    class FakeCookies:
        def set(self, name, value, domain=None):
            captured_domains.append(domain)

    class FakeResponse:
        status_code = 200
        text = "<html><body>" + ("useful page " * 200) + "</body></html>"
        url = "https://example.com:443/page"

    class FakeSession:
        def __init__(self, **kwargs):
            self.cookies = FakeCookies()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_safe_get(session, url, **kwargs):
        return FakeResponse()

    monkeypatch.setattr(http_scraping, "AsyncSession", FakeSession)
    monkeypatch.setattr(http_scraping, "safe_get", fake_safe_get)

    result = await http_scraping.quick_scrape(
        "https://example.com:443/page",
        cookies=[{"name": "session", "value": "saved"}],
        retries=0,
    )

    assert result.success is True
    assert captured_domains == [".example.com"]
