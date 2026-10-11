import asyncio
import html as html_lib
import json
import os
import re
import time
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from .proxy_config import AsyncSession, browser_proxy_options

from .browser_scraping import (
    _act_human,
    _browser_headless,
    _browser_launch_options,
    _capture_html,
    _do_scroll,
    _looks_like_interstitial,
    _scroll_around,
    _wait_loaded,
    full_scrape,
)
from .stealth_js import inject_helper_scripts, inject_stealth_scripts
from .browser_binary import find_chromium_binary
from .browser_network_guard import (
    install_browser_network_guard,
    service_worker_context_options,
)
from .safe_http import max_response_bytes, safe_get, safe_request


CHROME120_MAC_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

STABLE_HARD_SOURCES = {
    "bestbuy": "Akamai",
    "ebay": "Akamai",
    "bloomberg": "PerimeterX/HUMAN",
    "tripadvisor": "DataDome",
}

BEST_EFFORT_HARD_SOURCES = {
    "walmart": "Timed out in the 2026-07-11 verification sample",
    "zillow": "Timed out in the 2026-07-11 verification sample",
    "homedepot": "Returned a labeled 403 error shell in the 2026-07-11 verification sample",
}

EXPERIMENTAL_HARD_SOURCES = {}

CONDITIONAL_HARD_SOURCES = {
    "belk": "Requires scratch/invisible_probe_env; generic fallback remains available",
}


def _enabled_env(name, default=True):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def hard_sources_enabled():
    if not _enabled_env("YADA_HARD_SOURCES", True):
        return False
    return _enabled_env("YADA_EXPERIMENTAL_PX", True)


def experimental_hard_sources_enabled():
    return bool(EXPERIMENTAL_HARD_SOURCES) and hard_sources_enabled() and _enabled_env("YADA_EXPERIMENTAL_HARD_SOURCES", False)


def hard_source_status():
    return {
        "enabled": hard_sources_enabled(),
        "stable": STABLE_HARD_SOURCES,
        "best_effort": BEST_EFFORT_HARD_SOURCES,
        "experimental_enabled": experimental_hard_sources_enabled(),
        "experimental": EXPERIMENTAL_HARD_SOURCES,
        "conditional": CONDITIONAL_HARD_SOURCES,
    }


@dataclass
class SourceResult:
    success: bool
    html: str = ""
    status_code: int = 0
    reason: str = ""
    response_time: float = 0.0
    final_url: str = ""
    cookies: list | None = None


def source_name(url):
    try:
        parsed_url = urlparse(url)
        host = (parsed_url.hostname or "").lower().rstrip(".")
        path = parsed_url.path.rstrip("/").lower()
    except Exception:
        return None

    if host == "medium.com" or host.endswith(".medium.com"):
        return "medium"

    if host == "quora.com" or host.endswith(".quora.com"):
        return "quora"

    if host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be":
        return "youtube"

    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return "linkedin"

    if host == "instagram.com" or host.endswith(".instagram.com"):
        return "instagram"

    if host == "reddit.com" or host.endswith(".reddit.com"):
        return "reddit"

    if host == "news.ycombinator.com":
        return "hackernews"

    if host == "github.com" or host.endswith(".github.com"):
        return "github"

    if host == "arxiv.org" or host.endswith(".arxiv.org"):
        return "arxiv"

    if host == "crates.io" or host.endswith(".crates.io"):
        return "crates"

    if host == "semanticscholar.org" or host.endswith(".semanticscholar.org"):
        return "semantic_scholar"

    if host == "pubmed.ncbi.nlm.nih.gov" or host.endswith(".ncbi.nlm.nih.gov"):
        return "pubmed"

    if host == "aclanthology.org" or host.endswith(".aclanthology.org"):
        return "acl_anthology"

    if host == "stackoverflow.com" or host.endswith(".stackoverflow.com") or host.endswith(".stackexchange.com"):
        return "stackexchange"

    if host == "remotive.com" or host.endswith(".remotive.com"):
        return "remotive"

    if host == "clinicaltrials.gov" or host.endswith(".clinicaltrials.gov"):
        return "clinicaltrials"

    if host == "meta.discourse.org" and path == "/latest":
        return "discourse"

    if host == "mastodon.social" and path == "/explore":
        return "mastodon"

    if host == "sec.gov" or host.endswith(".sec.gov"):
        return "sec"

    if hard_sources_enabled():
        if host == "walmart.com" or host.endswith(".walmart.com"):
            return "walmart"
        if host == "bestbuy.com" or host.endswith(".bestbuy.com"):
            return "bestbuy"
        if host == "ebay.com" or host.endswith(".ebay.com"):
            return "ebay"
        if host == "zillow.com" or host.endswith(".zillow.com"):
            return "zillow"
        if host == "homedepot.com" or host.endswith(".homedepot.com"):
            return "homedepot"
        if host == "bloomberg.com" or host.endswith(".bloomberg.com"):
            return "bloomberg"
        if host == "tripadvisor.com" or host.endswith(".tripadvisor.com"):
            return "tripadvisor"

    if host == "belk.com" or host.endswith(".belk.com"):
        return "belk"

    px_domains = [
        "macys.com", "bloomingdales.com", "target.com", "stubhub.com", 
        "finishline.com", "footlocker.com", "goat.com", "kith.com", 
        "dsw.com", "homedepot.com", "saksfifthavenue.com", "nordstrom.com", 
        "upwork.com", "fubo.tv", "sneakersnstuff.com", "ssense.com", "stockx.com",
        "bloomberg.com", "tripadvisor.com",
        "crunchbase.com", "businessnewsdaily.com", "congress.gov", "math.stackexchange.com",
        "scholar.google.com", "greenhouse.io"
    ]
    if any(host == d or host.endswith(f".{d}") for d in px_domains):
        return "perimeterx"

    return None


async def source_scrape(url, cookies=None, structured=False, solve_challenges=False, memory=None, allow_browser=True):
    name = source_name(url)
    if name == "medium":
        path = urlparse(url).path.lower()
        if path.startswith(("/tag/", "/topic/")):
            return await scrape_medium(url)
        return await scrape_medium(url)
    if name == "quora":
        if not allow_browser:
            return await scrape_quora_static(url)
        return await scrape_quora(url)
    if name == "youtube":
        if _is_youtube_watch_url(url):
            return await scrape_youtube(url)
        return await scrape_youtube(url)
    if name == "linkedin":
        if structured:
            return await scrape_linkedin(url)
        return await scrape_linkedin(url)
    if name == "instagram":
        if structured:
            results = await scrape_instagram_bulk([url])
            return results[0] if results else None
        return await scrape_instagram(url)
    if name == "reddit":
        if memory is not None:
            return await scrape_reddit(url, cookies=cookies, memory=memory)
        return await scrape_reddit(url, cookies=cookies)
    if name == "hackernews":
        return await scrape_hackernews(url)
    if name in {"semantic_scholar", "pubmed", "github", "arxiv", "acl_anthology"} and not structured:
        # Structured/approximate implementations remain available to bulk
        # dispatch, while exact single pages use the richer generic receiver.
        return None
    if name == "semantic_scholar":
        return await scrape_semantic_scholar(url)
    if name == "pubmed":
        return await scrape_pubmed(url)
    if name == "crates":
        return await scrape_crates(url)
    if name in {"github", "arxiv", "acl_anthology"}:
        return await scrape_research_page(url, name)
    if name == "stackexchange":
        return await scrape_stackexchange(url)
    if name == "remotive":
        return await scrape_remotive(url)
    if name == "clinicaltrials":
        return await scrape_clinicaltrials(url)
    if name == "discourse":
        if not urlparse(url).path.rstrip("/").endswith("/latest"):
            return None
        return await scrape_discourse(url)
    if name == "mastodon":
        if not urlparse(url).path.rstrip("/").endswith("/explore"):
            return None
        return await scrape_mastodon(url)
    if name == "sec":
        return await scrape_static_http_source(url, name)
    if name == "walmart":
        return await scrape_walmart(url, cookies=cookies, solve_challenges=solve_challenges)
    if name == "bestbuy":
        return await scrape_bestbuy(url, cookies=cookies, solve_challenges=solve_challenges)
    if name == "ebay":
        return await scrape_ebay(url, cookies=cookies, solve_challenges=solve_challenges)
    if name == "zillow":
        return await scrape_zillow(url, cookies=cookies, solve_challenges=solve_challenges)
    if name == "homedepot":
        return await scrape_homedepot(url, cookies=cookies, solve_challenges=solve_challenges)
    if name == "bloomberg":
        return await scrape_bloomberg(url, cookies=cookies, solve_challenges=solve_challenges)
    if name == "tripadvisor":
        return await scrape_tripadvisor(url, cookies=cookies, solve_challenges=solve_challenges)
    if name == "perimeterx":
        return await scrape_perimeterx(url, cookies=cookies, solve_challenges=solve_challenges)
    if name == "belk":
        return await scrape_belk(url, cookies=cookies, solve_challenges=solve_challenges)
    return None


async def source_bulk_scrape(
    urls,
    cookies=None,
    solve_challenges=False,
    memory=None,
    allow_browser=True,
):
    urls = list(urls or [])
    if not urls:
        return []

    name = source_name(urls[0])
    if not name or any(source_name(url) != name for url in urls):
        return None

    if name == "walmart":
        return await scrape_walmart_bulk(
            urls,
            cookies=cookies,
            solve_challenges=solve_challenges,
        )
    if name == "bestbuy":
        return await scrape_bestbuy_bulk(
            urls,
            cookies=cookies,
            solve_challenges=solve_challenges,
        )
    if name == "quora":
        if not allow_browser:
            return await scrape_quora_static_bulk(urls)
        return await scrape_quora_bulk(urls)
    if name == "reddit":
        # Unsupported Reddit pages need the generic receiver.  The dedicated
        # route only handles URLs that resolve to a JSON endpoint.
        for url in urls:
            api_url = reddit_json_url(url)
            if not api_url or not urlparse(api_url).path.lower().endswith(".json"):
                return None
        if memory is not None:
            return await scrape_reddit_bulk(urls, cookies=cookies, memory=memory)
        return await scrape_reddit_bulk(urls, cookies=cookies)
    if name == "hackernews":
        return await scrape_hackernews_bulk(urls)
    if name == "semantic_scholar":
        # Crossref-backed results are approximate and must remain opt-in via
        # explicit structured mode rather than replacing exact auto capture.
        return None
    if name == "pubmed":
        return await scrape_pubmed_bulk(urls)
    if name == "crates":
        return await scrape_crates_bulk(urls)
    if name in {"github", "arxiv", "acl_anthology"}:
        # Compact research bulk remains available through explicit structured
        # mode; ordinary auto bulk keeps the richer generic receiver.
        return None
    if name == "medium":
        return await scrape_http_meta_bulk(urls, "medium")
    if name == "youtube":
        return await scrape_youtube_bulk(urls)
    if name == "linkedin":
        return await scrape_linkedin_bulk(urls)
    if name == "instagram":
        return await scrape_instagram_bulk(urls)
    if name == "stackexchange":
        return await scrape_stackexchange_bulk(urls)
    if name == "remotive":
        return await scrape_remotive_bulk(urls)
    if name == "clinicaltrials":
        return await scrape_clinicaltrials_bulk(urls)
    if name == "discourse":
        return await scrape_discourse_bulk(urls)
    if name == "mastodon":
        return await scrape_mastodon_bulk(urls)
    if name in {"sec", "zacks", "monster"}:
        return await scrape_static_http_source_bulk(urls, name)
    if name == "pytorch":
        return await scrape_pytorch_docs_bulk(urls)
    if name == "ebay":
        return await scrape_ebay_bulk(
            urls,
            cookies=cookies,
            solve_challenges=solve_challenges,
        )
    if name == "zillow":
        return await scrape_zillow_bulk(
            urls,
            cookies=cookies,
            solve_challenges=solve_challenges,
        )
    if name == "homedepot":
        return await scrape_homedepot_bulk(urls, cookies=cookies, solve_challenges=solve_challenges)
    if name == "bloomberg":
        return await scrape_bloomberg_bulk(urls, cookies=cookies, solve_challenges=solve_challenges)
    if name == "tripadvisor":
        return await scrape_tripadvisor_bulk(urls, cookies=cookies, solve_challenges=solve_challenges)
    if name == "perimeterx":
        return await scrape_perimeterx_bulk(
            urls,
            cookies=cookies,
            solve_challenges=solve_challenges,
        )

    return None


def _title_from_html(html):
    match = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    if not match:
        return ""
    return BeautifulSoup(match.group(1), "lxml").get_text(" ", strip=True)


def _is_youtube_watch_url(url):
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        return ("youtube.com" in host and parsed.path == "/watch") or host == "youtu.be"
    except Exception:
        return False


def _clean_line(text):
    return re.sub(r"\s+", " ", html_lib.unescape(text or "")).strip()


def _node_text(node):
    return _clean_line(node.get_text(" ", strip=True)) if node else ""


def _html_doc(title, url, sections):
    parts = [
        "<!doctype html>",
        "<html><head>",
        f"<title>{html_lib.escape(title or url)}</title>",
        "</head><body>",
        f"<h1>{html_lib.escape(title or url)}</h1>",
        f'<p><a href="{html_lib.escape(url)}">{html_lib.escape(url)}</a></p>',
    ]
    for heading, values in sections:
        values = [v for v in values if v]
        if not values:
            continue
        parts.append(f"<h2>{html_lib.escape(heading)}</h2>")
        for value in values:
            parts.append(f"<p>{html_lib.escape(value)}</p>")
    parts.append("</body></html>")
    return "\n".join(parts)


def _meta_value(soup, *names):
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return _clean_line(tag.get("content"))
    return ""


def _json_ld_lines(soup, limit=8):
    lines = []
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = node.string or node.get_text("", strip=False)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("@type", "name", "headline", "description", "brand", "sku"):
                value = item.get(key)
                if isinstance(value, dict):
                    value = value.get("name")
                if value:
                    lines.append(f"{key}: {_clean_line(str(value))}")
            offers = item.get("offers")
            if isinstance(offers, dict):
                price = offers.get("price") or offers.get("lowPrice")
                currency = offers.get("priceCurrency")
                if price:
                    price_line = f"price: {_clean_line(str(price))} {_clean_line(str(currency or ''))}".strip()
                    lines.append(price_line)
            if len(lines) >= limit:
                return lines[:limit]
    return lines[:limit]


def _compact_meta_html(html, url, family):
    soup = BeautifulSoup(html or "", "lxml")
    title = _meta_value(soup, "og:title", "twitter:title") or _title_from_html(html) or url
    description = _meta_value(soup, "og:description", "twitter:description", "description")
    image = _meta_value(soup, "og:image", "twitter:image")
    canonical = ""
    link = soup.find("link", attrs={"rel": "canonical"})
    if link and link.get("href"):
        canonical = link.get("href")

    lines = []
    if description:
        lines.append(description)
    if canonical and canonical != url:
        lines.append(canonical)
    if family == "instagram" and image:
        lines.append(f"Image URL: {image}")

    for tag in soup.find_all(["h1", "h2"], limit=8):
        text = _clean_line(tag.get_text(" ", strip=True))
        if text and text not in lines:
            lines.append(text)
    lines.extend(_json_ld_lines(soup))

    page_text = soup.get_text(" ", strip=True)
    if len(" ".join(lines)) < 180:
        for text in re.split(r"\s{2,}", page_text):
            text = _clean_line(text)
            if 40 <= len(text) <= 280 and text not in lines:
                lines.append(text)
            if len(" ".join(lines)) >= 500:
                break

    lowered = page_text[:3000].lower()
    blocked_markers = [
        "access denied",
        "robot or human",
        "unusual traffic",
        "please verify",
        "security check",
        "something went wrong",
        "try again later",
        "e002 retry",
        "login â€¢ instagram",
        "authwall",
        "checkpoint",
    ]
    blocked = any(marker in lowered for marker in blocked_markers)

    combined = " ".join([title, description, page_text[:2000]]).lower()
    enough = len(" ".join(lines)) >= 120 or bool(title and description)
    if family == "instagram":
        enough = enough and ("followers" in description.lower() or "instagram" not in title.lower())
    if family == "linkedin":
        enough = enough and "sign in" not in title.lower()
    if family == "flipkart":
        enough = enough and "flipkart" in combined
    if family == "youtube" and "/results" in urlparse(url).path:
        footer_only = "about press copyright contact us creators advertise" in combined
        enough = enough and not footer_only

    return _html_doc(title, url, [("Extracted", lines)]), bool(enough and not blocked)


