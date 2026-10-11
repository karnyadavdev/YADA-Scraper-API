import time
from types import SimpleNamespace

import pytest

import scraper.engine as engine_mod
from scraper.site_memory import SiteInfo, SiteMemory
import scraper.source_fast_paths as source_mod
import yada_scraper as api_mod
from scraper.engine import ScraperEngine
from scraper.browser_identity import make_identity


def _fake_result(**overrides):
    data = {
        "success": False,
        "html": "",
        "status_code": 0,
        "reason": "",
        "response_time": 0.01,
        "final_url": "https://example.com/",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_make_identity_uses_only_chromium_families():
    for _ in range(100):
        identity = make_identity()
        assert identity.browser_type in {"chrome", "edge"}
        assert identity.impersonate_id in {"chrome131", "chrome120"}


def test_source_name_finds_supported_hosts():
    assert source_mod.source_name("https://medium.com/tag/python") == "medium"
    assert source_mod.source_name("https://theaiinsider.quora.com/post") == "quora"
    assert source_mod.source_name("https://www.youtube.com/watch?v=abc") == "youtube"
    assert source_mod.source_name("https://youtu.be/abc") == "youtube"
    assert source_mod.source_name("https://www.linkedin.com/company/openai/") == "linkedin"
    assert source_mod.source_name("https://www.reddit.com/r/webscraping/") == "reddit"
    assert source_mod.source_name("https://example.com/post") is None


def test_experimental_px_source_names(monkeypatch):
    monkeypatch.setenv("YADA_EXPERIMENTAL_PX", "1")

    assert source_mod.source_name("https://www.walmart.com/search?q=laptop") == "walmart"
    assert source_mod.source_name("https://www.belk.com/search/?q=shoes") == "belk"


def test_reddit_json_post_extracts_readable_html():
    data = [
        {
            "data": {
                "children": [
                    {
                        "data": {
                            "title": "Reverse engineering PerimeterX new VM",
                            "subreddit_name_prefixed": "r/webscraping",
                            "author": "researcher",
                            "selftext": "Post body",
                            "url": "https://github.com/B9ph0met/px-vm",
                        }
                    }
                ]
            }
        },
        {
            "data": {
                "children": [
                    {
                        "kind": "t1",
                        "data": {
                            "author": "commenter",
                            "body": "PerimeterX is painful.",
                            "replies": "",
                        },
                    }
                ]
            }
        },
    ]

    html = source_mod.reddit_json_to_html(data, "https://www.reddit.com/r/webscraping/comments/x/test")

    assert "Reverse engineering PerimeterX new VM" in html
    assert "https://github.com/B9ph0met/px-vm" in html
    assert "commenter: PerimeterX is painful." in html


def test_reddit_json_url_rewrites_posts_and_listings():
    post = source_mod.reddit_json_url("https://www.reddit.com/r/webscraping/comments/abc/title/")
    listing = source_mod.reddit_json_url("https://www.reddit.com/r/webscraping/top/?t=week")

    assert post == "https://www.reddit.com/r/webscraping/comments/abc/title.json"
    assert listing == "https://www.reddit.com/r/webscraping/top.json?t=week"


def test_reddit_html_fallback_extracts_post_body():
    html = """
    <html><body>
      <shreddit-post
        post-title="How to bypass PerimeterX"
        subreddit-prefixed-name="r/webscraping"
        author="LongDivide2096"
        content-href="https://www.reddit.com/r/webscraping/comments/abc/title/">
        <shreddit-post-text-body>
          <p>Internal property: window._pxAppId property.</p>
          <p>Cookies: PerimeterX set the cookies: _px3, _pxhd, _px_vid.</p>
        </shreddit-post-text-body>
      </shreddit-post>
    </body></html>
    """

    out = source_mod.reddit_html_to_html(html, "https://www.reddit.com/r/webscraping/comments/abc/title/")

    assert "How to bypass PerimeterX" in out
    assert "r/webscraping" in out
    assert "u/LongDivide2096" in out
    assert "window._pxAppId" in out
    assert "_pxhd" in out


def test_site_memory_does_not_skip_source_paths(tmp_path):
    memory = SiteMemory(memory_path=str(tmp_path / "memory.json"))
    memory.profiles["www.belk.com"] = SiteInfo(
        domain="www.belk.com",
        failed_layers=["source_belk", "layer2_cookie"],
        last_scrape_time=time.time(),
    )

    failed = memory.get_failed_layers("https://www.belk.com/search/?q=dresses")

    assert "source_belk" not in failed
    assert "layer2_cookie" in failed


def test_perimeterx_markers_are_classified():
    html = """
    <script>window._pxAppId = "PXabc";</script>
    <script src="https://px-client.net/script.js"></script>
    <meta data-cookie="_px3 _pxhd _px_vid">
    """

    assert engine_mod.find_captcha(html) == "perimeterx"


def test_medium_apollo_state_extraction_builds_readable_html():
    state = {
        "Paragraph:post_2": {"text": "Second paragraph."},
        "Paragraph:post_1": {"text": "First paragraph."},
        "Paragraph:post_3": {"text": "Third paragraph."},
    }
    html = (
        "<html><head><title>Medium Test</title></head><body>"
        "<script>window.__APOLLO_STATE__ = "
        + __import__("json").dumps(state)
        + "</script></body></html>"
    )

    out = source_mod.medium_article_from_state(html, "https://medium.com/test")

    assert out is not None
    assert "Medium Test" in out
    assert out.index("First paragraph.") < out.index("Second paragraph.")


@pytest.mark.asyncio
async def test_failed_http_body_is_preserved(monkeypatch):
    html = "<html><title>Access Denied</title><body>Access Denied</body></html>"

    async def fake_raw_scrape(url):
        return _fake_result(
            html=html,
            status_code=403,
            reason="HTTP 403",
            final_url=url,
        )

    monkeypatch.setattr(engine_mod, "raw_scrape", fake_raw_scrape)

    engine = ScraperEngine()
    result = await engine._try_layer1("https://example.com/private", time.time(), [])

    assert result.ok is False
    assert result.captured is True
    assert result.capture_state == "captured_with_warnings"
    assert result.data["html"] == html
    assert "http_403" in result.diagnostics
    assert "access_denied_text_seen" in result.diagnostics


@pytest.mark.asyncio
async def test_captcha_body_is_warning_not_null(monkeypatch):
    html = "<html><body><div class='cf-turnstile'>captcha</div></body></html>"

    async def fake_stealth_scrape(url, fingerprint=None, **kwargs):
        return _fake_result(
            html=html,
            status_code=403,
            reason="Captured page still looks like an interstitial. CAPTCHA visible",
            final_url=url,
        )

    monkeypatch.setattr(engine_mod, "stealth_scrape", fake_stealth_scrape)

    engine = ScraperEngine()
    result = await engine._try_layer4("https://example.com/captcha", make_identity(chrome_only=True), time.time(), [])

    assert result.ok is False
    assert result.data["html"] == html
    assert result.best_attempt["layer"] == "layer4_stealth"
    assert any(d.startswith("captcha_marker_seen") for d in result.diagnostics)


@pytest.mark.asyncio
async def test_no_capture_stays_failed(monkeypatch):
    async def fake_full_scrape(url, fingerprint=None, **kwargs):
        return _fake_result(
            html=None,
            status_code=0,
            reason="Full browser capture failed: TimeoutError",
            final_url=url,
        )

    monkeypatch.setattr(engine_mod, "full_scrape", fake_full_scrape)

    engine = ScraperEngine()
    result = await engine._try_layer5("https://example.com/timeout", make_identity(chrome_only=True), time.time(), [])

    assert result.ok is False
    assert result.captured is False
    assert result.capture_state == "no_capture"
    assert result.data is None


@pytest.mark.asyncio
async def test_auto_escalates_but_keeps_best_captured_body(monkeypatch):
    small_html = "<html><body>Access Denied</body></html>"
    bigger_html = "<html><body>" + ("useful payload " * 200) + "</body></html>"

    async def fake_raw_scrape(url):
        return _fake_result(
            html=small_html,
            status_code=403,
            reason="HTTP 403",
            final_url=url,
        )

    async def fake_quick_scrape(url, fingerprint=None, cookies=None):
        return _fake_result(
            html=bigger_html,
            status_code=403,
            reason="HTTP 403",
            final_url=url,
        )

    async def fake_crawler_scrape(url, bot_type="googlebot"):
        return _fake_result(
            html="",
            status_code=0,
            reason=f"{bot_type} failed",
            final_url=url,
        )

    async def fake_stealth_scrape(url, fingerprint=None, **kwargs):
        return _fake_result(
            html="",
            status_code=0,
            reason="Stealth browser capture failed: TimeoutError",
            final_url=url,
        )

    async def fake_full_scrape(url, fingerprint=None, **kwargs):
        return _fake_result(
            html="",
            status_code=0,
            reason="Full browser capture failed: TimeoutError",
            final_url=url,
        )

    monkeypatch.setattr(engine_mod, "raw_scrape", fake_raw_scrape)
    monkeypatch.setattr(engine_mod, "quick_scrape", fake_quick_scrape)
    monkeypatch.setattr(engine_mod, "crawler_scrape", fake_crawler_scrape)
    monkeypatch.setattr(engine_mod, "stealth_scrape", fake_stealth_scrape)
    monkeypatch.setattr(engine_mod, "full_scrape", fake_full_scrape)

    engine = ScraperEngine()
    engine.memory.profiles = {}
    result = await engine._try_auto("https://example.com/page", make_identity(), time.time(), [])

    assert result.ok is False
    assert result.captured is True
    assert result.data["html"] == bigger_html
    assert result.best_attempt["layer"] == "layer2_cookie"
    assert len(result.tries) >= 5


@pytest.mark.asyncio
async def test_auto_continues_when_success_capture_still_looks_like_shell(monkeypatch):
    shell_html = "<html><body><div class='cf-turnstile'>captcha</div></body></html>"
    full_html = "<html><body>" + ("article text " * 400) + "</body></html>"

    async def fake_quick_scrape(url, fingerprint=None, cookies=None):
        return _fake_result(
            success=True,
            html=shell_html,
            status_code=200,
            reason="Loaded via static request",
            final_url=url,
        )

    async def fake_crawler_scrape(url, bot_type="googlebot"):
        return _fake_result(
            html="",
            status_code=0,
            reason=f"{bot_type} failed",
            final_url=url,
        )

    async def fake_stealth_scrape(url, fingerprint=None, **kwargs):
        return _fake_result(
            success=True,
            html=full_html,
            status_code=200,
            reason="Browser loaded page",
            final_url=url,
        )

    monkeypatch.setattr(engine_mod, "quick_scrape", fake_quick_scrape)
    monkeypatch.setattr(engine_mod, "crawler_scrape", fake_crawler_scrape)
    monkeypatch.setattr(engine_mod, "stealth_scrape", fake_stealth_scrape)

    engine = ScraperEngine()
    engine.memory.profiles = {}
    result = await engine._try_auto("https://example.com/page", make_identity(), time.time(), [])

    assert result.ok is True
    assert result.data["html"] == full_html
    assert result.best_attempt["layer"] == "layer4_stealth"
    assert result.tries[0]["retry_recommended"] is True


@pytest.mark.asyncio
async def test_auto_accepts_large_content_even_with_captcha_marker(monkeypatch):
    html = "<html><body>" + ("useful content " * 500) + "<div>captcha</div></body></html>"

    async def fake_quick_scrape(url, fingerprint=None, cookies=None):
        return _fake_result(
            success=True,
            html=html,
            status_code=200,
            reason="Loaded via static request",
            final_url=url,
        )

    monkeypatch.setattr(engine_mod, "quick_scrape", fake_quick_scrape)

    engine = ScraperEngine()
    engine.memory.profiles = {}
    result = await engine._try_auto("https://example.com/page", make_identity(), time.time(), [])

    assert result.ok is True
    assert result.data["html"] == html
    assert result.best_attempt["layer"] == "layer2_cookie"
    assert result.best_attempt["retry_recommended"] is False
    assert any(d.startswith("captcha_marker_seen") for d in result.diagnostics)


@pytest.mark.asyncio
async def test_auto_uses_source_fast_path_before_generic_layers(monkeypatch):
    html = "<html><body>" + ("source content " * 300) + "</body></html>"

    async def fake_source_scrape(url):
        return _fake_result(
            success=True,
            html=html,
            status_code=200,
            reason="source path",
            final_url=url,
        )

    monkeypatch.setattr(engine_mod, "source_scrape", fake_source_scrape)

    engine = ScraperEngine()
    engine.memory.profiles = {}
    result = await engine._try_auto("https://medium.com/test", make_identity(), time.time(), [])

    assert result.ok is True
    assert result.best_attempt["layer"] == "source_medium"
    assert result.data["html"] == html


@pytest.mark.asyncio
async def test_bulk_falls_back_when_cookie_fanout_is_retryable(monkeypatch):
    class FakeMemory:
        def get_valid_cookies(self, url):
            return [{"name": "sid", "value": "1"}]

        async def clear_cookies(self, url):
            return None

    class FakeEngine:
        def __init__(self):
            self.memory = FakeMemory()

        async def scrape_layer2_after_warmup(self, url):
            return SimpleNamespace(
                ok=True,
                diagnostics=[],
                best_attempt={"retry_recommended": True},
                to_dict=lambda: {
                    "success": True,
                    "layer_used": "layer2_cookie",
                    "best_attempt": {"retry_recommended": True},
                },
            )

        async def scrape(self, url, mode="auto", allow_layer5=True):
            return SimpleNamespace(
                ok=True,
                to_dict=lambda: {
                    "success": True,
                    "layer_used": "source_quora",
                    "best_attempt": {"retry_recommended": False},
                },
            )

    original_engine = api_mod.engine
    api_mod.engine = FakeEngine()
    try:
        result = await api_mod.do_bulk(
            api_mod.BulkRequest(
                urls=[
                    "https://example.com/one",
                    "https://example.com/two",
                ],
                mode=api_mod.ScrapeMode.auto,
                max_concurrency=1,
                same_domain_fanout=1,
            )
        )
    finally:
        api_mod.engine = original_engine

    assert result.succeeded == 2
    assert result.results[1]["bulk_role"] == "cookie_fanout_fallback_auto"
    assert result.results[1]["layer_used"] == "source_quora"
