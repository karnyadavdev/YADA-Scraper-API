import asyncio
import os
import random
import re
import time
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from .browser_identity import (
    TRACKER_DOMAINS,
    make_identity,
    random_sleep,
    small_pause,
    fake_mouse_move,
    rand_point,
)
from .stealth_js import inject_stealth_scripts, inject_helper_scripts
from .proxy_config import browser_proxy_options
from .browser_network_guard import (
    install_browser_network_guard,
    service_worker_context_options,
)


_stealth = Stealth()


def _browser_headless():
    return os.environ.get("YADA_BROWSER_VISIBLE", "").lower() not in {"1", "true", "yes", "on"}


def _browser_launch_options():
    opts = {}
    executable_path = os.environ.get("YADA_BROWSER_EXECUTABLE")
    if executable_path:
        opts["executable_path"] = executable_path
    channel = os.environ.get("YADA_BROWSER_CHANNEL")
    if channel:
        opts["channel"] = channel

    proxy_opts = browser_proxy_options()
    if proxy_opts:
        opts["proxy"] = proxy_opts

    return opts


async def _close_browser_page(page=None, context=None, browser=None):
    try:
        if page:
            await asyncio.wait_for(page.unroute_all(behavior="wait"), timeout=2.0)
    except Exception:
        pass
    try:
        if context:
            await asyncio.wait_for(context.unroute_all(behavior="wait"), timeout=2.0)
    except Exception:
        pass
    try:
        if page:
            await page.close()
    except Exception:
        pass
    try:
        if context:
            await context.close()
    except Exception:
        pass
    await asyncio.sleep(0)
    try:
        if browser:
            await browser.close()
    except Exception:
        pass


def _should_skip_playwright_stealth(url):
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
    except Exception:
        return False

    if host in {"patents.google.com", "www.semanticscholar.org", "semanticscholar.org", "meta.discourse.org", "www.swiggy.com", "swiggy.com"}:
        return True

    if host in {"www.google.com", "google.com"} and path.startswith("/maps"):
        return True

    return False


class BrowseOutput:
    def __init__(self, success, html=None, status_code=0, reason="", response_time=0.0, layer="", cookies=None, final_url=None):
        self.success = success
        self.html = html
        self.status_code = status_code
        self.reason = reason
        self.response_time = response_time
        self.layer = layer
        self.cookies = cookies or []
        self.final_url = final_url


