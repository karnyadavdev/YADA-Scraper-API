import asyncio
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlparse

from .browser_identity import make_identity, random_sleep
from .proxy_config import AsyncSession
from .safe_http import safe_get


@dataclass
class QuickResult:
    success: bool
    html: Optional[str] = None
    status_code: int = 0
    needs_browser: bool = False
    reason: str = ""
    response_time: float = 0.0
    final_url: Optional[str] = None


# Signals that the real page never rendered: empty SPA shells and "please
# enable JS" bodies come back HTTP 200 but hold no content.
JS_HINTS = [
    re.compile(r"<noscript>.*enable javascript", re.I | re.S),
    re.compile(r"you need to enable javascript", re.I),
    re.compile(r"please enable javascript", re.I),
    re.compile(r"this page requires javascript", re.I),
    re.compile(r"javascript is required", re.I),
    re.compile(r"<div id=[\"']__next[\"']>\s*</div>", re.I),
    re.compile(r"<div id=[\"']root[\"']>\s*</div>", re.I),
    re.compile(r"<div id=[\"']app[\"']>\s*</div>", re.I),
]


# Interstitials (Cloudflare "Just a moment", DataDome captcha) often arrive as
# HTTP 200, so the body text is the only giveaway.
PAGE_WARNING_HINTS = [
    re.compile(r"checking your browser", re.I),
    re.compile(r"cf-browser-verification", re.I),
    re.compile(r"just a moment\.\.\.", re.I),
    re.compile(r"ray id", re.I),
    re.compile(r"ddos-guard", re.I),
    re.compile(r"attention required.*cloudflare", re.I | re.S),
    re.compile(r"blocked by.*security", re.I),
    re.compile(r"access denied", re.I),
    re.compile(r"<title>\s*403\s", re.I),
    re.compile(r"captcha", re.I),
]


META_REFRESH_RE = re.compile(
    r"<meta[^>]+http-equiv=[\"']?refresh[\"']?[^>]+content=[\"'][^\"']*url=([^\"']+)[\"']",
    re.I,
)


def _find_meta_refresh_url(html, base_url):
    # meta refresh tags live in <head>; first 10KB is enough and keeps huge pages cheap
    match = META_REFRESH_RE.search(html[:10000] or "")
    if not match:
        return None
    target = match.group(1).strip().strip("'\"")
    if not target:
        return None
    return urljoin(base_url, target)



def _should_retry_with_browser(html, status_code):
    if not html:
        return True, "empty response body"

    if status_code == 403:
        return True, "HTTP 403"

    if status_code == 503:
        return True, "HTTP 503"

    if status_code >= 400:
        return True, f"HTTP {status_code}"

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    body = soup.find("body")
    body_text = body.get_text(strip=True) if body else ""
    body_len = len(body_text)

    if body_len < 500:
        for pattern in PAGE_WARNING_HINTS:
            if pattern.search(html[:5000]):
                return True, f"page warning: {pattern.pattern[:30]}"

    for pattern in JS_HINTS:
        if body_len < 800 and pattern.search(html[:10000]):
            return True, f"browser render suggested: {pattern.pattern[:30]}"

    if body_len < 100 and ("<html" in html.lower() or "<body" in html.lower()):
        return True, f"thin HTML text ({body_len} chars)"

    return False, ""