async def _scrape_http_meta_page_with_session(session, url, family, start):
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": CHROME120_MAC_UA,
    }
    try:
        response = await safe_get(session, url, headers=headers)
        raw = response.text or ""
        html, ok = _compact_meta_html(raw, str(response.url or url), family)
        if family == "medium":
            article = medium_article_from_state(raw, str(response.url or url))
            if article:
                html = article
                ok = True
        return SourceResult(
            ok and response.status_code < 400,
            html=html or raw,
            status_code=response.status_code,
            reason=(
                f"{family} public HTTP metadata source path"
                if ok
                else f"{family} public HTTP source path returned thin or blocked content"
            ),
            response_time=time.time() - start,
            final_url=str(response.url or url),
        )
    except Exception as exc:
        return SourceResult(
            False,
            status_code=0,
            reason=f"{family} public HTTP source path failed: {type(exc).__name__}",
            response_time=time.time() - start,
            final_url=url,
        )


async def scrape_http_meta_page(url, family, timeout=15.0):
    start = time.time()
    async with AsyncSession(impersonate="chrome120", timeout=timeout) as session:
        return await _scrape_http_meta_page_with_session(session, url, family, start)


async def scrape_http_meta_bulk(urls, family, timeout=15.0):
    results = [None] * len(urls)
    sem = asyncio.Semaphore(8)

    async def one(session, idx, url):
        async with sem:
            start = time.time()
            results[idx] = await _scrape_http_meta_page_with_session(session, url, family, start)

    async with AsyncSession(impersonate="chrome120", timeout=timeout, max_clients=min(10, max(1, len(urls)))) as session:
        await asyncio.gather(*(one(session, idx, url) for idx, url in enumerate(urls)))
    return results


async def _fetch_public_html(url, timeout=15.0):
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": CHROME120_MAC_UA,
    }
    async with AsyncSession(impersonate="chrome120", timeout=timeout) as session:
        response = await safe_get(session, url, headers=headers)
        return response, response.text or ""


def _useful_paragraphs(soup, limit=18):
    lines = []
    for tag in soup.find_all(["p", "li"], limit=120):
        text = _clean_line(tag.get_text(" ", strip=True))
        if 60 <= len(text) <= 1500 and text not in lines:
            lines.append(text)
        if len(lines) >= limit:
            break
    return lines


def research_html_from_page(raw, url, family):
    parser = "xml" if (raw or "").lstrip().startswith("<?xml") else "lxml"
    soup = BeautifulSoup(raw or "", parser)
    title = _meta_value(soup, "og:title", "twitter:title", "citation_title") or _title_from_html(raw) or url
    description = _meta_value(soup, "og:description", "twitter:description", "description", "citation_abstract")
    lines = []
    meta_lines = []

    if family == "arxiv":
        for selector in ("h1.title", "div.authors", "div.dateline", "td.tablecell.subjects", "blockquote.abstract"):
            node = soup.select_one(selector)
            if node:
                text = _clean_line(node.get_text(" ", strip=True))
                if text:
                    meta_lines.append(text)
        for result in soup.select("li.arxiv-result")[:12]:
            text = _clean_line(result.get_text(" ", strip=True))
            if text:
                lines.append(text[:1400])

    elif family == "pubmed":
        for selector in ("h1.heading-title", "div.authors-list", "button.journal-actions-trigger", "div.abstract"):
            node = soup.select_one(selector)
            if node:
                text = _clean_line(node.get_text(" ", strip=True))
                if text:
                    meta_lines.append(text)

    elif family == "acl_anthology":
        for selector in ("h1", ".card-title", "#abstract", "div.acl-abstract", "dl"):
            node = soup.select_one(selector)
            if node:
                text = _clean_line(node.get_text(" ", strip=True))
                if text:
                    meta_lines.append(text)

    elif family == "semantic_scholar":
        for selector in ("h1", "[data-selenium-selector='paper-detail-title']", ".abstract", "[data-selenium-selector='text-truncator-text']"):
            node = soup.select_one(selector)
            if node:
                text = _clean_line(node.get_text(" ", strip=True))
                if text:
                    meta_lines.append(text)

    elif family == "github":
        for selector in ("strong[itemprop='name'] a", "bdi", ".js-issue-title", "h1"):
            node = soup.select_one(selector)
            if node:
                text = _clean_line(node.get_text(" ", strip=True))
                if text:
                    meta_lines.append(text)
        for selector in (
            "article.markdown-body",
            "#readme",
            "[data-testid='issue-viewer-issue-container']",
            ".repository-content",
        ):
            node = soup.select_one(selector)
            if node:
                text = _clean_line(node.get_text(" ", strip=True))
                if text:
                    lines.append(text[:5000])
                    break

    for tag in soup.find_all(["h1", "h2", "h3"], limit=12):
        text = _clean_line(tag.get_text(" ", strip=True))
        if text and text not in meta_lines:
            meta_lines.append(text)

    if description:
        lines.append(description)
    lines.extend(meta_lines)
    lines.extend(_json_ld_lines(soup, limit=12))
    lines.extend(_useful_paragraphs(soup))

    clean = []
    seen = set()
    for line in lines:
        line = _clean_line(line)
        key = line.lower()
        if not line or key in seen:
            continue
        seen.add(key)
        clean.append(line)

    page_text = soup.get_text(" ", strip=True)
    lowered = page_text[:3000].lower()
    blocked = any(marker in lowered for marker in ("access denied", "captcha", "enable javascript", "verify you are human"))
    enough = len(" ".join(clean)) >= 220 or bool(title and description)
    return _html_doc(title, url, [("Extracted", clean[:32])]), bool(enough and not blocked)


async def scrape_research_page(url, family, timeout=15.0):
    start = time.time()
    try:
        response, raw = await _fetch_public_html(url, timeout=timeout)
        html, ok = research_html_from_page(raw, str(response.url or url), family)
        return SourceResult(
            ok and response.status_code < 400,
            html=html if html else raw,
            status_code=response.status_code,
            reason=f"{family} public research source path" if ok else f"{family} research source path returned thin content",
            response_time=time.time() - start,
            final_url=str(response.url or url),
        )
    except Exception as exc:
        return SourceResult(
            False,
            status_code=0,
            reason=f"{family} research source path failed: {type(exc).__name__}",
            response_time=time.time() - start,
            final_url=url,
        )


async def scrape_research_bulk(urls, family, timeout=15.0):
    results = [None] * len(urls)
    sem = asyncio.Semaphore(8)

    async def one(idx, url):
        async with sem:
            results[idx] = await scrape_research_page(url, family, timeout=timeout)

    await asyncio.gather(*(one(idx, url) for idx, url in enumerate(urls)))
    return results


def semantic_scholar_query_from_url(url):
    try:
        parsed = urlparse(url)
    except Exception:
        return ""

    parts = [part for part in parsed.path.split("/") if part]
    if "paper" in parts:
        idx = parts.index("paper")
        if idx + 1 < len(parts):
            slug = parts[idx + 1]
            return _clean_line(slug.replace("-", " "))
    return ""


def crossref_item_to_html(item, url):
    title = _clean_line(" ".join(item.get("title") or [])) or url
    abstract = _clean_line(BeautifulSoup(item.get("abstract") or "", "lxml").get_text(" ", strip=True))
    authors = []
    for author in (item.get("author") or [])[:10]:
        given = _clean_line(author.get("given") or "")
        family = _clean_line(author.get("family") or "")
        name = " ".join(part for part in (given, family) if part)
        if name:
            authors.append(name)

    published = ""
    date_parts = ((item.get("published-print") or item.get("published-online") or item.get("issued") or {}).get("date-parts") or [])
    if date_parts and date_parts[0]:
        published = "-".join(str(part) for part in date_parts[0])

    details = []
    for label, value in (
        ("Authors", ", ".join(authors)),
        ("Venue", " ".join(item.get("container-title") or [])),
        ("Publisher", item.get("publisher")),
        ("Published", published),
        ("DOI", item.get("DOI")),
        ("Crossref URL", item.get("URL")),
    ):
        value = _clean_line(str(value or ""))
        if value:
            details.append(f"{label}: {value}")

    summary = [abstract] if abstract else []
    return _html_doc(title, url, [("Paper Metadata", details), ("Abstract", summary)])


async def scrape_semantic_scholar(url, timeout=15.0):
    start = time.time()
    query = semantic_scholar_query_from_url(url)
    if len(query) < 8:
        return SourceResult(
            False,
            status_code=0,
            reason="Semantic Scholar URL did not expose a usable paper title",
            response_time=time.time() - start,
            final_url=url,
        )

    api_url = f"https://api.crossref.org/works?{urlencode({'query.title': query, 'rows': 1})}"
    headers = {
        "Accept": "application/json",
        "User-Agent": "YADA-Scraper-API/1.0",
    }
    try:
        async with AsyncSession(timeout=timeout) as session:
            response = await safe_get(session, api_url, headers=headers)
        data = json.loads(response.text or "{}")
        items = (((data.get("message") or {}).get("items")) or [])
        item = items[0] if items else {}
        html = crossref_item_to_html(item, url) if item else ""
        title = _clean_line(" ".join(item.get("title") or [])) if item else ""
        abstract = item.get("abstract") if item else ""
        ok = response.status_code < 400 and bool(title and (abstract or item.get("DOI")))
        return SourceResult(
            ok,
            html=html,
            status_code=response.status_code,
            reason="Semantic Scholar Crossref metadata source path" if ok else "Semantic Scholar metadata path returned thin content",
            response_time=time.time() - start,
            final_url=url,
        )
    except Exception as exc:
        return SourceResult(
            False,
            status_code=0,
            reason=f"Semantic Scholar metadata path failed: {type(exc).__name__}",
            response_time=time.time() - start,
            final_url=url,
        )


async def scrape_semantic_scholar_bulk(urls, timeout=15.0):
    results = [None] * len(urls)
    sem = asyncio.Semaphore(6)

    async def one(idx, url):
        async with sem:
            results[idx] = await scrape_semantic_scholar(url, timeout=timeout)

    await asyncio.gather(*(one(idx, url) for idx, url in enumerate(urls)))
    return results


def pubmed_id_from_url(url):
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    match = re.search(r"/(\d{6,12})(?:/|$)", parsed.path)
    return match.group(1) if match else ""


def pubmed_xml_to_html(raw, url):
    soup = BeautifulSoup(raw or "", "xml")
    article = soup.find("PubmedArticle") or soup.find("PubmedBookArticle")
    if not article:
        return "", False

    title = _node_text(article.find("ArticleTitle") or article.find("BookTitle"))
    abstract_lines = []
    for node in article.find_all("AbstractText"):
        label = node.get("Label")
        text = _clean_line(node.get_text(" ", strip=True))
        if text:
            abstract_lines.append(f"{label}: {text}" if label else text)

    authors = []
    for author in article.find_all("Author")[:14]:
        last = _node_text(author.find("LastName"))
        fore = _node_text(author.find("ForeName"))
        collective = _node_text(author.find("CollectiveName"))
        name = " ".join(part for part in (fore, last) if part) or collective
        if name:
            authors.append(name)

    details = []
    journal = _node_text(article.find("Journal").find("Title") if article.find("Journal") else None)
    year = _node_text(article.find("PubDate"))
    doi = ""
    pmid = _node_text(article.find("PMID"))
    for node in article.find_all("ArticleId"):
        if (node.get("IdType") or "").lower() == "doi":
            doi = _clean_line(node.get_text(" ", strip=True))
            break

    for label, value in (
        ("PMID", pmid),
        ("Authors", ", ".join(authors)),
        ("Journal", journal),
        ("Published", year),
        ("DOI", doi),
    ):
        if value:
            details.append(f"{label}: {value}")

    ok = bool(title and (abstract_lines or details))
    return _html_doc(title or url, url, [("Article Metadata", details), ("Abstract", abstract_lines)]), ok


async def scrape_pubmed(url, timeout=15.0):
    start = time.time()
    pmid = pubmed_id_from_url(url)
    if not pmid:
        return await scrape_research_page(url, "pubmed", timeout=timeout)

    api_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urlencode(
        {"db": "pubmed", "id": pmid, "retmode": "xml"}
    )
    try:
        async with AsyncSession(timeout=timeout) as session:
            response = await safe_get(session, api_url, headers={"Accept": "application/xml"})
        html, ok = pubmed_xml_to_html(response.text or "", url)
        return SourceResult(
            ok and response.status_code < 400,
            html=html,
            status_code=response.status_code,
            reason="PubMed NCBI XML source path" if ok else "PubMed XML source path returned thin content",
            response_time=time.time() - start,
            final_url=url,
        )
    except Exception:
        fallback = await scrape_research_page(url, "pubmed", timeout=timeout)
        fallback.reason = f"PubMed XML source path failed; {fallback.reason}"
        return fallback


async def scrape_pubmed_bulk(urls, timeout=15.0):
    results = [None] * len(urls)
    sem = asyncio.Semaphore(6)

    async def one(idx, url):
        async with sem:
            results[idx] = await scrape_pubmed(url, timeout=timeout)

    await asyncio.gather(*(one(idx, url) for idx, url in enumerate(urls)))
    return results


def crate_name_from_url(url):
    try:
        parts = [part for part in urlparse(url).path.split("/") if part]
    except Exception:
        return ""
    if len(parts) >= 2 and parts[0] == "crates":
        return parts[1]
    return ""


def crates_json_to_html(data, url):
    crate = (data or {}).get("crate") or {}
    versions = (data or {}).get("versions") or []
    name = _clean_line(crate.get("name") or crate.get("id") or url)
    description = _clean_line(crate.get("description") or "")
    latest = versions[0] if versions else {}
    lines = []
    for label, value in (
        ("Name", name),
        ("Description", description),
        ("Downloads", crate.get("downloads")),
        ("Recent downloads", crate.get("recent_downloads")),
        ("Latest version", latest.get("num") or crate.get("max_version")),
        ("Repository", crate.get("repository")),
        ("Homepage", crate.get("homepage")),
        ("Documentation", crate.get("documentation")),
        ("License", latest.get("license") or crate.get("license")),
        ("Updated", crate.get("updated_at")),
    ):
        value = _clean_line(str(value or ""))
        if value:
            lines.append(f"{label}: {value}")
    keywords = [item.get("keyword") for item in (data.get("keywords") or []) if isinstance(item, dict)]
    categories = [item.get("category") for item in (data.get("categories") or []) if isinstance(item, dict)]
    if keywords:
        lines.append("Keywords: " + ", ".join(_clean_line(value) for value in keywords if value))
    if categories:
        lines.append("Categories: " + ", ".join(_clean_line(value) for value in categories if value))
    return _html_doc(name or "crates.io", url, [("Crate", lines)])


async def scrape_crates(url, timeout=10.0):
    start = time.time()
    name = crate_name_from_url(url)
    if not name:
        return await scrape_research_page(url, "crates", timeout=timeout)
    api_url = f"https://crates.io/api/v1/crates/{name}"
    headers = {
        "Accept": "application/json",
        "User-Agent": CHROME120_MAC_UA,
    }
    try:
        async with AsyncSession(impersonate="chrome120", timeout=timeout) as session:
            response = await safe_get(session, api_url, headers=headers)
        data = json.loads(response.text or "{}")
        html = crates_json_to_html(data, url)
        ok = response.status_code < 400 and bool((data.get("crate") or {}).get("name"))
        return SourceResult(
            ok,
            html=html if ok else response.text,
            status_code=response.status_code,
            reason="crates.io public API source path" if ok else "crates.io API source path returned thin content",
            response_time=time.time() - start,
            final_url=url,
        )
    except Exception as exc:
        return SourceResult(
            False,
            status_code=0,
            reason=f"crates.io source path failed: {type(exc).__name__}",
            response_time=time.time() - start,
            final_url=url,
        )


async def scrape_crates_bulk(urls, timeout=10.0):
    results = [None] * len(urls)
    sem = asyncio.Semaphore(8)

    async def one(idx, url):
        async with sem:
            results[idx] = await scrape_crates(url, timeout=timeout)

    await asyncio.gather(*(one(idx, url) for idx, url in enumerate(urls)))
    return results


