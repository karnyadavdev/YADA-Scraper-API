import pytest

from scraper import proxy_config
from scraper.proxy_config import (
    AsyncSession,
    browser_proxy_options,
    curl_proxies,
    proxy_enabled,
    proxy_timeout_bonus,
    proxy_url,
)


@pytest.fixture(autouse=True)
def _clear_proxy_env(monkeypatch):
    for key in ("YADA_PROXY", "YADA_PROXY_OFF", "HTTPS_PROXY", "HTTP_PROXY",
                "YADA_PROXY_TIMEOUT_BONUS"):
        monkeypatch.delenv(key, raising=False)


def test_proxy_is_off_by_default():
    assert proxy_url() is None
    assert proxy_enabled() is False
    assert curl_proxies() == {}
    assert browser_proxy_options() is None


def test_yada_proxy_turns_proxy_on(monkeypatch):
    monkeypatch.setenv("YADA_PROXY", "http://1.2.3.4:8080")
    assert proxy_url() == "http://1.2.3.4:8080"
    assert proxy_enabled() is True
    assert curl_proxies() == {"http": "http://1.2.3.4:8080", "https": "http://1.2.3.4:8080"}
    assert browser_proxy_options() == {"server": "http://1.2.3.4:8080"}


def test_proxy_credentials_are_split_for_the_browser(monkeypatch):
    monkeypatch.setenv("YADA_PROXY", "http://user:pass@5.6.7.8:3128")
    assert browser_proxy_options() == {
        "server": "http://5.6.7.8:3128",
        "username": "user",
        "password": "pass",
    }
    assert curl_proxies() == {
        "http": "http://user:pass@5.6.7.8:3128",
        "https": "http://user:pass@5.6.7.8:3128",
    }


def test_proxy_falls_back_to_standard_env_vars(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://9.9.9.9:8080")
    assert proxy_url() == "http://9.9.9.9:8080"


def test_yada_proxy_off_wins_over_everything(monkeypatch):
    monkeypatch.setenv("YADA_PROXY", "http://1.2.3.4:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://9.9.9.9:8080")
    monkeypatch.setenv("YADA_PROXY_OFF", "1")
    assert proxy_url() is None
    assert curl_proxies() == {}
    assert browser_proxy_options() is None


def test_session_factory_forwards_proxies(monkeypatch):
    captured = {}

    class FakeCurlSession:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setenv("YADA_PROXY", "http://1.2.3.4:8080")
    monkeypatch.setattr(proxy_config, "_AsyncSession", FakeCurlSession)

    AsyncSession(impersonate="chrome131", timeout=5.0)

    assert captured["impersonate"] == "chrome131"
    assert captured["timeout"] == 5.0
    assert captured["proxies"] == {"http": "http://1.2.3.4:8080", "https": "http://1.2.3.4:8080"}


def test_session_factory_sends_no_proxies_when_off(monkeypatch):
    captured = {}

    class FakeCurlSession:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(proxy_config, "_AsyncSession", FakeCurlSession)

    AsyncSession(timeout=5.0)

    assert "proxies" not in captured


def test_ladder_layers_share_the_proxy_session():
    from scraper import http_scraping, source_fast_paths, ultimate_stealth

    for module in (http_scraping, source_fast_paths, ultimate_stealth):
        assert module.AsyncSession is AsyncSession, module.__name__


def test_tls_verification_stays_on_by_default(monkeypatch):
    from scraper import proxy_config as pc

    monkeypatch.setenv("YADA_PROXY", "http://1.2.3.4:8080")
    assert pc.proxy_insecure() is False

    monkeypatch.setenv("YADA_PROXY_INSECURE", "1")
    assert pc.proxy_insecure() is True


def test_insecure_flag_is_ignored_without_a_proxy(monkeypatch):
    from scraper import proxy_config as pc

    monkeypatch.setenv("YADA_PROXY_INSECURE", "1")
    assert pc.proxy_insecure() is False


def test_session_disables_verification_only_when_proxying_insecurely(monkeypatch):
    captured = {}

    class FakeCurlSession:
        def __init__(self, *args, **kwargs):
            captured.clear()
            captured.update(kwargs)

    monkeypatch.setattr(proxy_config, "_AsyncSession", FakeCurlSession)

    monkeypatch.setenv("YADA_PROXY", "http://1.2.3.4:8080")
    AsyncSession()
    assert "verify" not in captured

    monkeypatch.setenv("YADA_PROXY_INSECURE", "1")
    AsyncSession()
    assert captured["verify"] is False
    assert "proxies" in captured


def test_timeout_bonus_defaults_to_10_while_proxying(monkeypatch):
    monkeypatch.setenv("YADA_PROXY", "http://1.2.3.4:8080")
    assert proxy_timeout_bonus() == 10.0


def test_timeout_bonus_is_zero_without_a_proxy(monkeypatch):
    assert proxy_timeout_bonus() == 0.0
    monkeypatch.setenv("YADA_PROXY_TIMEOUT_BONUS", "42")
    assert proxy_timeout_bonus() == 0.0
    monkeypatch.setenv("YADA_PROXY", "http://1.2.3.4:8080")
    assert proxy_timeout_bonus() == 42.0


def test_timeout_bonus_clamps_bad_and_out_of_range_values(monkeypatch):
    monkeypatch.setenv("YADA_PROXY", "http://1.2.3.4:8080")
    for bad in ("", "   ", "banana", "-5", "10000"):
        monkeypatch.setenv("YADA_PROXY_TIMEOUT_BONUS", bad)
        value = proxy_timeout_bonus()
        if bad == "banana":
            assert value == 10.0, bad
        elif bad in ("", "   "):
            assert value == 10.0, bad
        else:
            assert 0.0 <= value <= 120.0, bad