async def raw_scrape(url, timeout=5.0, retries=1):
    """Layer 1: Raw, unprotected HTTP request."""
    start = time.time()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }

    for attempt in range(retries + 1):
        try:
            # TLS-level impersonation: sites drop connections whose TLS
            # handshake (JA3) doesn't match the User-Agent they were sent.
            async with AsyncSession(impersonate="chrome131", timeout=timeout) as session:
                response = await safe_get(session, url, headers=headers, timeout=timeout)
                html = response.text
                refresh_url = _find_meta_refresh_url(html, str(response.url or url))
                if refresh_url and refresh_url != str(response.url or url):
                    response = await safe_get(session, refresh_url, headers=headers, timeout=timeout)
                    html = response.text

                retry_with_browser, reason = _should_retry_with_browser(html, response.status_code)
                if retry_with_browser:
                    return QuickResult(
                        success=bool(html), status_code=response.status_code, html=html,
                        needs_browser=True, reason=reason, response_time=time.time() - start,
                        final_url=str(response.url or url),
                    )

                return QuickResult(
                    success=True, status_code=response.status_code, html=html,
                    needs_browser=False, reason="Loaded via Raw HTTP", response_time=time.time() - start,
                    final_url=str(response.url or url),
                )
        except Exception as e:
            await asyncio.sleep(1)
            if attempt == retries:
                return QuickResult(
                    success=False, status_code=0, needs_browser=True,
                    reason=f"Raw request failed: {type(e).__name__}", response_time=time.time() - start,
                    final_url=url,
                )

async def quick_scrape(url, fingerprint=None, timeout=7.0, retries=1, cookies=None):
    """Try to get the page without a full browser instance."""
    if fingerprint is None:
        fingerprint = make_identity()

    start = time.time()

    cookie_dict = {}
    if cookies:
        if isinstance(cookies, dict):
            cookie_dict = cookies
        elif isinstance(cookies, list):
            for c in cookies:
                if isinstance(c, dict) and "name" in c and "value" in c:
                    cookie_dict[c["name"]] = c["value"]

    if cookie_dict:
        parsed = urlparse(url)
        fingerprint.headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"

    for attempt in range(retries + 1):
        try:
            # .json endpoints (instagram-style) have no DOM to sniff, so the
            # decision runs on status codes alone
            if url.split("?")[0].endswith(".json"):
                cookie_str = "; ".join([f"{k}={v}" for k, v in cookie_dict.items()])
                req_headers = dict(fingerprint.headers)
                req_headers["Accept"] = "application/json"
                if cookie_str:
                    req_headers["Cookie"] = cookie_str
                async with AsyncSession(timeout=timeout) as session:
                    response = await safe_get(session, url, headers=req_headers, timeout=timeout)
                status_code = response.status_code
                html = response.text
                final_url = str(response.url or url)
                elapsed = time.time() - start
                if status_code in (401, 403, 407, 503):
                    need_retry, msg = True, f"HTTP {status_code}"
                elif status_code == 429:
                    need_retry, msg = True, "HTTP 429 (Rate Limit)"
                else:
                    need_retry, msg = _should_retry_with_browser(html, status_code)
                captured = (status_code == 200) and not need_retry
                return QuickResult(
                    success=captured, status_code=status_code, html=html,
                    needs_browser=need_retry, reason=msg if need_retry else "Success",
                    response_time=elapsed, final_url=final_url,
                )
            async with AsyncSession(
                impersonate=fingerprint.impersonate_id or "chrome120",
                timeout=timeout,
            ) as session:
                base_domain = urlparse(url).hostname or ""
                if not base_domain.startswith("."):
                    base_domain = "." + base_domain
                for name, value in cookie_dict.items():
                    session.cookies.set(name, value, domain=base_domain)
                    
                response = await safe_get(
                    session,
                    url,
                    headers=fingerprint.headers,
                    timeout=timeout,
                )

                elapsed = time.time() - start
                html = response.text
                refresh_url = _find_meta_refresh_url(html, str(response.url or url))
                if refresh_url and refresh_url != str(response.url or url):
                    response = await safe_get(
                        session,
                        refresh_url,
                        headers=fingerprint.headers,
                        timeout=timeout,
                    )
                    elapsed = time.time() - start
                    html = response.text

                retry_with_browser, reason = _should_retry_with_browser(html, response.status_code)

                if retry_with_browser:
                    return QuickResult(
                        success=bool(html), html=html,
                        status_code=response.status_code,
                        needs_browser=True,
                        reason=reason, response_time=elapsed,
                        final_url=str(response.url or url),
                    )

                return QuickResult(
                    success=True, html=html,
                    status_code=response.status_code,
                    needs_browser=False,
                    reason="Captured via static request",
                    response_time=elapsed,
                    final_url=str(response.url or url),
                )

        except Exception as e:
            if attempt < retries:
                import asyncio
                await asyncio.sleep(random_sleep(base=1.0, jitter=0.5))
                fingerprint = make_identity()
                continue

            return QuickResult(
                success=False, status_code=0,
                needs_browser=True,
                reason=f"Static request failed: {type(e).__name__}",
                response_time=time.time() - start,
                final_url=url,
            )


