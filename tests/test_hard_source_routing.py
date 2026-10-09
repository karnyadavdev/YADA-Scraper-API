import pytest

from scraper import source_fast_paths as sources
from scraper.browser_scraping import BrowseOutput


@pytest.mark.asyncio
async def test_protected_source_reuses_saved_cookies_in_guarded_browser(monkeypatch):
    seen = {}
    cookies = [{"name": "session", "value": "opaque", "domain": "www.bestbuy.com", "path": "/"}]

    async def fake_full_scrape(url, **kwargs):
        seen["url"] = url
        seen["cookies"] = kwargs["cookies"]
        return BrowseOutput(success=True, html="<html>ok</html>", status_code=200, final_url=url)

    monkeypatch.setattr(sources, "full_scrape", fake_full_scrape)

    result = await sources.source_scrape(
        "https://www.bestbuy.com/site/example/123.p",
        cookies=cookies,
    )

    assert result.success
    assert seen == {
        "url": "https://www.bestbuy.com/site/example/123.p?intl=nosplash",
        "cookies": cookies,
    }


@pytest.mark.asyncio
async def test_coverage_hard_route_uses_the_same_guarded_capture(monkeypatch):
    seen = []

    async def fake_full_scrape(url, **kwargs):
        seen.append(url)
        return BrowseOutput(success=True, html="<html>ok</html>", status_code=200, final_url=url)

    monkeypatch.setattr(sources, "full_scrape", fake_full_scrape)

    result = await sources._guarded_hard_source("https://www.walmart.com/search?q=laptop", "Walmart")

    assert result.success
    assert seen == ["https://www.walmart.com/search?q=laptop"]
