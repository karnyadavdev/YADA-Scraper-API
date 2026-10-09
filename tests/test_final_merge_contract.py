import asyncio
import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
import yada_scraper as api_mod
from fastapi import HTTPException

from scraper import engine as engine_mod
from scraper import source_fast_paths as source_mod
from scraper.site_memory import SiteMemory
from scraper.subprocess_utils import run_subprocess
from scraper.browser_binary import find_chromium_binary


def test_superseded_named_routes_use_generic_receiver():
    assert source_mod.source_name("https://www.vinted.com/catalog?search_text=nike") is None
    assert source_mod.source_name("https://www.leboncoin.fr/recherche?text=iphone") is None
    assert source_mod.source_name("https://www.yelp.com/search?find_desc=Restaurants") is None
    assert source_mod.source_name("https://pytorch.org/docs/stable/index.html") is None
    assert source_mod.source_name("https://meta.discourse.org/t/a-topic/123") is None
    assert source_mod.source_name("https://mastodon.social/@openai") is None


@pytest.mark.asyncio
async def test_approximate_and_optional_research_bulk_are_not_auto_defaults():
    assert await source_mod.source_bulk_scrape(
        ["https://www.semanticscholar.org/paper/test"]
    ) is None
    assert await source_mod.source_bulk_scrape(
        ["https://github.com/psf/requests"]
    ) is None


def test_remotive_filter_respects_requested_category_and_job():
    jobs = [
        {
            "category": "Software Development",
            "url": "https://remotive.com/remote-jobs/software-development/python-engineer-123",
        },
        {
            "category": "Sales",
            "url": "https://remotive.com/remote-jobs/sales/account-executive-456",
        },
    ]
    data = {"jobs": jobs}

    category = source_mod._remotive_jobs_for_url(
        data, "https://remotive.com/remote-jobs/software-dev"
    )
    exact_job = source_mod._remotive_jobs_for_url(
        data,
        "https://remotive.com/remote-jobs/software-development/python-engineer-123",
    )

    assert category == [jobs[0]]
    assert exact_job == [jobs[0]]


def test_clinicaltrials_exact_url_mapping_extracts_nct_id():
    assert (
        source_mod.clinicaltrials_nct_id_from_url(
            "https://clinicaltrials.gov/study/nct04280705"
        )
        == "NCT04280705"
    )
    assert source_mod.clinicaltrials_nct_id_from_url("https://clinicaltrials.gov/search") == ""


@pytest.mark.asyncio
async def test_downloads_single_coverage_routes_remain_reachable(monkeypatch):
    calls = []

    async def fake_youtube(url):
        calls.append(("youtube", url))
        return object()

    async def fake_reddit(url, cookies=None):
        calls.append(("reddit", url, cookies))
        return object()

    monkeypatch.setattr(source_mod, "scrape_youtube", fake_youtube)
    monkeypatch.setattr(source_mod, "scrape_reddit", fake_reddit)

    await source_mod.source_scrape("https://www.youtube.com/watch?v=abc")
    await source_mod.source_scrape(
        "https://www.reddit.com/r/python/comments/abc/post/",
        cookies=[{"name": "reddit_session", "value": "1"}],
    )

    assert calls[0][0] == "youtube"
    assert calls[1][0] == "reddit"
    assert calls[1][2][0]["name"] == "reddit_session"


@pytest.mark.asyncio
async def test_downloads_instagram_bulk_is_registered(monkeypatch):
    sentinel = [object()]

    async def fake_instagram_bulk(urls):
        return sentinel

    monkeypatch.setattr(
        source_mod,
        "scrape_instagram_bulk",
        fake_instagram_bulk,
    )
    result = await source_mod.source_bulk_scrape(
        ["https://www.instagram.com/openai/"]
    )

    assert result is sentinel


def test_mobile_auto_is_selective_and_remains_explicitly_available():
    assert engine_mod._mobile_auto_enabled("https://www.indeed.com/jobs?q=python")
    assert not engine_mod._mobile_auto_enabled("https://example.com/")


