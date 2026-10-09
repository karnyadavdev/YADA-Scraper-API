import asyncio
import time
from types import SimpleNamespace

import pytest

import scraper.engine as engine_mod
import scraper.http_scraping as http_mod
from scraper.site_memory import SiteInfo, SiteMemory
from scraper.anti_bot_signals import anti_bot_signals
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
        assert identity.impersonate_id in {"chrome120", "edge101"}


def test_source_name_finds_supported_hosts():
    assert source_mod.source_name("https://medium.com/tag/python") == "medium"
    assert source_mod.source_name("https://theaiinsider.quora.com/post") == "quora"
    assert source_mod.source_name("https://www.youtube.com/watch?v=abc") == "youtube"
    assert source_mod.source_name("https://youtu.be/abc") == "youtube"
    assert source_mod.source_name("https://www.linkedin.com/company/openai/") == "linkedin"
    assert source_mod.source_name("https://www.reddit.com/r/webscraping/") == "reddit"
    assert source_mod.source_name("https://news.ycombinator.com/item?id=40000000") == "hackernews"
    assert source_mod.source_name("https://github.com/openai/openai-python") == "github"
    assert source_mod.source_name("https://arxiv.org/abs/1706.03762") == "arxiv"
    assert source_mod.source_name("https://crates.io/crates/serde") == "crates"
    assert source_mod.source_name("https://www.semanticscholar.org/paper/test") == "semantic_scholar"
    assert source_mod.source_name("https://pubmed.ncbi.nlm.nih.gov/123456/") == "pubmed"
    assert source_mod.source_name("https://aclanthology.org/2020.acl-main.447/") == "acl_anthology"
    assert source_mod.source_name("https://stackoverflow.com/questions/231767/what-does-the-yield-keyword-do-in-python") == "stackexchange"
    assert source_mod.source_name("https://math.stackexchange.com/questions/1000") == "stackexchange"
    assert source_mod.source_name("https://remotive.com/remote-jobs/software-dev") == "remotive"
    assert source_mod.source_name("https://clinicaltrials.gov") == "clinicaltrials"
    assert source_mod.source_name("https://meta.discourse.org") is None
    assert source_mod.source_name("https://meta.discourse.org/latest") == "discourse"
    assert source_mod.source_name("https://mastodon.social/explore") == "mastodon"
    assert source_mod.source_name("https://www.sec.gov/Archives/test.htm") == "sec"
    assert source_mod.source_name("https://pytorch.org/docs/stable/index.html") is None
    assert source_mod.source_name("https://example.com/post") is None


def test_hard_vendor_source_names_are_enabled_by_default(monkeypatch):
    monkeypatch.delenv("YADA_EXPERIMENTAL_PX", raising=False)
    monkeypatch.delenv("YADA_HARD_SOURCES", raising=False)
    assert source_mod.source_name("https://www.walmart.com/search?q=laptop") == "walmart"
    assert source_mod.source_name("https://www.bestbuy.com/site/searchpage.jsp?st=laptop") == "bestbuy"
    assert source_mod.source_name("https://www.ebay.com/sch/i.html?_nkw=gpu") == "ebay"
    assert source_mod.source_name("https://www.vinted.com/catalog?search_text=nike") is None
    assert source_mod.source_name("https://www.zillow.com/homes/San-Francisco,-CA_rb/") == "zillow"
    assert source_mod.source_name("https://www.homedepot.com/s/drill") == "homedepot"
    assert source_mod.source_name("https://www.tripadvisor.com/Hotels-g60713-San_Francisco_California-Hotels.html") == "tripadvisor"
    assert source_mod.source_name("https://www.reuters.com/world/") is None
    assert source_mod.source_name("https://www.leboncoin.fr/recherche?text=iphone") is None
    assert source_mod.source_name("https://www.belk.com/search/?q=shoes") == "belk"


def test_experimental_hard_vendor_source_names_require_opt_in(monkeypatch):
    monkeypatch.delenv("YADA_EXPERIMENTAL_PX", raising=False)
    monkeypatch.delenv("YADA_HARD_SOURCES", raising=False)
    monkeypatch.setenv("YADA_EXPERIMENTAL_HARD_SOURCES", "1")

    assert source_mod.source_name("https://www.leboncoin.fr/recherche?text=iphone") is None
    assert source_mod.source_name("https://www.belk.com/search/?q=shoes") == "belk"


def test_hard_vendor_source_names_can_be_disabled(monkeypatch):
    monkeypatch.setenv("YADA_HARD_SOURCES", "0")
    monkeypatch.setenv("YADA_EXPERIMENTAL_HARD_SOURCES", "1")

    assert source_mod.source_name("https://www.walmart.com/search?q=laptop") is None
    assert source_mod.source_name("https://www.zillow.com/homes/San-Francisco,-CA_rb/") is None
    assert source_mod.source_name("https://www.leboncoin.fr/recherche?text=iphone") is None


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


def test_reddit_json_response_helper_preserves_direct_json_urls():
    body = __import__("json").dumps({
        "data": {
            "children": [
                {
                    "data": {
                        "title": "New technology thread",
                        "subreddit_name_prefixed": "r/technology",
                        "score": 42,
                        "num_comments": 7,
                        "permalink": "/r/technology/comments/abc/new_technology_thread/",
                    }
                }
            ]
        }
    })

    result = source_mod._reddit_json_source_result_from_body(
        body,
        200,
        "https://www.reddit.com/r/technology/new.json?limit=10",
        "Reddit page-context JSON fetch",
        time.time(),
    )

    assert result.success is True
    assert result.final_url.endswith("/new.json?limit=10")
    assert result.reason == "Reddit page-context JSON fetch source path"
    assert "New technology thread" in result.html


def test_reddit_json_response_helper_rejects_security_block():
    result = source_mod._reddit_json_source_result_from_body(
        "<html><body>You've been blocked by network security.</body></html>",
        403,
        "https://www.reddit.com/r/technology/new.json?limit=10",
        "Reddit page-context JSON fetch",
        time.time(),
    )

    assert result.success is False
    assert "network security block" in result.reason


