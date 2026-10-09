import pytest

from scraper import ultimate_stealth
from scraper.http_scraping import QuickResult


class _Response:
    status_code = 200
    text = "<html><body>usable content</body></html>"
    url = "https://example.com/final"


class _Session:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_layer6_returns_the_standard_engine_result_contract(monkeypatch):
    seen = {}

    async def fake_safe_get(session, url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs["headers"]
        return _Response()

    monkeypatch.setattr(ultimate_stealth, "AsyncSession", _Session)
    monkeypatch.setattr(ultimate_stealth, "safe_get", fake_safe_get)

    result = await ultimate_stealth.scrape_ultimate("https://example.com/start")

    assert isinstance(result, QuickResult)
    assert result.success
    assert result.html == _Response.text
    assert result.final_url == _Response.url
    assert seen["url"] == "https://example.com/start"
    assert "Accept" in seen["headers"]
