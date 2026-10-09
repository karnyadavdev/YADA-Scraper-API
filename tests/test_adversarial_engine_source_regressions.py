import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from scraper import browser_identity as fingerprint_mod
from scraper import browser_scraping as browser_mod
from scraper import source_fast_paths as coverage
from scraper import engine as engine_mod
from scraper import http_scraping
from scraper import site_memory as site_memory_mod
from scraper import source_fast_paths as sources
from scraper.engine import ScraperEngine
from scraper.site_memory import SiteInfo, SiteMemory


def test_named_sources_accept_explicit_default_ports():
    reddit = "https://www.reddit.com:443/r/python/"
    assert sources.source_name(reddit) == "reddit"
    assert coverage.source_name(reddit) == "reddit"


def test_site_memory_keys_profiles_by_hostname_not_port(tmp_path):
    memory = SiteMemory(memory_path=str(tmp_path / "memory.json"))
    memory.profiles = {
        "example.com": SiteInfo(
            domain="example.com",
            cookies=[{"name": "session", "value": "saved"}],
        )
    }

    assert memory.get_valid_cookies("https://example.com/page")
    assert memory.get_valid_cookies("https://example.com:443/page")
    assert memory._get_domain("http://www.example.com:80/page") == "example.com"


def test_reddit_rewrite_requires_an_actual_reddit_hostname():
    engine = ScraperEngine()
    unrelated = "https://notreddit.com/r/python/"
    reddit = "https://old.reddit.com/r/python/"

    assert engine._rewrite_reddit_url(unrelated) == unrelated
    assert engine._rewrite_reddit_url(reddit).endswith("/r/python.json")


@pytest.mark.asyncio
async def test_empty_http_body_is_not_a_successful_capture(monkeypatch):
    class FakeResponse:
        text = ""
        status_code = 204
        url = "https://example.com/empty"

    class FakeSession:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_safe_get(session, url, **kwargs):
        return FakeResponse()

    monkeypatch.setattr(http_scraping, "AsyncSession", FakeSession)
    monkeypatch.setattr(http_scraping, "safe_get", fake_safe_get)

    raw = await http_scraping.raw_scrape(FakeResponse.url, retries=0)
    assert raw.success is False
    assert raw.needs_browser is True
    assert raw.reason == "empty response body"

    packed = ScraperEngine()._pack_result(
        FakeResponse.url,
        "",
        "layer1_simple",
        time.time(),
        [],
        res=SimpleNamespace(
            status_code=204,
            final_url=FakeResponse.url,
            reason="empty response body",
            cookies=None,
        ),
    )
    assert packed.ok is False
    assert packed.captured is False
    assert packed.data is None


@pytest.mark.asyncio
async def test_unsupported_reddit_bulk_url_uses_generic_fallback(monkeypatch):
    async def dedicated_route_must_not_run(*args, **kwargs):
        raise AssertionError("unsupported Reddit page reached JSON bulk route")

    monkeypatch.setattr(
        sources,
        "scrape_reddit_bulk",
        dedicated_route_must_not_run,
    )

    assert await sources.source_bulk_scrape(
        ["https://www.reddit.com/settings/profile"]
    ) is None


@pytest.mark.asyncio
async def test_hackernews_unknown_path_does_not_substitute_top_stories():
    html, ok = await sources.hackernews_api_html(
        "https://news.ycombinator.com/user?id=pg",
        session=object(),
    )
    assert html == ""
    assert ok is False


def test_selected_static_sources_do_not_use_browser_admission():
    assert {"medium", "youtube", "instagram"}.issubset(
        engine_mod.HTTP_ONLY_SOURCE_NAMES
    )


def test_walmart_source_is_http_only():
    assert "walmart" in engine_mod.HTTP_ONLY_SOURCE_NAMES