@pytest.mark.asyncio
async def test_reddit_direct_json_fails_honestly_without_browser_after_request_block(monkeypatch):
    listing_body = __import__("json").dumps({
        "data": {
            "children": [
                {
                    "data": {
                        "title": "Fallback fetched Reddit post",
                        "subreddit_name_prefixed": "r/technology",
                        "score": 11,
                        "num_comments": 3,
                        "permalink": "/r/technology/comments/abc/fallback/",
                    }
                }
            ]
        }
    })

    class FakeBlockedResponse:
        status = 403
        url = "https://www.reddit.com/r/technology/new.json?limit=10"
        headers = {"content-type": "text/html"}

        async def text(self):
            return "<html><body>You've been blocked by network security.</body></html>"

    class FakeRequest:
        async def get(self, *args, **kwargs):
            return FakeBlockedResponse()

    class FakePage:
        def __init__(self):
            self.goto_urls = []
            self.fetch_urls = []

        async def goto(self, url, **kwargs):
            self.goto_urls.append(url)

        async def wait_for_timeout(self, ms):
            return None

        async def evaluate(self, script, url):
            self.fetch_urls.append(url)
            return {
                "status": 200,
                "url": url,
                "contentType": "application/json",
                "body": listing_body,
            }

    fake_page = FakePage()
    launches = []

    class FakeContext:
        request = FakeRequest()

        async def route(self, pattern, handler):
            assert pattern == "**/*"

        async def new_page(self):
            return fake_page

        async def cookies(self):
            return []

        async def close(self):
            return None

    class FakeBrowser:
        async def new_context(self, **kwargs):
            assert kwargs["service_workers"] == "block"
            return FakeContext()

        async def close(self):
            return None

    class FakeChromium:
        async def launch(self, **kwargs):
            launches.append(1)
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def no_stealth(page, platform):
        return None

    class FakeHttpBlockedResponse:
        status_code = 403
        url = "https://www.reddit.com/r/technology/new.json?limit=10"
        headers = {"content-type": "text/html"}
        text = "<html><body>You've been blocked by network security.</body></html>"

    async def blocked_http_probe(*args, **kwargs):
        return FakeHttpBlockedResponse()

    class FakeHttpSession:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(source_mod, "async_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(source_mod, "inject_stealth_scripts", no_stealth)
    monkeypatch.setattr(source_mod, "safe_get", blocked_http_probe)
    monkeypatch.setattr(source_mod, "AsyncSession", FakeHttpSession)

    result = await source_mod.scrape_reddit(
        "https://www.reddit.com/r/technology/new.json?limit=10",
        timeout=5,
    )

    assert result.success is False
    assert "page-context" not in result.reason
    assert launches == []
    assert fake_page.goto_urls == []
    assert fake_page.fetch_urls == []


def test_reddit_json_url_rewrites_posts_and_listings():
    post = source_mod.reddit_json_url("https://www.reddit.com/r/webscraping/comments/abc/title/")
    listing = source_mod.reddit_json_url("https://www.reddit.com/r/webscraping/top/?t=week")
    bare_host = source_mod.reddit_json_url("https://reddit.com/r/webscraping/new/")

    assert post == "https://www.reddit.com/r/webscraping/comments/abc/title.json"
    assert listing == "https://www.reddit.com/r/webscraping/top.json?t=week"
    assert bare_host == "https://www.reddit.com/r/webscraping/new.json"


def test_instagram_meta_extract_includes_profile_image():
    html = """
    <html><head>
      <meta property="og:title" content="OpenAI (@openai) • Instagram photos and videos">
      <meta property="og:description" content="6M Followers, 7 Following, 1,790 Posts">
      <meta property="og:image" content="https://cdn.example/openai.jpg">
    </head><body></body></html>
    """

    out, ok = source_mod._compact_meta_html(html, "https://www.instagram.com/openai/", "instagram")

    assert ok is True
    assert "Image URL: https://cdn.example/openai.jpg" in out


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


def test_stackexchange_question_helpers_build_readable_html():
    question = {
        "title": "What does yield do?",
        "tags": ["python", "generator"],
        "score": 42,
        "view_count": 1000,
        "link": "https://stackoverflow.com/questions/1/test",
        "body": "<p>What functionality does yield provide?</p>",
    }
    answers = [
        {
            "is_accepted": True,
            "score": 100,
            "body": "<p>Yield turns a function into a generator.</p>",
        }
    ]

    html = source_mod.stackexchange_question_to_html(question, answers, "https://stackoverflow.com/questions/1/test")

    assert source_mod.stackexchange_site_from_url("https://stackoverflow.com/questions/1/test") == "stackoverflow"
    assert source_mod.stackexchange_site_from_url("https://math.stackexchange.com/questions/1/test") == "math"
    assert source_mod.stackexchange_question_id("https://stackoverflow.com/questions/231767/test") == "231767"
    assert "What does yield do?" in html
    assert "Yield turns a function into a generator." in html


def test_remotive_jobs_helpers_build_readable_html():
    data = {
        "job-count": 1,
        "jobs": [
            {
                "title": "Backend Engineer",
                "company_name": "Example Co",
                "job_type": "full_time",
                "candidate_required_location": "Worldwide",
                "url": "https://remotive.com/remote-jobs/software-dev/backend-engineer",
                "description": "<p>Build APIs and keep systems boring.</p>",
            }
        ],
    }

    html = source_mod.remotive_jobs_to_html(data, "https://remotive.com/remote-jobs/software-dev")

    assert source_mod.remotive_category_from_url("https://remotive.com/remote-jobs/software-dev") == "software-dev"
    assert "Backend Engineer" in html
    assert "Build APIs and keep systems boring." in html


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


def test_malformed_memory_file_does_not_break_startup(tmp_path):
    path = tmp_path / "memory.json"
    for payload in ('[{"a": 1}]', '{"x": 3}'):
        path.write_text(payload, encoding="utf-8")
        memory = SiteMemory(memory_path=str(path))
        assert memory.profiles == {}


def test_perimeterx_markers_are_classified():
    html = """
    <script>window._pxAppId = "PXabc";</script>
    <script src="https://px-client.net/script.js"></script>
    <meta data-cookie="_px3 _pxhd _px_vid">
    """

    assert engine_mod.find_captcha(html) == "perimeterx"


def test_vendor_signals_find_common_hard_vendors():
    html = """
    <script src="https://ct.captcha-delivery.com/c.js"></script>
    <script>window._pxAppId = "PXabc";</script>
    <script src="https://client.perimeterx.net/auditor.js"></script>
    <img src="https://fst-ec.perimeterx.net/?id=PXabc">
    """
    cookies = [
        {"name": "datadome", "value": "x"},
        {"name": "_abck", "value": "y"},
        {"name": "_px3", "value": "z"},
    ]

    signals = anti_bot_signals(html=html, cookies=cookies)

    assert "akamai" in signals["vendors"]
    assert "datadome" in signals["vendors"]
    assert "perimeterx" in signals["vendors"]


def test_human_verification_text_is_retryable():
    html = """
    <html><head><title>Just a moment...</title></head>
    <body>Performing security verification. Enable JavaScript and cookies to continue.</body></html>
    """

    diagnostics = engine_mod._diagnostics_for_capture(html, 200, "")

    assert "human_verification_text_seen" in diagnostics
    assert engine_mod._retry_recommended(html, 200, diagnostics)


@pytest.mark.parametrize(
    ("url", "html", "expected"),
    [
        (
            "https://www.bestbuy.com/site/searchpage.jsp?st=laptop",
            "<html><body>Best Buy Choose a country Canada United States</body></html>",
            "false_positive:bestbuy_country_selector",
        ),
        (
            "https://scholar.google.com/scholar?q=attention",
            "<html><body>The system can't perform the operation now. Try again later.</body></html>",
            "false_positive:google_scholar_temporary_block",
        ),
        (
            "https://www.homedepot.com/s/laptop",
            "<html><body>Oops!! Something went wrong Access Denied</body></html>",
            "false_positive:homedepot_error_shell",
        ),
        (
            "https://math.stackexchange.com/questions/1001/test",
            "<html><title>Page not found - Mathematics Stack Exchange</title><body>Stack Exchange</body></html>",
            "false_positive:stackexchange_page_not_found",
        ),
        (
            "https://www.walmart.com/search?q=laptop",
            "<html><body>Walmart We're having a technical issue. Please try again.</body></html>",
            "false_positive:walmart_technical_issue",
        ),
        (
            "https://www.glassdoor.com/Reviews/OpenAI-Reviews-E123.htm",
            "<html><body>Glassdoor Sign in to Glassdoor. Security check required.</body></html>",
            "false_positive:glassdoor_auth_wall",
        ),
        (
            "https://www.quora.com/graphql/gql_para_POST",
            '<html><body>{"errors":[{"message":"Just a moment..."}],"status":403,"path":"Quora GraphQL"}</body></html>',
            "false_positive:quora_graphql_error",
        ),
    ],
)
def test_known_false_positive_shells_are_retryable(url, html, expected):
    diagnostics = engine_mod._diagnostics_for_capture(html, 200, "", url=url)

    assert expected in diagnostics
    assert engine_mod._retry_recommended(html, 200, diagnostics)


def test_large_useful_capture_with_js_marker_is_not_forced_retry():
    html = '<html><body><div id="__next"></div>' + ("useful docs content " * 180) + "</body></html>"

    diagnostics = engine_mod._diagnostics_for_capture(html, 200, "")

    assert "possible_js_shell" in diagnostics
    assert engine_mod._retry_recommended(html, 200, diagnostics) is False


def test_http_js_marker_allows_large_useful_static_content():
    html = '<html><body><div id="__next"></div>' + ("useful docs content " * 180) + "</body></html>"

    retry, reason = http_mod._should_retry_with_browser(html, 200)

    assert retry is False
    assert reason == ""


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


def test_quora_document_decoder_extracts_sections():
    document = {
        "sections": [
            {"spans": [{"text": "First answer paragraph."}]},
            {"spans": [{"text": "Second answer paragraph."}]},
        ]
    }
    raw = __import__("json").dumps(document)
    escaped = raw.replace("\\", "\\\\").replace('"', '\\"')

    lines = source_mod._quora_decode_document(escaped)

    assert lines == ["First answer paragraph.", "Second answer paragraph."]


def test_hackernews_html_extracts_comments():
    raw = """
    <html><head><title>HN Test</title></head><body>
      <tr class="athing comtr"><td></td><td class="default">
        <div><a class="hnuser">pg</a></div>
        <div class="commtext">Interesting research thread.</div>
      </td></tr>
    </body></html>
    """

    html, ok = source_mod.hackernews_html_from_page(raw, "https://news.ycombinator.com/item?id=1")

    assert ok is True
    assert "pg: Interesting research thread." in html


def test_hackernews_item_id_reads_query():
    assert source_mod.hackernews_item_id("https://news.ycombinator.com/item?id=41284574") == "41284574"
    assert source_mod.hackernews_item_id("https://news.ycombinator.com/news") == ""


def test_semantic_scholar_query_uses_paper_slug():
    url = "https://www.semanticscholar.org/paper/Attention-Is-All-You-Need-Vaswani-Shazeer/204e307"

    assert source_mod.semantic_scholar_query_from_url(url) == "Attention Is All You Need Vaswani Shazeer"


def test_pubmed_xml_extracts_article():
    raw = """
    <PubmedArticleSet><PubmedArticle><MedlineCitation>
      <PMID>31452104</PMID>
      <Article>
        <Journal><Title>Example Journal</Title></Journal>
        <ArticleTitle>Research title</ArticleTitle>
        <Abstract><AbstractText>Useful abstract text.</AbstractText></Abstract>
        <AuthorList><Author><ForeName>Ada</ForeName><LastName>Lovelace</LastName></Author></AuthorList>
      </Article>
    </MedlineCitation></PubmedArticle></PubmedArticleSet>
    """

    html, ok = source_mod.pubmed_xml_to_html(raw, "https://pubmed.ncbi.nlm.nih.gov/31452104/")

    assert source_mod.pubmed_id_from_url("https://pubmed.ncbi.nlm.nih.gov/31452104/") == "31452104"
    assert ok is True
    assert "Research title" in html
    assert "Ada Lovelace" in html
    assert "Useful abstract text." in html


def test_crates_json_builds_readable_html():
    data = {
        "crate": {
            "id": "serde",
            "name": "serde",
            "description": "Serialization framework",
            "downloads": 10,
            "repository": "https://github.com/serde-rs/serde",
        },
        "versions": [{"num": "1.0.0", "license": "MIT OR Apache-2.0"}],
    }

    html = source_mod.crates_json_to_html(data, "https://crates.io/crates/serde")

    assert source_mod.crate_name_from_url("https://crates.io/crates/serde") == "serde"
    assert "Serialization framework" in html
    assert "1.0.0" in html


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
async def test_auto_stops_on_protected_403_when_solver_disabled(monkeypatch):
    html = "<html><body>Access Denied. Verify you are human.</body></html>"

    async def fake_quick_scrape(url, fingerprint=None, cookies=None):
        return _fake_result(
            success=True,
            html=html,
            status_code=403,
            reason="HTTP 403",
            final_url=url,
        )

    async def fail_crawler(*args, **kwargs):
        raise AssertionError("crawler should not run after protected 403 capture")

    async def fail_browser(*args, **kwargs):
        raise AssertionError("browser should not run after protected 403 capture")

    async def fake_advanced_waf(url):
        return _fake_result(success=True, html=html, status_code=403, reason="HTTP 403", final_url=url)

    async def fake_ultimate(url):
        return _fake_result(success=True, html=html, status_code=403, reason="HTTP 403", final_url=url)

    monkeypatch.setattr(engine_mod, "quick_scrape", fake_quick_scrape)
    monkeypatch.setattr(engine_mod, "advanced_waf_scrape", fake_advanced_waf)
    monkeypatch.setattr(engine_mod, "scrape_ultimate", fake_ultimate)
    monkeypatch.setattr(engine_mod, "crawler_scrape", fail_crawler)
    monkeypatch.setattr(engine_mod, "stealth_scrape", fail_browser)

    engine = ScraperEngine()
    engine.memory.profiles = {}
    result = await engine._try_auto(
        "https://example.com/page",
        make_identity(),
        time.time(),
        [],
        solve_challenges=False,
    )

    assert result.ok is False
    assert result.best_attempt["layer"] == "layer2_cookie"
    assert result.best_attempt["retry_recommended"] is True
    assert "http_403" in result.diagnostics


@pytest.mark.asyncio
async def test_vuln06_headless_launcher_never_invoked_on_known_blocked_403(monkeypatch):
    """VULN-06 regression test: Assert that when early HTTP layers receive a protected 403,
    the headless browser launcher (stealth_scrape / full_scrape) is NEVER invoked,
    preventing heavy Chromium process spawning on known-blocked challenges.
    """
    from unittest.mock import AsyncMock

    html = "<html><body>Access Denied. Cloudflare Ray ID. Verify you are human.</body></html>"

    async def fake_raw_scrape(url):
        return _fake_result(
            success=False,
            html="",
            status_code=403,
            reason="Forbidden",
            final_url=url,
        )

    async def fake_quick_scrape(url, fingerprint=None, cookies=None):
        return _fake_result(
            success=True,
            html=html,
            status_code=403,
            reason="HTTP 403",
            final_url=url,
        )

    mock_stealth = AsyncMock()
    mock_full = AsyncMock()

    async def fake_advanced_waf(url):
        return _fake_result(success=True, html=html, status_code=403, reason="HTTP 403", final_url=url)

    async def fake_ultimate(url):
        return _fake_result(success=True, html=html, status_code=403, reason="HTTP 403", final_url=url)

    monkeypatch.setattr(engine_mod, "raw_scrape", fake_raw_scrape)
    monkeypatch.setattr(engine_mod, "quick_scrape", fake_quick_scrape)
    monkeypatch.setattr(engine_mod, "advanced_waf_scrape", fake_advanced_waf)
    monkeypatch.setattr(engine_mod, "scrape_ultimate", fake_ultimate)
    monkeypatch.setattr(engine_mod, "stealth_scrape", mock_stealth)
    monkeypatch.setattr(engine_mod, "full_scrape", mock_full)

    engine = ScraperEngine()
    engine.memory.profiles = {}
    result = await engine._try_auto(
        "https://example.com/protected-page",
        make_identity(),
        time.time(),
        [],
        solve_challenges=False,
    )

    # 1. Early layer captured the block
    assert result.ok is False
    assert result.best_attempt["layer"] == "layer2_cookie"
    assert result.best_attempt["retry_recommended"] is True
    assert result.usable is False

    # 2. Crucial VULN-06 assertion: Headless browser launcher was NEVER invoked
    mock_stealth.assert_not_called()
    mock_full.assert_not_called()


@pytest.mark.asyncio
async def test_auto_protected_403_usable_contract(monkeypatch):
    html = "<html><body>Access Denied. Verify you are human.</body></html>"

    async def fake_quick_scrape(url, fingerprint=None, cookies=None):
        return _fake_result(
            success=True,
            html=html,
            status_code=403,
            reason="HTTP 403",
            final_url=url,
        )

    async def fail_crawler(*args, **kwargs):
        raise AssertionError("crawler should not run after protected 403 capture")

    async def fail_browser(*args, **kwargs):
        raise AssertionError("browser should not run after protected 403 capture")

    async def fake_advanced_waf(url):
        return _fake_result(success=True, html=html, status_code=403, reason="HTTP 403", final_url=url)

    async def fake_ultimate(url):
        return _fake_result(success=True, html=html, status_code=403, reason="HTTP 403", final_url=url)

    monkeypatch.setattr(engine_mod, "quick_scrape", fake_quick_scrape)
    monkeypatch.setattr(engine_mod, "advanced_waf_scrape", fake_advanced_waf)
    monkeypatch.setattr(engine_mod, "scrape_ultimate", fake_ultimate)
    monkeypatch.setattr(engine_mod, "crawler_scrape", fail_crawler)
    monkeypatch.setattr(engine_mod, "stealth_scrape", fail_browser)

    engine = ScraperEngine()
    engine.memory.profiles = {}
    result = await engine._try_auto(
        "https://example.com/page",
        make_identity(),
        time.time(),
        [],
        solve_challenges=False,
    )

    assert result.ok is False
    assert result.usable is False
    assert result.to_dict()["usable"] is False


@pytest.mark.asyncio
async def test_auto_normal_200_usable_contract(monkeypatch):
    html = "<html><body>" + ("useful regular content " * 40) + "</body></html>"

    async def fake_quick_scrape(url, fingerprint=None, cookies=None):
        return _fake_result(
            success=True,
            html=html,
            status_code=200,
            reason="OK",
            final_url=url,
        )

    monkeypatch.setattr(engine_mod, "quick_scrape", fake_quick_scrape)

    engine = ScraperEngine()
    engine.memory.profiles = {}
    result = await engine._try_auto(
        "https://example.com/page",
        make_identity(),
        time.time(),
        [],
        solve_challenges=False,
    )

    assert result.ok is True
    assert result.usable is True
    assert result.to_dict()["usable"] is True


@pytest.mark.asyncio
async def test_scrape_response_model_includes_usable():
    from scraper.engine import ScrapeResult
    res_ok = ScrapeResult(
        ok=True,
        url="https://example.com",
        data={"html": "abc"},
        best_attempt={"retry_recommended": False},
    )
    d_ok = res_ok.to_dict()
    assert d_ok["usable"] is True
    model_ok = api_mod.ScrapeResponse(**d_ok)
    assert model_ok.usable is True

    res_blocked = ScrapeResult(
        ok=True,
        url="https://example.com",
        data={"html": "Access Denied"},
        best_attempt={"retry_recommended": True},
    )
    d_blocked = res_blocked.to_dict()
    assert d_blocked["usable"] is False
    model_blocked = api_mod.ScrapeResponse(**d_blocked)
    assert model_blocked.usable is False

    # Assert ScrapeResponse defaults to usable=False when key is omitted
    model_default_fail = api_mod.ScrapeResponse(success=False, url="https://example.com")
    assert model_default_fail.usable is False
    model_default_ok = api_mod.ScrapeResponse(success=True, url="https://example.com")
    assert model_default_ok.usable is False

    # Assert ScrapeResult.usable is a read-only property (no setter)
    with pytest.raises(AttributeError):
        res_ok.usable = False


def test_scrape_endpoint_post_protected_403_usable_contract(monkeypatch):
    from starlette.testclient import TestClient
    from scraper import url_safety

    async def fake_dns(host, port):
        return {"93.184.216.34"}

    monkeypatch.setattr(url_safety, "_resolve_addresses", fake_dns)

    html = "<html><body>Access Denied. Verify you are human.</body></html>"

    async def fake_quick_scrape(url, fingerprint=None, cookies=None):
        return _fake_result(
            success=True,
            html=html,
            status_code=403,
            reason="HTTP 403",
            final_url=url,
        )

    async def fail_crawler(*args, **kwargs):
        raise AssertionError("crawler should not run after protected 403 capture")

    async def fail_browser(*args, **kwargs):
        raise AssertionError("browser should not run after protected 403 capture")

    async def fake_advanced_waf(url):
        return _fake_result(success=True, html=html, status_code=403, reason="HTTP 403", final_url=url)

    async def fake_ultimate(url):
        return _fake_result(success=True, html=html, status_code=403, reason="HTTP 403", final_url=url)

    monkeypatch.setattr(engine_mod, "quick_scrape", fake_quick_scrape)
    monkeypatch.setattr(engine_mod, "advanced_waf_scrape", fake_advanced_waf)
    monkeypatch.setattr(engine_mod, "scrape_ultimate", fake_ultimate)
    monkeypatch.setattr(engine_mod, "crawler_scrape", fail_crawler)
    monkeypatch.setattr(engine_mod, "stealth_scrape", fail_browser)
    monkeypatch.setattr(api_mod.engine.memory, "profiles", {})

    client = TestClient(api_mod.app)

    # Test POST /scrape with JSON body
    response = client.post("/scrape", json={"url": "https://example.com/page"})
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is False
    assert data["usable"] is False

    # Test POST /scrape with query parameter
    response_query = client.post("/scrape", params={"url": "https://example.com/page"})
    assert response_query.status_code == 200
    data_query = response_query.json()
    assert data_query["success"] is False
    assert data_query["usable"] is False


def test_scrape_endpoint_post_normal_200_usable_contract(monkeypatch):
    from starlette.testclient import TestClient
    from scraper import url_safety

    async def fake_dns(host, port):
        return {"93.184.216.34"}

    monkeypatch.setattr(url_safety, "_resolve_addresses", fake_dns)

    html = "<html><body>" + ("useful regular content " * 40) + "</body></html>"

    async def fake_quick_scrape(url, fingerprint=None, cookies=None):
        return _fake_result(
            success=True,
            html=html,
            status_code=200,
            reason="OK",
            final_url=url,
        )

    monkeypatch.setattr(engine_mod, "quick_scrape", fake_quick_scrape)
    monkeypatch.setattr(api_mod.engine.memory, "profiles", {})

    client = TestClient(api_mod.app)
    response = client.post("/scrape", json={"url": "https://example.com/page"})
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["usable"] is True

    # Test POST /scrape/bulk positive control
    response_bulk = client.post("/scrape/bulk", json={"urls": ["https://example.com/page"]})
    assert response_bulk.status_code == 200
    data_bulk = response_bulk.json()
    assert data_bulk["succeeded"] == 1
    assert data_bulk["results"][0]["usable"] is True



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

        async def scrape(self, url, mode="auto", allow_layer5=True, solve_challenges=True, skip_source=False):
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


@pytest.mark.asyncio
async def test_bulk_cookie_fanout_fallback_honors_allow_browser_false(monkeypatch):
    class FakeMemory:
        def get_valid_cookies(self, url):
            return [{"name": "sid", "value": "1"}]

        async def clear_cookies(self, url):
            return None

    class FakeEngine:
        def __init__(self):
            self.memory = FakeMemory()
            self.seen_allow_browser = []

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

        async def scrape(
            self,
            url,
            mode="auto",
            allow_layer5=True,
            allow_browser=True,
            solve_challenges=True,
            skip_source=False,
        ):
            self.seen_allow_browser.append(allow_browser)
            return SimpleNamespace(
                ok=True,
                to_dict=lambda: {
                    "success": True,
                    "layer_used": "layer1_simple",
                    "best_attempt": {"retry_recommended": False},
                },
            )

    original_engine = api_mod.engine
    fake_engine = FakeEngine()
    api_mod.engine = fake_engine
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
                allow_browser=False,
            )
        )
    finally:
        api_mod.engine = original_engine

    assert result.succeeded == 2
    assert result.results[1]["bulk_role"] == "cookie_fanout_fallback_auto"
    assert fake_engine.seen_allow_browser == [False, False]