async def _restore_cookies(context, cookies):
    """Restore only Playwright-compatible cookies without widening their scope."""
    restored = []
    allowed = {"name", "value", "url", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
    for cookie in cookies or []:
        if not isinstance(cookie, dict) or not cookie.get("name"):
            continue
        if cookie.get("value") is None:
            continue
        item = {key: cookie[key] for key in allowed if key in cookie and cookie[key] is not None}
        if "domain" not in item and "url" not in item:
            continue
        item.setdefault("path", "/")
        restored.append(item)
    if restored:
        try:
            await context.add_cookies(restored)
        except Exception:
            # A malformed or expired cookie must not abort an otherwise-safe capture.
            pass


async def _do_scroll(page, max_scrolls=8, timeout=10.0):
    start = time.time()
    prev_h = 0

    for i in range(max_scrolls):
        if (time.time() - start) >= timeout:
            break

        try:
            await asyncio.wait_for(page.evaluate("document.body.scrollHeight"), timeout=2.0)

            amt = random.randint(300, 700)
            await asyncio.wait_for(page.evaluate(f"window.scrollBy(0, {amt})"), timeout=2.0)

            await asyncio.sleep(small_pause() + random.uniform(0.3, 0.8))

            new_h = await asyncio.wait_for(page.evaluate("document.body.scrollHeight"), timeout=2.0)
            if new_h == prev_h and i > 2:
                break
            prev_h = new_h
        except Exception:
            break

    try:
        if random.random() < 0.3:
            await asyncio.wait_for(page.evaluate("window.scrollTo(0, 0)"), timeout=2.0)
            await asyncio.sleep(small_pause())
    except Exception:
        pass


def _is_youtube_watch_url(url):
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        return ("youtube.com" in host or "youtu.be" in host) and (
            parsed.path == "/watch" or "youtu.be" in host
        )
    except Exception:
        return False


async def _maybe_load_youtube_comments(page, url, timeout=15.0):
    if not _is_youtube_watch_url(url):
        return

    start = time.time()
    try:
        await page.evaluate(
            """
            () => {
                const comments = document.querySelector('#comments');
                if (comments) {
                    comments.scrollIntoView({ block: 'center' });
                } else {
                    window.scrollBy(0, Math.max(900, window.innerHeight));
                }
            }
            """
        )
    except Exception:
        return

    while (time.time() - start) < timeout:
        try:
            count = await page.evaluate(
                "() => document.querySelectorAll('ytd-comment-thread-renderer, ytd-comment-renderer').length"
            )
            if count:
                break
            await page.evaluate("window.scrollBy(0, Math.max(600, window.innerHeight * 0.75))")
            await asyncio.sleep(1.2)
        except Exception:
            return

    for _ in range(4):
        try:
            await page.evaluate("window.scrollBy(0, Math.max(500, window.innerHeight * 0.65))")
            await asyncio.sleep(0.8)
        except Exception:
            break


def _is_reddit_url(url):
    try:
        parsed = urlparse(url)
        return "reddit.com" in parsed.netloc.lower()
    except Exception:
        return False


async def _maybe_load_reddit_content(page, url, fingerprint=None, timeout=15.0):
    if not _is_reddit_url(url):
        return False

    wait_start = time.time()
    vw = getattr(fingerprint, "scr_w", 1280) if fingerprint else 1280
    vh = getattr(fingerprint, "scr_h", 800) if fingerprint else 800

    while (time.time() - wait_start) < timeout:
        try:
            tags_count = await page.evaluate(
                """() => {
                return document.querySelectorAll('shreddit-post, shreddit-comment, shreddit-profile, faceplate-tracker, shreddit-title').length;
            }"""
            )
            if tags_count > 0:
                await asyncio.sleep(1.0)
                return True

            rx = random.randint(0, vw)
            ry = random.randint(0, vh)
            await page.mouse.move(rx, ry)
            await page.mouse.wheel(0, random.randint(100, 500))
            await asyncio.sleep(1.0)
        except Exception:
            await asyncio.sleep(1.0)
            continue
    return False


async def _wait_loaded(page, timeout=10000):
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass


async def _capture_html(page):
    html = await page.content()

    try:
        body_text = await page.evaluate(
            "() => document.body ? document.body.innerText.replace(/\\s+/g, ' ').trim() : ''"
        )
        shadow_text = await page.evaluate(
            """
            () => {
                const walk = (node) => {
                    let out = "";
                    if (node.nodeType === Node.TEXT_NODE) {
                        out += node.textContent + " ";
                    }
                    if (node.shadowRoot) {
                        out += walk(node.shadowRoot);
                    }
                    for (const child of node.childNodes || []) {
                        out += walk(child);
                    }
                    return out;
                };
                return walk(document.body).replace(/\\s+/g, " ").trim();
            }
            """
        )
    except Exception:
        return html

    rendered_text = max([body_text or "", shadow_text or ""], key=len)

    if not rendered_text:
        return html

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    page_text = soup.get_text(" ", strip=True)
    if len(rendered_text) <= max(len(page_text) * 2, len(page_text) + 500):
        return html

    rendered_node = soup.new_tag("div")
    rendered_node["data-yada-rendered-text"] = "true"
    rendered_node["style"] = "display:none"
    rendered_node.string = rendered_text

    if soup.body:
        soup.body.append(rendered_node)
        return str(soup)

    return f'{html}<body>{rendered_node}</body>'


async def stealth_scrape(url, fingerprint=None, timeout=20.0, retries=1, solve_challenges=False):
    # Retained only for callers from older integrations; active challenge
    # solving has been removed.
    del solve_challenges
    if fingerprint is None:
        fingerprint = make_identity()

    start = time.time()

    for attempt in range(retries + 1):
        browser = None
        ctx = None
        page = None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=_browser_headless(),
                    **_browser_launch_options(),
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-extensions",
                        "--disable-infobars",
                    ],
                )

                ctx = await browser.new_context(
                    user_agent=fingerprint.ua_string,
                    viewport={
                        "width": fingerprint.scr_w,
                        "height": fingerprint.scr_h,
                    },
                    locale=fingerprint.lang,
                    timezone_id=fingerprint.tz,
                    color_scheme="light",
                    permissions=["geolocation"],
                    **service_worker_context_options(),
                )

                await install_browser_network_guard(ctx, _block_trackers)

                page = await ctx.new_page()

                if not _should_skip_playwright_stealth(url):
                    await _stealth.apply_stealth_async(page)
                await inject_stealth_scripts(page, fingerprint.os_name)

                if _is_reddit_url(url):
                    warmup_url = "https://www.reddit.com/"
                    if url.rstrip("/") == "https://www.reddit.com":
                        warmup_url = "https://www.reddit.com/r/popular/"
                    try:
                        await page.goto(
                            warmup_url,
                            wait_until="domcontentloaded",
                            timeout=15000,
                        )
                        await asyncio.sleep(2.0)
                    except Exception:
                        pass

                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int(timeout * 1000),
                )

                status = response.status if response else 0

                await _wait_loaded(page)

                if _is_reddit_url(url):
                    await _maybe_load_reddit_content(page, url, fingerprint=fingerprint)
                else:
                    await _do_scroll(page, max_scrolls=5, timeout=8.0)
                await _maybe_load_youtube_comments(page, url)

                html = await _capture_html(page)

                final_url = page.url or ""
                if final_url.startswith("chrome-error://") or "chromewebdata" in final_url or "chrome-error://" in (html or ""):
                    await _close_browser_page(page, ctx, browser)
                    if attempt < retries:
                        await asyncio.sleep(random_sleep(base=2.0, jitter=1.0))
                        fingerprint = make_identity()
                        continue
                    return BrowseOutput(
                        success=False,
                        html="",
                        status_code=0,
                        reason="Browser navigation failed (navigation aborted / network reset)",
                        response_time=time.time() - start,
                        layer="layer4_stealth",
                        final_url=url,
                    )

                if status in (403, 503) or _looks_like_interstitial(html):
                    final_url = page.url
                    await _close_browser_page(page, ctx, browser)
                    res_status = status if status in (403, 429, 503) else 403
                    return BrowseOutput(
                        success=bool(html), html=html, status_code=res_status,
                        reason="Captured page still looks like an interstitial; challenge solving is unavailable.",
                        response_time=time.time() - start,
                        layer="layer4_stealth",
                        final_url=final_url,
                    )

                cookies = await ctx.cookies()
                final_url = page.url
                await _close_browser_page(page, ctx, browser)
                return BrowseOutput(
                    success=True, html=html, status_code=status,
                    reason="Captured via stealth browser",
                    response_time=time.time() - start,
                    layer="layer4_stealth",
                    cookies=cookies,
                    final_url=final_url,
                )
        except asyncio.CancelledError:
            await asyncio.shield(_close_browser_page(page, ctx, browser))
            raise
        except Exception as e:
            await _close_browser_page(page, ctx, browser)
            if attempt < retries:
                await asyncio.sleep(random_sleep(base=2.0, jitter=1.0))
                fingerprint = make_identity()
                continue

            return BrowseOutput(
                success=False, status_code=0,
                reason=f"Stealth browser capture failed: {type(e).__name__}",
                response_time=time.time() - start,
                layer="layer4_stealth",
                final_url=url,
            )



