import asyncio
from types import SimpleNamespace

import pytest

from scraper.browser_admission import BrowserAdmission, configured_browser_concurrency
from scraper import engine as engine_mod
from scraper.site_memory import SiteMemory


def _capture(url, layer="test"):
    return SimpleNamespace(
        success=True,
        html="<html><body>useful rendered content</body></html>",
        status_code=200,
        reason="ok",
        response_time=0.01,
        layer=layer,
        cookies=[],
        final_url=url,
    )


def test_browser_limit_is_environment_configurable(monkeypatch):
    monkeypatch.setenv("YADA_BROWSER_SOURCE_CONCURRENCY", "7")
    assert configured_browser_concurrency() == 7

    monkeypatch.setenv("YADA_BROWSER_SOURCE_CONCURRENCY", "invalid")
    assert configured_browser_concurrency() == 2

    monkeypatch.setenv("YADA_BROWSER_SOURCE_CONCURRENCY", "0")
    assert configured_browser_concurrency() == 1


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_admission_slot():
    admission = BrowserAdmission(limit=1, poll_interval=0.001)
    release_holder = asyncio.Event()
    holder_started = asyncio.Event()

    async def holder():
        async with admission.slot():
            holder_started.set()
            await release_holder.wait()

    async def waiter():
        async with admission.slot():
            pytest.fail("cancelled waiter must not enter the slot")

    holder_task = asyncio.create_task(holder())
    await holder_started.wait()
    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)
    waiter_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_task

    release_holder.set()
    await holder_task
    assert admission.active == 0


@pytest.mark.asyncio
async def test_cancelled_active_operation_releases_admission_slot():
    admission = BrowserAdmission(limit=1)
    entered = asyncio.Event()

    async def operation():
        async with admission.slot():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(operation())
    await entered.wait()
    assert admission.active == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert admission.active == 0

    async with admission.slot():
        assert admission.active == 1
    assert admission.active == 0


@pytest.mark.asyncio
async def test_browser_gate_covers_explicit_modes_and_compare_across_engines(monkeypatch, tmp_path):
    admission = BrowserAdmission(limit=1, poll_interval=0.001)
    monkeypatch.setattr(engine_mod, "browser_admission", admission)

    active = 0
    peak = 0
    calls = []

    async def fake_browser(url, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append((url, kwargs.get("solve_challenges", "NOT_PASSED")))
        try:
            await asyncio.sleep(0.015)
            return _capture(url)
        finally:
            active -= 1

    async def fake_http(url, **kwargs):
        return _capture(url, layer="http")

    monkeypatch.setattr(engine_mod, "stealth_scrape", fake_browser)
    monkeypatch.setattr(engine_mod, "full_scrape", fake_browser)
    monkeypatch.setattr(engine_mod, "raw_scrape", fake_http)
    monkeypatch.setattr(engine_mod, "mobile_scrape", fake_http)
    monkeypatch.setattr(engine_mod, "quick_scrape", fake_http)
    monkeypatch.setattr(engine_mod, "crawler_scrape", fake_http)

    engines = [engine_mod.ScraperEngine() for _ in range(3)]
    for index, engine in enumerate(engines):
        engine.memory = SiteMemory(str(tmp_path / f"memory-{index}.json"))

    layer4, layer5, compared = await asyncio.gather(
        engines[0].scrape("https://layer4.example", mode="layer4"),
        engines[1].scrape("https://layer5.example", mode="layer5"),
        engines[2].compare("https://compare.example"),
    )

    assert layer4.ok and layer5.ok
    assert len(compared.layers) == 9
    assert len(calls) == 4
    compare_calls = [solve for url, solve in calls if url == "https://compare.example"]
    assert compare_calls == [False, False]
    assert peak == 1
    assert admission.active == 0


@pytest.mark.asyncio
async def test_browser_gate_covers_source_single_and_bulk_without_dropping_tail(monkeypatch, tmp_path):
    admission = BrowserAdmission(limit=1, poll_interval=0.001)
    monkeypatch.setattr(engine_mod, "browser_admission", admission)
    monkeypatch.setattr(engine_mod, "source_name", lambda url: "browser_source")

    active = 0
    peak = 0
    challenge_values = []

    async def enter(call):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.015)
            return call()
        finally:
            active -= 1

    async def fake_source(url, cookies=None, structured=False, solve_challenges=True):
        challenge_values.append(("single", solve_challenges))
        return await enter(lambda: _capture(url, layer="source"))

    async def fake_bulk(urls, cookies=None, solve_challenges=True):
        challenge_values.append(("bulk", solve_challenges))
        return await enter(lambda: [_capture(urls[0], layer="source_bulk")])

    monkeypatch.setattr(engine_mod, "source_scrape", fake_source)
    monkeypatch.setattr(engine_mod, "source_bulk_scrape", fake_bulk)

    single_engine = engine_mod.ScraperEngine()
    bulk_engine = engine_mod.ScraperEngine()
    single_engine.memory = SiteMemory(str(tmp_path / "single.json"))
    bulk_engine.memory = SiteMemory(str(tmp_path / "bulk.json"))

    single, bulk = await asyncio.gather(
        single_engine.scrape(
            "https://source.example/one",
            mode="source",
            solve_challenges=False,
        ),
        bulk_engine.scrape_source_bulk(
            ["https://source.example/one", "https://source.example/two"],
            solve_challenges=False,
        ),
    )

    assert single.ok
    assert len(bulk) == 2
    assert bulk[0].ok
    assert not bulk[1].ok
    assert bulk[1].url == "https://source.example/two"
    assert bulk[1].err == "Source bulk returned no result for URL"
    assert sorted(challenge_values) == [("bulk", False), ("single", False)]
    assert peak == 1
    assert admission.active == 0