@pytest.mark.asyncio
async def test_bulk_http_only_source_runs_when_browser_disabled(monkeypatch):
    class FakeMemory:
        def get_valid_cookies(self, url):
            return []

    class FakeEngine:
        def __init__(self):
            self.memory = FakeMemory()
            self.urls = []

        async def scrape_source_bulk(self, urls):
            self.urls = list(urls)
            return [
                SimpleNamespace(
                    to_dict=lambda url=url: {
                        "success": True,
                        "url": url,
                        "layer_used": "source_walmart_bulk",
                        "best_attempt": {"retry_recommended": False},
                    }
                )
                for url in urls
            ]

        async def scrape(self, url, mode="auto", allow_layer5=True, allow_browser=True, **kwargs):
            return SimpleNamespace(
                ok=True,
                to_dict=lambda: {
                    "success": True,
                    "url": url,
                    "layer_used": "layer1_simple",
                    "best_attempt": {"retry_recommended": False},
                },
            )

    original_engine = api_mod.engine
    fake_engine = FakeEngine()
    api_mod.engine = fake_engine
    try:
        result = await api_mod.do_bulk(
            api_mod.BulkRequest(
                urls=[
                    "https://www.walmart.com/ip/one",
                    "https://www.walmart.com/ip/two",
                ],
                mode=api_mod.ScrapeMode.auto,
                max_concurrency=2,
                allow_browser=False,
            )
        )
    finally:
        api_mod.engine = original_engine

    assert fake_engine.urls == [
        "https://www.walmart.com/ip/one",
        "https://www.walmart.com/ip/two",
    ]
    assert result.succeeded == 2
    assert result.results[0]["bulk_role"] == "source_walmart_bulk"