async def full_scrape(
    url,
    fingerprint=None,
    timeout=30.0,
    retries=1,
    solve_challenges=False,
    cookies=None,
):
    # Retained only for callers from older integrations; active challenge
    # solving has been removed.
    del solve_challenges
    if fingerprint is None:
        fingerprint = make_identity()

    start = time.time()
    parsed = urlparse(url)
    homepage = f"{parsed.scheme}://{parsed.netloc}/"

    for attempt in range(retries + 1):
        browser = None
        ctx = None
        page = None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=_browser_headless(),
                    **_browser_launch_options(),
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-extensions",
                        "--disable-infobars",
                        "--disable-background-timer-throttling",
                        "--disable-renderer-backgrounding",
                    ],
                )

                ctx = await browser.new_context(
                    user_agent=fingerprint.ua_string,
                    viewport={
                        "width": fingerprint.scr_w,
                        "height": fingerprint.scr_h,
                    },
                    locale=fingerprint.lang,
                    timezone_id=fingerprint.tz,
                    color_scheme="light",
                    permissions=["geolocation"],
                    java_script_enabled=True,
                    **service_worker_context_options(),
                )

                await install_browser_network_guard(ctx, _block_trackers)
                await _restore_cookies(ctx, cookies)

                page = await ctx.new_page()

                if not _should_skip_playwright_stealth(url):
                    await _stealth.apply_stealth_async(page)
                await inject_stealth_scripts(page, fingerprint.os_name)


                if url != homepage:
                    try:
                        await page.goto(
                            homepage,
                            wait_until="domcontentloaded",
                            timeout=25000,
                        )
                        await asyncio.sleep(random_sleep(base=1.5, jitter=0.5))
                        await _act_human(page, fingerprint, quick=True)
                    except Exception:
                        pass


                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int(timeout * 1000),
                )

                status = response.status if response else 0

                await _wait_loaded(page)

                await inject_helper_scripts(page)

                await _act_human(page, fingerprint, quick=False)
                await _scroll_around(page, fingerprint)
                await _maybe_load_youtube_comments(page, url)

                html = await _capture_html(page)

                final_url = page.url or ""
                if final_url.startswith("chrome-error://") or "chromewebdata" in final_url or "chrome-error://" in (html or ""):
                    await _close_browser_page(page, ctx, browser)
                    if attempt < retries:
                        await asyncio.sleep(random_sleep(base=3.0, jitter=1.5))
                        fingerprint = make_identity()
                        continue
                    return BrowseOutput(
                        success=False,
                        html="",
                        status_code=0,
                        reason="Browser navigation failed (navigation aborted / network reset)",
                        response_time=time.time() - start,
                        layer="layer5_full",
                        final_url=url,
                    )

                if status in (403, 503) or _looks_like_interstitial(html):
                    final_url = page.url
                    await _close_browser_page(page, ctx, browser)
                    res_status = status if status in (403, 429, 503) else 403
                    return BrowseOutput(
                        success=bool(html), html=html, status_code=res_status,
                        reason="Captured page still looks like an interstitial; challenge solving is unavailable.",
                        response_time=time.time() - start,
                        layer="layer5_full",
                        final_url=final_url,
                    )

                cookies = await ctx.cookies()
                final_url = page.url
                await _close_browser_page(page, ctx, browser)
                return BrowseOutput(
                    success=True, html=html, status_code=status,
                    reason="Captured via full browser",
                    response_time=time.time() - start,
                    layer="layer5_full",
                    cookies=cookies,
                    final_url=final_url,
                )
        except asyncio.CancelledError:
            await asyncio.shield(_close_browser_page(page, ctx, browser))
            raise
        except Exception as e:
            await _close_browser_page(page, ctx, browser)
            if attempt < retries:
                await asyncio.sleep(random_sleep(base=3.0, jitter=1.5))
                fingerprint = make_identity()
                continue

            return BrowseOutput(
                success=False, status_code=0,
                reason=f"Full browser capture failed: {type(e).__name__}",
                response_time=time.time() - start,
                layer="layer5_full",
                final_url=url,
            )