async def advanced_waf_scrape(url, timeout=12.0):
    """
    Layer 1c: Advanced WAF Bypass Matrix.
    Systematically rotates through proven curl_cffi impersonations and Referers
    to bypass strict WAFs without executing JS.
    """
    strategies = [
        {
            "name": "Safari iOS Mobile",
            "impersonate": "safari17_0",
            "headers": {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://t.co/"
            }
        },
        {
            "name": "Safari macOS",
            "impersonate": "safari15_3",
            "headers": {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.3 Safari/605.1.15",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.facebook.com/"
            }
        },
        {
            "name": "Googlebot Search",
            "impersonate": "chrome116",
            "headers": {
                "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.google.com/"
            }
        },
        {
            "name": "Bingbot",
            "impersonate": "edge116",
            "headers": {
                "User-Agent": "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
                "Accept": "*/*"
            }
        }
    ]
    
    start = time.time()
    for strategy in strategies:
        try:
            async with AsyncSession(impersonate=strategy["impersonate"], timeout=timeout) as session:
                response = await safe_get(session, url, headers=strategy["headers"], timeout=timeout)
                html = response.text
                elapsed = time.time() - start
                
                retry_with_browser, reason = _should_retry_with_browser(html, response.status_code)
                
                if not retry_with_browser:
                    return QuickResult(
                        success=True, html=html,
                        status_code=response.status_code,
                        needs_browser=False,
                        reason=f"Captured via Layer 1c ({strategy['name']})",
                        response_time=elapsed,
                        final_url=str(response.url or url),
                    )
        except Exception:
            continue
            
    return QuickResult(
        success=False, status_code=0, html=None,
        needs_browser=True,
        reason="Layer 1c exhausted all advanced WAF strategies",
        response_time=time.time() - start,
        final_url=url,
    )


async def mobile_scrape(url, timeout=7.0, retries=1):
    """
    Layer 1b: Ultimate Mobile Emulation bypassing WAFs via curl_cffi and Android JA3.
    """
    start = time.time()
    for attempt in range(retries + 1):
        try:
            async with AsyncSession(impersonate="chrome131_android", timeout=timeout) as session:
                response = await safe_get(session, url, timeout=timeout)
                elapsed = time.time() - start
                html = response.text
                
                refresh_url = _find_meta_refresh_url(html, str(response.url or url))
                if refresh_url and refresh_url != str(response.url or url):
                    response = await safe_get(session, refresh_url, timeout=timeout)
                    elapsed = time.time() - start
                    html = response.text

                retry_with_browser, reason = _should_retry_with_browser(html, response.status_code)

                if retry_with_browser:
                    return QuickResult(
                        success=bool(html), html=html,
                        status_code=response.status_code,
                        needs_browser=True,
                        reason=reason, response_time=elapsed,
                        final_url=str(response.url or url),
                    )

                return QuickResult(
                    success=True, html=html,
                    status_code=response.status_code,
                    needs_browser=False,
                    reason="Captured via Mobile JA3 impersonation",
                    response_time=elapsed,
                    final_url=str(response.url or url),
                )

        except Exception as e:
            if attempt < retries:
                await asyncio.sleep(random_sleep(base=1.0, jitter=0.5))
                continue

            return QuickResult(
                success=False, status_code=0,
                needs_browser=True,
                reason=f"Mobile scrape failed: {type(e).__name__}",
                response_time=time.time() - start,
                final_url=url,
            )