@pytest.mark.asyncio
async def test_bulk_uses_source_bulk_hook(monkeypatch):
    class FakeMemory:
        def get_valid_cookies(self, url):
            return []

    class FakeEngine:
        def __init__(self):
            self.memory = FakeMemory()
            self.urls = []

        async def scrape_source_bulk(self, urls):
            self.urls = urls
            return [
                SimpleNamespace(
                    to_dict=lambda url=url: {
                        "success": True,
                        "url": url,
                        "layer_used": "source_bestbuy",
                        "best_attempt": {"retry_recommended": False},
                    }
                )
                for url in urls
            ]

    fake_engine = FakeEngine()
    original_engine = api_mod.engine
    api_mod.engine = fake_engine
    try:
        result = await api_mod.do_bulk(
            api_mod.BulkRequest(
                urls=[
                    "https://www.bestbuy.com/site/searchpage.jsp?st=laptop",
                    "https://www.bestbuy.com/site/searchpage.jsp?st=monitor",
                ],
                mode=api_mod.ScrapeMode.auto,
                max_concurrency=1,
                same_domain_fanout=1,
            )
        )
    finally:
        api_mod.engine = original_engine

    assert fake_engine.urls == [
        "https://www.bestbuy.com/site/searchpage.jsp?st=laptop",
        "https://www.bestbuy.com/site/searchpage.jsp?st=monitor",
    ]
    assert result.succeeded == 2
    assert result.results[0]["bulk_role"] == "source_bestbuy_bulk"
    assert result.results[1]["bulk_role"] == "source_bestbuy_bulk"


