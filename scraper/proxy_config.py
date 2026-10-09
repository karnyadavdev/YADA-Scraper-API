"""Optional outbound proxy support.

Turned ON by setting ``YADA_PROXY`` (falls back to ``HTTPS_PROXY`` / ``HTTP_PROXY``).
Turned OFF again with ``YADA_PROXY_OFF=1`` even if a proxy is inherited from the shell.

Everything that talks to the network takes its session/proxy from here so the
switch is global instead of being wired into a single layer.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from curl_cffi.requests import AsyncSession as _AsyncSession

_TRUTHY = {"1", "true", "yes", "on"}

__all__ = [
    "AsyncSession",
    "browser_proxy_options",
    "curl_proxies",
    "proxy_enabled",
    "proxy_insecure",
    "proxy_timeout_bonus",
    "proxy_url",
]


def proxy_url() -> str | None:
    """Return the configured proxy URL, or ``None`` when proxying is off."""
    if os.environ.get("YADA_PROXY_OFF", "").strip().lower() in _TRUTHY:
        return None
    for key in ("YADA_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return None


def proxy_enabled() -> bool:
    return proxy_url() is not None


def proxy_insecure() -> bool:
    """Skip TLS verification while proxying.

    Needed for TLS-inspecting proxies (ScraperAPI and friends re-issue the
    target certificate with their own CA).  Off by default on purpose.
    """
    if not proxy_url():
        return False
    return os.environ.get("YADA_PROXY_INSECURE", "").strip().lower() in _TRUTHY


def proxy_timeout_bonus() -> float:
    """Extra seconds allowed per HTTP attempt while proxying.

    The proxy gateway adds real round-trip time (measured median ~4s), so
    tight per-attempt budgets need a bonus when the proxy is on. Returns 0
    when proxying is off so direct budgets stay exactly as configured.
    """
    if not proxy_url():
        return 0.0
    raw = (os.getenv("YADA_PROXY_TIMEOUT_BONUS", "10") or "").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 10.0
    return max(0.0, min(value, 120.0))


def curl_proxies() -> dict:
    """curl_cffi ``proxies=`` payload (``{}`` when proxying is off)."""
    url = proxy_url()
    if not url:
        return {}
    return {"http": url, "https": url}


def browser_proxy_options() -> dict | None:
    """Playwright ``launch(proxy=...)`` payload (``None`` when proxying is off)."""
    url = proxy_url()
    if not url:
        return None
    parts = urlsplit(url)
    if parts.username and parts.password:
        return {
            "server": f"{parts.scheme}://{parts.hostname}:{parts.port}",
            "username": parts.username,
            "password": parts.password,
        }
    return {"server": url}


def AsyncSession(*args, **kwargs):
    """Drop-in replacement for ``curl_cffi.requests.AsyncSession`` with proxy wiring."""
    proxies = curl_proxies()
    if proxies:
        kwargs.setdefault("proxies", proxies)
        if proxy_insecure():
            kwargs.setdefault("verify", False)
    return _AsyncSession(*args, **kwargs)