@pytest.mark.asyncio
async def test_reddit_stale_cookie_never_launches_browser(monkeypatch):
    launches = 0
    listing = json.dumps({
        "data": {
            "children": [
                {
                    "data": {
                        "title": "A real Reddit post",
                        "subreddit_name_prefixed": "r/python",
                        "score": 10,
                        "num_comments": 3,
                        "permalink": "/r/python/comments/abc/post/",
                    }
                }
            ]
        }
    })

    class FakeResponse:
        text = "blocked"
        status_code = 403
        headers = {"content-type": "text/html"}
        url = "https://www.reddit.com/r/python.json"

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_safe_get(*args, **kwargs):
        return FakeResponse()

    class FakeMemory:
        def __init__(self, *args, **kwargs):
            pass

        def get_valid_cookies(self, url):
            return []

        async def save_cookies(self, url, cookies):
            pass

    class FakePage:
        async def goto(self, *args, **kwargs):
            return None

        async def evaluate(self, script, urls):
            return [
                {"success": True, "status": 200, "data": listing}
                for _ in urls
            ]

    class FakeContext:
        async def new_page(self):
            return FakePage()

        async def cookies(self):
            return [{"name": "reddit_session", "value": "fresh"}]

        async def close(self):
            pass

    class FakeBrowser:
        async def new_context(self, **kwargs):
            return FakeContext()

        async def close(self):
            pass

    class FakeChromium:
        async def launch(self, **kwargs):
            nonlocal launches
            launches += 1
            return FakeBrowser()

    class FakePlaywrightContext:
        async def __aenter__(self):
            return SimpleNamespace(chromium=FakeChromium())

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(coverage, "AsyncSession", FakeSession)
    monkeypatch.setattr(coverage, "safe_get", fake_safe_get)
    monkeypatch.setattr(coverage, "async_playwright", FakePlaywrightContext)
    monkeypatch.setattr(coverage, "install_browser_network_guard", noop)
    monkeypatch.setattr(coverage, "inject_stealth_scripts", noop)
    monkeypatch.setattr(coverage, "_wait_loaded", noop)
    monkeypatch.setattr(coverage, "_act_human", noop)
    monkeypatch.setattr(site_memory_mod, "SiteMemory", FakeMemory)
    monkeypatch.setattr(coverage, "_reddit_lock", None)

    result = await asyncio.wait_for(
        coverage.scrape_reddit_bulk(
            ["https://www.reddit.com/r/python/"],
            cookies=[{"name": "reddit_session", "value": "stale"}],
        ),
        timeout=1.0,
    )

    assert launches == 0
    assert len(result) == 1
    assert result[0].success is False
    assert "browser" not in (result[0].reason or "").lower()
    assert "page-context" not in (result[0].reason or "")


@pytest.mark.asyncio
async def test_try_auto_preserves_named_source_html_when_layer1_fails(monkeypatch):
    captcha_html = "<html><body><div class='cf-turnstile'>Cloudflare challenge</div></body></html>"

    async def fake_source_scrape(url, **kwargs):
        return http_scraping.QuickResult(
            success=True,
            html=captcha_html,
            status_code=200,
            needs_browser=True,
            reason="Cloudflare challenge page",
            response_time=0.05,
            final_url=url,
        )

    async def fake_raw_scrape(url, retries=0):
        return http_scraping.QuickResult(
            success=False,
            html=None,
            status_code=0,
            needs_browser=True,
            reason="Raw request timed out",
            response_time=0.1,
            final_url=url,
        )

    observed_crawler_html = []

    async def fake_crawler_personas(url, start, attempts, skip_layers=None, html=None, captures=None, solve_challenges=False):
        observed_crawler_html.append(html)
        return None, html

    engine = ScraperEngine()
    engine.memory.get_failed_layers = lambda url: {
        "layer2_cookie",
        "layer1b_mobile",
        "layer1c_advanced_waf",
        "layer4_stealth",
        "layer6_ultimate",
    }
    monkeypatch.setattr(engine_mod, "source_scrape", fake_source_scrape)
    monkeypatch.setattr(engine_mod, "raw_scrape", fake_raw_scrape)
    monkeypatch.setattr(engine, "_try_crawler_personas", fake_crawler_personas)

    result = await engine._try_auto(
        "https://medium.com/test-story",
        None,
        time.time(),
        [],
        allow_layer5=False,
    )

    # Downstream crawler personas must receive the preserved named source HTML
    assert observed_crawler_html == [captcha_html]
    # Diagnostics must have received the preserved HTML and detected the captcha marker
    assert result.notes.get("captcha_detected") == "cloudflare_turnstile"


@pytest.mark.asyncio
async def test_bestbuy_source_normalizes_intl_nosplash(monkeypatch):
    seen_single = []
    seen_bulk = []

    async def fake_guarded_hard_source(url, source, **kwargs):
        seen_single.append(url)
        return sources.SourceResult(True, html="<html>OK</html>", status_code=200)

    async def fake_guarded_hard_bulk(urls, source, **kwargs):
        seen_bulk.extend(urls)
        return [sources.SourceResult(True, html="<html>OK</html>", status_code=200) for _ in urls]

    monkeypatch.setattr(sources, "_guarded_hard_source", fake_guarded_hard_source)
    monkeypatch.setattr(sources, "_guarded_hard_bulk", fake_guarded_hard_bulk)

    await sources.scrape_bestbuy("https://www.bestbuy.com/site/test-product")
    assert seen_single == ["https://www.bestbuy.com/site/test-product?intl=nosplash"]

    await sources.scrape_bestbuy_bulk([
        "https://www.bestbuy.com/site/test-1",
        "https://www.bestbuy.com/site/test-2?existing=1",
    ])
    assert seen_bulk == [
        "https://www.bestbuy.com/site/test-1?intl=nosplash",
        "https://www.bestbuy.com/site/test-2?existing=1&intl=nosplash",
    ]