def hackernews_html_from_page(raw, url):
    soup = BeautifulSoup(raw or "", "lxml")
    title = _title_from_html(raw) or "Hacker News"
    story_lines = []
    comment_lines = []

    for row in soup.select("tr.athing")[:40]:
        title_node = row.select_one(".titleline a") or row.select_one("a.storylink")
        if not title_node:
            continue
        text = _clean_line(title_node.get_text(" ", strip=True))
        href = title_node.get("href") or ""
        if href and href.startswith("item?"):
            href = urljoin("https://news.ycombinator.com/", href)
        if text:
            story_lines.append(f"{text} {href}".strip())

    for row in soup.select("tr.comtr")[:80]:
        author_node = row.select_one(".hnuser")
        text_node = row.select_one(".commtext")
        if not text_node:
            continue
        body = _clean_line(text_node.get_text(" ", strip=True))
        author = _clean_line(author_node.get_text(" ", strip=True)) if author_node else ""
        if body:
            comment_lines.append(f"{author}: {body}" if author else body)

    if not story_lines:
        for node in soup.select(".titleline a, a.storylink")[:40]:
            text = _clean_line(node.get_text(" ", strip=True))
            href = node.get("href") or ""
            if text:
                story_lines.append(f"{text} {href}".strip())

    ok = bool(story_lines or comment_lines)
    return _html_doc(title, url, [("Stories", story_lines), ("Comments", comment_lines)]), ok


def hackernews_item_id(url):
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    if (parsed.hostname or "").lower().rstrip(".") != "news.ycombinator.com":
        return ""
    if parsed.path.rstrip("/") != "/item":
        return ""
    item_id = (parse_qs(parsed.query).get("id") or [""])[0]
    return item_id if item_id.isdigit() else ""


async def _hackernews_api_get(session, endpoint):
    response = await safe_get(session, f"https://hacker-news.firebaseio.com/v0/{endpoint}", timeout=10)
    if response.status_code >= 400:
        return None
    try:
        return json.loads(response.text or "null")
    except Exception:
        return None


def _hackernews_item_text(item):
    if not isinstance(item, dict):
        return ""
    return _clean_line(BeautifulSoup(item.get("text") or "", "lxml").get_text(" ", strip=True))


def _hackernews_item_line(item):
    title = _clean_line((item or {}).get("title") or "")
    if not title:
        return ""
    by = _clean_line((item or {}).get("by") or "")
    score = (item or {}).get("score")
    comments = (item or {}).get("descendants")
    href = (item or {}).get("url") or f"https://news.ycombinator.com/item?id={(item or {}).get('id')}"
    bits = [title]
    if by:
        bits.append(f"by {by}")
    if score is not None:
        bits.append(f"{score} points")
    if comments is not None:
        bits.append(f"{comments} comments")
    bits.append(href)
    return " | ".join(bits)


async def hackernews_api_html(url, session, include_comments=True):
    item_id = hackernews_item_id(url)
    if item_id:
        item = await _hackernews_api_get(session, f"item/{item_id}.json")
        if not isinstance(item, dict):
            return "", False

        story_lines = [_hackernews_item_line(item)]
        text = _hackernews_item_text(item)
        if text:
            story_lines.append(text)

        comments = []
        if include_comments:
            kids = (item.get("kids") or [])[:12]
            child_items = await asyncio.gather(
                *(_hackernews_api_get(session, f"item/{kid}.json") for kid in kids),
                return_exceptions=True,
            )
            for child in child_items:
                if not isinstance(child, dict) or child.get("deleted") or child.get("dead"):
                    continue
                text = _hackernews_item_text(child)
                author = _clean_line(child.get("by") or "")
                if text:
                    comments.append(f"{author}: {text}" if author else text)

        title = _clean_line(item.get("title") or "Hacker News item")
        ok = bool(story_lines[0] or comments)
        return _html_doc(title, url, [("Story", story_lines), ("Comments", comments)]), ok

    parsed = urlparse(url)
    feed_by_path = {
        "news": "topstories",
        "newest": "newstories",
        "new": "newstories",
        "front": "topstories",
        "best": "beststories",
        "ask": "askstories",
        "show": "showstories",
        "jobs": "jobstories",
    }
    requested_path = parsed.path.strip("/") or "news"
    if requested_path not in feed_by_path:
        # User/submitted/thread pages are not feeds. Returning top stories for
        # them is successful-looking but unrelated content, so let the exact
        # public HTML fallback handle those paths.
        return "", False
    feed = feed_by_path[requested_path]
    ids = await _hackernews_api_get(session, f"{feed}.json")
    if not isinstance(ids, list):
        return "", False

    items = await asyncio.gather(
        *(_hackernews_api_get(session, f"item/{item_id}.json") for item_id in ids[:20]),
        return_exceptions=True,
    )
    stories = []
    for item in items:
        if isinstance(item, dict):
            line = _hackernews_item_line(item)
            if line:
                stories.append(line)

    return _html_doc(f"Hacker News {feed}", url, [("Stories", stories)]), bool(stories)


async def scrape_hackernews(url, timeout=12.0):
    start = time.time()
    try:
        async with AsyncSession(timeout=timeout) as session:
            html, ok = await hackernews_api_html(url, session)
        if ok:
            return SourceResult(
                True,
                html=html,
                status_code=200,
                reason="Hacker News Firebase API source path",
                response_time=time.time() - start,
                final_url=url,
            )
    except Exception:
        pass

    try:
        response, raw = await _fetch_public_html(url, timeout=timeout)
        html, ok = hackernews_html_from_page(raw, str(response.url or url))
        return SourceResult(
            ok and response.status_code < 400,
            html=html if html else raw,
            status_code=response.status_code,
            reason="Hacker News public HTML source path" if ok else "Hacker News source path returned thin content",
            response_time=time.time() - start,
            final_url=str(response.url or url),
        )
    except Exception as exc:
        return SourceResult(
            False,
            status_code=0,
            reason=f"Hacker News source path failed: {type(exc).__name__}",
            response_time=time.time() - start,
            final_url=url,
        )


async def scrape_hackernews_bulk(urls, timeout=12.0):
    results = [None] * len(urls)
    sem = asyncio.Semaphore(8)
    start = time.time()

    async def one(session, idx, url):
        async with sem:
            try:
                html, ok = await hackernews_api_html(url, session, include_comments=False)
                if ok:
                    results[idx] = SourceResult(
                        True,
                        html=html,
                        status_code=200,
                        reason="Hacker News Firebase API source path",
                        response_time=time.time() - start,
                        final_url=url,
                    )
                    return
            except Exception:
                pass
            results[idx] = await scrape_hackernews(url, timeout=timeout)

    async with AsyncSession(timeout=timeout) as session:
        await asyncio.gather(*(one(session, idx, url) for idx, url in enumerate(urls)))
    return results


async def scrape_youtube_static(url, timeout=15.0):
    host = urlparse(url).netloc.lower()
    if "youtube.com" not in host and "youtu.be" not in host:
        return None
    return await scrape_http_meta_page(url, "youtube", timeout=timeout)


async def scrape_youtube_static_bulk(urls, timeout=15.0):
    return await scrape_http_meta_bulk(urls, "youtube", timeout=timeout)


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _youtube_text(value):
    if not isinstance(value, dict):
        return ""
    if value.get("simpleText"):
        return _clean_line(value.get("simpleText"))
    runs = value.get("runs")
    if isinstance(runs, list):
        return _clean_line("".join((run or {}).get("text", "") for run in runs))
    return ""


def _youtube_initial_data(html):
    pos = html.find("ytInitialData")
    if pos == -1:
        return None
    raw = _extract_balanced_json(html, pos)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _youtube_client_version(html):
    match = re.search(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"', html or "")
    if match:
        return match.group(1)
    match = re.search(r'"clientVersion":"([^"]+)"', html or "")
    if match:
        return match.group(1)
    return ""


def _youtube_comment_tokens(data, limit=8):
    tokens = []
    for item in _walk_json(data):
        command = item.get("continuationCommand")
        if not isinstance(command, dict):
            continue
        token = command.get("token")
        if not token or token in tokens:
            continue
        # The comments continuation is usually an Eg0 token. Keep a few other
        # tokens as backup because YouTube changes this shape often.
        if token.startswith("Eg0"):
            tokens.insert(0, token)
        else:
            tokens.append(token)
        if len(tokens) >= limit:
            break
    return tokens


def _youtube_comment_lines(data, limit=40):
    comments = []
    seen = set()

    def add(author, body):
        body = _clean_line(body)
        author = _clean_line(author)
        if not body or body in seen:
            return
        seen.add(body)
        comments.append(f"{author}: {body}" if author else body)

    for item in _walk_json(data):
        payload = item.get("commentEntityPayload")
        if isinstance(payload, dict):
            props = payload.get("properties") or {}
            author = payload.get("author") or {}
            content = props.get("content") or {}
            body = content.get("content") if isinstance(content, dict) else content
            add(author.get("displayName") or props.get("authorButtonA11y"), body)

        renderer = item.get("commentRenderer")
        if isinstance(renderer, dict):
            add(_youtube_text(renderer.get("authorText")), _youtube_text(renderer.get("contentText")))

        thread = item.get("commentThreadRenderer")
        if isinstance(thread, dict):
            renderer = ((thread.get("comment") or {}).get("commentRenderer") or {})
            add(_youtube_text(renderer.get("authorText")), _youtube_text(renderer.get("contentText")))

        if len(comments) >= limit:
            break

    return comments[:limit]


def _youtube_video_meta(html, data, url):
    soup = BeautifulSoup(html or "", "lxml")
    title = (
        _meta_value(soup, "og:title", "twitter:title")
        or _title_from_html(html)
        or url
    )
    description = _meta_value(soup, "og:description", "twitter:description", "description")
    channel = ""
    author = soup.find("span", attrs={"itemprop": "author"})
    if author:
        name = author.find("link", attrs={"itemprop": "name"})
        if name and name.get("content"):
            channel = _clean_line(name.get("content"))
    if not channel:
        name = soup.find("link", attrs={"itemprop": "name"})
        if name and name.get("content"):
            channel = _clean_line(name.get("content"))
    if data:
        for item in _walk_json(data):
            owner = item.get("videoOwnerRenderer")
            if isinstance(owner, dict):
                channel = channel or _youtube_text(owner.get("title"))
                break
        if not channel:
            for item in _walk_json(data):
                for key in ("ownerText", "ownerBylineText"):
                    channel = _youtube_text(item.get(key))
                    if channel:
                        break
                if channel:
                    break
    return _clean_line(title), _clean_line(channel), _clean_line(description)


async def scrape_youtube_watch_fast(url, timeout=18.0):
    start = time.time()
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": CHROME120_MAC_UA,
    }
    try:
        async with AsyncSession(impersonate="chrome120", timeout=timeout) as session:
            response = await safe_get(session, url, headers=headers)
            html = response.text or ""
            final_url = str(response.url or url)
            data = _youtube_initial_data(html)
            client_version = _youtube_client_version(html)
            if not data or not client_version:
                return SourceResult(
                    False,
                    html=html,
                    status_code=response.status_code,
                    reason="YouTube fast comments path could not read initial data",
                    response_time=time.time() - start,
                    final_url=final_url,
                )

            title, channel, description = _youtube_video_meta(html, data, final_url)
            comments = []
            api_headers = {
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Content-Type": "application/json",
                "Origin": "https://www.youtube.com",
                "Referer": final_url,
                "User-Agent": CHROME120_MAC_UA,
                "X-YouTube-Client-Name": "1",
                "X-YouTube-Client-Version": client_version,
            }
            for token in _youtube_comment_tokens(data):
                payload = {
                    "context": {
                        "client": {
                            "clientName": "WEB",
                            "clientVersion": client_version,
                            "hl": "en",
                            "gl": "US",
                        }
                    },
                    "continuation": token,
                }
                api_response = await safe_request(
                    session,
                    "POST",
                    "https://www.youtube.com/youtubei/v1/next?prettyPrint=false",
                    headers=api_headers,
                    json=payload,
                )
                if api_response.status_code >= 400:
                    continue
                try:
                    api_data = json.loads(api_response.text or "{}")
                except Exception:
                    continue
                comments = _youtube_comment_lines(api_data)
                if comments:
                    break

            if not comments:
                return SourceResult(
                    False,
                    html=html,
                    status_code=response.status_code,
                    reason="YouTube fast comments path found no comments",
                    response_time=time.time() - start,
                    final_url=final_url,
                )

            out = _html_doc(
                title,
                final_url,
                [
                    ("Channel", [channel]),
                    ("Description", [description]),
                    ("Comments", comments),
                ],
            )
            return SourceResult(
                True,
                html=out,
                status_code=response.status_code,
                reason=f"YouTube watch fast comments path with {len(comments)} comments",
                response_time=time.time() - start,
                final_url=final_url,
            )
    except Exception as exc:
        return SourceResult(
            False,
            status_code=0,
            reason=f"YouTube fast comments path failed: {type(exc).__name__}",
            response_time=time.time() - start,
            final_url=url,
        )


async def scrape_youtube_bulk(urls, timeout=18.0):
    results = [None] * len(urls)
    watch_items = []
    static_items = []
    for idx, url in enumerate(urls):
        if _is_youtube_watch_url(url):
            watch_items.append((idx, url))
        else:
            static_items.append((idx, url))

    if static_items:
        static_results = await scrape_youtube_static_bulk([url for _, url in static_items], timeout=min(timeout, 15.0))
        for (idx, _), result in zip(static_items, static_results):
            results[idx] = result

    if watch_items:
        sem = asyncio.Semaphore(4)

        async def one(idx, url):
            async with sem:
                fast = await scrape_youtube_watch_fast(url, timeout=timeout)
                if fast.success:
                    return idx, fast
                static = await scrape_youtube_static(url, timeout=min(timeout, 12.0))
                if static and static.success:
                    static.reason = f"{static.reason}; fast comments miss={fast.reason}"
                    return idx, static
                return idx, fast

        for idx, result in await asyncio.gather(*(one(idx, url) for idx, url in watch_items)):
            results[idx] = result

    return results


def reddit_json_url(url):
    parsed = urlparse(url)
    path = parsed.path
    static_exts = (".png", ".jpg", ".jpeg", ".gif", ".js", ".css")
    if path.endswith(".json") or any(path.endswith(ext) for ext in static_exts):
        return url

    is_comments = "/comments/" in path
    is_subreddit_or_listing = "/r/" in path
    is_user_profile = "/user/" in path or "/u/" in path
    if not is_comments and not is_subreddit_or_listing and not is_user_profile:
        return None

    netloc = parsed.netloc.lower()
    if netloc == "reddit.com":
        netloc = "www.reddit.com"

    return urlunparse((
        parsed.scheme,
        netloc,
        path.rstrip("/") + ".json",
        parsed.params,
        parsed.query,
        parsed.fragment,
    ))


def _reddit_body(value):
    return _clean_line(
        value.get("body")
        or value.get("selftext")
        or value.get("title")
        or ""
    )


def _walk_reddit_comments(children, out):
    for child in children or []:
        if not isinstance(child, dict):
            continue
        kind = child.get("kind")
        data = child.get("data") or {}
        if kind == "t1":
            body = _reddit_body(data)
            author = _clean_line(data.get("author") or "")
            if body:
                out.append(f"{author}: {body}" if author else body)
            replies = data.get("replies")
            if isinstance(replies, dict):
                _walk_reddit_comments((replies.get("data") or {}).get("children"), out)


def reddit_json_to_html(data, url):
    sections = []

    if isinstance(data, list) and data:
        post_listing = data[0]
        comments_listing = data[1] if len(data) > 1 else None
        post_children = ((post_listing.get("data") or {}).get("children") or [])
        if not post_children:
            return None
        post = (post_children[0].get("data") or {})
        title = _clean_line(post.get("title") or url)
        post_lines = []
        if post.get("subreddit_name_prefixed"):
            post_lines.append(post.get("subreddit_name_prefixed"))
        if post.get("author"):
            post_lines.append(f"u/{post.get('author')}")
        if post.get("selftext"):
            post_lines.append(post.get("selftext"))
        if post.get("url") and post.get("url") != url:
            post_lines.append(post.get("url"))
        sections.append(("Post", post_lines))

        comments = []
        if isinstance(comments_listing, dict):
            _walk_reddit_comments((comments_listing.get("data") or {}).get("children"), comments)
        sections.append(("Comments", comments[:100]))
        return _html_doc(title, url, sections)

    if isinstance(data, dict):
        listing = data.get("data") or {}
        children = listing.get("children") or []
        title = f"Reddit listing: {url}"
        posts = []
        for child in children:
            post = (child.get("data") or {}) if isinstance(child, dict) else {}
            post_title = _clean_line(post.get("title") or "")
            if not post_title:
                continue
            subreddit = _clean_line(post.get("subreddit_name_prefixed") or "")
            comments = post.get("num_comments")
            score = post.get("score")
            detail = post_title
            meta = []
            if subreddit:
                meta.append(subreddit)
            if score is not None:
                meta.append(f"{score} points")
            if comments is not None:
                meta.append(f"{comments} comments")
            if meta:
                detail += " - " + ", ".join(meta)
            if post.get("permalink"):
                detail += f" https://www.reddit.com{post.get('permalink')}"
            posts.append(detail)
        if not posts:
            return None
        return _html_doc(title, url, [("Posts", posts)])

    return None