async def crawler_scrape(url, bot_type="googlebot", timeout=5.0, retries=1):
    """
    Experimental Layer: Impersonate a search engine crawler.
    Many sites (like Medium) serve full content to bots for SEO.
    """
    start = time.time()

    bots = {
        "googlebot": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        "bingbot": "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
        "duckduckbot": "DuckDuckBot/1.1; (+http://duckduckgo.com/duckduckbot.html)",
        "duckduckgo": "DuckDuckBot/1.1; (+http://duckduckgo.com/duckduckbot.html)",
        "twitterbot": "Twitterbot/1.0",
        "facebookbot": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    }

    ua = bots.get(bot_type, bots["googlebot"])
    impersonation_candidates = {
        "googlebot": ["chrome110", "edge101", None, "chrome120"],
        "bingbot": ["edge101", "chrome110", "chrome120", None],
        "duckduckbot": ["chrome120", "chrome110", "edge101", None],
        "duckduckgo": ["chrome120", "chrome110", "edge101", None],
        "twitterbot": ["chrome120", "chrome110", None],
        "facebookbot": ["chrome120", "chrome110", None],
    }.get(bot_type, ["chrome120", "chrome110", None])

    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    }

    last_error = None
    for attempt, impersonate_id in enumerate(impersonation_candidates[:retries + 1]):
        try:
            async with AsyncSession(
                impersonate=impersonate_id,
                timeout=timeout,
            ) as session:
                response = await safe_get(
                    session,
                    url,
                    headers=headers,
                    timeout=timeout,
                )

                elapsed = time.time() - start
                html = response.text
                refresh_url = _find_meta_refresh_url(html, str(response.url or url))
                if refresh_url and refresh_url != str(response.url or url):
                    response = await safe_get(
                        session,
                        refresh_url,
                        headers=headers,
                        timeout=timeout,
                    )
                    elapsed = time.time() - start
                    html = response.text

                retry_with_browser, reason = _should_retry_with_browser(html, response.status_code)

                if retry_with_browser:
                    last_error = QuickResult(
                        success=bool(html), html=html,
                        status_code=response.status_code,
                        needs_browser=True,
                        reason=f"Bot persona ({impersonate_id or 'none'}) captured with warnings: {reason}",
                        response_time=elapsed,
                        final_url=str(response.url or url),
                    )
                    if attempt < min(retries, len(impersonation_candidates) - 1):
                        continue
                    return QuickResult(
                        success=bool(html), html=html,
                        status_code=response.status_code,
                        needs_browser=True,
                        reason=f"Bot persona ({impersonate_id or 'none'}) captured with warnings: {reason}",
                        response_time=elapsed,
                        final_url=str(response.url or url),
                    )

                return QuickResult(
                    success=True, html=html,
                    status_code=response.status_code,
                    needs_browser=False,
                    reason=f"Captured via bot persona ({bot_type}, {impersonate_id or 'none'})",
                    response_time=elapsed,
                    final_url=str(response.url or url),
                )

        except Exception as e:
            last_error = QuickResult(
                success=False, status_code=0,
                needs_browser=True,
                reason=f"Bot persona request failed with {impersonate_id or 'none'}: {type(e).__name__}",
                response_time=time.time() - start,
                final_url=url,
            )
            if attempt < min(retries, len(impersonation_candidates) - 1):
                await asyncio.sleep(random_sleep(base=1.0, jitter=0.5))
                continue

            return last_error

    return last_error or QuickResult(
        success=False, status_code=0,
        reason="Bot persona request ended before making a request",
        response_time=time.time() - start,
        final_url=url,
    )