@pytest.mark.asyncio
async def test_chrome_error_url_diagnostic_and_pack_result():
    engine = engine_mod.ScraperEngine()
    fake_res = browser_mod.BrowseOutput(
        success=True,
        html="<html><head></head><body></body></html>",
        status_code=200,
        reason="Navigation finished",
        final_url="chrome-error://chromewebdata/",
        layer="layer4_stealth",
    )
    attempt = engine_mod._attempt_from_result("layer4_stealth", fake_res)
    assert "false_positive:chrome_error_page" in attempt["diagnostics"]
    assert attempt["retry_recommended"] is True

    # Pack result should reject the chrome-error body
    res = engine._pack_result(
        "https://example.com",
        fake_res.html,
        "layer4_stealth",
        time.time(),
        [attempt],
        res=fake_res,
    )
    assert res.ok is False
    assert res.captured is False
    assert res.data is None
    assert "false_positive:chrome_error_page" in res.diagnostics


@pytest.mark.asyncio
async def test_auto_engine_rejects_chrome_error_and_escalates_to_layer6(monkeypatch):
    js_shell = '<html><body>Loading... <div id="root"></div></body></html>'
    full_content = '<html><body>' + ('Article text content ' * 100) + '</body></html>'

    async def fake_raw_scrape(url):
        return engine_mod.QuickResult(
            success=False,
            html="",
            status_code=0,
            reason="Raw scrape failed",
            final_url=url,
        )

    async def fake_quick_scrape(url, fingerprint=None, cookies=None):
        return engine_mod.QuickResult(
            success=True,
            html=js_shell,
            status_code=200,
            reason="JS shell loaded",
            final_url=url,
        )

    async def fake_stealth_scrape(url, **kwargs):
        return browser_mod.BrowseOutput(
            success=False,
            html="",
            status_code=0,
            reason="Browser navigation failed (navigation aborted / network reset)",
            final_url=url,
            layer="layer4_stealth",
        )

    async def fake_ultimate(url):
        return engine_mod.QuickResult(
            success=True,
            html=full_content,
            status_code=200,
            reason="TLS impersonation succeeded",
            final_url=url,
        )

    monkeypatch.setattr(engine_mod, "raw_scrape", fake_raw_scrape)
    monkeypatch.setattr(engine_mod, "quick_scrape", fake_quick_scrape)
    monkeypatch.setattr(engine_mod, "stealth_scrape", fake_stealth_scrape)
    monkeypatch.setattr(engine_mod, "scrape_ultimate", fake_ultimate)

    engine = engine_mod.ScraperEngine()
    engine.memory.profiles = {}
    result = await engine._try_auto(
        "https://example.com/item",
        fingerprint_mod.make_identity(),
        time.time(),
        [],
        skip_source=True,
    )

    assert result.ok is True
    assert result.winning_layer == "layer6_ultimate"
    assert result.data["html"] == full_content