def _reddit_json_source_result_from_body(body, status_code, final_url, reason_prefix, started, cookie_list=None):
    body = body or ""
    lower = body.lower()
    if not body.lstrip().startswith(("{", "[")):
        blocked = "you've been blocked by network security" in lower
        return SourceResult(
            False,
            html=body,
            status_code=status_code,
            reason=(
                f"{reason_prefix} got network security block"
                if blocked
                else f"{reason_prefix} got non-JSON response"
            ),
            response_time=time.time() - started,
            final_url=final_url,
            cookies=cookie_list or None,
        )

    try:
        data = json.loads(body)
    except Exception:
        return SourceResult(
            False,
            html=body,
            status_code=status_code,
            reason=f"{reason_prefix} returned invalid JSON",
            response_time=time.time() - started,
            final_url=final_url,
            cookies=cookie_list or None,
        )

    if status_code >= 400:
        return SourceResult(
            False,
            html=body,
            status_code=status_code,
            reason=f"{reason_prefix} returned HTTP {status_code}",
            response_time=time.time() - started,
            final_url=final_url,
            cookies=cookie_list or None,
        )

    html = reddit_json_to_html(data, final_url)
    if not html:
        return SourceResult(
            False,
            html=body,
            status_code=status_code,
            reason=f"{reason_prefix} could not extract readable content",
            response_time=time.time() - started,
            final_url=final_url,
            cookies=cookie_list or None,
        )

    return SourceResult(
        True,
        html=html,
        status_code=status_code,
        reason=f"{reason_prefix} source path",
        response_time=time.time() - started,
        final_url=final_url,
        cookies=cookie_list or None,
    )


def _reddit_lines_from_html(node):
    lines = []
    seen = set()
    for tag in node.find_all(["p", "li"], recursive=True):
        text = _clean_line(tag.get_text(" ", strip=True))
        if not text or text in seen:
            continue
        seen.add(text)
        lines.append(text)
    return lines


def reddit_html_to_html(html, url):
    if not html:
        return None

    soup = BeautifulSoup(html, "lxml")
    page_text = soup.get_text(" ", strip=True).lower()
    if "you've been blocked by network security" in page_text:
        return None

    post = soup.find("shreddit-post")
    title = ""
    post_lines = []

    if post:
        title = _clean_line(post.get("post-title") or "")
        subreddit = _clean_line(post.get("subreddit-prefixed-name") or "")
        author = _clean_line(post.get("author") or "")
        if subreddit:
            post_lines.append(subreddit)
        if author:
            post_lines.append(f"u/{author}")

        content_url = _clean_line(post.get("content-href") or "")
        if content_url and "reddit.com" not in urlparse(content_url).netloc.lower():
            post_lines.append(content_url)

    if not title:
        heading = soup.find("h1", id=re.compile(r"^post-title-")) or soup.find("h1")
        title = _clean_line(heading.get_text(" ", strip=True) if heading else "")

    body = soup.find("shreddit-post-text-body")
    if body:
        post_lines.extend(_reddit_lines_from_html(body))
    elif post:
        post_lines.extend(_reddit_lines_from_html(post))

    comments = []
    for comment in soup.find_all("shreddit-comment"):
        author = _clean_line(comment.get("author") or "")
        body = _reddit_lines_from_html(comment)
        for line in body:
            comments.append(f"{author}: {line}" if author else line)

    enough_text = sum(len(line) for line in post_lines + comments) >= 120
    if not title or not enough_text:
        return None

    sections = [("Post", post_lines)]
    if comments:
        sections.append(("Comments", comments[:100]))
    return _html_doc(title, url, sections)


def stackexchange_site_from_url(url):
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "stackoverflow.com":
        return "stackoverflow"
    if host == "serverfault.com":
        return "serverfault"
    if host == "superuser.com":
        return "superuser"
    if host.endswith(".stackexchange.com"):
        return host.removesuffix(".stackexchange.com")
    return ""


def stackexchange_question_id(url):
    match = re.search(r"/questions/(\d+)", urlparse(url).path)
    return match.group(1) if match else ""


def _plain_html(value):
    if not value:
        return ""
    return _clean_line(BeautifulSoup(value, "lxml").get_text(" ", strip=True))


def stackexchange_question_to_html(question, answers, url):
    title = _clean_line(question.get("title") or url)
    tags = ", ".join(question.get("tags") or [])
    score = question.get("score")
    views = question.get("view_count")
    meta = []
    if tags:
        meta.append(f"Tags: {tags}")
    if score is not None:
        meta.append(f"Score: {score}")
    if views is not None:
        meta.append(f"Views: {views}")
    if question.get("link"):
        meta.append(question.get("link"))

    body = _plain_html(question.get("body"))
    answer_lines = []
    for answer in answers or []:
        bits = []
        if answer.get("is_accepted"):
            bits.append("Accepted answer")
        if answer.get("score") is not None:
            bits.append(f"Score: {answer.get('score')}")
        text = _plain_html(answer.get("body"))
        if text:
            bits.append(text)
        if bits:
            answer_lines.append(" - ".join(bits))

    return _html_doc(title, url, [
        ("Question", meta + [body]),
        ("Answers", answer_lines[:20]),
    ])


async def scrape_stackexchange(url, timeout=20.0):
    return (await scrape_stackexchange_bulk([url], timeout=timeout))[0]


async def scrape_stackexchange_bulk(urls, timeout=20.0):
    start_all = time.time()
    results = [None] * len(urls)
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": "YADA source path/1.0",
    }

    async with AsyncSession(impersonate="chrome120", timeout=timeout) as session:
        for idx, url in enumerate(urls):
            started = time.time()
            site = stackexchange_site_from_url(url)
            question_id = stackexchange_question_id(url)
            if not site or not question_id:
                results[idx] = SourceResult(
                    False,
                    status_code=0,
                    reason="StackExchange source path only handles /questions/{id} URLs",
                    response_time=time.time() - started,
                    final_url=url,
                )
                continue

            try:
                query = urlencode({
                    "order": "desc",
                    "sort": "activity",
                    "site": site,
                    "filter": "withbody",
                })
                q_url = f"https://api.stackexchange.com/2.3/questions/{question_id}?{query}"
                a_url = f"https://api.stackexchange.com/2.3/questions/{question_id}/answers?order=desc&sort=votes&site={site}&filter=withbody"
                q_resp, a_resp = await asyncio.gather(
                    safe_get(session, q_url, headers=headers),
                    safe_get(session, a_url, headers=headers),
                )
                q_data = json.loads(q_resp.text or "{}")
                a_data = json.loads(a_resp.text or "{}")
                items = q_data.get("items") or []
                if not items:
                    results[idx] = SourceResult(
                        False,
                        html=q_resp.text or "",
                        status_code=q_resp.status_code,
                        reason="StackExchange API returned no matching question",
                        response_time=time.time() - started,
                        final_url=url,
                    )
                    continue

                html = stackexchange_question_to_html(items[0], a_data.get("items") or [], url)
                text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
                ok = len(text) > 500 and bool(items[0].get("title"))
                results[idx] = SourceResult(
                    ok,
                    html=html,
                    status_code=q_resp.status_code,
                    reason="StackExchange API source path" if ok else "StackExchange API returned thin content",
                    response_time=time.time() - started,
                    final_url=url,
                )
            except Exception as exc:
                results[idx] = SourceResult(
                    False,
                    status_code=0,
                    reason=f"StackExchange source path failed: {type(exc).__name__}",
                    response_time=time.time() - started,
                    final_url=url,
                )

    for idx, url in enumerate(urls):
        if results[idx] is None:
            results[idx] = SourceResult(
                False,
                status_code=0,
                reason="StackExchange source path did not run",
                response_time=time.time() - start_all,
                final_url=url,
            )
    return results


def remotive_category_from_url(url):
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "remote-jobs":
        return parts[1]
    return ""


REMOTIVE_CATEGORY_NAMES = {
    "all-others": "All others",
    "artificial-intelligence": "Artificial Intelligence",
    "communications": "Communications",
    "customer-support": "Customer Service",
    "customer-service": "Customer Service",
    "data": "Data and Analytics",
    "data-and-analytics": "Data and Analytics",
    "devops": "DevOps / Sysadmin",
    "finance-legal": "Finance / Legal",
    "human-resources": "Human Resources",
    "information-technology": "Information Technology",
    "marketing": "Marketing",
    "medical": "Medical",
    "product": "Product Management",
    "product-management": "Product Management",
    "qa": "Quality Assurance",
    "quality-assurance": "Quality Assurance",
    "sales": "Sales",
    "software-dev": "Software Development",
    "software-development": "Software Development",
    "writing": "Writing",
}


def remotive_job_id_from_url(url):
    match = re.search(r"-(\d+)/?$", urlparse(url).path)
    return match.group(1) if match else ""


def _remotive_jobs_for_url(data, url):
    jobs = list(data.get("jobs") or [])
    job_id = remotive_job_id_from_url(url)
    if job_id:
        return [job for job in jobs if str(job.get("url") or "").rstrip("/").endswith(f"-{job_id}")]

    category_slug = remotive_category_from_url(url)
    if not category_slug:
        return []
    category_name = REMOTIVE_CATEGORY_NAMES.get(category_slug)
    if not category_name:
        return []
    return [job for job in jobs if _clean_line(job.get("category") or "") == category_name]


def remotive_jobs_to_html(data, url):
    jobs = data.get("jobs") or []
    category = data.get("job-count") or len(jobs)
    title = f"Remotive remote jobs ({category})"
    lines = []
    for job in jobs[:50]:
        bits = [
            _clean_line(job.get("title") or ""),
            _clean_line(job.get("company_name") or ""),
            _clean_line(job.get("job_type") or ""),
            _clean_line(job.get("candidate_required_location") or ""),
            _clean_line(job.get("url") or ""),
        ]
        description = _plain_html(job.get("description"))
        if description:
            bits.append(description[:1200])
        line = " - ".join(bit for bit in bits if bit)
        if line:
            lines.append(line)
    return _html_doc(title, url, [("Jobs", lines)])


async def scrape_remotive(url, timeout=20.0):
    return (await scrape_remotive_bulk([url], timeout=timeout))[0]


async def scrape_remotive_bulk(urls, timeout=20.0):
    results = []
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": "YADA source path/1.0",
    }
    async with AsyncSession(impersonate="chrome120", timeout=timeout) as session:
        for url in urls:
            started = time.time()
            api_url = "https://remotive.com/api/remote-jobs"
            try:
                response = await safe_get(session, api_url, headers=headers)
                data = json.loads(response.text or "{}")
                matching_jobs = _remotive_jobs_for_url(data, url)
                filtered = dict(data)
                filtered["jobs"] = matching_jobs
                filtered["job-count"] = len(matching_jobs)
                html = remotive_jobs_to_html(filtered, url)
                text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
                ok = response.status_code == 200 and bool(matching_jobs) and len(text) > 200
                results.append(SourceResult(
                    ok,
                    html=html if ok else "",
                    status_code=response.status_code,
                    reason="Remotive public jobs API exact URL mapping" if ok else "Remotive API feed had no exact URL match",
                    response_time=time.time() - started,
                    final_url=api_url,
                ))
            except Exception as exc:
                results.append(SourceResult(
                    False,
                    status_code=0,
                    reason=f"Remotive source path failed: {type(exc).__name__}",
                    response_time=time.time() - started,
                    final_url=url,
                ))
    return results


def clinicaltrials_to_html(data, url):
    lines = []
    for study in (data.get("studies") or [])[:30]:
        protocol = study.get("protocolSection") or {}
        ident = protocol.get("identificationModule") or {}
        status = protocol.get("statusModule") or {}
        desc = protocol.get("descriptionModule") or {}
        conditions = (protocol.get("conditionsModule") or {}).get("conditions") or []
        nct_id = ident.get("nctId") or ""
        title = ident.get("briefTitle") or ident.get("officialTitle") or ""
        brief = desc.get("briefSummary") or ""
        bits = [
            nct_id,
            title,
            status.get("overallStatus") or "",
            ", ".join(conditions[:5]),
            brief[:1000],
        ]
        line = " - ".join(_clean_line(bit) for bit in bits if bit)
        if line:
            lines.append(line)
    return _html_doc("ClinicalTrials.gov studies", url, [("Studies", lines)])


def clinicaltrials_nct_id_from_url(url):
    match = re.search(r"\b(NCT\d{8})\b", urlparse(url).path, re.I)
    return match.group(1).upper() if match else ""


async def scrape_clinicaltrials(url, timeout=20.0):
    return (await scrape_clinicaltrials_bulk([url], timeout=timeout))[0]


async def scrape_clinicaltrials_bulk(urls, timeout=20.0):
    results = []
    headers = {"Accept": "application/json", "Accept-Language": "en-US,en;q=0.9", "User-Agent": "YADA source path/1.0"}
    async with AsyncSession(impersonate="chrome120", timeout=timeout) as session:
        for url in urls:
            started = time.time()
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            term = " ".join(query.get("term") or query.get("query.term") or []).strip()
            requested_nct = clinicaltrials_nct_id_from_url(url)
            if requested_nct:
                api_url = f"https://clinicaltrials.gov/api/v2/studies/{requested_nct}"
            elif term:
                requested_nct = ""
                api_url = "https://clinicaltrials.gov/api/v2/studies?" + urlencode({
                    "pageSize": "20",
                    "query.term": term,
                })
            else:
                results.append(SourceResult(
                    False,
                    status_code=0,
                    reason="ClinicalTrials URL has no exact NCT ID or search term",
                    response_time=time.time() - started,
                    final_url=url,
                ))
                continue
            try:
                response = await safe_get(session, api_url, headers=headers)
                status = response.status_code
                if status != 200:
                    results.append(SourceResult(
                        False,
                        status_code=status,
                        reason=f"ClinicalTrials API returned HTTP {status}",
                        response_time=time.time() - started,
                        final_url=api_url,
                    ))
                    continue
                try:
                    ctype = (getattr(response, "headers", None) or {}).get("content-type") or ""
                except Exception:
                    ctype = ""
                if ctype and "json" not in ctype.lower():
                    results.append(SourceResult(
                        False,
                        status_code=status,
                        reason=f"ClinicalTrials API returned non-JSON content-type ({ctype.split(';')[0].strip() or 'unknown'})",
                        response_time=time.time() - started,
                        final_url=api_url,
                    ))
                    continue
                try:
                    data = json.loads(response.text or "{}")
                except json.JSONDecodeError as exc:
                    results.append(SourceResult(
                        False,
                        status_code=status,
                        reason=f"ClinicalTrials API returned invalid JSON body ({type(exc).__name__})",
                        response_time=time.time() - started,
                        final_url=api_url,
                    ))
                    continue
                normalized = {"studies": [data]} if requested_nct else data
                html = clinicaltrials_to_html(normalized, url)
                text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
                studies = normalized.get("studies") or []
                returned_nct = ""
                if studies:
                    returned_nct = (((studies[0].get("protocolSection") or {}).get("identificationModule") or {}).get("nctId") or "").upper()
                exact = not requested_nct or returned_nct == requested_nct
                ok = response.status_code == 200 and bool(studies) and exact and len(text) > 200
                results.append(SourceResult(
                    ok,
                    html=html if ok else response.text,
                    status_code=response.status_code,
                    reason="ClinicalTrials public API exact URL mapping" if ok else "ClinicalTrials API returned non-matching or thin content",
                    response_time=time.time() - started,
                    final_url=api_url,
                ))
            except Exception as exc:
                results.append(SourceResult(False, status_code=0, reason=f"ClinicalTrials source path failed: {type(exc).__name__}", response_time=time.time() - started, final_url=url))
    return results


def discourse_latest_to_html(data, url):
    topics = ((data.get("topic_list") or {}).get("topics") or [])[:50]
    users = {u.get("id"): u.get("username") for u in data.get("users") or []}
    lines = []
    for topic in topics:
        title = _clean_line(topic.get("title") or "")
        author = users.get((topic.get("posters") or [{}])[0].get("user_id"), "")
        slug = topic.get("slug") or ""
        topic_url = f"https://meta.discourse.org/t/{slug}/{topic.get('id')}" if slug and topic.get("id") else ""
        bits = [title, f"by {author}" if author else "", f"{topic.get('posts_count')} posts" if topic.get("posts_count") else "", topic_url]
        line = " - ".join(str(bit) for bit in bits if bit)
        if line:
            lines.append(line)
    return _html_doc("Discourse latest topics", url, [("Topics", lines)])