@pytest.mark.asyncio
async def test_explicit_source_exception_returns_failed_result(monkeypatch, tmp_path):
    monkeypatch.setattr(engine_mod, "browser_admission", BrowserAdmission(limit=1))
    monkeypatch.setattr(engine_mod, "source_name", lambda url: "broken")

    async def broken_source(url, cookies=None, structured=False, solve_challenges=True):
        assert solve_challenges is False
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(engine_mod, "source_scrape", broken_source)
    engine = engine_mod.ScraperEngine()
    engine.memory = SiteMemory(str(tmp_path / "memory.json"))

    result = await engine.scrape(
        "https://broken.example",
        mode="source",
        solve_challenges=False,
    )

    assert not result.ok
    assert result.winning_layer == "source_broken"
    assert result.tries[0]["reason"] == "Named source raised RuntimeError"


@pytest.mark.asyncio
async def test_http_only_sources_do_not_consume_browser_slots(monkeypatch, tmp_path):
    admission = BrowserAdmission(limit=1, poll_interval=0.001)
    monkeypatch.setattr(engine_mod, "browser_admission", admission)
    monkeypatch.setattr(engine_mod, "source_name", lambda url: "pubmed")

    active = 0
    peak = 0

    async def fake_source(url, cookies=None, structured=False, solve_challenges=False):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.015)
            return _capture(url, layer="source_pubmed")
        finally:
            active -= 1

    monkeypatch.setattr(engine_mod, "source_scrape", fake_source)

    first = engine_mod.ScraperEngine()
    second = engine_mod.ScraperEngine()
    first.memory = SiteMemory(str(tmp_path / "first.json"))
    second.memory = SiteMemory(str(tmp_path / "second.json"))

    results = await asyncio.gather(
        first.scrape("https://pubmed.ncbi.nlm.nih.gov/1/", mode="source"),
        second.scrape("https://pubmed.ncbi.nlm.nih.gov/2/", mode="source"),
    )

    assert all(result.ok for result in results)
    assert peak == 2
    assert admission.active == 0