@pytest.mark.asyncio
async def test_auto_engine_preserves_earlier_capture_when_browser_hits_chrome_error(monkeypatch):
    js_shell = '<html><body>Initial content <div id="root"></div></body></html>'

    async def fake_raw_scrape(url):
        return engine_mod.QuickResult(
            success=False,
            html="",
            status_code=0,
            reason="Raw scrape failed",
            final_url=url,
        )

    async def fake_quick_scrape(url, fingerprint=None, cookies=None):
        return engine_mod.QuickResult(
            success=True,
            html=js_shell,
            status_code=200,
            reason="JS shell",
            final_url=url,
        )

    async def fake_stealth_scrape(url, **kwargs):
        # Even if stealth_scrape were to produce the chrome error page
        return browser_mod.BrowseOutput(
            success=False,
            html="<html><body></body></html>",
            status_code=0,
            reason="Browser navigation failed",
            final_url="chrome-error://chromewebdata/",
            layer="layer4_stealth",
        )

    async def fake_ultimate(url):
        return engine_mod.QuickResult(
            success=False,
            html="",
            status_code=0,
            reason="Ultimate failed",
            final_url=url,
        )

    async def fake_crawler_scrape(url, bot_type="googlebot"):
        return engine_mod.QuickResult(
            success=False,
            html="",
            status_code=0,
            reason=f"{bot_type} failed",
            final_url=url,
        )

    monkeypatch.setattr(engine_mod, "raw_scrape", fake_raw_scrape)
    monkeypatch.setattr(engine_mod, "quick_scrape", fake_quick_scrape)
    monkeypatch.setattr(engine_mod, "crawler_scrape", fake_crawler_scrape)
    monkeypatch.setattr(engine_mod, "stealth_scrape", fake_stealth_scrape)
    monkeypatch.setattr(engine_mod, "scrape_ultimate", fake_ultimate)

    engine = engine_mod.ScraperEngine()
    engine.memory.profiles = {}
    result = await engine._try_auto(
        "https://example.com/page",
        fingerprint_mod.make_identity(),
        time.time(),
        [],
        allow_layer5=False,
        skip_source=True,
    )

    # Layer 2's body must be preserved as the best attempt, NOT the chrome-error body
    assert result.best_attempt is not None
    assert result.best_attempt["layer"] == "layer2_cookie"
    assert result.data["html"] == js_shell


def test_small_valid_page_is_not_treated_as_chrome_error():
    small_html = "<html><body>OK</body></html>"
    diagnostics = engine_mod._diagnostics_for_capture(
        html=small_html,
        status_code=200,
        reason="OK",
        url="https://example.com/api",
    )
    assert "false_positive:chrome_error_page" not in diagnostics


@pytest.mark.asyncio
async def test_layer6_js_shell_escalates_to_layer5_browser(monkeypatch):
    js_shell = '<html><body><div id="root"></div></body></html>'
    full_dom = '<html><body>' + ('Rendered DOM text content ' * 100) + '</body></html>'

    async def fake_raw_scrape(url):
        return engine_mod.QuickResult(False, status_code=0, reason="raw failed", final_url=url)

    async def fake_quick_scrape(url, **kw):
        return engine_mod.QuickResult(False, status_code=0, reason="quick failed", final_url=url)

    async def fake_crawler_scrape(url, **kw):
        return engine_mod.QuickResult(False, status_code=0, reason="crawler failed", final_url=url)

    async def fake_stealth_scrape(url, **kw):
        return browser_mod.BrowseOutput(False, status_code=0, reason="stealth failed", final_url=url)

    async def fake_ultimate(url):
        return engine_mod.QuickResult(True, html=js_shell, status_code=200, reason="TLS 200 OK", final_url=url)

    async def fake_full_scrape(url, **kw):
        return browser_mod.BrowseOutput(True, html=full_dom, status_code=200, reason="Rendered via Playwright", final_url=url, layer="layer5_full")

    monkeypatch.setattr(engine_mod, "raw_scrape", fake_raw_scrape)
    monkeypatch.setattr(engine_mod, "quick_scrape", fake_quick_scrape)
    monkeypatch.setattr(engine_mod, "crawler_scrape", fake_crawler_scrape)
    monkeypatch.setattr(engine_mod, "stealth_scrape", fake_stealth_scrape)
    monkeypatch.setattr(engine_mod, "scrape_ultimate", fake_ultimate)
    monkeypatch.setattr(engine_mod, "full_scrape", fake_full_scrape)

    engine = engine_mod.ScraperEngine()
    engine.memory.profiles = {}
    result = await engine._try_auto(
        "https://example.com/spa-app",
        fingerprint_mod.make_identity(),
        time.time(),
        [],
        allow_layer5=True,
        skip_source=True,
    )

    assert result.ok is True
    assert result.winning_layer == "layer5_full"
    assert result.data["html"] == full_dom