async def scrape_discourse(url, timeout=20.0):
    return (await scrape_discourse_bulk([url], timeout=timeout))[0]


async def scrape_discourse_bulk(urls, timeout=20.0):
    results = []
    headers = {"Accept": "application/json", "Accept-Language": "en-US,en;q=0.9", "User-Agent": "YADA source path/1.0"}
    async with AsyncSession(impersonate="chrome120", timeout=timeout) as session:
        for url in urls:
            started = time.time()
            api_url = "https://meta.discourse.org/latest.json"
            try:
                response = await safe_get(session, api_url, headers=headers)
                data = json.loads(response.text or "{}")
                html = discourse_latest_to_html(data, url)
                text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
                ok = response.status_code == 200 and len(text) > 1000
                results.append(SourceResult(ok, html=html if ok else response.text, status_code=response.status_code, reason="Discourse latest JSON source path" if ok else "Discourse JSON returned thin content", response_time=time.time() - started, final_url=api_url))
            except Exception as exc:
                results.append(SourceResult(False, status_code=0, reason=f"Discourse source path failed: {type(exc).__name__}", response_time=time.time() - started, final_url=url))
    return results


def mastodon_trends_to_html(data, url):
    lines = []
    for status in (data or [])[:40]:
        account = status.get("account") or {}
        author = account.get("acct") or account.get("username") or ""
        content = _plain_html(status.get("content"))
        bits = [
            f"@{author}" if author else "",
            status.get("url") or "",
            f"{status.get('reblogs_count')} boosts" if status.get("reblogs_count") is not None else "",
            f"{status.get('favourites_count')} favourites" if status.get("favourites_count") is not None else "",
            content,
        ]
        line = " - ".join(_clean_line(str(bit)) for bit in bits if bit)
        if line:
            lines.append(line)
    return _html_doc("Mastodon trending statuses", url, [("Statuses", lines)])


async def scrape_mastodon(url, timeout=20.0):
    return (await scrape_mastodon_bulk([url], timeout=timeout))[0]


async def scrape_mastodon_bulk(urls, timeout=20.0):
    results = []
    headers = {"Accept": "application/json", "Accept-Language": "en-US,en;q=0.9", "User-Agent": "YADA source path/1.0"}
    async with AsyncSession(impersonate="chrome120", timeout=timeout) as session:
        for url in urls:
            started = time.time()
            api_url = "https://mastodon.social/api/v1/trends/statuses"
            try:
                response = await safe_get(session, api_url, headers=headers)
                data = json.loads(response.text or "[]")
                html = mastodon_trends_to_html(data, url)
                text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
                ok = response.status_code == 200 and len(data or []) > 0 and len(text) > 1000
                results.append(SourceResult(ok, html=html if ok else response.text, status_code=response.status_code, reason="Mastodon public trends API source path" if ok else "Mastodon API returned thin content", response_time=time.time() - started, final_url=api_url))
            except Exception as exc:
                results.append(SourceResult(False, status_code=0, reason=f"Mastodon source path failed: {type(exc).__name__}", response_time=time.time() - started, final_url=url))
    return results


STATIC_SOURCE_RULES = {
    "sec": {"terms": ("apple inc", "form 10-k", "commission file"), "min_text": 50000, "reason": "SEC filing static HTML source path"},
    "zacks": {"terms": ("zacks", "stock price", "aapl"), "min_text": 5000, "reason": "Zacks static HTML source path"},
    "monster": {"terms": ("monster", "search results", "jobs"), "min_text": 1000, "reason": "Monster static HTML source path"},
}


async def scrape_static_http_source(url, name, timeout=20.0):
    return (await scrape_static_http_source_bulk([url], name, timeout=timeout))[0]


async def scrape_static_http_source_bulk(urls, name, timeout=20.0):
    rule = STATIC_SOURCE_RULES.get(name) or {"terms": (), "min_text": 1000, "reason": "Static HTML source path"}
    results = []
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": CHROME120_MAC_UA,
    }
    async with AsyncSession(impersonate="chrome120", timeout=timeout) as session:
        for url in urls:
            started = time.time()
            try:
                response = await safe_get(session, url, headers=headers)
                html = response.text or ""
                content_type = (response.headers.get("content-type") or "").lower()
                parser = "xml" if "xml" in content_type or html.lstrip().startswith("<?xml") else "lxml"
                text = BeautifulSoup(html, parser).get_text(" ", strip=True)
                text_l = text.lower()
                hard_shell = any(marker in text_l for marker in ("just a moment", "enable javascript and cookies", "access denied", "captcha"))
                term_ok = all(term in text_l for term in rule["terms"])
                ok = response.status_code < 400 and len(text) >= rule["min_text"] and term_ok and not hard_shell
                results.append(SourceResult(
                    ok,
                    html=html,
                    status_code=response.status_code,
                    reason=rule["reason"] if ok else f"{rule['reason']} returned thin or blocked content",
                    response_time=time.time() - started,
                    final_url=str(response.url or url),
                ))
            except Exception as exc:
                results.append(SourceResult(False, status_code=0, reason=f"{rule['reason']} failed: {type(exc).__name__}", response_time=time.time() - started, final_url=url))
    return results


async def scrape_pytorch_docs(url, timeout=20.0):
    return (await scrape_pytorch_docs_bulk([url], timeout=timeout))[0]


async def scrape_pytorch_docs_bulk(urls, timeout=20.0):
    results = []
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": CHROME120_MAC_UA,
    }
    async with AsyncSession(impersonate="chrome120", timeout=timeout) as session:
        for url in urls:
            started = time.time()
            try:
                response = await safe_get(session, url, headers=headers)
                html = response.text or ""
                final_url = str(response.url or url)
                text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
                if "Redirecting" in text[:80] and "Continue to" in text[:160]:
                    soup = BeautifulSoup(html, "lxml")
                    link = soup.find("a")
                    href = link.get("href") if link else ""
                    if href:
                        next_url = urljoin(final_url, href)
                        response = await safe_get(session, next_url, headers=headers)
                        html = response.text or ""
                        final_url = str(response.url or next_url)
                        text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)

                text_l = text.lower()
                ok = (
                    response.status_code < 400
                    and len(text) > 5000
                    and "pytorch" in text_l
                    and "documentation" in text_l
                )
                results.append(SourceResult(
                    ok,
                    html=html,
                    status_code=response.status_code,
                    reason="PyTorch docs redirect-aware source path" if ok else "PyTorch docs source path returned thin content",
                    response_time=time.time() - started,
                    final_url=final_url,
                ))
            except Exception as exc:
                results.append(SourceResult(False, status_code=0, reason=f"PyTorch docs source path failed: {type(exc).__name__}", response_time=time.time() - started, final_url=url))
    return results


def _cookie_dict(cookies):
    out = {}
    for cookie in cookies or []:
        if isinstance(cookie, dict) and cookie.get("name"):
            out[cookie["name"]] = cookie.get("value", "")
    return out


_reddit_lock = None


async def _solve_reddit_js_challenge(session, url, timeout=10.0):
    """
    Solve Reddit's lightweight proof-of-work JS challenge via HTTP in curl_cffi.
    Returns a list of cookie dicts formatted for SiteMemory / Playwright.
    """
    if session is None:
        async with AsyncSession(impersonate="chrome120", timeout=timeout) as s:
            return await _solve_reddit_js_challenge(s, url, timeout=timeout)

    clean_url = url
    if clean_url.endswith(".json"):
        clean_url = clean_url[:-5]
    if not clean_url.endswith("/"):
        clean_url += "/"

    headers = {
        "User-Agent": CHROME120_MAC_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = await safe_get(session, clean_url, headers=headers)
        html = resp.text or ""
        if "js_challenge" not in html:
            cookies = []
            for k, v in session.cookies.items():
                cookies.append({"name": k, "value": v, "domain": ".reddit.com", "path": "/"})
            return cookies or None

        soup = BeautifulSoup(html, "lxml")
        form = soup.find("form")
        script = soup.find("script")
        if not form or not script or not script.string:
            cookies = [{"name": k, "value": v, "domain": ".reddit.com", "path": "/"} for k, v in session.cookies.items()]
            return cookies or None

        m = re.search(r'\("([a-f0-9]+)"\)', script.string)
        if not m:
            cookies = [{"name": k, "value": v, "domain": ".reddit.com", "path": "/"} for k, v in session.cookies.items()]
            return cookies or None
        token = m.group(1)

        jsc_token_input = form.find("input", {"name": "jsc_token"})
        jsc_token = jsc_token_input.get("value", "") if jsc_token_input else ""
        action = form.get("action", clean_url)
        target_url = urljoin(clean_url, action)

        params = {
            "solution": token + token,
            "js_challenge": "1",
            "jsc_token": jsc_token,
            "jsc_orig_r": "",
        }

        await safe_get(session, target_url, params=params, headers={**headers, "Referer": clean_url})
        cookies = []
        for k, v in session.cookies.items():
            cookies.append({"name": k, "value": v, "domain": ".reddit.com", "path": "/"})
        return cookies or None
    except Exception:
        return None


async def scrape_reddit_bulk(urls, timeout=15.0, cookies=None, memory=None):
    started_all = time.time()
    results = [None] * len(urls)
    seed_cookie_list = list(cookies or [])
    seed_cookies = _cookie_dict(cookies)
    by_host = {}
    for idx, url in enumerate(urls):
        api_url = reddit_json_url(url)
        if not api_url:
            results[idx] = SourceResult(
                False,
                status_code=0,
                reason="Reddit source path only handles subreddit/listing/comment URLs",
                response_time=0.0,
                final_url=url,
            )
            continue
        host = urlparse(api_url).netloc.lower()
        by_host.setdefault(host, []).append((idx, url, api_url))

    headers = {
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.reddit.com/",
        "User-Agent": CHROME120_MAC_UA,
    }

    async def read_json(session, idx, original_url, api_url, cookies, cookie_list=None):
        started = time.time()
        try:
            response = await safe_get(
                session,
                api_url,
                headers=headers,
                cookies=cookies,
            )
            body = response.text or ""
            ctype = response.headers.get("content-type", "")
            lower = body.lower()
            if "application/json" not in ctype and not body.lstrip().startswith(("{", "[")):
                blocked = "you've been blocked by network security" in lower
                return SourceResult(
                    False,
                    html=body,
                    status_code=response.status_code,
                    reason=(
                        "Reddit warmed JSON bulk got network security block"
                        if blocked
                        else "Reddit warmed JSON bulk got non-JSON response"
                    ),
                    response_time=time.time() - started,
                    final_url=str(response.url or api_url),
                )

            return _reddit_json_source_result_from_body(
                body,
                response.status_code,
                str(response.url or api_url),
                "Reddit warmed JSON bulk",
                started,
                cookie_list,
            )
        except Exception as exc:
            return SourceResult(
                False,
                status_code=0,
                reason=f"Reddit warmed JSON bulk failed: {type(exc).__name__}",
                response_time=time.time() - started,
                final_url=api_url,
            )

    def retryable_reddit_result(result):
        reason = (result.reason or "").lower()
        return (
            not result.success
            and (
                result.status_code == 0
                or result.status_code in (403, 429)
                or "network security" in reason
                or "non-json" in reason
                or "timeout" in reason
            )
        )

    async def run_json_items(items, request_cookies, warm_error="", cookie_list=None, request_timeout=None):
        sem = asyncio.Semaphore(8)

        async def one(item):
            idx, original_url, api_url = item
            async with sem:
                result = await read_json(session, idx, original_url, api_url, request_cookies, cookie_list)
                if warm_error and not result.success:
                    result.reason = f"{result.reason}; warmup={warm_error}"
                return item, result

        async with AsyncSession(
            impersonate="chrome120",
            timeout=request_timeout or timeout,
            max_clients=min(10, max(1, len(items))),
        ) as session:
            return await asyncio.gather(*(one(item) for item in items))

    if seed_cookies:
        needs_warmup = {}
        for host, items in by_host.items():
            for item, result in await run_json_items(items, seed_cookies, cookie_list=seed_cookie_list):
                idx, _, _ = item
                results[idx] = result
                if retryable_reddit_result(result):
                    needs_warmup.setdefault(host, []).append(item)
        by_host = needs_warmup
        if not by_host:
            return results

    if by_host:
        needs_warmup = {}
        for host, items in by_host.items():
            first = items[0]
            probe = await run_json_items([first], {}, request_timeout=min(timeout, 3.0))
            _, probe_result = probe[0]
            results[first[0]] = probe_result

            if retryable_reddit_result(probe_result):
                needs_warmup.setdefault(host, []).extend(items)
                continue

            remaining_items = items[1:]
            if probe_result.success and not remaining_items:
                continue

            if not probe_result.success:
                needs_warmup.setdefault(host, []).append(first)

            for item, result in await run_json_items(remaining_items, {}, request_timeout=min(timeout, 5.0)):
                idx, _, _ = item
                results[idx] = result
                if retryable_reddit_result(result):
                    needs_warmup.setdefault(host, []).append(item)
        by_host = needs_warmup
        if not by_host:
            return results

    if by_host:
        try:
            for host, b_items in list(by_host.items()):
                sample_url = next((u for _, u, api in b_items if u != api), b_items[0][1])
                js_cookies = await _solve_reddit_js_challenge(None, sample_url, timeout=min(timeout or 10.0, 10.0))
                if js_cookies:
                    js_cookie_dict = _cookie_dict(js_cookies)
                    if memory is not None:
                        await memory.save_cookies("https://www.reddit.com", js_cookies)
                    resolved = []
                    for item, res in await run_json_items(b_items, js_cookie_dict, cookie_list=js_cookies, request_timeout=min(timeout or 5.0, 5.0)):
                        idx, _, _ = item
                        results[idx] = res
                        if not retryable_reddit_result(res):
                            resolved.append(item)
                    by_host[host] = [item for item in b_items if item not in resolved]
                    if not by_host[host]:
                        del by_host[host]
        except Exception:
            pass

    if not by_host:
        return results

    for idx, url in enumerate(urls):
        if results[idx] is None:
            results[idx] = SourceResult(
                False,
                status_code=0,
                reason="Reddit JSON bulk could not fetch this URL",
                response_time=time.time() - started_all,
                final_url=reddit_json_url(url) or url,
            )

    return results


def _default_chrome_binary():
    return find_chromium_binary()


def _walmart_query(url):
    parsed = urlparse(url)
    path = parsed.path.lower()
    query = " ".join(parse_qs(parsed.query).get("q", [])).strip()
    if query:
        return query
    if "/ip/" in path or "/browse/" in path or "/brand/" in path or "/cp/" in path:
        parts = [p for p in path.split("/") if p]
        return parts[0] if parts else "walmart"
    return ""


def _json_object(stdout):
    stdout = (stdout or "").strip()
    json_start = stdout.rfind("\n{")
    if json_start != -1:
        return stdout[json_start + 1:]
    if "{" in stdout:
        return stdout[stdout.find("{"):]
    return stdout


def _walmart_result(url, query, data):
    html = data.get("html") or ""
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True) if html else ""
    text_l = text.lower()
    html_l = html.lower()
    item_text = " ".join(data.get("item_texts") or []).lower()
    hard_challenge = (
        "robot or human" in text_l
        or "press & hold" in text_l
        or "px-captcha" in html_l
        or "we're having a technical issue" in text_l
    )
    product_markers = ("add to cart", "sort by", "shipping", "results for")
    has_products = (
        len(text) > 2500
        and data.get("items_found", 0) > 0
        and sum(1 for marker in product_markers if marker in text_l) >= 2
        and any(part.lower() in (text_l + " " + item_text) for part in query.split()[:2])
    )
    ok = bool(html) and has_products and not hard_challenge
    reason = "Walmart zendriver warm session" if ok else (
        "Walmart warm session returned captured page without clean product results"
    )
    if "we're having a technical issue" in text_l:
        reason = "Walmart warm session returned technical-issue shell"
    if data.get("error"):
        reason = f"{reason}: {data.get('error')}"

    return SourceResult(
        ok,
        html=html,
        status_code=200 if html else 0,
        reason=reason,
        response_time=data.get("elapsed") or 0.0,
        final_url=data.get("final_url") or url,
        cookies=[],
    )