@pytest.mark.asyncio
async def test_bestbuy_guarded_source_uses_browser_admission(monkeypatch, tmp_path):
    admission = BrowserAdmission(limit=1, poll_interval=0.001)
    monkeypatch.setattr(engine_mod, "browser_admission", admission)
    monkeypatch.setattr(engine_mod, "source_name", lambda url: "bestbuy")

    active = 0
    peak = 0

    async def fake_source(url, cookies=None, structured=False, solve_challenges=False):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.015)
            return _capture(url, layer="source_bestbuy")
        finally:
            active -= 1

    monkeypatch.setattr(engine_mod, "source_scrape", fake_source)

    first = engine_mod.ScraperEngine()
    second = engine_mod.ScraperEngine()
    first.memory = SiteMemory(str(tmp_path / "first.json"))
    second.memory = SiteMemory(str(tmp_path / "second.json"))

    results = await asyncio.gather(
        first.scrape("https://www.bestbuy.com/site/one", mode="source"),
        second.scrape("https://www.bestbuy.com/site/two", mode="source"),
    )

    assert all(result.ok for result in results)
    assert peak == 1
    assert admission.active == 0


@pytest.mark.asyncio
async def test_allow_browser_false_named_source_auto_bypasses_browser(monkeypatch, tmp_path):
    browser_called = False

    async def fake_browser_source(url, **kwargs):
        nonlocal browser_called
        browser_called = True
        return _capture(url, layer="source_ebay")

    async def fake_quick_scrape(url, fingerprint=None, cookies=None, **kwargs):
        return _capture(url, layer="layer2_cookie")

    monkeypatch.setattr(engine_mod, "source_name", lambda url: "ebay")
    monkeypatch.setattr(engine_mod, "source_scrape", fake_browser_source)
    monkeypatch.setattr(engine_mod, "raw_scrape", lambda url, **kwargs: _capture(url, layer="layer1_simple"))
    monkeypatch.setattr(engine_mod, "quick_scrape", fake_quick_scrape)

    engine = engine_mod.ScraperEngine()
    engine.memory = SiteMemory(str(tmp_path / "mem.json"))

    res = await engine.scrape("https://www.ebay.com/itm/123", mode="auto", allow_browser=False)
    assert res.ok
    assert not browser_called, "Browser-backed source fast path was called despite allow_browser=False!"
    assert res.winning_layer != "source_ebay"


@pytest.mark.asyncio
async def test_allow_browser_false_named_source_explicit_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(engine_mod, "source_name", lambda url: "ebay")
    engine = engine_mod.ScraperEngine()
    engine.memory = SiteMemory(str(tmp_path / "mem.json"))

    with pytest.raises(ValueError, match="Browser-backed source mode 'ebay' is disabled"):
        await engine.scrape("https://www.ebay.com/itm/123", mode="source", allow_browser=False)


@pytest.mark.asyncio
async def test_allow_browser_false_http_only_source_allowed(monkeypatch, tmp_path):
    monkeypatch.setattr(engine_mod, "source_name", lambda url: "reddit")
    async def fake_reddit_source(url, **kwargs):
        return _capture(url, layer="source_reddit")
    monkeypatch.setattr(engine_mod, "source_scrape", fake_reddit_source)

    engine = engine_mod.ScraperEngine()
    engine.memory = SiteMemory(str(tmp_path / "mem.json"))

    res = await engine.scrape("https://www.reddit.com/r/Python", mode="source", allow_browser=False)
    assert res.ok
    assert res.winning_layer == "source_reddit"


@pytest.mark.asyncio
async def test_allow_browser_false_scrape_source_bulk_returns_none(tmp_path):
    engine = engine_mod.ScraperEngine()
    engine.memory = SiteMemory(str(tmp_path / "mem.json"))

    # eBay is browser-backed -> returns None when allow_browser=False
    res_ebay = await engine.scrape_source_bulk(["https://www.ebay.com/itm/123"], allow_browser=False)
    assert res_ebay is None
