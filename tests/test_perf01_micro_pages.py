import time
import pytest
from scraper import engine as engine_mod
from scraper.engine import ScraperEngine
from scraper.browser_identity import make_identity
from scraper.http_scraping import QuickResult


def _fake_result(success=True, html="", status_code=200, reason="OK", final_url="https://example.com", content_type="text/html"):
    res = QuickResult(
        success=success,
        html=html,
        status_code=status_code,
        needs_browser=False,
        reason=reason,
        response_time=0.05,
        final_url=final_url,
    )
    res.content_type = content_type
    return res


@pytest.mark.asyncio
async def test_perf01_html_micro_page_accepted_at_layer1(monkeypatch):
    html = "<html><body>OK</body></html>"

    async def fake_raw_scrape(url):
        return _fake_result(success=True, html=html, status_code=200, reason="OK", final_url=url, content_type="text/html")

    async def fail_ladder(*args, **kwargs):
        raise AssertionError("Waterfall escalated unnecessarily for clean static micro-page!")

    monkeypatch.setattr(engine_mod, "raw_scrape", fake_raw_scrape)
    monkeypatch.setattr(engine_mod, "quick_scrape", fail_ladder)
    monkeypatch.setattr(engine_mod, "mobile_scrape", fail_ladder)
    monkeypatch.setattr(engine_mod, "crawler_scrape", fail_ladder)
    monkeypatch.setattr(engine_mod, "stealth_scrape", fail_ladder)

    engine = ScraperEngine()
    engine.memory.profiles = {}
    result = await engine._try_auto("https://example.com/healthz", make_identity(), time.time(), [])

    assert result.ok is True
    assert result.usable is True
    assert result.winning_layer == "layer1_simple"
    assert result.best_attempt["retry_recommended"] is False


@pytest.mark.asyncio
async def test_perf01_plain_text_micro_page_accepted(monkeypatch):
    text_body = "OK"

    async def fake_raw_scrape(url):
        return _fake_result(success=True, html=text_body, status_code=200, reason="OK", final_url=url, content_type="text/plain")

    async def fail_ladder(*args, **kwargs):
        raise AssertionError("Waterfall escalated unnecessarily for plain text!")

    monkeypatch.setattr(engine_mod, "raw_scrape", fake_raw_scrape)
    monkeypatch.setattr(engine_mod, "quick_scrape", fail_ladder)
    monkeypatch.setattr(engine_mod, "stealth_scrape", fail_ladder)

    engine = ScraperEngine()
    engine.memory.profiles = {}
    result = await engine._try_auto("https://example.com/health", make_identity(), time.time(), [])

    assert result.ok is True
    assert result.usable is True
    assert result.winning_layer == "layer1_simple"
    assert result.best_attempt["retry_recommended"] is False


@pytest.mark.asyncio
async def test_perf01_json_micro_page_accepted(monkeypatch):
    json_body = '{"status": "ok"}'

    async def fake_raw_scrape(url):
        return _fake_result(success=True, html=json_body, status_code=200, reason="OK", final_url=url, content_type="application/json")

    async def fail_ladder(*args, **kwargs):
        raise AssertionError("Waterfall escalated unnecessarily for JSON payload!")

    monkeypatch.setattr(engine_mod, "raw_scrape", fake_raw_scrape)
    monkeypatch.setattr(engine_mod, "quick_scrape", fail_ladder)
    monkeypatch.setattr(engine_mod, "stealth_scrape", fail_ladder)

    engine = ScraperEngine()
    engine.memory.profiles = {}
    result = await engine._try_auto("https://example.com/api/status", make_identity(), time.time(), [])

    assert result.ok is True
    assert result.usable is True
    assert result.winning_layer == "layer1_simple"
    assert result.best_attempt["retry_recommended"] is False


@pytest.mark.parametrize("shell_html,description", [
    ('<div id="root"></div><script src="app.js"></script>', "React mount point with script"),
    ('<div id="app"></div><script src="app.js"></script>', "Vue mount point with script"),
    ('<div id="__next"></div><script src="app.js"></script>', "Next.js mount point with script"),
    ('<app-root></app-root><script src="app.js"></script>', "Angular mount point with script"),
    ('<html><noscript>Please enable JavaScript</noscript><body>Loading</body></html>', "Noscript warning"),
    ('<html><head><META HTTP-EQUIV="Refresh" content="0; url=/login"></head><body>Redirecting</body></html>', "Meta refresh"),
    ('<html><body>Just a moment...</body></html>', "Cloudflare challenge marker"),
    ('<html><body>Access Denied</body></html>', "Access denied text seen"),
    ('<html><body></body></html>', "Empty body skeleton"),
    ('<html><body><iframe src="https://example.com/widget"></iframe></body></html>', "Embedded iframe must escalate"),
    ('<html><frameset cols="50%,50%"><frame src="page1.html"></frameset></html>', "Embedded frameset must escalate"),
])
@pytest.mark.asyncio
async def test_perf01_must_escalate_corpus_rejected_from_premature_acceptance(monkeypatch, shell_html, description):
    """Ensure that SPA shells, noscript warnings, challenges, and blocks are NEVER falsely accepted as clean micro-pages."""
    layer1_hit = False
    escalated_to_layer2 = False

    async def fake_raw_scrape(url):
        nonlocal layer1_hit
        layer1_hit = True
        return _fake_result(success=True, html=shell_html, status_code=200, reason="OK", final_url=url, content_type="text/html")

    async def fake_quick_scrape(url, fingerprint=None, cookies=None):
        nonlocal escalated_to_layer2
        escalated_to_layer2 = True
        return _fake_result(success=True, html=shell_html, status_code=200, reason="OK", final_url=url, content_type="text/html")

    async def fake_stealth_scrape(url, **kwargs):
        from scraper.browser_scraping import BrowseOutput
        return BrowseOutput(success=True, html="<html><body>Hydrated rendered content</body></html>", status_code=200, reason="Rendered", response_time=1.0, final_url=url)

    monkeypatch.setattr(engine_mod, "raw_scrape", fake_raw_scrape)
    monkeypatch.setattr(engine_mod, "quick_scrape", fake_quick_scrape)
    monkeypatch.setattr(engine_mod, "stealth_scrape", fake_stealth_scrape)

    engine = ScraperEngine()
    engine.memory.profiles = {}
    result = await engine._try_auto("https://example.com/target", make_identity(), time.time(), [])

    assert layer1_hit is True
    # Crucial assertion: layer 1 MUST NOT win for these shells/stubs
    assert result.winning_layer != "layer1_simple", f"Failed: {description} was prematurely accepted at layer1_simple!"
    assert escalated_to_layer2 is True, f"Failed: {description} did not escalate beyond layer1!"