def test_about_blank_and_empty_skeleton_rejected_while_valid_200_preserved():
    # 1. about:blank with status 0 must be rejected from captures
    blank_res = browser_mod.BrowseOutput(
        success=False,
        html="<html><head></head><body></body></html>",
        status_code=0,
        reason="Navigation aborted",
        final_url="about:blank",
    )
    capture = engine_mod._capture_from_result("layer4_stealth", blank_res)
    assert capture is None, "about:blank with status 0 should not be saved as a valid capture"

    # 2. Empty skeleton with status 0 must be rejected
    skeleton_res = browser_mod.BrowseOutput(
        success=False,
        html="<html><body></body></html>",
        status_code=0,
        reason="Aborted",
        final_url="https://example.com",
    )
    capture = engine_mod._capture_from_result("layer4_stealth", skeleton_res)
    assert capture is None, "status 0 skeleton should not be saved as a valid capture"

    # 3. Legitimate small response with status 200 must be PRESERVED and ACCEPTED
    valid_res = engine_mod.QuickResult(
        success=True,
        html="<html><body>OK</body></html>",
        status_code=200,
        reason="OK",
        final_url="https://example.com/api",
    )
    capture = engine_mod._capture_from_result("layer1_simple", valid_res)
    assert capture is not None, "Valid 200 response must be accepted into captures"
    assert capture["html"] == "<html><body>OK</body></html>"
    assert capture["status_code"] == 200

    # 4. Legitimate 403 block page must be preserved so YADA can keep best attempt
    forbidden_res = engine_mod.QuickResult(
        success=False,
        html="<html><body>Access Denied - Cloudflare 403</body></html>",
        status_code=403,
        reason="Forbidden",
        final_url="https://example.com/protected",
    )
    capture = engine_mod._capture_from_result("layer2_cookie", forbidden_res)
    assert capture is not None, "403 block page must be preserved for fallback diagnostics"
    assert capture["status_code"] == 403


@pytest.mark.parametrize(
    "html, limit",
    [
        ("<p>a\ud800b\udfff</p>", 1000),               # under limit
        ("<p>" + "x" * 500 + "\ud800</p>", 100),       # over limit
    ],
)
def test_lone_surrogates_do_not_crash(monkeypatch, html, limit):
    monkeypatch.setattr(engine_mod, "_max_content_bytes", lambda: limit)
    out, _ = engine_mod._bounded_html(html)
    json.dumps({"html": out}, ensure_ascii=False).encode("utf-8")
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in out)


def test_pack_result_with_lone_surrogates_serializes_cleanly():
    eng = ScraperEngine()
    surrogate_html = "<html><body>Surrogate test: \ud800\udfff</body></html>"
    res = SimpleNamespace(
        success=True,
        html=surrogate_html,
        status_code=200,
        final_url="https://example.com/surrogate",
        reason="OK",
    )
    packed = eng._pack_result("https://example.com/surrogate", res.html, "layer1_simple", time.time(), [], res=res)
    assert packed.ok is True
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in packed.data["html"])
    encoded_json = json.dumps(packed.to_dict(), ensure_ascii=False).encode("utf-8")
    assert b"Surrogate test:" in encoded_json


@pytest.mark.asyncio
async def test_scrape_reddit_bulk_uses_passed_memory_and_never_instantiates_sitememory(monkeypatch, tmp_path):
    path = tmp_path / "memory.json"
    memory = SiteMemory(str(path))
    await memory.save_cookies("https://example.com", [{"name": "existing", "value": "keep_me"}])

    # Forbid any new SiteMemory instantiations
    def forbidden_init(self, *args, **kwargs):
        raise RuntimeError("Rogue SiteMemory instantiation forbidden!")
    monkeypatch.setattr(SiteMemory, "__init__", forbidden_init)

    listing_json = json.dumps({
        "data": {
            "children": [
                {
                    "data": {
                        "title": "Python Post",
                        "selftext": "Content of post",
                        "author": "tester",
                        "score": 10,
                        "num_comments": 2,
                        "permalink": "/r/python/comments/1",
                        "url": "https://reddit.com/r/python/comments/1"
                    }
                }
            ]
        }
    })

    fake_resp = SimpleNamespace(
        status_code=200,
        text=listing_json,
        json=lambda: json.loads(listing_json),
        cookies={},
        url="https://www.reddit.com/r/python.json",
        headers={"content-type": "application/json"}
    )

    async def fake_safe_get(*args, **kwargs):
        return fake_resp

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, *args, **kwargs):
            return fake_resp

    monkeypatch.setattr(coverage, "AsyncSession", FakeSession)
    monkeypatch.setattr(coverage, "safe_get", fake_safe_get)
    monkeypatch.setattr(coverage, "_reddit_lock", None)

    # 1. Run with memory passed
    results = await coverage.scrape_reddit_bulk(
        ["https://www.reddit.com/r/python/"],
        memory=memory,
    )
    assert len(results) == 1
    assert results[0].success is True
    # Verify existing profile remains intact in memory
    assert "example.com" in memory.profiles
    assert memory.profiles["example.com"].cookies[0]["value"] == "keep_me"

    # 2. Run with memory=None to ensure it also NEVER instantiates SiteMemory
    results_none = await coverage.scrape_reddit_bulk(
        ["https://www.reddit.com/r/python/"],
        memory=None,
    )
    assert len(results_none) == 1
    assert results_none[0].success is True


