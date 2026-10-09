"""Safe final HTTP fallback for the scraper waterfall.

Layer 6 is intentionally an HTTP/TLS-impersonation transport.  Browser
fallbacks belong to the guarded Playwright layers; keeping them out of this
module prevents an alternate transport from bypassing the network policy.
"""

from __future__ import annotations

import time
from typing import Optional

from .http_scraping import QuickResult
from .proxy_config import AsyncSession
from .safe_http import safe_get


# status codes miss 200-ok challenges, so the challenge marker strings
# in the body are the reliable tell
_BLOCK_MARKERS = (
    "enable javascript",
    "datadome",
    "cloudflare",
    "cf-browser-verification",
    "px-captcha",
    "perimeterx",
)


async def scrape_layer6_tls(
    url: str,
    headers: Optional[dict[str, str]] = None,
    timeout: float = 15.0,
) -> QuickResult:
    """Fetch through the shared bounded, redirect-safe TLS transport."""
    started = time.time()
    request_headers = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    if headers:
        request_headers.update(headers)

    try:
        async with AsyncSession(impersonate="chrome120", timeout=timeout) as session:
            response = await safe_get(session, url, headers=request_headers, timeout=timeout)
        html = response.text or ""
        blocked = response.status_code in {403, 429, 503} or any(
            marker in html.lower() for marker in _BLOCK_MARKERS
        )
        return QuickResult(
            success=bool(html) and response.status_code < 400 and not blocked,
            html=html,
            status_code=response.status_code,
            needs_browser=blocked,
            reason=(
                "Layer 6 TLS response appears blocked or challenged"
                if blocked
                else "Captured via Layer 6 TLS impersonation"
            ),
            response_time=time.time() - started,
            final_url=str(response.url or url),
        )
    except Exception as exc:
        return QuickResult(
            success=False,
            status_code=0,
            needs_browser=True,
            reason=f"Layer 6 TLS request failed: {type(exc).__name__}",
            response_time=time.time() - started,
            final_url=url,
        )


async def scrape_ultimate(
    url: str,
    force_engine: bool = False,
    headers: Optional[dict[str, str]] = None,
) -> QuickResult:
    """Return the standard scraper result contract for Layer 6.

    ``force_engine`` is retained for source compatibility.  The historical
    DrissionPage engine was removed because it was neither packaged nor able
    to enforce redirect/subresource isolation.
    """
    result = await scrape_layer6_tls(url, headers=headers)
    if force_engine:
        result.reason = f"{result.reason}; guarded browser fallback is Layer 5"
    return result