def _extract_walmart_next_data_html(html, original_url):
    if not html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    next_tag = soup.find("script", id="__NEXT_DATA__")
    if not next_tag or not next_tag.string:
        return html
    try:
        data = json.loads(next_tag.string)
    except Exception:
        return html

    page_props = data.get("props", {}).get("pageProps", {})
    initial_data = page_props.get("initialData", {})

    # 1. Single Product Detail IP Page (/ip/...)
    data_obj = initial_data.get("data", {})
    product_obj = data_obj.get("product", {})
    if product_obj:
        name = product_obj.get("name") or product_obj.get("title")
        if name:
            brand = product_obj.get("brand", "N/A")
            price_info = product_obj.get("priceInfo", {}) or {}
            curr_price = price_info.get("currentPrice", {}).get("price") or price_info.get("linePrice") or price_info.get("price")
            price_str = f"${curr_price}" if curr_price else "N/A"
            rating_info = product_obj.get("rating", {}) or {}
            avg_rating = rating_info.get("averageRating", "N/A")
            num_reviews = rating_info.get("numberOfReviews", 0)
            short_desc = product_obj.get("shortDescription", "")

            html_doc = [
                "<!DOCTYPE html>",
                "<html>",
                f"<head><title>{name} - Walmart</title></head>",
                "<body>",
                f"  <h1>{name}</h1>",
                f"  <p class='brand'>Brand: <strong>{brand}</strong></p>",
                f"  <p class='price'>Price: <strong>{price_str}</strong></p>",
                f"  <p class='rating'>Rating: {avg_rating} ({num_reviews} reviews)</p>",
                f"  <div class='description'>{short_desc}</div>",
                "</body>",
                "</html>"
            ]
            return "\n".join(html_doc)

    # 2. Search & Category Browse Pages (/search?q=... and /browse/...)
    search_result = initial_data.get("searchResult", {})
    item_stacks = search_result.get("itemStacks", [])

    products = []
    for stack in item_stacks:
        items = stack.get("items", [])
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("title")
            if not name:
                continue
            price_info = item.get("priceInfo", {}) or {}
            line_price = price_info.get("linePrice")
            price = line_price if line_price else item.get("price")
            if isinstance(price, (int, float)):
                price_str = f"${price:.2f}"
            elif isinstance(price, str):
                price_str = price if price.startswith("$") else f"${price}"
            else:
                price_str = "N/A"

            rating_info = item.get("rating", {}) or {}
            average_rating = rating_info.get("averageRating", "N/A")
            num_reviews = rating_info.get("numberOfReviews", 0)

            canonical_url = item.get("canonicalUrl", "")
            if canonical_url and not canonical_url.startswith("http"):
                product_url = f"https://www.walmart.com{canonical_url}"
            else:
                product_url = canonical_url or original_url

            products.append({
                "name": name,
                "price": price_str,
                "rating": average_rating,
                "reviews": num_reviews,
                "url": product_url
            })

    # 3. Category Portals (/cp/...) fallback
    if not products:
        seen_names = set()
        def extract_portal_products(node):
            if isinstance(node, dict):
                is_portal_product = ("name" in node and "priceInfo" in node and ("id" in node or "usItemId" in node))
                if is_portal_product:
                    name = node.get("name") or node.get("title")
                    if name and isinstance(name, str) and name not in seen_names:
                        seen_names.add(name)
                        price_info = node.get("priceInfo", {}) or {}
                        line_price = price_info.get("linePrice")
                        price = line_price if line_price else node.get("price")
                        if isinstance(price, (int, float)):
                            price_str = f"${price:.2f}"
                        elif isinstance(price, str):
                            price_str = price if price.startswith("$") else f"${price}"
                        else:
                            price_str = "N/A"
                        products.append({
                            "name": name.strip(),
                            "price": price_str,
                            "rating": "N/A",
                            "reviews": 0,
                            "url": ""
                        })
                for k, v in node.items():
                    extract_portal_products(v)
            elif isinstance(node, list):
                for item in node:
                    extract_portal_products(item)
                    
        extract_portal_products(data)

    if not products:
        return html

    html_lines = [
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        f"  <title>Walmart Search Results: {len(products)} Products Found</title>",
        "</head>",
        "<body>",
        f"  <h1>Walmart Search Results ({len(products)} items)</h1>",
        "  <div class='product-grid'>",
    ]
    for p in products:
        html_lines.append(
            f"    <div class='product-card'>"
            f"<h2><a href='{p['url']}'>{p['name']}</a></h2>"
            f"<p class='price'>Price: <strong>{p['price']}</strong></p>"
            f"<p class='rating'>Rating: {p['rating']} ({p['reviews']} reviews)</p>"
            f"</div>"
        )
    html_lines.extend(["  </div>", "</body>", "</html>"])
    return "\n".join(html_lines)


async def _scrape_walmart_referer_bypass(url, timeout=12.0):
    start = time.time()
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        "Referer": "https://www.google.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with AsyncSession(impersonate="chrome120") as session:
            resp = await safe_get(session, url, headers=headers, timeout=timeout)
            elapsed = time.time() - start
            body = resp.text or ""
            parsed_html = _extract_walmart_next_data_html(body, url)
            soup_p = BeautifulSoup(parsed_html, "html.parser")
            for t in soup_p(["script", "style", "svg", "noscript", "iframe"]):
                t.decompose()
            parsed_text = soup_p.get_text(separator=" ", strip=True)
            parsed_text_l = parsed_text.lower()

            blocked = (
                "robot or human" in parsed_text_l
                or "blocked by network security" in parsed_text_l
                or "access denied" in parsed_text_l
                or "px-captcha" in body.lower()
            )

            useful = (
                resp.status_code == 200
                and len(parsed_text) > 250
                and ("walmart" in parsed_text_l or "brand" in parsed_text_l or "price" in parsed_text_l)
                and not blocked
            )

            if useful:
                return SourceResult(
                    success=True,
                    html=parsed_html,
                    status_code=200,
                    reason="Walmart 1.8s Google referer bypass",
                    response_time=elapsed,
                    final_url=str(resp.url or url),
                )
    except Exception:
        pass
    return None


async def scrape_walmart(url, timeout=120.0, cookies=None, solve_challenges=False):
    import time
    bypass_res = await _scrape_walmart_referer_bypass(url, timeout=min(timeout or 12.0, 12.0))
    if bypass_res and bypass_res.success:
        return bypass_res

    from scraper.http_scraping import advanced_waf_scrape
    start = time.time()
    w_res = await advanced_waf_scrape(url, timeout=timeout or 45.0)
    if w_res:
        return SourceResult(
            success=w_res.success, html=w_res.html or "", status_code=w_res.status_code,
            reason=w_res.reason or "advanced_waf_scrape fallback",
            response_time=time.time()-start, final_url=w_res.final_url or url
        )
    return SourceResult(False, status_code=0, reason="Fallback failed", response_time=time.time()-start, final_url=url)


async def scrape_walmart_bulk(urls, timeout=None, cookies=None, solve_challenges=False):
    from scraper.http_scraping import advanced_waf_scrape
    import time, asyncio
    results = [None] * len(urls)
    
    sem = asyncio.Semaphore(3)

    async def _process_url(idx, url):
        # 1. Fast Google Bypass (Throttled)
        async with sem:
            b_res = await _scrape_walmart_referer_bypass(url, timeout=12.0)
        
        if b_res and b_res.success:
            results[idx] = b_res
            return

        # 2. Fallback to Layer 1c silently
        s = time.time()
        w_res = await advanced_waf_scrape(url, timeout=timeout or 45.0)
        if w_res:
            results[idx] = SourceResult(
                success=w_res.success, html=w_res.html or "", status_code=w_res.status_code,
                reason=w_res.reason or "advanced_waf_scrape fallback",
                response_time=time.time()-s, final_url=w_res.final_url or url
            )
        else:
            results[idx] = SourceResult(False, status_code=0, reason="Fallback failed", response_time=time.time()-s, final_url=url)

    await asyncio.gather(*[_process_url(i, u) for i, u in enumerate(urls)])
    return results


def _bestbuy_url(url):
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query.setdefault("intl", ["nosplash"])
    return urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        urlencode(query, doseq=True),
        parsed.fragment,
    ))






async def scrape_vinted(url, timeout=30.0):
    return (await scrape_vinted_bulk([url], timeout=timeout))[0]


async def scrape_vinted_bulk(urls, timeout=30.0):
    async def one(session, url):
        started = time.time()
        last = None
        for attempt in range(3):
            try:
                res = await safe_get(session, url)
                html = res.text or ""
                text, text_l = _text_lower(html)
                html_l = html.lower()
                hard_shell = (
                    "captcha-delivery.com" in html_l
                    or "please enable js and disable any ad blocker" in text_l
                    or "access denied" in text_l[:2000]
                )
                product_dom = (
                    'data-testid="product-item-id-' in html_l
                    or "feed-grid__item" in html_l
                    or "new-item-box__container" in html_l
                )
                useful = (
                    bool(html)
                    and res.status_code < 400
                    and (len(text) > 2500 or (len(text) > 400 and product_dom))
                    and "vinted" in text_l
                    and ("catalog" in text_l or "items" in text_l or "brand" in text_l or product_dom)
                    and not hard_shell
                )
                last = SourceResult(
                    useful,
                    html=html,
                    status_code=res.status_code,
                    reason="Vinted DataDome fast path" if useful else "Vinted fast path returned thin or challenge page",
                    response_time=time.time() - started,
                    final_url=str(res.url),
                )
                if useful:
                    return last
                await asyncio.sleep(1.5 + attempt)
            except Exception as exc:
                last = SourceResult(
                    False,
                    status_code=0,
                    reason=f"Vinted fast path failed: {type(exc).__name__}: {exc}",
                    response_time=time.time() - started,
                    final_url=url,
                )
                await asyncio.sleep(1.5 + attempt)

        return last

    sem = asyncio.Semaphore(8)
    async with AsyncSession(impersonate="chrome120", timeout=timeout) as session:
        async def limited(url):
            async with sem:
                return await one(session, url)

        return await asyncio.gather(*(limited(url) for url in urls))




def _text_lower(html):
    text = BeautifulSoup(html or "", "lxml").get_text(" ", strip=True) if html else ""
    return text, text.lower()










async def _scrape_zillow_referer_bypass(url, timeout=12.0):
    start = time.time()
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        "Referer": "https://www.google.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with AsyncSession(impersonate="chrome120") as session:
            resp = await safe_get(session, url, headers=headers, timeout=timeout)
            elapsed = time.time() - start
            body = resp.text or ""
            soup = BeautifulSoup(body, "html.parser")
            for t in soup(["script", "style", "svg", "noscript", "iframe"]):
                t.decompose()
            text = soup.get_text(separator=" ", strip=True)
            text_l = text.lower()

            blocked = (
                "access to this page has been denied" in text_l
                or "press & hold" in text_l
                or "px-captcha" in body.lower()
            )

            useful = (
                resp.status_code == 200
                and len(text) > 1500
                and "zillow" in text_l
                and not blocked
            )

            if useful:
                return SourceResult(
                    success=True,
                    html=body,
                    status_code=200,
                    reason="Zillow 1.8s Google referer bypass",
                    response_time=elapsed,
                    final_url=str(resp.url or url),
                )
    except Exception:
        pass
    return None
























def _extract_balanced_json(text, start):
    json_start = text.find("{", start)
    if json_start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for idx in range(json_start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[json_start:idx + 1]

    return None


def medium_article_from_state(html, url):
    marker = "window.__APOLLO_STATE__"
    pos = html.find(marker)
    if pos == -1:
        return None

    raw = _extract_balanced_json(html, pos)
    if not raw:
        return None

    try:
        state = json.loads(raw)
    except Exception:
        return None

    paragraphs = []
    for key, value in state.items():
        if not key.startswith("Paragraph:") or not isinstance(value, dict):
            continue
        text = value.get("text")
        if not text:
            continue
        match = re.search(r"_(\d+)$", key)
        order = int(match.group(1)) if match else 999999
        paragraphs.append((order, text.strip()))

    if len(paragraphs) < 3:
        return None

    paragraphs.sort()
    seen = set()
    clean = []
    for _, text in paragraphs:
        if text in seen:
            continue
        seen.add(text)
        clean.append(text)

    title = _title_from_html(html) or url
    parts = [
        "<!doctype html>",
        "<html><head>",
        f"<title>{title}</title>",
        "</head><body>",
        f"<h1>{title}</h1>",
        f'<p><a href="{url}">{url}</a></p>',
    ]
    for paragraph in clean:
        parts.append(f"<p>{paragraph}</p>")
    parts.append("</body></html>")
    return "\n".join(parts)


async def scrape_medium(url, timeout=20.0):
    start = time.time()
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": CHROME120_MAC_UA,
    }

    try:
        async with AsyncSession(impersonate="chrome120", timeout=timeout) as session:
            response = await safe_get(session, url, headers=headers)
            html = response.text
            article = medium_article_from_state(html, str(response.url or url))
            if article:
                return SourceResult(
                    True,
                    html=article,
                    status_code=response.status_code,
                    reason="Medium article extracted from Apollo state",
                    response_time=time.time() - start,
                    final_url=str(response.url or url),
                )

            return SourceResult(
                bool(html),
                html=html,
                status_code=response.status_code,
                reason="Medium source path did not find article paragraphs",
                response_time=time.time() - start,
                final_url=str(response.url or url),
            )
    except Exception as exc:
        return SourceResult(
            False,
            status_code=0,
            reason=f"Medium source path failed: {type(exc).__name__}",
            response_time=time.time() - start,
            final_url=url,
        )


def _instagram_resource_identity(url):
    """Return the requested Instagram resource, excluding host/query cosmetics."""
    try:
        parsed = urlparse(str(url or ""))
    except (TypeError, ValueError):
        return None

    host = (parsed.hostname or "").lower().rstrip(".")
    if host != "instagram.com" and not host.endswith(".instagram.com"):
        return None

    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        return "/"
    return "/" + "/".join(segments)


def _instagram_bulk_result(url, response, start):
    """Convert one HTTP response without treating generic/login metadata as data."""
    from .http_scraping import QuickResult

    raw = response.text or ""
    final_url = str(response.url or url)
    soup = BeautifulSoup(raw, "lxml")

    def meta_value(*names):
        for name in names:
            node = soup.find("meta", attrs={"property": name})
            if node is None:
                node = soup.find("meta", attrs={"name": name})
            if node and node.get("content"):
                value = html_lib.unescape(str(node.get("content"))).strip()
                if value:
                    return value
        return ""

    description = meta_value("og:description", "twitter:description", "description")
    image_url = meta_value("og:image", "twitter:image")
    title = meta_value("og:title", "twitter:title")
    if not title and soup.title:
        title = html_lib.unescape(soup.title.get_text(" ", strip=True)).strip()

    requested_identity = _instagram_resource_identity(url)
    final_identity = _instagram_resource_identity(final_url)
    identity_matches = bool(requested_identity and requested_identity == final_identity)

    combined = " ".join((title, description, soup.get_text(" ", strip=True)[:3000])).lower()
    blocked_markers = (
        "login â€¢ instagram",
        "log in to instagram",
        "sign up â€¢ instagram",
        "create an account or log in",
        "checkpoint_required",
        "challenge_required",
        "please wait a few minutes before you try again",
        "page isn't available",
        "page is not available",
    )
    generic_titles = {"", "instagram", "instagram login", "login â€¢ instagram"}
    metadata_specific = bool(
        len(description) >= 20
        and (title.strip().lower() not in generic_titles or image_url)
    )
    status_ok = 200 <= int(response.status_code or 0) < 400
    title_lower = title.strip().lower()
    auth_title = "instagram" in title_lower and any(
        marker in title_lower for marker in ("login", "log in", "sign up")
    )
    blocked = auth_title or any(marker in combined for marker in blocked_markers)
    success = bool(status_ok and identity_matches and metadata_specific and not blocked)

    lines = [
        f"Requested URL: {url}",
        f"Title: {title or 'Not found'}",
        "",
        "Details:",
        description or "Not found",
        "",
        f"Image URL: {image_url or 'Not found'}",
    ]

    if success:
        reason = "Instagram Googlebot metadata extraction"
    elif not status_ok:
        reason = f"Instagram metadata request returned HTTP {response.status_code}"
    elif not identity_matches:
        reason = "Instagram response redirected to a different resource"
    elif blocked:
        reason = "Instagram returned a login, challenge, or unavailable-page shell"
    else:
        reason = "Instagram response did not contain resource-specific metadata"

    return QuickResult(
        success=success,
        status_code=int(response.status_code or 0),
        html="\n".join(lines),
        needs_browser=not success,
        reason=reason,
        response_time=time.time() - start,
        final_url=final_url,
    )


