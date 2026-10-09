"""Phase 3a: _text_from_html must parse each unique page only once.

Fails before the fix (repeated calls re-parse), passes after (content-keyed cache).
"""
from scraper import engine


def _clear_cache():
    engine._TEXT_CACHE.clear()


def test_repeated_calls_parse_once_per_unique_html(monkeypatch):
    _clear_cache()
    import bs4

    real = bs4.BeautifulSoup
    calls = {"n": 0}

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(bs4, "BeautifulSoup", counting)
    html = "<html><body><p>alpha beta</p></body></html>"
    first = engine._text_from_html(html)
    second = engine._text_from_html(html)
    assert first == second == "alpha beta"
    assert calls["n"] == 1, f"expected 1 parse for the same page, got {calls['n']}"


def test_cache_does_not_cross_contaminate_pages():
    _clear_cache()
    page_a = "<html><body><p>site alpha unique text</p></body></html>"
    page_b = "<html><body><p>site beta other text</p></body></html>"
    for _ in range(2):
        text_a = engine._text_from_html(page_a)
        text_b = engine._text_from_html(page_b)
        assert "site alpha unique text" in text_a
        assert "site beta other text" in text_b
        assert "site beta" not in text_a
        assert "site alpha" not in text_b


def test_cache_size_is_bounded():
    _clear_cache()
    for i in range(12):
        engine._text_from_html(f"<html><body><p>page {i} content</p></body></html>")
    assert 1 <= len(engine._TEXT_CACHE) <= engine._TEXT_CACHE_MAX_ENTRIES


def test_large_html_bypasses_cache():
    _clear_cache()
    unit = "<p>filler</p>"
    big = unit * ((engine._TEXT_CACHE_MAX_INPUT // len(unit)) + 10)
    assert len(big) > engine._TEXT_CACHE_MAX_INPUT
    text = engine._text_from_html(big)
    assert "filler" in text
    assert big not in engine._TEXT_CACHE
    assert len(engine._TEXT_CACHE) == 0


def test_fallback_path_is_cached(monkeypatch):
    _clear_cache()
    import bs4

    calls = {"n": 0}

    def broken(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(bs4, "BeautifulSoup", broken)
    html = "<html><body><p>broken path text</p></body></html>"
    first = engine._text_from_html(html)
    second = engine._text_from_html(html)
    assert "broken path text" in first
    assert first == second
    assert calls["n"] == 1, f"fallback result should be cached, got {calls['n']} parses"