async def _act_human(page, fp, quick=False):
    vw, vh = fp.scr_w, fp.scr_h

    num_moves = random.randint(1, 3) if quick else random.randint(3, 6)

    pos = (vw // 2, vh // 2)

    for _ in range(num_moves):
        target = rand_point(vw, vh)

        path = fake_mouse_move(pos, target, num=random.randint(12, 25))

        for pt in path:
            try:
                await page.mouse.move(
                    max(0, min(pt[0], vw)),
                    max(0, min(pt[1], vh)),
                )
            except Exception:
                break
            await asyncio.sleep(random.uniform(0.005, 0.02))

        pos = target

        await asyncio.sleep(small_pause() + random.uniform(0.1, 0.5))



    if random.random() < 0.4 and not quick:
        try:
            links = await page.query_selector_all("a")
            if links:
                link = random.choice(links[:10])
                await link.scroll_into_view_if_needed()
                await asyncio.sleep(0.2)
                box = await link.bounding_box()
                if box:
                    target = (
                        int(box["x"] + box["width"] / 2),
                        int(box["y"] + box["height"] / 2),
                    )
                    path = fake_mouse_move(pos, target)
                    for pt in path:
                        try:
                            await page.mouse.move(
                                max(0, min(pt[0], vw)),
                                max(0, min(pt[1], vh)),
                            )
                        except Exception:
                            break
                        await asyncio.sleep(random.uniform(0.01, 0.03))
                    await asyncio.sleep(random_sleep(base=0.8, jitter=0.3))
        except Exception:
            pass



async def _scroll_around(page, fp):
    num_scrolls = random.randint(4, 10)

    for i in range(num_scrolls):
        dist = random.randint(150, 500)


        if i > 2 and random.random() < 0.2:
            dist = -random.randint(50, 200)

        try:
            await asyncio.wait_for(page.evaluate(f"window.scrollBy(0, {dist})"), timeout=2.0)
        except Exception:
            break

        pause = random_sleep(base=0.8, jitter=0.4)
        await asyncio.sleep(pause)


        try:
            at_bottom = await asyncio.wait_for(
                page.evaluate("(window.innerHeight + window.scrollY) >= document.body.scrollHeight - 50"),
                timeout=2.0,
            )
            if at_bottom and i > 2:
                break
        except Exception:
            break


async def _block_trackers(route, request):
    url = request.url.lower()

    async def finish(action):
        try:
            await action()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            message = str(exc).lower()
            expected_teardown = (
                "has been closed",
                "execution context was destroyed",
                "target page, context or browser has been closed",
                "most likely because of a navigation",
            )
            if not any(marker in message for marker in expected_teardown):
                raise

    for domain in TRACKER_DOMAINS:
        if domain in url:
            await finish(route.abort)
            return


    bad_paths = [
        "/fingerprint", "/fp.js", "/bot-detect",
        # NOTE: Do NOT block /cdn-cgi/challenge or /challenge-platform here!
        # Those are Cloudflare's own challenge scripts that we NEED to load
        # in order to pass verification and get the cf_clearance cookie.
    ]
    for path in bad_paths:
        if path in url:
            await finish(route.abort)
            return

    await finish(route.continue_)


_warning_patterns = [
    re.compile(r"access\s+denied", re.I),
    re.compile(r"blocked", re.I),
    re.compile(r"captcha", re.I),
    re.compile(r"checking your browser", re.I),
    re.compile(r"cf-browser-verification", re.I),
    re.compile(r"just a moment", re.I),
]

_strong_warning_patterns = [
    re.compile(r"px-captcha", re.I),
    re.compile(r"datadome", re.I),
    re.compile(r"geo\.captcha", re.I),
    re.compile(r"cf-turnstile", re.I),
    re.compile(r"security alert\s+alert\s+request blocked", re.I),
    re.compile(r"<title>\s*pardon our interruption", re.I),
    re.compile(r"as you were browsing something about your browser made us think you were a bot", re.I),
    re.compile(r"humans only", re.I),
    re.compile(r"pardon our interruption", re.I),
    re.compile(r"error page\s*\|\s*ebay", re.I),
    re.compile(r"something went wrong on our end", re.I),
    re.compile(r"oops!!?\s+something went wrong", re.I),
]

_interstitial_patterns = [
    re.compile(r"<title>\s*just a moment", re.I),
    re.compile(r"^\s*403\s+forbidden\s+403\s+forbidden", re.I),
    re.compile(r"<title>\s*access denied\s*-\s*macy", re.I),
    re.compile(r"<title>\s*403\s*-\s*access denied\s*\|\s*edmunds", re.I),
    re.compile(r"403\s+access denied.*you don[â€™']t have permission to access this page", re.I | re.S),
    re.compile(r"you don[â€™']t have access to this page", re.I),
    re.compile(r"humans only", re.I),
    re.compile(r"cf-turnstile", re.I),
    re.compile(r"error page\s*\|\s*ebay", re.I),
    re.compile(r"something went wrong on our end", re.I),
    re.compile(r"<title>\s*error page\s*</title>.*oops!!?\s+something went wrong", re.I | re.S),
]

_challenge_shell_patterns = [
    re.compile(r"robot or human\?.*activate and hold the button", re.I | re.S),
    re.compile(r"are you a person or a robot", re.I),
    re.compile(r"press & hold", re.I),
]

def _looks_like_interstitial(html):
    if not html:
        return True

    snippet = html[:50000]
    if any(p.search(snippet) for p in _interstitial_patterns):
        return True

    shell_challenge = any(p.search(snippet) for p in _challenge_shell_patterns)
    if shell_challenge:
        from bs4 import BeautifulSoup
        text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
        if len(text) < 1500:
            return True

    hits = sum(1 for p in _warning_patterns if p.search(snippet))
    strong_hit = any(p.search(snippet) for p in _strong_warning_patterns)


    if hits >= 2 or strong_hit:
        # If the HTML is massive, it means the real content loaded and the CAPTCHA is just an overlay.
        if len(html) > 300000:
            return False

        if len(html) > 20000:
            # Check if strong hit is just a script tag in a successfully loaded page
            if "js.ddocdn.com" in html or "datadome.js" in html or "perimeterx" in html:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "lxml")
                text = soup.get_text(strip=True)
                if len(text) > 1000:
                    return False
            if not strong_hit:
                return False
        return True

    return False
