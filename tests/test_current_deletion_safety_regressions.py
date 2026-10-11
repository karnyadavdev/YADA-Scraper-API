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


@pytest.mark.asyncio
async def test_quick_scrape_cookies_do_not_mutate_shared_fingerprint(monkeypatch):
    from scraper.browser_identity import make_identity

    class FakeCookies:
        def set(self, name, value, domain=None):
            pass

    class FakeResponse:
        status_code = 200
        text = "<html><body>" + ("useful page " * 200) + "</body></html>"
        url = "https://example.com/page"

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

    fingerprint = make_identity()
    original_keys = set(fingerprint.headers)

    result = await http_scraping.quick_scrape(
        "https://example.com/page",
        fingerprint=fingerprint,
        cookies=[{"name": "session", "value": "saved"}],
        retries=0,
    )

    assert result.success is True
    assert "Referer" not in fingerprint.headers
    assert set(fingerprint.headers) == original_keys


@pytest.mark.asyncio
async def test_quick_scrape_json_endpoint_uses_tls_impersonation(monkeypatch):
    from scraper.browser_identity import make_identity

    captured_kwargs = {}

    class FakeResponse:
        status_code = 200
        text = '{"ok": true, "data": []}'
        url = "https://www.instagram.com/graphql/query.json"

    class FakeSession:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_safe_get(session, url, **kwargs):
        return FakeResponse()

    monkeypatch.setattr(http_scraping, "AsyncSession", FakeSession)
    monkeypatch.setattr(http_scraping, "safe_get", fake_safe_get)

    result = await http_scraping.quick_scrape(
        "https://www.instagram.com/graphql/query.json",
        fingerprint=make_identity(),
        retries=0,
    )

    assert result.status_code == 200
    assert captured_kwargs.get("impersonate") in {"chrome131", "chrome120"}


def test_all_cffi_impersonate_strings_exist_in_installed_curl_cffi():
    from curl_cffi.requests.impersonate import BrowserType

    from scraper.http_scraping import WAF_STRATEGIES

    used_elsewhere = [
        "chrome131",
        "chrome120",
        "chrome110",
        "chrome116",
        "chrome131_android",
        "edge101",
        "safari15_3",
        "safari17_0",
    ]

    for strategy in WAF_STRATEGIES:
        name = strategy["impersonate"]
        assert name in BrowserType.__members__, f"WAF strategy {strategy['name']!r} uses unknown impersonate {name!r}"

    for name in used_elsewhere:
        assert name in BrowserType.__members__, f"unknown impersonate {name!r}"