async def scrape_instagram_bulk(urls, timeout=45.0):
    from .http_scraping import QuickResult

    async def fetch(session, url):
        start = time.time()
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5"
        }
        try:
            response = await safe_get(session, url, headers=headers, timeout=timeout)
            return _instagram_bulk_result(url, response, start)
        except Exception as e:
            return QuickResult(
                success=False, status_code=0, needs_browser=True,
                reason=str(e), response_time=time.time() - start,
                final_url=url
            )

    async with AsyncSession(impersonate="chrome120", max_clients=min(10, max(1, len(urls)))) as session:
        tasks = [fetch(session, url) for url in urls]
        return await asyncio.gather(*tasks)


async def scrape_instagram(url, timeout=15.0):
    return await scrape_http_meta_page(url, "instagram", timeout=timeout)


async def _scrape_flipkart_referer_bypass(url, timeout=15.0):
    start = time.time()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.google.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with AsyncSession(impersonate="chrome120") as session:
            resp = await safe_get(session, url, headers=headers, timeout=timeout)
            elapsed = time.time() - start
            body = resp.text or ""
            soup = BeautifulSoup(body, "html.parser")
            for t in soup(["script", "style", "svg", "noscript", "iframe"]):
                t.decompose()
            text = soup.get_text(separator=" ", strip=True)
            text_l = text.lower()

            useful = (
                resp.status_code == 200
                and len(text) > 1500
                and "flipkart" in text_l
            )

            if useful:
                return SourceResult(
                    success=True,
                    html=body,
                    status_code=200,
                    reason="Flipkart 1.8s Google referer bypass",
                    response_time=elapsed,
                    final_url=str(resp.url or url),
                )
    except Exception:
        pass
    return None


async def scrape_flipkart(url, timeout=15.0):
    fast = await _scrape_flipkart_referer_bypass(url, timeout=min(timeout, 12.0))
    if fast and fast.success:
        return fast
    return await scrape_http_meta_page(url, "flipkart", timeout=timeout)


async def scrape_reddit(url, timeout=25.0, cookies=None, memory=None):
    if not reddit_json_url(url):
        return None
    return (await scrape_reddit_bulk([url], timeout=min(timeout, 15.0), cookies=cookies, memory=memory))[0]


QUORA_DOC_END_KEYS = (
    "id",
    "isDeleted",
    "isQuestionBannerRemoved",
    "author",
    "__typename",
    "creationTime",
    "updatedTime",
    "url",
    "num[A-Z_a-z]*",
    "viewer",
    "network",
    "tribeItem",
    "qid",
    "slug",
    "answerCount",
    "isRecentlyGeneratedQuestion",
    "shouldBlurAllImages",
)


def _quora_decode_document(raw):
    try:
        text = json.loads('"' + raw + '"')
    except Exception:
        return []
    text = text.replace('\\"', '"').replace("\\\\u", "\\u")
    try:
        data, _ = json.JSONDecoder().raw_decode(text)
    except Exception:
        return []
    lines = []
    for section in data.get("sections") or []:
        spans = section.get("spans") or []
        line = _clean_line("".join((span or {}).get("text", "") for span in spans))
        if line:
            lines.append(line)
    return lines


def _quora_documents(html, key):
    end = "|".join(QUORA_DOC_END_KEYS)
    pattern = rf'{key}\\":\\"(.*?)(?=\\",\\"(?:{end})\\")'
    docs = []
    for match in re.finditer(pattern, html or ""):
        lines = _quora_decode_document(match.group(1))
        if lines:
            docs.append(lines)
    return docs


def _quora_author_names(html):
    names = []
    for match in re.finditer(r'givenName\\":\\"([^"\\]{2,120})\\"', html or ""):
        name = _clean_line(match.group(1))
        if name and name not in names and not name.lower().startswith("there is help"):
            names.append(name)
        if len(names) >= 6:
            break
    return names


def _quora_js_string(raw, start):
    if start >= len(raw) or raw[start] != '"':
        raise ValueError("not a string literal")
    out = []
    j = start + 1
    escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}
    while j < len(raw):
        c = raw[j]
        if c == "\\":
            nxt = raw[j + 1:j + 2]
            if nxt in escapes:
                out.append(escapes[nxt])
                j += 2
                continue
            if nxt == "u":
                out.append(chr(int(raw[j + 2:j + 6], 16)))
                j += 6
                continue
            out.append(nxt)
            j += 2
            continue
        if c == '"':
            return "".join(out), j + 1
        out.append(c)
        j += 1
    raise ValueError("unterminated string")


def _quora_topic_payload(raw):
    """Topic pages SSR their data as JSON pushed into inlineQueryResults."""
    raw = raw or ""
    for match in re.finditer(r'inlineQueryResults\.results\["[0-9a-f]+"\]\.push\(', raw):
        try:
            inner, _ = _quora_js_string(raw, match.end())
            payload = json.loads(inner)
        except Exception:
            continue
        topic = ((payload or {}).get("data") or {}).get("topic")
        if isinstance(topic, dict) and topic.get("name"):
            return topic
    return None


def _quora_topic_static_html(raw, title, url):
    topic = _quora_topic_payload(raw)
    if not topic:
        return None
    name = _clean_line(str(topic.get("name") or ""))
    if not name:
        return None
    topic_lines = [f"Topic: {name}"]
    followers = topic.get("numFollowers")
    if isinstance(followers, int):
        topic_lines.append(f"Followers: {followers:,}")
    topic_url = _clean_line(str(topic.get("url") or ""))
    if topic_url:
        topic_lines.append("https://www.quora.com" + topic_url if topic_url.startswith("/") else topic_url)

    author_lines = []
    for item in topic.get("mostViewedAuthors") or []:
        if not isinstance(item, dict):
            continue
        user = item.get("user") or {}
        display = ""
        for n in user.get("names") or []:
            if not isinstance(n, dict):
                continue
            given = _clean_line(str(n.get("givenName") or ""))
            family = _clean_line(str(n.get("familyName") or ""))
            if given:
                display = (given + " " + family).strip()
                break
        if not display:
            continue
        bits = display
        views = item.get("numViews")
        answers = item.get("numPublicMostViewedAnswers")
        if isinstance(views, int):
            bits += f" - {views:,} answer views"
        if isinstance(answers, int):
            bits += f", {answers:,} top answers"
        author_lines.append(bits)
        if len(author_lines) >= 8:
            break

    related_lines = []
    for rel in topic.get("relatedTopics") or []:
        if isinstance(rel, dict):
            rel_name = _clean_line(str(rel.get("name") or ""))
            if rel_name:
                related_lines.append(rel_name)
        if len(related_lines) >= 8:
            break

    if not author_lines and not related_lines and len(topic_lines) < 2:
        return None
    sections = [
        ("Topic", topic_lines),
        ("Most Viewed Authors", author_lines),
        ("Related Topics", related_lines),
    ]
    html = _html_doc(title or name, url, sections)
    return html, True, "Quora static topic payload source path"


def _quora_static_html(raw, url):
    soup = BeautifulSoup(raw or "", "lxml")
    title = _meta_value(soup, "og:title", "twitter:title") or _title_from_html(raw) or url
    description = _meta_value(soup, "og:description", "twitter:description", "description")
    canonical = ""
    link = soup.find("link", attrs={"rel": "canonical"})
    if link and link.get("href"):
        canonical = link.get("href")

    question_lines = []
    for docs in (_quora_documents(raw, "title"), _quora_documents(raw, "originalQuestionText")):
        for doc in docs:
            joined = " ".join(doc)
            if 20 <= len(joined) <= 400 and joined not in question_lines:
                question_lines.append(joined)
            if len(question_lines) >= 4:
                break

    answer_lines = []
    seen = set()
    boilerplate = (
        "need help? contact a suicide hotline",
        "worldwide",
        "united states",
        "call the 988",
        "lifeline",
    )
    for doc in _quora_documents(raw, "content"):
        joined = " ".join(doc)
        if len(joined) < 180:
            continue
        if any(marker in joined.lower() for marker in boilerplate):
            continue
        for line in doc:
            key = line.lower()
            if key in seen:
                continue
            seen.add(key)
            answer_lines.append(line)
        if len(" ".join(answer_lines)) >= 3500:
            break

    summary_lines = []
    if description:
        summary_lines.append(description)
    if canonical and canonical != url:
        summary_lines.append(canonical)

    authors = _quora_author_names(raw)
    sections = [
        ("Question", question_lines or [title]),
        ("Answer", answer_lines),
        ("Authors", authors),
        ("Summary", summary_lines),
    ]
    html = _html_doc(title, url, sections)

    page_text = soup.get_text(" ", strip=True)
    shell_only = (
        "something went wrong" in page_text.lower()
        and "please enable javascript" in page_text.lower()
    )
    enough_answer = len(" ".join(answer_lines)) >= 220
    enough_summary = bool(description and len(description) >= 120)
    ok = enough_answer or enough_summary
    reason = (
        "Quora static document source path"
        if enough_answer
        else "Quora static metadata summary source path"
        if enough_summary
        else "Quora static source path returned thin content"
    )
    if not ok:
        topic_html = _quora_topic_static_html(raw, title, url)
        if topic_html:
            html, ok, reason = topic_html
    return html, bool(ok), reason, shell_only


async def scrape_quora_static(url, timeout=15.0):
    start = time.time()
    last = None
    # Quora often serves a challenge shell on the first hit and real data on
    # the next one; one retry turns that coin-flip into a pass.
    for _ in range(2):
        try:
            response, raw = await _fetch_public_html(url, timeout=timeout)
            html, ok, reason, _ = _quora_static_html(raw, str(response.url or url))
            last = SourceResult(
                ok and response.status_code < 400,
                html=html if html else raw,
                status_code=response.status_code,
                reason=reason,
                response_time=time.time() - start,
                final_url=str(response.url or url),
            )
            if last.success:
                return last
        except Exception as exc:
            last = SourceResult(
                False,
                status_code=0,
                reason=f"Quora static source path failed: {type(exc).__name__}",
                response_time=time.time() - start,
                final_url=url,
            )
    return last


async def scrape_quora_static_bulk(urls, timeout=15.0):
    results = [None] * len(urls)
    sem = asyncio.Semaphore(8)

    async def one(idx, url):
        async with sem:
            results[idx] = await scrape_quora_static(url, timeout=timeout)

    await asyncio.gather(*(one(idx, url) for idx, url in enumerate(urls)))
    return results


async def scrape_quora(url, timeout=35.0):
    start = time.time()
    browser = None
    stealth = Stealth()
    parsed = urlparse(url)
    homepage = f"{parsed.scheme}://{parsed.netloc}/"

    static = await scrape_quora_static(url, timeout=min(timeout, 15.0))
    if static and static.success:
        return static

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
                **service_worker_context_options(),
                user_agent=CHROME120_MAC_UA,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="America/New_York",
                color_scheme="light",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"macOS"',
                },
            )
            await install_browser_network_guard(ctx)
            page = await ctx.new_page()
            await stealth.apply_stealth_async(page)
            await inject_stealth_scripts(page, "macOS")

            if url != homepage:
                try:
                    await page.goto(homepage, wait_until="domcontentloaded", timeout=12000)
                    await _wait_loaded(page, timeout=7000)
                    await _act_human(page, SimpleNamespace(scr_w=1280, scr_h=800), quick=True)
                except Exception:
                    pass

            response = await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            status = response.status if response else 0
            await _wait_loaded(page, timeout=12000)
            await inject_helper_scripts(page)
            fp = SimpleNamespace(scr_w=1280, scr_h=800)
            await _act_human(page, fp, quick=False)
            await _scroll_around(page, fp)
            await _do_scroll(page, max_scrolls=5, timeout=9.0)
            await asyncio.sleep(1.0)

            html = await _capture_html(page)
            cookies = await ctx.cookies()
            final_url = page.url
            await ctx.close()
            await browser.close()

            if _looks_like_interstitial(html):
                return SourceResult(
                    bool(html),
                    html=html,
                    status_code=status if status in (403, 429, 503) else 403,
                    reason="Quora source path captured an interstitial",
                    response_time=time.time() - start,
                    final_url=final_url,
                    cookies=cookies,
                )

            return SourceResult(
                True,
                html=html,
                status_code=status,
                reason="Quora rendered source path",
                response_time=time.time() - start,
                final_url=final_url,
                cookies=cookies,
            )
    except Exception as exc:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        return SourceResult(
            False,
            status_code=0,
            reason=f"Quora source path failed: {type(exc).__name__}",
            response_time=time.time() - start,
            final_url=url,
        )


async def scrape_quora_bulk(urls, timeout=45.0):
    start_all = time.time()
    results = [None] * len(urls)
    static_results = await scrape_quora_static_bulk(urls, timeout=min(timeout, 15.0))
    render_items = []
    for idx, result in enumerate(static_results):
        if result and result.success:
            results[idx] = result
        else:
            render_items.append((idx, urls[idx]))

    if not render_items:
        return results

    by_host = {}
    for idx, url in render_items:
        parsed = urlparse(url)
        by_host.setdefault(parsed.netloc.lower(), []).append((idx, url))

    browser = None
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
            stealth = Stealth()

            for _, items in by_host.items():
                first_url = items[0][1]
                parsed = urlparse(first_url)
                homepage = f"{parsed.scheme}://{parsed.netloc}/"
                ctx = await browser.new_context(
                    **service_worker_context_options(),
                    user_agent=CHROME120_MAC_UA,
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                    timezone_id="America/New_York",
                    color_scheme="light",
                    extra_http_headers={
                        "Accept-Language": "en-US,en;q=0.9",
                        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                        "Sec-Ch-Ua-Mobile": "?0",
                        "Sec-Ch-Ua-Platform": '"macOS"',
                    },
                )
                await install_browser_network_guard(ctx)
                page = await ctx.new_page()
                await stealth.apply_stealth_async(page)
                await inject_stealth_scripts(page, "macOS")
                fp = SimpleNamespace(scr_w=1280, scr_h=800)

                try:
                    await page.goto(homepage, wait_until="domcontentloaded", timeout=12000)
                    await _wait_loaded(page, timeout=7000)
                    await _act_human(page, fp, quick=True)
                except Exception:
                    pass

                for idx, url in items:
                    started = time.time()
                    status = 0
                    final_url = url
                    html = ""
                    reason = "Quora rendered bulk source path"
                    cookies = []
                    try:
                        response = await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
                        status = response.status if response else 0
                        await _wait_loaded(page, timeout=12000)
                        await inject_helper_scripts(page)
                        await _act_human(page, fp, quick=False)
                        await _scroll_around(page, fp)
                        await _do_scroll(page, max_scrolls=5, timeout=8.0)
                        await asyncio.sleep(0.8)
                        html = await _capture_html(page)
                        final_url = page.url
                        cookies = await ctx.cookies()
                        if _looks_like_interstitial(html):
                            reason = "Quora bulk source path captured an interstitial"
                            status = status if status in (403, 429, 503) else 403
                    except Exception as exc:
                        reason = f"Quora bulk source path failed: {type(exc).__name__}"

                    results[idx] = SourceResult(
                        bool(html) and not _looks_like_interstitial(html),
                        html=html,
                        status_code=status,
                        reason=reason,
                        response_time=time.time() - started,
                        final_url=final_url,
                        cookies=cookies,
                    )

                await ctx.close()

            await browser.close()
    except Exception as exc:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        for idx, url in enumerate(urls):
            if results[idx] is None:
                results[idx] = SourceResult(
                    False,
                    status_code=0,
                    reason=f"Quora bulk source path failed: {type(exc).__name__}",
                    response_time=time.time() - start_all,
                    final_url=url,
                )

    return results