@pytest.mark.asyncio
async def test_reddit_bulk_keeps_source_result_instead_of_slow_fallback(monkeypatch):
    class FakeMemory:
        def get_valid_cookies(self, url):
            return []

    class FakeEngine:
        def __init__(self):
            self.memory = FakeMemory()
            self.scrape_calls = []

        async def scrape_source_bulk(self, urls):
            return [
                SimpleNamespace(
                    to_dict=lambda: {
                        "success": False,
                        "url": urls[0],
                        "content": {"html": "<html>blocked</html>"},
                        "layer_used": "source_reddit",
                        "best_attempt": {"retry_recommended": True},
                        "error": "reddit json miss",
                    }
                )
            ]

        async def scrape(self, *args, **kwargs):
            self.scrape_calls.append((args, kwargs))
            return SimpleNamespace(
                ok=True,
                to_dict=lambda: {
                    "success": True,
                    "layer_used": "layer4_stealth",
                    "best_attempt": {"retry_recommended": False},
                },
            )

    fake_engine = FakeEngine()
    original_engine = api_mod.engine
    api_mod.engine = fake_engine
    try:
        result = await api_mod.do_bulk(
            api_mod.BulkRequest(
                urls=["https://www.reddit.com/r/webscraping/new/"],
                mode=api_mod.ScrapeMode.auto,
                max_concurrency=1,
                same_domain_fanout=1,
            )
        )
    finally:
        api_mod.engine = original_engine

    assert result.failed == 1
    assert result.results[0]["bulk_role"] == "source_reddit_bulk"
    assert result.results[0]["layer_used"] == "source_reddit"
    assert fake_engine.scrape_calls == []