def test_raw_http_skip_is_limited_to_named_hard_targets():
    scraper = engine_mod.ScraperEngine()
    assert not scraper._should_skip_layer1("https://example.com/")
    assert scraper._should_skip_layer1("https://www.target.com/s?searchTerm=shoes")


def test_unrelated_dd_js_name_is_not_labeled_datadome():
    html = '<html><body><script src="/assets/dd.js"></script>Useful page</body></html>'
    diagnostics = engine_mod._diagnostics_for_capture(html, 200, "")
    assert "captcha_marker_seen:datadome" not in diagnostics


def test_xml_capture_uses_xml_text_parser_without_losing_content():
    xml = '<?xml version="1.0"?><Error><Code>AccessDenied</Code></Error>'
    assert "AccessDenied" in engine_mod._text_from_html(xml)


def test_chromium_binary_honors_portable_environment_override(monkeypatch, tmp_path):
    binary = tmp_path / "chromium"
    binary.write_bytes(b"")
    monkeypatch.setenv("YADA_BROWSER_EXECUTABLE", str(binary))
    assert find_chromium_binary() == str(binary)


@pytest.mark.asyncio
async def test_async_subprocess_timeout_kills_promptly():
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        await run_subprocess(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.2,
        )
    assert time.monotonic() - started < 3


@pytest.mark.asyncio
async def test_async_subprocess_outer_cancellation_cleans_promptly():
    task = asyncio.create_task(
        run_subprocess(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=30,
        )
    )
    await asyncio.sleep(0.2)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 3


@pytest.mark.asyncio
async def test_structured_mode_opts_into_compact_single_source(monkeypatch, tmp_path):
    structured_seen = False

    async def fake_source(url, cookies=None, structured=False):
        nonlocal structured_seen
        structured_seen = structured
        return SimpleNamespace(
            success=True,
            html="<html><body>compact structured record</body></html>",
            status_code=200,
            reason="structured",
            response_time=0.01,
            final_url=url,
            cookies=[],
        )

    monkeypatch.setattr(engine_mod, "source_name", lambda url: "pubmed")
    monkeypatch.setattr(engine_mod, "source_scrape", fake_source)

    scraper = engine_mod.ScraperEngine()
    scraper.memory = SiteMemory(str(tmp_path / "memory.json"))
    result = await scraper.scrape("https://pubmed.ncbi.nlm.nih.gov/123/", mode="structured")

    assert structured_seen
    assert result.ok
    assert result.winning_layer == "source_pubmed"


@pytest.mark.asyncio
async def test_site_memory_deferred_flush_stays_atomic_and_refreshes_layer2(tmp_path):
    path = tmp_path / "memory.json"
    memory = SiteMemory(str(path))
    memory.defer_writes = True

    await memory.record_failure("https://www.example.com/page", "layer2_cookie", "blocked")
    await memory.save_cookies(
        "https://www.example.com/page",
        [{"name": "sid", "value": "1", "expires": -1}],
    )

    assert not path.exists()
    assert "layer2_cookie" not in memory.get_failed_layers("https://example.com/page")

    memory.defer_writes = False
    await memory.flush()

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "example.com" in saved
    assert saved["example.com"]["cookies"][0]["name"] == "sid"