async def scrape_youtube(url, timeout=35.0):
    if not _is_youtube_watch_url(url):
        return await scrape_youtube_static(url, timeout=min(timeout, 15.0))

    start = time.time()
    browser = None
    fast = await scrape_youtube_watch_fast(url, timeout=min(timeout, 18.0))
    if fast.success:
        return fast

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
                **service_worker_context_options(),
                user_agent=CHROME120_MAC_UA,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                timezone_id="America/New_York",
                color_scheme="light",
            )
            await install_browser_network_guard(ctx)
            page = await ctx.new_page()
            response = await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            status = response.status if response else 0
            await _wait_loaded(page, timeout=10000)

            for _ in range(7):
                await page.evaluate("window.scrollBy(0, Math.max(700, window.innerHeight * 0.8))")
                await asyncio.sleep(0.8)

            try:
                await page.wait_for_selector(
                    "ytd-comment-thread-renderer, ytd-comment-renderer",
                    timeout=8000,
                )
            except Exception:
                pass

            data = await page.evaluate(
                """
                () => {
                  const text = (el) => (el && el.innerText || '').replace(/\\s+/g, ' ').trim();
                  const comments = [];
                  for (const node of document.querySelectorAll('ytd-comment-thread-renderer, ytd-comment-renderer')) {
                    const author = text(node.querySelector('#author-text, #author-text span'));
                    const body = text(node.querySelector('#content-text, yt-attributed-string#content-text'));
                    if (body && !comments.some((c) => c.body === body)) {
                      comments.push({author, body});
                    }
                    if (comments.length >= 40) break;
                  }
                  const title =
                    text(document.querySelector('h1 yt-formatted-string')) ||
                    text(document.querySelector('h1')) ||
                    document.title;
                  const channel = text(document.querySelector('ytd-video-owner-renderer #channel-name'));
                  const description = text(document.querySelector('#description-inline-expander, ytd-text-inline-expander'));
                  return { title, channel, description, comments };
                }
                """
            )

            final_url = page.url
            await ctx.close()
            await browser.close()

            comment_lines = []
            for item in data.get("comments") or []:
                author = _clean_line(item.get("author"))
                body = _clean_line(item.get("body"))
                if author and body:
                    comment_lines.append(f"{author}: {body}")
                elif body:
                    comment_lines.append(body)

            title = _clean_line(data.get("title")) or url
            sections = [
                ("Channel", [_clean_line(data.get("channel"))]),
                ("Description", [_clean_line(data.get("description"))]),
                ("Comments", comment_lines),
            ]
            html = _html_doc(title, final_url, sections)
            reason = (
                f"YouTube watch page with {len(comment_lines)} comments"
                if comment_lines
                else "YouTube watch page rendered, no comments found"
            )
            return SourceResult(
                True,
                html=html,
                status_code=status,
                reason=reason,
                response_time=time.time() - start,
                final_url=final_url,
            )
    except Exception as exc:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        return SourceResult(
            False,
            status_code=0,
            reason=f"YouTube source path failed: {type(exc).__name__}",
            response_time=time.time() - start,
            final_url=url,
        )


async def scrape_linkedin(url, timeout=30.0):
    """
    Super fast LinkedIn scraper that completely skips the heavy Playwright browser.
    We just rewrite the URLs to hit LinkedIn's hidden APIs and grab the data instantly!
    """
    start = time.time()
    
    # 0. Instantly reject User Profiles (they require auth cookies, and we disabled Playwright)
    if "/in/" in url:
        return SourceResult(
            success=False,
            html="",
            status_code=403,
            reason="User Profiles are strictly gated. Fast API bypass failed, and Playwright fallback is disabled.",
            response_time=time.time() - start,
            final_url=url,
        )
        
    # 1. Rewrite the URL if it's a Job or a Post
    try:
        if "/jobs/view/" in url:
            # Extract the job ID from the end of the URL slug
            slug = url.split('/jobs/view/')[1].split('/')[0].split('?')[0]
            job_id = slug.split('-')[-1]
            if job_id.isdigit():
                url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
                
        elif "/posts/" in url:
            import re
            match = re.search(r'-(activity|share|ugcPost)-([0-9]{19})', url)
            if match:
                urn_type = match.group(1)
                activity_id = match.group(2)
                url = f"https://www.linkedin.com/embed/feed/update/urn:li:{urn_type}:{activity_id}"
    except Exception:
        pass # If our trick fails to parse, we just try the original URL!

    # 2. Fetch the data using our lightning-fast HTTP client
    fast_result = await scrape_http_meta_page(url, "linkedin", timeout=min(timeout, 15.0))
    
    # 3. If we succeeded, awesome! Return the data.
    if fast_result and fast_result.success:
        return fast_result

    # 4. If we failed, we hard-stop here.
    # We DO NOT want to waste 80 seconds falling back to Playwright since we know it'll get blocked anyway.
    return SourceResult(
        success=False,
        html="",
        status_code=fast_result.status_code if fast_result else 0,
        reason="Fast API bypass failed (Target is likely gated). Playwright fallback disabled per user rules.",
        response_time=time.time() - start,
        final_url=url,
    )


async def scrape_linkedin_bulk(urls, timeout=15.0):
    """
    Super fast BULK LinkedIn scraper! 
    Instead of opening 10 heavy Playwright tabs, we just rewrite all the URLs and fetch them concurrently.
    """
    rewritten_urls = []
    
    for url in urls:
        new_url = url
        try:
            if "/jobs/" in url:
                if "/view/" in url:
                    slug = url.split('/jobs/view/')[1].split('/')[0].split('?')[0]
                    job_id = slug.split('-')[-1]
                else:
                    job_id = url.split('/')[-1].split('?')[0]
                if job_id.isdigit():
                    new_url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
                    
            elif "/posts/" in url:
                import re
                match = re.search(r'-(activity|share|ugcPost)-([0-9]{19})', url)
                if match:
                    urn_type = match.group(1)
                    activity_id = match.group(2)
                    new_url = f"https://www.linkedin.com/embed/feed/update/urn:li:{urn_type}:{activity_id}"
        except Exception:
            pass
            
        rewritten_urls.append(new_url)
        
    # Fetch in chunks of 2 to prevent LinkedIn's strict IP rate-limiting
    results = []
    for i in range(0, len(rewritten_urls), 2):
        chunk = rewritten_urls[i:i+2]
        chunk_res = await scrape_http_meta_bulk(chunk, "linkedin", timeout=timeout)
        results.extend(chunk_res)
        if i + 2 < len(rewritten_urls):
            await asyncio.sleep(2.0)
    
    # Clean up results for any Profiles that slipped through
    for i, orig_url in enumerate(urls):
        if "/in/" in orig_url and results[i]:
            results[i].success = False
            results[i].status_code = 403
            results[i].reason = "User Profiles are strictly gated. Playwright disabled."
            
    return results


LINKEDIN_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)

_LINKEDIN_HARD_WALL = [
    "authwall",
    "checkpoint",
    "security verification",
    "please verify",
    "sign in to",
    "join now",
    "log in to linkedin",
]

_LINKEDIN_PUBLIC_MARKERS = [
    "overview",
    "about us",
    "followers",
    "view all",
    "employees",
    "jobs",
    "post",
    "activity",
    "experience",
    "education",
    "skills",
    "recommendations",
    "connections",
    "about",
    "company size",
    "headquarters",
    "founded",
    "industry",
]




source_linkedin_bulk = scrape_linkedin_bulk


# The original protected-site implementations below this registry were based on
# Zendriver subprocesses.  They could not apply the request-time SSRF guard and
# buffered unrestricted HTML through stdout.  Keep the public route names, but
# make every active protected-source route use the same guarded Playwright
# capture that powers Layer 5.
def _bounded_source_html(html):
    html = html or ""
    limit = max_response_bytes()
    encoded = html.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return html, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


async def _guarded_hard_source(
    url,
    source,
    *,
    timeout=45.0,
    cookies=None,
    solve_challenges=False,
):
    # The parameter remains for compatibility with older callers.  Browser
    # captures are passive; the CAPTCHA solver was removed.
    del solve_challenges
    result = await full_scrape(
        url,
        timeout=max(1.0, min(float(timeout or 45.0), 90.0)),
        retries=0,
        cookies=cookies,
    )
    html, truncated = _bounded_source_html(result.html)
    reason = f"{source} guarded browser source path"
    if truncated:
        reason += "; response truncated to configured limit"
    if not result.success:
        reason = f"{reason} failed: {result.reason}"
    return SourceResult(
        bool(result.success and html),
        html=html,
        status_code=result.status_code,
        reason=reason,
        response_time=result.response_time,
        final_url=result.final_url or url,
        cookies=result.cookies,
    )


async def _guarded_hard_bulk(
    urls,
    source,
    *,
    timeout=45.0,
    cookies=None,
    solve_challenges=False,
):
    # ``scrape_source_bulk`` already holds one browser-admission slot.  Run one
    # capture at a time here instead of opening an unbounded tab fan-out inside
    # that slot.
    return [
        await _guarded_hard_source(
            url,
            source,
            timeout=timeout,
            cookies=cookies,
            solve_challenges=solve_challenges,
        )
        for url in urls
    ]





async def scrape_bestbuy(url, timeout=45.0, cookies=None, solve_challenges=False):
    url = _bestbuy_url(url)
    return await _guarded_hard_source(url, "Best Buy", timeout=timeout, cookies=cookies, solve_challenges=solve_challenges)

async def scrape_bestbuy_bulk(urls, timeout=45.0, cookies=None, solve_challenges=False):
    urls = [_bestbuy_url(u) for u in (urls or [])]
    return await _guarded_hard_bulk(urls, "Best Buy", timeout=timeout, cookies=cookies, solve_challenges=solve_challenges)

async def _ebay_playwright_warm_bulk(urls, timeout=65.0, cookies=None, solve_challenges=False):
    urls = list(urls or [])
    if not urls:
        return []

    from .browser_identity import make_identity
    from .browser_scraping import _restore_cookies, _close_browser_page

    fp = make_identity()
    stealth = Stealth()

    proxy_settings = browser_proxy_options()

    results = []

    async def _filter_trackers(route, request):
        rtype = getattr(request, "resource_type", "")
        req_url = getattr(request, "url", "").lower()
        if rtype in ["image", "media", "font"] or any(t in req_url for t in [
            "analytics", "telemetry", "ebayadservices", "doubleclick",
            "scorecardresearch", "criteo", "adsystem", "googletagmanager"
        ]):
            try:
                await route.abort("blockedbyclient")
            except Exception:
                pass
        else:
            try:
                await route.continue_()
            except Exception:
                pass

    try:
        async with async_playwright() as p:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-infobars",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--ignore-certificate-errors",
            ]
            launch_kwargs = {
                "headless": _browser_headless(),
                **_browser_launch_options(),
                "args": launch_args,
            }
            if proxy_settings:
                launch_kwargs["proxy"] = proxy_settings

            browser = await p.chromium.launch(**launch_kwargs)
            ctx = await browser.new_context(
                ignore_https_errors=True,
                user_agent=fp.ua_string,
                viewport={"width": fp.scr_w, "height": fp.scr_h},
                locale=fp.lang,
                timezone_id=fp.tz,
                color_scheme="light",
                **service_worker_context_options(),
            )
            await install_browser_network_guard(ctx, _filter_trackers)
            if cookies:
                await _restore_cookies(ctx, cookies)

            page = await ctx.new_page()
            await stealth.apply_stealth_async(page)
            await inject_stealth_scripts(page, fp.os_name)

            # Warmup session on eBay homepage to obtain Akamai cookies if not already active
            active_cookies = await ctx.cookies()
            has_ebay_cookies = any("ebay" in c.get("domain", "").lower() for c in active_cookies)
            if not has_ebay_cookies:
                try:
                    await page.goto("https://www.ebay.com/", wait_until="domcontentloaded", timeout=45000)
                    await asyncio.sleep(1.5)
                    await _act_human(page, fp, quick=True)
                except Exception:
                    pass

            captured_cookies = await ctx.cookies()

            for url in urls:
                start_time = time.time()
                success = False
                html = ""
                status = 0
                reason = "eBay warm session"
                elapsed = 0.0

                for attempt in range(2):
                    try:
                        resp = await page.goto(url, wait_until="domcontentloaded", timeout=int((timeout or 50.0) * 1000))
                        status = resp.status if resp else 0
                        await _wait_loaded(page, timeout=5000)
                        await inject_helper_scripts(page)
                        await _scroll_around(page, fp)
                        html = await _capture_html(page)
                        elapsed = time.time() - start_time

                        html_len = len(html or "")
                        text_l = (html or "").lower()
                        is_blocked = "error page | ebay" in text_l or status in (403, 503) or html_len < 10000
                        success = not is_blocked and status == 200
                        if not success:
                            reason = f"eBay page block/status={status}"
                        break
                    except Exception as e:
                        if attempt == 0:
                            await asyncio.sleep(2.0)
                            continue
                        elapsed = time.time() - start_time
                        reason = f"eBay navigation failed: {e}"

                html_bounded, truncated = _bounded_source_html(html)
                if truncated:
                    reason += "; truncated"

                results.append(SourceResult(
                    success=success,
                    html=html_bounded,
                    status_code=status,
                    reason=reason,
                    response_time=elapsed,
                    final_url=page.url or url,
                    cookies=captured_cookies,
                ))
                await asyncio.sleep(1.0)

            await _close_browser_page(page, ctx, browser)
    except Exception as exc:
        for url in urls[len(results):]:
            results.append(SourceResult(
                success=False,
                html="",
                status_code=0,
                reason=f"eBay warm bulk exception: {exc}",
                response_time=0.0,
                final_url=url,
            ))

    return results

async def scrape_ebay(url, timeout=65.0, cookies=None, solve_challenges=False):
    res = await _ebay_playwright_warm_bulk([url], timeout=timeout, cookies=cookies, solve_challenges=solve_challenges)
    return res[0] if res else SourceResult(False, reason="eBay scrape returned empty result")

async def scrape_ebay_bulk(urls, timeout=65.0, cookies=None, solve_challenges=False):
    return await _ebay_playwright_warm_bulk(urls, timeout=timeout, cookies=cookies, solve_challenges=solve_challenges)

async def scrape_zillow(url, timeout=45.0, cookies=None, solve_challenges=False):
    fast = await _scrape_zillow_referer_bypass(url, timeout=min(timeout or 12.0, 12.0))
    if fast and fast.success:
        return fast
    return await _guarded_hard_source(url, "Zillow", timeout=timeout, cookies=cookies, solve_challenges=solve_challenges)

async def scrape_zillow_bulk(urls, timeout=45.0, cookies=None, solve_challenges=False):
    sem = asyncio.Semaphore(5)
    async def one(u):
        async with sem:
            fast = await _scrape_zillow_referer_bypass(u, timeout=min(timeout or 12.0, 12.0))
            if fast and fast.success:
                return fast
            return await _guarded_hard_source(u, "Zillow", timeout=timeout, cookies=cookies, solve_challenges=solve_challenges)
    return await asyncio.gather(*(one(u) for u in (urls or [])))

async def scrape_homedepot(url, timeout=45.0, cookies=None, solve_challenges=False):
    return await _guarded_hard_source(url, "Home Depot", timeout=timeout, cookies=cookies, solve_challenges=solve_challenges)

async def scrape_homedepot_bulk(urls, timeout=45.0, cookies=None, solve_challenges=False):
    return await _guarded_hard_bulk(urls, "Home Depot", timeout=timeout, cookies=cookies, solve_challenges=solve_challenges)

async def scrape_bloomberg(url, timeout=45.0, cookies=None, solve_challenges=False):
    return await _guarded_hard_source(url, "Bloomberg", timeout=timeout, cookies=cookies, solve_challenges=solve_challenges)

async def scrape_bloomberg_bulk(urls, timeout=45.0, cookies=None, solve_challenges=False):
    return await _guarded_hard_bulk(urls, "Bloomberg", timeout=timeout, cookies=cookies, solve_challenges=solve_challenges)

async def scrape_tripadvisor(url, timeout=45.0, cookies=None, solve_challenges=False):
    return await _guarded_hard_source(url, "Tripadvisor", timeout=timeout, cookies=cookies, solve_challenges=solve_challenges)

async def scrape_tripadvisor_bulk(urls, timeout=45.0, cookies=None, solve_challenges=False):
    return await _guarded_hard_bulk(urls, "Tripadvisor", timeout=timeout, cookies=cookies, solve_challenges=solve_challenges)



async def scrape_perimeterx(url, timeout=45.0, cookies=None, solve_challenges=False):
    return await _guarded_hard_source(url, "protected-site", timeout=timeout, cookies=cookies, solve_challenges=solve_challenges)

async def scrape_perimeterx_bulk(urls, timeout=45.0, cookies=None, solve_challenges=False):
    return await _guarded_hard_bulk(urls, "protected-site", timeout=timeout, cookies=cookies, solve_challenges=solve_challenges)

async def scrape_belk(url, timeout=45.0, cookies=None, solve_challenges=False):
    return await _guarded_hard_source(url, "Belk", timeout=timeout, cookies=cookies, solve_challenges=solve_challenges)



async def scrape_linkedin_v2(url, timeout=30.0):
    "Compatibility alias for the guarded LinkedIn source route."
    return await scrape_linkedin(url, timeout=timeout)

source_linkedin = scrape_linkedin_v2