@pytest.mark.asyncio
async def test_bulk_non_source_warmup_uses_auto_before_browser(monkeypatch):
    async def allow_fixture_url(url):
        return url

    monkeypatch.setattr(api_mod, "normalize_public_url", allow_fixture_url)

    class FakeMemory:
        def get_valid_cookies(self, url):
            return []

    class FakeEngine:
        def __init__(self):
            self.memory = FakeMemory()
            self.scrape_calls = []
            self.warmup_calls = []

        async def scrape(self, url, mode="auto", allow_layer5=True, solve_challenges=True, skip_source=False):
            self.scrape_calls.append({
                "url": url,
                "mode": mode,
                "allow_layer5": allow_layer5,
                "solve_challenges": solve_challenges,
                "skip_source": skip_source,
            })
            return SimpleNamespace(
                to_dict=lambda: {
                    "success": True,
                    "url": url,
                    "layer_used": "layer2_cookie",
                    "best_attempt": {"retry_recommended": False},
                }
            )

        async def warmup_layer4(self, url, solve_challenges=True):
            self.warmup_calls.append(url)
            return SimpleNamespace(
                to_dict=lambda: {
                    "success": True,
                    "url": url,
                    "layer_used": "layer4_stealth",
                    "best_attempt": {"retry_recommended": False},
                }
            )

        async def scrape_layer2_after_warmup(self, url):
            return SimpleNamespace(
                ok=True,
                diagnostics=[],
                best_attempt={"retry_recommended": False},
                to_dict=lambda: {
                    "success": True,
                    "url": url,
                    "layer_used": "layer2_cookie",
                    "best_attempt": {"retry_recommended": False},
                },
            )

    fake_engine = FakeEngine()
    original_engine = api_mod.engine
    api_mod.engine = fake_engine
    try:
        result = await api_mod.do_bulk(
            api_mod.BulkRequest(
                urls=[
                    "https://example.invalid/one",
                    "https://example.invalid/two",
                ],
                mode=api_mod.ScrapeMode.auto,
                max_concurrency=1,
                same_domain_fanout=1,
            )
        )
    finally:
        api_mod.engine = original_engine

    assert result.succeeded == 2
    assert fake_engine.warmup_calls == []
    assert fake_engine.scrape_calls[0]["url"] == "https://example.invalid/one"
    assert result.results[0]["bulk_role"] == "domain_auto_warmup"
    assert result.results[1]["bulk_role"] == "cookie_fanout"