@pytest.mark.asyncio
async def test_site_memory_accepts_relative_custom_path(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    memory = SiteMemory("memory.json")
    await memory.record_success("https://example.com", "layer1_simple", 0.1)

    saved = json.loads((tmp_path / "memory.json").read_text(encoding="utf-8"))
    assert saved["example.com"]["best_layer"] == "layer1_simple"


@pytest.mark.asyncio
async def test_bulk_source_exception_falls_back_per_item(monkeypatch, tmp_path):
    class FakeMemory:
        defer_writes = False

        def get_valid_cookies(self, url):
            return []

        async def flush(self):
            return None

    class FakeEngine:
        def __init__(self):
            self.memory = FakeMemory()
            self.fallback_urls = []

        async def scrape_source_bulk(self, urls):
            raise RuntimeError("source failed")

        async def scrape(self, url, skip_source=False, **kwargs):
            self.fallback_urls.append((url, skip_source))
            return SimpleNamespace(to_dict=lambda: {
                "success": True,
                "url": url,
                "layer_used": "layer1_simple",
                "best_attempt": {"retry_recommended": False},
            })

    async def allow_fixture_url(url):
        return url

    original_engine = api_mod.engine
    monkeypatch.setattr(api_mod, "source_name", lambda url: "crates")
    monkeypatch.setattr(api_mod, "normalize_public_url", allow_fixture_url)
    fake_engine = FakeEngine()
    api_mod.engine = fake_engine
    try:
        result = await api_mod.do_bulk(
            api_mod.BulkRequest(
                urls=[
                    "https://crates.io/crates/serde",
                    "https://crates.io/crates/tokio",
                ],
                mode=api_mod.ScrapeMode.auto,
                timeout_seconds=5,
            )
        )
    finally:
        api_mod.engine = original_engine

    assert result.succeeded == 2
    assert result.failed == 0
    assert all(item["layer_used"] == "layer1_simple" for item in result.results)
    assert all(item["bulk_role"] == "source_crates_fallback_auto" for item in result.results)
    assert fake_engine.fallback_urls == [
        ("https://crates.io/crates/serde", True),
        ("https://crates.io/crates/tokio", True),
    ]


@pytest.mark.asyncio
async def test_short_source_bulk_result_falls_back_only_missing_item(monkeypatch):
    class FakeMemory:
        defer_writes = False

        def get_valid_cookies(self, url):
            return []

        async def flush(self):
            return None

    def result(url, layer):
        return SimpleNamespace(to_dict=lambda: {
            "success": True,
            "url": url,
            "layer_used": layer,
            "best_attempt": {"retry_recommended": False},
        })

    class FakeEngine:
        def __init__(self):
            self.memory = FakeMemory()
            self.fallback_urls = []

        async def scrape_source_bulk(self, urls):
            return [result(urls[0], "source_crates")]

        async def scrape(self, url, skip_source=False, **kwargs):
            self.fallback_urls.append((url, skip_source))
            return result(url, "layer1_simple")

    async def allow_fixture_url(url):
        return url

    fake_engine = FakeEngine()
    original_engine = api_mod.engine
    monkeypatch.setattr(api_mod, "source_name", lambda url: "crates")
    monkeypatch.setattr(api_mod, "normalize_public_url", allow_fixture_url)
    api_mod.engine = fake_engine
    try:
        response = await api_mod.do_bulk(api_mod.BulkRequest(urls=[
            "https://crates.io/crates/serde",
            "https://crates.io/crates/tokio",
        ]))
    finally:
        api_mod.engine = original_engine

    assert response.succeeded == 2
    assert response.results[0]["bulk_role"] == "source_crates_bulk"
    assert response.results[1]["bulk_role"] == "source_crates_fallback_auto"
    assert fake_engine.fallback_urls == [("https://crates.io/crates/tokio", True)]


def test_api_key_enforcement(monkeypatch):
    monkeypatch.setenv("YADA_API_KEY", "expected-secret")

    with pytest.raises(HTTPException) as exc_info:
        api_mod._enforce_api_key("wrong-secret")
    assert exc_info.value.status_code == 401

    api_mod._enforce_api_key("expected-secret")


@pytest.mark.asyncio
async def test_single_and_compare_timeouts_return_504(monkeypatch):
    class FakeEngine:
        async def scrape(self, **kwargs):
            return None

        async def compare(self, **kwargs):
            return None

    async def immediate_timeout(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    original_engine = api_mod.engine
    api_mod.engine = FakeEngine()
    monkeypatch.setattr(api_mod.asyncio, "wait_for", immediate_timeout)
    try:
        with pytest.raises(HTTPException) as scrape_exc:
            await api_mod.do_scrape(
                url="https://example.com",
                mode=api_mod.ScrapeMode.auto,
                allow_layer5=False,
                timeout_seconds=1,
                x_api_key=None,
            )
        with pytest.raises(HTTPException) as compare_exc:
            await api_mod.do_compare(
                url="https://example.com",
                timeout_seconds=1,
                x_api_key=None,
            )
    finally:
        api_mod.engine = original_engine

    assert scrape_exc.value.status_code == 504
    assert compare_exc.value.status_code == 504
