"""compare() must honor the scrape budget (review finding K).

The old implementation awaited asyncio.gather with no overall deadline,
so a slow target could hold the /compare endpoint (and browser slots)
indefinitely. The fix bounds the wait, cancels stragglers, and reports
them as timeout entries while preserving any layers that finished.
"""

import asyncio
import os
import time

import pytest

from scraper import engine as engine_mod
from scraper.engine import ScraperEngine
from scraper.http_scraping import QuickResult
from scraper.site_memory import SiteMemory


async def _ok(url, *args, **kwargs):
    return QuickResult(
        success=True,
        html="<html><body>" + ("content " * 2000) + "</body></html>",
        status_code=200,
        reason="Success",
        response_time=0.01,
        final_url=url,
    )


async def _slow(url, *args, **kwargs):
    await asyncio.sleep(60)
    return _ok(url)


@pytest.mark.asyncio
async def test_compare_returns_within_budget_and_marks_timed_out_layers(monkeypatch, tmp_path):
    memory = SiteMemory(str(tmp_path / "memory.json"))
    engine = ScraperEngine.__new__(ScraperEngine)
    engine.memory = memory

    for name in ("raw_scrape", "mobile_scrape", "quick_scrape", "crawler_scrape", "scrape_ultimate"):
        monkeypatch.setattr(engine_mod, name, _slow)

    async def slow_browser(url, *args, **kwargs):
        await asyncio.sleep(60)
        return _ok(url)

    monkeypatch.setattr(engine, "_admitted_browser_call", slow_browser, raising=False)
    monkeypatch.setenv("YADA_SCRAPE_BUDGET", "10")

    started = time.time()
    result = await engine.compare("https://slow.example")
    elapsed = time.time() - started

    assert elapsed < 20.0, f"compare took {elapsed:.1f}s, budget was 10s"
    assert result.total_time < 20.0
    assert len(result.layers) == 9

    timed_out = [layer for layer in result.layers if "timeout" in layer["diagnostics"]]
    assert len(timed_out) == 9
    for layer in timed_out:
        assert layer["success"] is False
        assert "budget" in layer["reason"] or "timed out" in layer["reason"].lower()
    assert result.fastest is None


@pytest.mark.asyncio
async def test_compare_unaffected_when_all_layers_finish_fast(monkeypatch, tmp_path):
    memory = SiteMemory(str(tmp_path / "memory.json"))
    engine = ScraperEngine.__new__(ScraperEngine)
    engine.memory = memory

    for name in ("raw_scrape", "mobile_scrape", "quick_scrape", "crawler_scrape", "scrape_ultimate"):
        monkeypatch.setattr(engine_mod, name, _ok)

    async def fast_browser(url, *args, **kwargs):
        return _ok(url)

    monkeypatch.setattr(engine, "_admitted_browser_call", fast_browser, raising=False)

    result = await engine.compare("https://fast.example")
    assert len(result.layers) == 9
    assert all("timeout" not in layer["diagnostics"] for layer in result.layers)
    assert result.fastest is not None