@pytest.mark.asyncio
async def test_scrape_api_auto_disables_layer5_by_default(monkeypatch):
    calls = []

    class FakeEngine:
        async def scrape(self, url, mode="auto", allow_layer5=True):
            calls.append({
                "url": url,
                "mode": mode,
                "allow_layer5": allow_layer5,
            })
            return SimpleNamespace(
                to_dict=lambda: {
                    "success": True,
                    "url": url,
                    "layer_used": "source_medium",
                }
            )

    original_engine = api_mod.engine
    api_mod.engine = FakeEngine()
    try:
        await api_mod.do_scrape(
            url="example.com",
            mode=api_mod.ScrapeMode.auto,
            allow_layer5=False,
            timeout_seconds=120.0,
        )
        await api_mod.do_scrape(
            url="example.com",
            mode=api_mod.ScrapeMode.auto,
            allow_layer5=True,
            timeout_seconds=120.0,
        )
    finally:
        api_mod.engine = original_engine

    assert calls[0] == {
        "url": "https://example.com",
        "mode": "auto",
        "allow_layer5": False,
    }
    assert calls[1]["allow_layer5"] is True


@pytest.mark.asyncio
async def test_health_reports_hard_source_status(monkeypatch):
    monkeypatch.delenv("YADA_HARD_SOURCES", raising=False)
    monkeypatch.delenv("YADA_EXPERIMENTAL_HARD_SOURCES", raising=False)
    monkeypatch.delenv("YADA_BROWSER_SOURCE_CONCURRENCY", raising=False)

    result = await api_mod.health()

    hard_sources = result["hard_sources"]
    assert hard_sources["enabled"] is True
    assert hard_sources["browser_source_concurrency"] == 2
    assert "walmart" not in hard_sources["stable"]
    assert "walmart" in hard_sources["best_effort"]
    assert "belk" not in hard_sources["stable"]
    assert "belk" in hard_sources["conditional"]
    assert "vinted" not in hard_sources["stable"]
    assert hard_sources["stable"]["bloomberg"] == "PerimeterX/HUMAN"
    assert hard_sources["stable"]["tripadvisor"] == "DataDome"
    assert hard_sources["experimental_enabled"] is False
    assert hard_sources["experimental"] == {}


def test_http_attempt_timeout_env_override(monkeypatch):
    monkeypatch.delenv("YADA_HTTP_ATTEMPT_TIMEOUT", raising=False)
    monkeypatch.delenv("YADA_PROXY_TIMEOUT_BONUS", raising=False)
    assert engine_mod._http_attempt_timeout() == 10.0

    monkeypatch.setenv("YADA_HTTP_ATTEMPT_TIMEOUT", "15")
    assert engine_mod._http_attempt_timeout() == 15.0

    for bad in ("", "   ", "banana", "0", "-3", "9999", "inf", "nan"):
        monkeypatch.setenv("YADA_HTTP_ATTEMPT_TIMEOUT", bad)
        assert engine_mod._http_attempt_timeout() == 10.0, bad


