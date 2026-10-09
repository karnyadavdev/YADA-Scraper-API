"""Quora topic pages must decode from SSR payloads (no browser needed)."""
import json

from scraper import source_fast_paths as s


def _topic_html(with_payload=True, followers=76485245):
    parts = [
        "<html><head><title>History - Quora</title></head><body>",
        "<div>Something went wrong. Please enable Javascript and refresh the page to continue.</div>",
    ]
    if with_payload:
        payload = {
            "data": {
                "topic": {
                    "__typename": "Topic",
                    "name": "History",
                    "url": "/topic/History",
                    "tid": 930,
                    "numFollowers": followers,
                    "mostViewedAuthors": [
                        {
                            "user": {
                                "names": [
                                    {"givenName": "Matthew", "familyName": "Anthony"},
                                    {"__typename": "UserName"},
                                ]
                            },
                            "numViews": 1134131,
                            "numPublicMostViewedAnswers": 35,
                        },
                        {
                            "user": {"names": [{"givenName": "Jim", "familyName": "Morrison"}]},
                            "numViews": 893401,
                            "numPublicMostViewedAnswers": 3736,
                        },
                    ],
                    "relatedTopics": [{"name": "World History"}],
                },
                "viewer": {"id": "Vmlld2VyQDA6MA=="},
            }
        }
        escaped = json.dumps(json.dumps(payload))[1:-1]
        parts.append(
            '<script>window.ansFrontendGlobals.data.inlineQueryResults'
            f'.results["{"ab" * 32}"].push("{escaped}");</script>'
        )
    parts.append("</body></html>")
    return "".join(parts)


def test_topic_payload_parses():
    topic = s._quora_topic_payload(_topic_html())
    assert topic is not None
    assert topic["name"] == "History"
    assert topic["numFollowers"] == 76485245


def test_topic_payload_absent_on_shell():
    assert s._quora_topic_payload(_topic_html(with_payload=False)) is None


def test_topic_static_html_passes_without_browser():
    html, ok, reason, _shell = s._quora_static_html(
        _topic_html(), "https://www.quora.com/topic/History"
    )
    assert ok is True
    assert reason == "Quora static topic payload source path"
    assert "History" in html
    assert "76,485,245" in html
    assert "Matthew Anthony" in html
    assert "3,736 top answers" in html
    assert "World History" in html


def test_topic_shell_without_payload_stays_thin():
    html, ok, reason, _shell = s._quora_static_html(
        _topic_html(with_payload=False), "https://www.quora.com/topic/History"
    )
    assert ok is False
    assert reason == "Quora static source path returned thin content"


def test_static_retries_once_after_challenge(monkeypatch):
    import asyncio

    calls = {"n": 0}

    class FakeResponse:
        status_code = 403
        url = "https://www.quora.com/topic/History"

    async def fake_fetch(url, timeout=15.0):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(), "<html><body>Just a moment...</body></html>"
        resp = FakeResponse()
        resp.status_code = 200
        return resp, _topic_html()

    monkeypatch.setattr(s, "_fetch_public_html", fake_fetch)
    result = asyncio.run(s.scrape_quora_static("https://www.quora.com/topic/History"))

    assert result.success is True
    assert result.status_code == 200
    assert calls["n"] == 2
    assert result.reason == "Quora static topic payload source path"


def test_source_scrape_routes_quora_static_when_disabled(monkeypatch):
    import asyncio

    seen = {}

    async def fake_static(url, timeout=15.0):
        seen["static"] = url
        return s.SourceResult(True, html="<html>ok</html>", status_code=200)

    async def fake_full(url, timeout=35.0):
        seen["full"] = url
        return s.SourceResult(True, html="<html>ok</html>", status_code=200)

    monkeypatch.setattr(s, "scrape_quora_static", fake_static)
    monkeypatch.setattr(s, "scrape_quora", fake_full)

    asyncio.run(s.source_scrape("https://www.quora.com/topic/History", allow_browser=False))
    assert seen == {"static": "https://www.quora.com/topic/History"}

    seen.clear()
    asyncio.run(s.source_scrape("https://www.quora.com/topic/History", allow_browser=True))
    assert seen == {"full": "https://www.quora.com/topic/History"}


def test_source_bulk_scrape_routes_quora_static_when_disabled(monkeypatch):
    import asyncio

    seen = {}
    urls = ["https://www.quora.com/topic/History", "https://www.quora.com/topic/Machine-Learning"]

    async def fake_static_bulk(urls, timeout=15.0):
        seen["static_bulk"] = list(urls)
        return [s.SourceResult(True, html="<html>ok</html>", status_code=200) for _ in urls]

    async def fake_full_bulk(urls, timeout=45.0):
        seen["full_bulk"] = list(urls)
        return [s.SourceResult(True, html="<html>ok</html>", status_code=200) for _ in urls]

    monkeypatch.setattr(s, "scrape_quora_static_bulk", fake_static_bulk)
    monkeypatch.setattr(s, "scrape_quora_bulk", fake_full_bulk)

    asyncio.run(s.source_bulk_scrape(urls, allow_browser=False))
    assert seen == {"static_bulk": urls}

    seen.clear()
    asyncio.run(s.source_bulk_scrape(urls, allow_browser=True))
    assert seen == {"full_bulk": urls}


def test_engine_auto_runs_quora_source_with_browser_disabled(monkeypatch):
    import asyncio

    import scraper.engine as engine_mod
    from scraper.engine import ScraperEngine

    seen = {}

    async def fake_source_scrape(
        url,
        cookies=None,
        structured=False,
        solve_challenges=False,
        memory=None,
        allow_browser=True,
    ):
        seen["allow_browser"] = allow_browser
        return s.SourceResult(
            True,
            html="<html><body>topic content</body></html>",
            status_code=200,
            reason="Quora static topic payload source path",
            response_time=0.01,
            final_url=url,
        )

    monkeypatch.setattr(engine_mod, "source_scrape", fake_source_scrape)
    eng = ScraperEngine()
    monkeypatch.setattr(eng.memory, "get_failed_layers", lambda url: set())

    result = asyncio.run(
        eng.scrape("https://www.quora.com/topic/History", mode="auto", allow_browser=False)
    )
    assert seen.get("allow_browser") is False
    assert result.to_dict()["layer_used"] == "source_quora"


def test_bulk_api_runs_quora_static_bulk_when_browser_disabled(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import yada_scraper as api

    class FakeMemory:
        def get_valid_cookies(self, url):
            return []

    class FakeEngine:
        def __init__(self):
            self.memory = FakeMemory()
            self.seen = []

        async def scrape_source_bulk(self, urls, allow_browser=True):
            self.seen.append(allow_browser)
            return [
                SimpleNamespace(
                    to_dict=lambda url=url: {
                        "success": True,
                        "url": url,
                        "layer_used": "source_quora",
                        "best_attempt": {"retry_recommended": False},
                    }
                )
                for url in urls
            ]

    fake = FakeEngine()
    original_engine = api.engine
    api.engine = fake
    try:
        result = asyncio.run(
            api.do_bulk(
                api.BulkRequest(
                    urls=[
                        "https://www.quora.com/topic/History",
                        "https://www.quora.com/topic/Machine-Learning",
                    ],
                    mode=api.ScrapeMode.auto,
                    max_concurrency=2,
                    allow_browser=False,
                )
            )
        )
    finally:
        api.engine = original_engine

    assert fake.seen == [False]
    assert result.succeeded == 2
    assert result.results[0]["bulk_role"] == "source_quora_bulk"