def test_bounded_http_budget_is_resolved_at_call_time(monkeypatch):
    seen = {}

    async def fake_wait_for(awaitable, timeout=None):
        seen["timeout"] = timeout
        await awaitable
        return "ok"

    async def noop():
        return None

    monkeypatch.setattr(engine_mod.asyncio, "wait_for", fake_wait_for)
    for key in ("YADA_HTTP_ATTEMPT_TIMEOUT", "YADA_PROXY", "YADA_PROXY_OFF",
                "YADA_PROXY_TIMEOUT_BONUS", "HTTPS_PROXY", "HTTP_PROXY"):
        monkeypatch.delenv(key, raising=False)

    asyncio.run(engine_mod._bounded_http(noop(), "https://example.com", "Probe"))
    assert seen["timeout"] == 10.0

    monkeypatch.setenv("YADA_PROXY", "http://1.2.3.4:8080")
    asyncio.run(engine_mod._bounded_http(noop(), "https://example.com", "Probe"))
    assert seen["timeout"] == 20.0

    monkeypatch.setenv("YADA_PROXY_TIMEOUT_BONUS", "4")
    asyncio.run(engine_mod._bounded_http(noop(), "https://example.com", "Probe"))
    assert seen["timeout"] == 14.0

    asyncio.run(engine_mod._bounded_http(noop(), "https://example.com", "Probe", timeout=4.0))
    assert seen["timeout"] == 4.0


def test_scrape_budget_env_override(monkeypatch):
    monkeypatch.delenv("YADA_SCRAPE_BUDGET", raising=False)
    assert engine_mod._scrape_budget() == 105.0

    monkeypatch.setenv("YADA_SCRAPE_BUDGET", "60")
    assert engine_mod._scrape_budget() == 60.0

    for bad in ("", "   ", "banana", "5", "9999", "inf", "nan"):
        monkeypatch.setenv("YADA_SCRAPE_BUDGET", bad)
        assert engine_mod._scrape_budget() == 105.0, bad


@pytest.mark.asyncio
async def test_budget_exhaustion_stops_ladder_and_returns_best_capture(monkeypatch):
    # Layer 1 fails slowly but leaves a captured body; the 2.0s budget is
    # spent by the end of layer 2, so waf/layer6/crawlers/stealth/full
    # must never start, and the captured body must be returned honestly.
    async def fake_raw(url, **kw):
        await asyncio.sleep(0.6)
        return _fake_result(
            success=False,
            html="<html><body>" + ("useful content " * 500) + "</body></html>",
            status_code=200,
            reason="Raw request failed: TestError",
            response_time=0.6,
            final_url=url,
        )

    async def fake_quick(url, **kw):
        await asyncio.sleep(0.6)
        return _fake_result(
            success=False,
            html="",
            status_code=0,
            reason="Static request failed: TestError",
            response_time=0.6,
            final_url=url,
        )

    async def must_not_run(*a, **kw):
        pytest.fail("stage must not start once the budget is nearly spent")

    monkeypatch.setattr(engine_mod, "_scrape_budget", lambda: 2.0)
    monkeypatch.setattr(engine_mod, "raw_scrape", fake_raw)
    monkeypatch.setattr(engine_mod, "quick_scrape", fake_quick)
    monkeypatch.setattr(engine_mod, "advanced_waf_scrape", must_not_run)
    monkeypatch.setattr(engine_mod, "scrape_ultimate", must_not_run)
    monkeypatch.setattr(engine_mod, "crawler_scrape", must_not_run)
    monkeypatch.setattr(engine_mod, "stealth_scrape", must_not_run)
    monkeypatch.setattr(engine_mod, "full_scrape", must_not_run)

    engine = ScraperEngine()
    engine.memory.profiles = {}
    result = await engine._try_auto("https://example.com/page", make_identity(), time.time(), [])

    layers = [t["layer"] for t in result.tries]
    assert layers == ["layer1_simple", "layer2_cookie"]
    assert result.winning_layer == "layer1_simple"
    assert result.ok is True
    assert "useful content" in result.data["html"]


@pytest.mark.asyncio
async def test_browser_admission_slot_timeout_fails_fast(monkeypatch):
    from scraper.browser_admission import BrowserAdmission

    admission = BrowserAdmission(limit=1)
    await admission.acquire()  # occupy the only slot
    monkeypatch.setattr(engine_mod, "browser_admission", admission)

    async def fake_browser(url, **kw):
        pytest.fail("browser call must not run when no slot is free")

    started = time.time()
    res = await engine_mod.ScraperEngine._admitted_browser_call(
        fake_browser,
        "https://example.com/x",
        deadline=time.time() + 30,
        slot_wait=0.05,
    )
    assert res.success is False
    assert "admission" in res.reason.lower()
    assert time.time() - started < 2.0
    admission.release()


@pytest.mark.asyncio
async def test_direct_layer1c_mode_dispatches_to_advanced_waf(monkeypatch):
    async def fake_advanced_waf(url, **kwargs):
        return _fake_result(
            success=True,
            html="<html><body>layer1c content " + "x" * 1200 + "</body></html>",
            status_code=200,
        )

    monkeypatch.setattr(engine_mod, "advanced_waf_scrape", fake_advanced_waf)
    engine = ScraperEngine()
    result = await engine.scrape("https://example.com/page", mode="layer1c")

    assert result.ok is True
    assert result.winning_layer == "layer1c_advanced_waf"


@pytest.mark.asyncio
async def test_direct_layer6_mode_dispatches_to_ultimate_tls(monkeypatch):
    async def fake_ultimate(url, **kwargs):
        return _fake_result(
            success=True,
            html="<html><body>layer6 content " + "x" * 1200 + "</body></html>",
            status_code=200,
        )

    monkeypatch.setattr(engine_mod, "scrape_ultimate", fake_ultimate)
    engine = ScraperEngine()
    result = await engine.scrape("https://example.com/page", mode="layer6")

    assert result.ok is True
    assert result.winning_layer == "layer6_ultimate"


def test_layer1c_and_layer6_modes_are_in_api_enum():
    assert api_mod.ScrapeMode.layer1c.value == "layer1c"
    assert api_mod.ScrapeMode.layer6.value == "layer6"
