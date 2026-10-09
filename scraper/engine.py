import asyncio
from collections import OrderedDict
from html.parser import HTMLParser
import inspect
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from .browser_identity import make_identity
from .browser_admission import browser_admission
from .anti_bot_signals import vendor_diagnostics
from .browser_scraping import full_scrape, stealth_scrape
from .http_scraping import QuickResult, crawler_scrape, mobile_scrape, quick_scrape, raw_scrape, advanced_waf_scrape
from .proxy_config import proxy_timeout_bonus
from .site_memory import SiteMemory
from .source_fast_paths import source_bulk_scrape, source_name, source_scrape
from .ultimate_stealth import scrape_ultimate


CRAWLER_PERSONAS = [
    ("googlebot", "layer3_crawler"),
    ("bingbot", "layer3_crawler_bing"),
    ("duckduckbot", "layer3_crawler_duckduckgo"),
]

def _http_attempt_timeout() -> float:
    raw = (os.getenv("YADA_HTTP_ATTEMPT_TIMEOUT") or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return 10.0
    return value if 0.5 <= value <= 120.0 else 10.0


def _source_call_timeout() -> float:
    """Budget for one whole named-source call (single or bulk).

    The source path used to run unbounded and could eat the entire request
    budget (askKara kills at 100s) before the layer ladder got a chance.
    Cap it: default 90s (slow observed success was bestbuy at ~85s) plus the
    proxy round-trip bonus, tunable via ``YADA_SOURCE_CALL_TIMEOUT``.
    """
    raw = (os.getenv("YADA_SOURCE_CALL_TIMEOUT") or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return 90.0
    value = value if 1.0 <= value <= 600.0 else 90.0
    return value + proxy_timeout_bonus()


def _scrape_budget() -> float:
    """Overall wall-clock budget for one scrape call, all layers combined.

    Per-attempt timers existed but nothing bounded the whole ladder, so a
    slow run could take minutes and be killed from outside (API 504) with
    nothing returned. Default 105s: above the slowest observed success
    (~85s Best Buy), below the API's own 120s wait.
    Tunable via YADA_SCRAPE_BUDGET.
    """
    raw = (os.getenv("YADA_SCRAPE_BUDGET") or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return 105.0
    return value if 10.0 <= value <= 600.0 else 105.0


# Do not start a ladder stage with less than this many seconds left; it
# could not finish anyway and we would rather return what we already have.
_BUDGET_STOP_FLOOR = 1.0


MOBILE_AUTO_DOMAINS = {"indeed.com"}
HTTP_ONLY_SOURCE_NAMES = frozenset({
    "medium",
    "youtube",
    "instagram",
    "hackernews",
    "reddit",
    "semantic_scholar",
    "pubmed",
    "crates",
    "github",
    "arxiv",
    "acl_anthology",
    "stackexchange",
    "remotive",
    "clinicaltrials",
    "discourse",
    "mastodon",
    "sec",
    "zacks",
    "monster",
    "pytorch",
    "walmart",
})

# Sources that can serve a browser-free static path when allow_browser is
# False (their normal source function may launch a browser, but a static
# variant exists and is selected downstream via allow_browser=False).
STATIC_HTTP_SOURCE_NAMES = frozenset({
    "quora",
})


def _max_content_bytes():
    default = 8 * 1024 * 1024
    try:
        configured = int(os.getenv("YADA_MAX_CONTENT_BYTES", str(default)))
    except (TypeError, ValueError):
        return default
    return configured if configured > 0 else default


_LONE_SURROGATES = re.compile(r"[\ud800-\udfff]")


def _bounded_html(html):
    """Return UTF-8-safe bounded text and optional truncation metadata."""
    if not html:
        return html, None
    try:
        encoded = html.encode("utf-8")
    except UnicodeEncodeError:
        html = _LONE_SURROGATES.sub("\ufffd", html)
        encoded = html.encode("utf-8")
    limit = _max_content_bytes()
    if len(encoded) <= limit:
        return html, None
    bounded = encoded[:limit].decode("utf-8", errors="ignore")
    return bounded, {
        "content_truncated": True,
        "content_original_bytes": len(encoded),
        "content_returned_bytes": len(bounded.encode("utf-8")),
        "content_limit_bytes": limit,
    }


def _mobile_auto_enabled(url):
    from urllib.parse import urlparse

    host = urlparse(url).netloc.lower().split(":", 1)[0]
    return any(host == domain or host.endswith(f".{domain}") for domain in MOBILE_AUTO_DOMAINS)


CAPTCHA_PATTERNS = {
    "cloudflare_turnstile": [
        re.compile(r"challenges\.cloudflare\.com/turnstile", re.I),
        re.compile(r"cf-turnstile", re.I),
    ],
    "recaptcha_v2": [
        re.compile(r"google\.com/recaptcha/api", re.I),
        re.compile(r"g-recaptcha", re.I),
        re.compile(r"grecaptcha", re.I),
    ],
    "recaptcha_v3": [
        re.compile(r"recaptcha/api\.js\?.*render=", re.I),
        re.compile(r"grecaptcha\.execute", re.I),
        re.compile(r"recaptcha/enterprise\.js", re.I),
        re.compile(r"grecaptcha\.enterprise\.execute", re.I),
    ],
    "hcaptcha": [
        re.compile(r"hcaptcha\.com/1/api", re.I),
        re.compile(r"h-captcha", re.I),
    ],
    "datadome": [
        re.compile(r"datadome\.co", re.I),
        re.compile(r"captcha-delivery\.com", re.I),
        re.compile(r"\bdatadome\b", re.I),
    ],
    "perimeterx": [
        re.compile(r"perimeterx\.net", re.I),
        re.compile(r"px-captcha", re.I),
        re.compile(r"window\._pxAppId", re.I),
        re.compile(r"\bauditor\.js\b", re.I),
        re.compile(r"fst-ec\.perimeterx\.net", re.I),
        re.compile(r"\b_px(?:3|hd|_vid)\b", re.I),
        re.compile(r"px-(?:cdn|chk|client)\.net", re.I),
        re.compile(r"/xhr/api/v2/collector", re.I),
    ],
    "cloudflare_challenge": [
        re.compile(r"checking your browser", re.I),
        re.compile(r"cf-browser-verification", re.I),
        re.compile(r"just a moment\.\.\.", re.I),
    ],
    "generic_captcha": [
        re.compile(r"captcha", re.I),
    ],
}


def find_captcha(html):
    if not html:
        return None

    chunk = html[:100000]

    for ctype in [
        "cloudflare_turnstile", "recaptcha_v3", "recaptcha_v2",
        "hcaptcha", "datadome", "perimeterx", "cloudflare_challenge",
        "generic_captcha",
    ]:
        for r in CAPTCHA_PATTERNS[ctype]:
            if r.search(chunk):
                return ctype

    return None




_TEXT_CACHE = OrderedDict()
_TEXT_CACHE_MAX_ENTRIES = 8
_TEXT_CACHE_MAX_INPUT = 2_000_000


def _text_from_html(html):
    if not html:
        return ""
    cacheable = len(html) <= _TEXT_CACHE_MAX_INPUT
    if cacheable:
        hit = _TEXT_CACHE.get(html)
        if hit is not None:
            _TEXT_CACHE.move_to_end(html)
            return hit
    try:
        from bs4 import BeautifulSoup
        parser = "xml" if html.lstrip().startswith("<?xml") else "lxml"
        soup = BeautifulSoup(html, parser)
        text = soup.get_text(" ", strip=True)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
    if cacheable:
        _TEXT_CACHE[html] = text
        _TEXT_CACHE.move_to_end(html)
        while len(_TEXT_CACHE) > _TEXT_CACHE_MAX_ENTRIES:
            _TEXT_CACHE.popitem(last=False)
    return text


def _known_false_positive_diagnostics(html="", text="", reason="", url=""):
    html = html or ""
    text = text or _text_from_html(html)
    reason = reason or ""
    haystack = " ".join((text[:12000], html[:60000], reason)).lower()
    diagnostics = []
    host = ""
    try:
        from urllib.parse import urlparse
        host = urlparse(url or "").netloc.lower()
        if host.startswith("www."):
            host = host[4:]
    except Exception:
        host = ""

    if (
        ("bestbuy." in host or "best buy" in haystack)
        and "choose a country" in haystack
        and "united states" in haystack
        and "canada" in haystack
    ):
        diagnostics.append("false_positive:bestbuy_country_selector")

    if "scholar.google." in host and "the system can't perform the operation now" in haystack:
        diagnostics.append("false_positive:google_scholar_temporary_block")

    if "homedepot." in host or "home depot" in haystack:
        if "access denied" in haystack or "oops!! something went wrong" in haystack:
            diagnostics.append("false_positive:homedepot_error_shell")

    if (
        "stackexchange.com" in host
        or host in {"stackoverflow.com", "serverfault.com", "superuser.com"}
        or "stack exchange" in haystack
    ) and (
        "page not found - " in haystack
        or "page not found |" in haystack
        or "question not found" in haystack
    ):
        diagnostics.append("false_positive:stackexchange_page_not_found")

    if "walmart." in host and "we're having a technical issue" in haystack:
        diagnostics.append("false_positive:walmart_technical_issue")

    if "glassdoor." in host or "glassdoor" in haystack:
        glassdoor_auth_markers = (
            "sign in to glassdoor",
            "glassdoor login",
            "create an account or sign in",
            "security check",
            "authentication",
            "verify your identity",
            "access your account",
        )
        if any(marker in haystack for marker in glassdoor_auth_markers):
            diagnostics.append("false_positive:glassdoor_auth_wall")

    if "quora." in host or '"__typename"' in haystack or '"errors"' in haystack:
        quora_graphql_error = (
            ("quora" in haystack or "qgraphql" in haystack or "graphql" in haystack)
            and (
                '"status":403' in haystack
                or '"status": 403' in haystack
                or '"errors"' in haystack
                or '"error"' in haystack
            )
        )
        if quora_graphql_error:
            diagnostics.append("false_positive:quora_graphql_error")

    url_l = (url or "").lower()
    if (
        url_l.startswith("chrome-error://")
        or "chromewebdata" in url_l
        or "chrome-error://" in haystack
        or "chromewebdata" in haystack
    ):
        diagnostics.append("false_positive:chrome_error_page")

    return diagnostics


def _diagnostics_for_capture(html=None, status_code=0, reason="", cookies=None, url=""):
    diagnostics = []
    html = html or ""
    reason_l = (reason or "").lower()
    text = _text_from_html(html)
    html_l = html.lower()
    short_text = text[:5000].lower()

    diagnostics.extend(vendor_diagnostics(html=html, cookies=cookies, url=url))

    if status_code == 403:
        diagnostics.append("http_403")
    elif status_code == 429:
        diagnostics.append("http_429")
    elif status_code == 503:
        diagnostics.append("http_503")
    elif status_code >= 400:
        diagnostics.append(f"http_{status_code}")

    ctype = find_captcha(html)
    if ctype:
        diagnostics.append(f"captcha_marker_seen:{ctype}")

    if "access denied" in short_text or "<title>access denied" in html_l[:10000]:
        diagnostics.append("access_denied_text_seen")
    if "you've been blocked by network security" in short_text:
        diagnostics.append("network_security_block_seen")
    if (
        "are you a robot" in short_text
        or "verify you are human" in short_text
        or "verify your session" in short_text
        or "human verification" in short_text
        or "humans only" in short_text
        or "performing security verification" in short_text
    ):
        diagnostics.append("human_verification_text_seen")

    login_markers = ("sign in", "log in", "login", "create account", "subscribe to continue")
    if len(text) < 2000 and any(marker in short_text for marker in login_markers):
        diagnostics.append("login_wall_possible")

    js_shell_markers = (
        '<div id="root"></div>',
        "<div id='root'></div>",
        '<div id="app"></div>',
        '<div id="__next"></div>',
        "<app-root></app-root>",
        "<app-root",
        "enable javascript",
        "javascript is required",
    )
    if any(marker in html_l[:20000] for marker in js_shell_markers):
        diagnostics.append("possible_js_shell")

    if html and len(text) < 100 and ("<html" in html_l or "<body" in html_l):
        diagnostics.append("thin_text")

    if "captcha" in reason_l and not any(d.startswith("captcha_marker_seen") for d in diagnostics):
        diagnostics.append("captcha_reason_seen")
    if "challenge" in reason_l:
        diagnostics.append("challenge_reason_seen")
    if "timeout" in reason_l:
        diagnostics.append("timeout")
    if "failed" in reason_l and not html:
        diagnostics.append("no_capture_error")

    diagnostics.extend(_known_false_positive_diagnostics(html, text, reason, url))

    return sorted(set(diagnostics))


def _capture_state(captured, diagnostics):
    if not captured:
        return "no_capture"
    return "captured_with_warnings" if diagnostics else "captured"


class _MicroPageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.has_script = False
        self.has_noscript = False
        self.has_iframe = False
        self.has_meta_refresh = False
        self.has_mount_point = False

    def handle_starttag(self, tag, attrs):
        tag_l = tag.lower()
        if tag_l == "script":
            self.has_script = True
        elif tag_l == "noscript":
            self.has_noscript = True
        elif tag_l in ("iframe", "frameset", "frame"):
            self.has_iframe = True
        elif tag_l == "app-root":
            self.has_mount_point = True

        attrs_dict = {k.lower(): (v or "").lower() for k, v in attrs}
        if tag_l == "meta" and attrs_dict.get("http-equiv") == "refresh":
            self.has_meta_refresh = True

        tag_id = attrs_dict.get("id", "")
        if tag_id in ("root", "app", "__next"):
            self.has_mount_point = True


def _is_static_complete_micro_page(html, status_code, diagnostics, text=None, content_type=None):
    if (status_code // 100) != 2:
        return False
    if text is None:
        text = _text_from_html(html) if html else ""
    if not text.strip():
        return False
    diag_set = set(diagnostics or [])
    if diag_set - {"thin_text"}:
        return False
    if content_type and "html" not in content_type.lower():
        return True
    html_stripped = (html or "").strip()
    if not (html_stripped.startswith("<") or "<html" in html_stripped.lower() or "<body" in html_stripped.lower()):
        return True
    parser = _MicroPageParser()
    try:
        parser.feed(html)
    except Exception:
        return False
    return not (
        parser.has_script
        or parser.has_noscript
        or parser.has_iframe
        or parser.has_meta_refresh
        or parser.has_mount_point
    )


def _retry_recommended(html, status_code, diagnostics, reason="", text_len=None, content_type=None):
    if not html:
        return True

    if text_len is None:
        text_len = len(_text_from_html(html))

    diagnostics = set(diagnostics or [])
    reason_l = (reason or "").lower()

    if status_code in (403, 429, 503):
        return True

    if status_code >= 400:
        return True

    if "no_capture_error" in diagnostics or "timeout" in diagnostics:
        return True

    if "network_security_block_seen" in diagnostics:
        return True

    if any(item.startswith("false_positive:") for item in diagnostics):
        return True

    if "thin_text" in diagnostics and text_len < 500:
        if _is_static_complete_micro_page(html, status_code, diagnostics, content_type=content_type):
            pass
        else:
            return True

    if "possible_js_shell" in diagnostics and text_len < 800:
        return True

    short_page_warnings = {
        "access_denied_text_seen",
        "login_wall_possible",
        "challenge_reason_seen",
        "captcha_reason_seen",
        "human_verification_text_seen",
    }
    if diagnostics.intersection(short_page_warnings) and text_len < 2500:
        return True

    captcha_seen = any(d.startswith("captcha_marker_seen") for d in diagnostics)
    if captcha_seen and ("blocked" in reason_l or "challenge" in reason_l):
        return True

    if captcha_seen and text_len < 2500:
        return True

    return False


def _protected_retry_capture(attempt, solve_challenges=False):
    # Kept only for backward-compatible direct calls.  There is no active
    # challenge solver, so retry policy is independent of this old flag.
    del solve_challenges
    if not attempt.get("captured") or not attempt.get("retry_recommended"):
        return False
    status = attempt.get("status_code") or 0
    diagnostics = set(attempt.get("diagnostics") or [])
    if status not in (403, 429, 503):
        return False
    protected_markers = {
        "access_denied_text_seen",
        "network_security_block_seen",
        "human_verification_text_seen",
        "challenge_reason_seen",
        "captcha_reason_seen",
    }
    return bool(diagnostics.intersection(protected_markers)) or any(
        item.startswith("captcha_marker_seen") or item.startswith("vendor_marker_seen")
        for item in diagnostics
    )


def _attempt_from_result(layer, res):
    html = getattr(res, "html", None) or ""
    text_len = len(_text_from_html(html)) if html else 0
    status_code = getattr(res, "status_code", 0) or 0
    reason = getattr(res, "reason", "") or ""
    diagnostics = _diagnostics_for_capture(
        html,
        status_code,
        reason,
        cookies=getattr(res, "cookies", None),
        url=getattr(res, "final_url", None),
    )
    content_type = getattr(res, "content_type", None) or getattr(res, "headers", {}).get("content-type")
    retry_recommended = _retry_recommended(
        html,
        status_code,
        diagnostics,
        reason,
        text_len,
        content_type=content_type,
    )
    return {
        "layer": layer,
        "success": bool(getattr(res, "success", False)),
        "captured": bool(html),
        "capture_state": _capture_state(bool(html), diagnostics),
        "retry_recommended": retry_recommended,
        "status_code": status_code,
        "reason": reason,
        "time": getattr(res, "response_time", 0.0) or 0.0,
        "html_length": len(html),
        "text_length": text_len,
        "diagnostics": diagnostics,
        "final_url": getattr(res, "final_url", None),
    }


async def _bounded_http(coro, url, label, timeout=None, deadline=None):
    cap = timeout if timeout is not None else _http_attempt_timeout() + proxy_timeout_bonus()
    if deadline is not None:
        remaining = deadline - time.time()
        if remaining <= 0:
            if asyncio.iscoroutine(coro):
                coro.close()  # caller already built it, so close it to avoid "never awaited"
            return QuickResult(
                False,
                status_code=0,
                needs_browser=True,
                reason=f"{label} skipped: scrape budget exhausted",
                response_time=0.0,
                final_url=url,
            )
        cap = min(cap, remaining)
    try:
        return await asyncio.wait_for(coro, timeout=cap)
    except asyncio.TimeoutError:
        return QuickResult(
            False,
            status_code=0,
            needs_browser=True,
            reason=f"{label} timed out after {cap}s",
            response_time=cap,
            final_url=url,
        )


def _capture_score(capture):
    html_len = capture.get("html_length", 0)
    text_len = capture.get("text_length", 0)
    status = capture.get("status_code", 0)
    diagnostics = capture.get("diagnostics", [])
    score = min(html_len, 200000) * 0.001 + min(text_len, 50000)
    if 200 <= status < 400:
        score += 500
    if status in (403, 429, 503):
        score -= 250
    if "possible_js_shell" in diagnostics:
        score -= 500
    if "thin_text" in diagnostics:
        score -= 200
    if capture.get("retry_recommended"):
        score -= 750
    return score


def _capture_from_result(layer, res):
    attempt = _attempt_from_result(layer, res)
    html = getattr(res, "html", None) or ""
    status_code = getattr(res, "status_code", 0) or attempt.get("status_code", 0) or 0
    final_url = (getattr(res, "final_url", "") or getattr(res, "url", "") or "").lower()
    is_empty_skeleton = (
        status_code == 0
        and (
            final_url == "about:blank"
            or html.strip() in {
                "<html><head></head><body></body></html>",
                "<html><body></body></html>",
            }
        )
    )
    if not html or is_empty_skeleton or "false_positive:chrome_error_page" in attempt.get("diagnostics", []):
        return None
    capture = dict(attempt)
    capture["html"] = html
    capture["score"] = _capture_score(capture)
    return capture


def _remember_capture(captures, layer, res):
    capture = _capture_from_result(layer, res)
    if capture:
        captures.append(capture)
    return capture


CAPTCHA_DESCRIPTIONS = {
    "cloudflare_turnstile": "Cloudflare Turnstile marker seen.",
    "recaptcha_v2": "Google reCAPTCHA v2 marker seen.",
    "recaptcha_v3": "Google reCAPTCHA v3 marker seen.",
    "hcaptcha": "hCaptcha marker seen.",
    "datadome": "DataDome marker seen.",
    "perimeterx": "PerimeterX marker seen.",
    "cloudflare_challenge": "Cloudflare challenge marker seen.",
    "generic_captcha": "CAPTCHA marker seen.",
}


def _summarize_attempts(attempts, last_html=None):
    info = {
        "summary": "",
        "details": [],
        "captcha_detected": None,
        "captcha_info": None,
        "suggestion": "",
    }

    ctype = find_captcha(last_html) if last_html else None

    if ctype:
        info["captcha_detected"] = ctype
        info["captcha_info"] = CAPTCHA_DESCRIPTIONS.get(ctype, "Unknown CAPTCHA.")

    for att in attempts:
        name = att["layer"]
        reason = att.get("reason", "")
        code = att.get("status_code", 0)

        detail = f"Layer '{name}'"
        if code == 403:
            detail += ": HTTP 403."
        elif code == 503:
            detail += ": 503, likely a challenge page."
        elif code >= 400:
            detail += f": Got an error code {code}."
        elif "challenge" in reason.lower():
            detail += ": Challenge marker mentioned."
        elif "javascript" in reason.lower():
            detail += ": Browser rendering may help."
        elif "thin content" in reason.lower():
            detail += ": Thin page text."
        else:
            detail += f": {reason}"

        info["details"].append(detail)

    total = len(attempts)

    if ctype:
        info["summary"] = f"Captured content with challenge markers after {total} layer attempts."
        info["suggestion"] = "Review captured content and diagnostics before deciding if a deeper retry is useful."
    elif any(a.get("status_code") == 403 for a in attempts):
        info["summary"] = f"HTTP 403 appeared during {total} layer attempts."
        info["suggestion"] = "Review captured body; it may be an error shell or still contain useful payload."
    else:
        info["summary"] = f"Tried {total} layers without a clean capture."
        info["suggestion"] = "Use captured output and diagnostics to decide the next step."

    return info


@dataclass
class ScrapeResult:
    ok: bool
    url: str
    data: Optional[dict] = None
    winning_layer: str = ""
    elapsed: float = 0.0
    tries: list = field(default_factory=list)
    err: str = ""
    notes: Optional[dict] = None
    captured: bool = False
    capture_state: str = "no_capture"
    diagnostics: list = field(default_factory=list)
    best_attempt: Optional[dict] = None
    html_length: int = 0
    text_length: int = 0
    status_code: int = 0
    final_url: Optional[str] = None
    content_truncation: Optional[dict] = None

    @property
    def usable(self) -> bool:
        return bool(self.ok and not bool((self.best_attempt or {}).get("retry_recommended")))

    def to_dict(self):
        out = {
            "success": self.ok,
            "usable": self.usable,
            "url": self.url,
            "content": self.data,
            "layer_used": self.winning_layer,
            "total_time": round(self.elapsed, 3),
            "layer_attempts": self.tries,
            "error": self.err,
            "captured": self.captured,
            "capture_state": self.capture_state,
            "diagnostics": self.diagnostics,
            "best_attempt": self.best_attempt,
            "html_length": self.html_length,
            "text_length": self.text_length,
            "status_code": self.status_code,
            "final_url": self.final_url or self.url,
        }
        if self.notes:
            out["diagnosis"] = self.notes
        if self.content_truncation:
            out.update(self.content_truncation)
        return out


@dataclass
class CompareData:
    url: str
    total_time: float
    layers: list = field(default_factory=list)
    fastest: Optional[str] = None
    tip: str = ""

    def to_dict(self):
        return {
            "url": self.url,
            "total_time": round(self.total_time, 3),
            "layers": self.layers,
            "fastest_successful": self.fastest,
            "recommendation": self.tip,
        }


class ScraperEngine:

    def __init__(self):
        self.memory = SiteMemory()
        self._warmup_fingerprints = {}

    def _rewrite_reddit_url(self, url):
        from urllib.parse import urlparse, urlunparse
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower().rstrip(".")
            if host == "reddit.com" or host.endswith(".reddit.com"):
                path = parsed.path
                static_exts = (".png", ".jpg", ".jpeg", ".gif", ".js", ".css")
                if not path.endswith(".json") and not any(
                    path.endswith(ext) for ext in static_exts
                ):
                    is_comments = "/comments/" in path
                    is_subreddit_or_listing = "/r/" in path

                    if is_comments or is_subreddit_or_listing:
                        clean_path = path.rstrip("/") + ".json"
                        rewritten_url = urlunparse((
                            parsed.scheme,
                            parsed.netloc,
                            clean_path,
                            parsed.params,
                            parsed.query,
                            parsed.fragment
                        ))
                        print(f"[Engine] Rewriting Reddit HTML URL to JSON API: {rewritten_url}")
                        return rewritten_url
        except Exception as e:
            print(f"[Engine] Error rewriting Reddit URL: {e}")
        return url

    def _should_skip_layer1(self, url):
        # Skip raw un-spoofed requests (layer1_simple) for known commercial/WAF targets to prevent IP poisoning
        try:
            return source_name(url) in {
                "walmart",
                "bestbuy",
                "ebay",
                "belk",
                "zillow",
                "homedepot",
                "bloomberg",
                "tripadvisor",
                "perimeterx",
                "linkedin",
                "instagram",
            }
        except Exception:
            pass
        return False

    async def scrape(self, url, mode="auto", allow_layer5=True, solve_challenges=False, skip_source=False, allow_browser=True):
        start = time.time()
        attempts = []
        fp = make_identity()

        if not allow_browser and mode in {"layer4", "layer5"}:
            raise ValueError(f"Browser mode '{mode}' is disabled because allow_browser is False")

        if mode == "layer1":
            res = await self._try_layer1(url, start, attempts)
        elif mode == "layer1c":
            res = await self._try_layer1c(url, start, attempts)
        elif mode == "layer6":
            res = await self._try_layer6(url, start, attempts)
        elif mode in {"layer1b", "mobile"}:
            res = await self._try_mobile(url, start, attempts)
        elif mode in {"source", "structured"}:
            res = await self._try_source(
                url,
                start,
                attempts,
                solve_challenges=solve_challenges,
                allow_browser=allow_browser,
            )
        elif mode == "layer2":
            res = await self._try_layer2(url, fp, start, attempts)
        elif mode == "layer3":
            res = await self._try_layer3(url, start, attempts)
        elif mode == "layer4":
            res = await self._try_layer4(
                url,
                make_identity(chrome_only=True),
                start,
                attempts,
                solve_challenges=solve_challenges,
            )
        elif mode == "layer5":
            res = await self._try_layer5(
                url,
                make_identity(chrome_only=True),
                start,
                attempts,
                solve_challenges=solve_challenges,
            )
        elif mode == "auto":
            res = await self._try_auto(
                url,
                fp,
                start,
                attempts,
                allow_layer5=allow_layer5,
                solve_challenges=solve_challenges,
                skip_source=skip_source,
                allow_browser=allow_browser,
            )
        else:
            raise ValueError(f"Unknown scrape mode: {mode}")

        await self.memory.record_attempt(
            url, res.tries,
            success_layer=res.winning_layer if res.ok else None,
        )

        return res

    async def compare(self, url, solve_challenges=False):
        start = time.time()
        fp1 = make_identity()
        fp_cookies = make_identity()
        fp2 = make_identity(chrome_only=True)
        fp3 = make_identity(chrome_only=True)
        valid_cookies = self.memory.get_valid_cookies(url)

        tasks = [
            raw_scrape(url),
            mobile_scrape(url),
            quick_scrape(url, fingerprint=fp1),
            crawler_scrape(url, bot_type="googlebot"),
            crawler_scrape(url, bot_type="bingbot"),
            crawler_scrape(url, bot_type="duckduckbot"),
            self._admitted_browser_call(
                stealth_scrape,
                url,
                fingerprint=fp2,
                solve_challenges=solve_challenges,
            ),
            self._admitted_browser_call(
                full_scrape,
                url,
                fingerprint=fp3,
                solve_challenges=solve_challenges,
            ),
            scrape_ultimate(url)
        ]

        if valid_cookies:
            tasks.append(quick_scrape(url, fingerprint=fp_cookies, cookies=valid_cookies))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        layers = []
        best_time = float("inf")
        best_layer = None

        def process_result(res, name, display_name):
            nonlocal best_time, best_layer
            if isinstance(res, Exception):
                layers.append({
                    "layer": name,
                    "display_name": display_name,
                    "success": False, "status_code": 0,
                    "captured": False,
                    "capture_state": "no_capture",
                    "reason": str(res), "time": 0.0,
                    "html_length": 0,
                    "text_length": 0,
                    "diagnostics": ["exception"],
                    "final_url": url,
                })
                return

            attempt = _attempt_from_result(name, res)

            layers.append({
                "layer": name,
                "display_name": display_name,
                **attempt,
            })

            if attempt["captured"] and not attempt["retry_recommended"] and res.response_time < best_time:
                best_time = res.response_time
                best_layer = name

        process_result(results[0], "layer1_simple", "Layer 1: Simple Request (Raw HTTP)")
        process_result(results[1], "layer1b_mobile", "Layer 1b: Mobile JA3 Emulation")
        process_result(results[2], "layer2_cookie_no_cookies", "Layer 2: Hybrid Cookie (No Cookies Cached)")
        process_result(results[3], "layer3_crawler", "Layer 3: Googlebot Bot Impersonation")
        process_result(results[4], "layer3_crawler_bing", "Layer 3: Bingbot Bot Impersonation")
        process_result(results[5], "layer3_crawler_duckduckgo", "Layer 3: DuckDuckBot Impersonation")
        process_result(results[6], "layer4_stealth", "Layer 4: Playwright (Stealth Headless)")
        process_result(results[7], "layer5_full", "Layer 5: Full Browser")
        process_result(results[8], "layer6_ultimate", "Layer 6: TLS Impersonation Fallback")

        if valid_cookies:
            process_result(results[9], "layer2_cookie", "Layer 2: Hybrid Cookie (With Cookies Cached)")

        fast_layer = best_layer in ("layer1_simple", "layer2_cookie") or (
            best_layer or ""
        ).startswith("layer3_crawler")
        tip = (
            "Use Layer 1, 2, or 3 when they capture enough content; they are faster."
            if fast_layer
            else "A browser layer is the better default for this target."
        )
        return CompareData(
            url=url,
            total_time=time.time() - start,
            layers=layers,
            fastest=best_layer,
            tip=tip,
        )

    async def _try_auto(self, url, fp, start, attempts, allow_layer5=True, solve_challenges=False, skip_source=False, allow_browser=True):
        html = None
        captures = []
        protected_block = None
        skip = self.memory.get_failed_layers(url)
        deadline = start + _scrape_budget()

        source = source_name(url)
        if (
            source
            and not skip_source
            and f"source_{source}" not in skip
            and (allow_browser or source in HTTP_ONLY_SOURCE_NAMES or source in STATIC_HTTP_SOURCE_NAMES)
        ):
            source_cookies = self.memory.get_valid_cookies(url)
            source_parameters = inspect.signature(source_scrape).parameters
            source_kwargs = {}
            if "cookies" in source_parameters:
                source_kwargs["cookies"] = source_cookies
            if "solve_challenges" in source_parameters:
                source_kwargs["solve_challenges"] = solve_challenges
            if "memory" in source_parameters:
                source_kwargs["memory"] = self.memory
            if "allow_browser" in source_parameters:
                source_kwargs["allow_browser"] = allow_browser
            try:
                res = await self._source_call(
                    source,
                    source_scrape,
                    url,
                    deadline=deadline,
                    **source_kwargs,
                )
            except Exception as exc:
                layer = f"source_{source}"
                failed = QuickResult(
                    success=False,
                    status_code=0,
                    needs_browser=True,
                    reason=f"Named source raised {type(exc).__name__}",
                    response_time=time.time() - start,
                    final_url=url,
                )
                attempts.append(_attempt_from_result(layer, failed))
                res = None
            if res:
                layer = f"source_{source}"
                attempt = _attempt_from_result(layer, res)
                attempts.append(attempt)
                _remember_capture(captures, layer, res)
                if res.success and not attempt["retry_recommended"]:
                    if getattr(res, "cookies", None):
                        await self.memory.save_cookies(url, res.cookies)
                    return self._pack_result(url, res.html, layer, start, attempts, res=res)
                
                if source == "linkedin":
                    # Strict Fast-Path Enforcement: Do not try Playwright or other layers for LinkedIn if the fast-path fails
                    return self._pack_result(url, res.html if res else None, layer, start, attempts, res=res)
                
                html = res.html or html

        if time.time() >= deadline - _BUDGET_STOP_FLOOR:
            return self._budget_stop(url, start, attempts, captures, html)

        if "layer1_simple" not in skip:
            if self._should_skip_layer1(url):
                print(f"[Engine] Skipping un-spoofed layer1_simple for {url} to prevent WAF session escalation.")
            else:
                res = await _bounded_http(raw_scrape(url), url, "Raw request", deadline=deadline)
                attempt = _attempt_from_result("layer1_simple", res)
                attempts.append(attempt)
                _remember_capture(captures, "layer1_simple", res)
                if res.success and not attempt["retry_recommended"]:
                    return self._pack_result(url, res.html, "layer1_simple", start, attempts, res=res)
                html = res.html or html

        if time.time() >= deadline - _BUDGET_STOP_FLOOR:
            return self._budget_stop(url, start, attempts, captures, html)

        if "layer2_cookie" not in skip:
            valid_cookies = self.memory.get_valid_cookies(url)
            res = await _bounded_http(quick_scrape(url, fingerprint=fp, cookies=valid_cookies), url, "Static request", deadline=deadline)
            attempt = _attempt_from_result("layer2_cookie", res)
            attempts.append(attempt)
            _remember_capture(captures, "layer2_cookie", res)
            if res.success and not attempt["retry_recommended"]:
                return self._pack_result(url, res.html, "layer2_cookie", start, attempts, res=res)
            # VULN-06 Contract Design: A protected 403/429 block (access denied, CAPTCHA, or
            # WAF challenge) is remembered here but escalation continues for now. The cheap
            # non-browser transports (layer1b, layer1c, layer6) still get their chance to
            # bypass it; the stop happens only after they are exhausted, before the crawler
            # and headless Chromium stages.
            if protected_block is None and _protected_retry_capture(attempt, solve_challenges=solve_challenges):
                protected_block = ("layer2_cookie", res)
            html = res.html or html

        if time.time() >= deadline - _BUDGET_STOP_FLOOR:
            return self._budget_stop(url, start, attempts, captures, html)

        if _mobile_auto_enabled(url) and "layer1b_mobile" not in skip:
            res = await _bounded_http(mobile_scrape(url), url, "Mobile request", deadline=deadline)
            attempt = _attempt_from_result("layer1b_mobile", res)
            attempts.append(attempt)
            _remember_capture(captures, "layer1b_mobile", res)
            if res.success and not attempt["retry_recommended"]:
                return self._pack_result(url, res.html, "layer1b_mobile", start, attempts, res=res)
            if protected_block is None and _protected_retry_capture(attempt, solve_challenges=solve_challenges):
                protected_block = ("layer1b_mobile", res)
            html = res.html or html
            
        if time.time() >= deadline - _BUDGET_STOP_FLOOR:
            return self._budget_stop(url, start, attempts, captures, html)

        if "layer1c_advanced_waf" not in skip:
            res = await _bounded_http(advanced_waf_scrape(url), url, "Advanced WAF request", deadline=deadline)
            attempt = _attempt_from_result("layer1c_advanced_waf", res)
            attempts.append(attempt)
            _remember_capture(captures, "layer1c_advanced_waf", res)
            if res.success and not attempt["retry_recommended"]:
                return self._pack_result(url, res.html, "layer1c_advanced_waf", start, attempts, res=res)
            if protected_block is None and _protected_retry_capture(attempt, solve_challenges=solve_challenges):
                protected_block = ("layer1c_advanced_waf", res)
            html = res.html or html

        if time.time() >= deadline - _BUDGET_STOP_FLOOR:
            return self._budget_stop(url, start, attempts, captures, html)

        # Layer 6 is HTTP-only TLS impersonation. It runs alongside the other
        # non-browser transports so every cheap bypass attempt happens before
        # escalation stops, and before any headless Chromium is launched.
        if "layer6_ultimate" not in skip:
            res = await _bounded_http(
                scrape_ultimate(url),
                url,
                "Layer 6 TLS impersonation",
                deadline=deadline,
            )
            attempt = _attempt_from_result("layer6_ultimate", res)
            attempts.append(attempt)
            _remember_capture(captures, "layer6_ultimate", res)
            if res.success and not attempt["retry_recommended"]:
                if getattr(res, "cookies", None):
                    await self.memory.save_cookies(url, res.cookies)
                return self._pack_result(url, res.html, "layer6_ultimate", start, attempts, res=res)
            if protected_block is None and _protected_retry_capture(attempt, solve_challenges=solve_challenges):
                protected_block = ("layer6_ultimate", res)
            html = res.html or html

        # VULN-06 guard: every non-browser transport has now had its chance to
        # bypass a protected 403/429/503 challenge. Stop here rather than
        # burning CPU/RAM on crawler personas and headless Chromium for a block
        # we have already observed and cannot solve while solvers are disabled.
        if protected_block is not None:
            blocked_layer, blocked_res = protected_block
            return self._pack_result(
                url,
                getattr(blocked_res, "html", None),
                blocked_layer,
                start,
                attempts,
                res=blocked_res,
            )

        if time.time() >= deadline - _BUDGET_STOP_FLOOR:
            return self._budget_stop(url, start, attempts, captures, html)

        res, html = await self._try_crawler_personas(
            url,
            start,
            attempts,
            skip_layers=skip,
            html=html,
            captures=captures,
            solve_challenges=solve_challenges,
        )
        if res:
            return res

        if time.time() >= deadline - _BUDGET_STOP_FLOOR:
            return self._budget_stop(url, start, attempts, captures, html)

        if allow_browser and "layer4_stealth" not in skip:
            fp_stealth = make_identity(chrome_only=True)
            res = await self._admitted_browser_call(
                stealth_scrape,
                url,
                deadline=deadline,
                fingerprint=fp_stealth,
                solve_challenges=solve_challenges,
            )
            attempt = _attempt_from_result("layer4_stealth", res)
            attempts.append(attempt)
            _remember_capture(captures, "layer4_stealth", res)
            if res.success and not attempt["retry_recommended"]:
                if getattr(res, "cookies", None):
                    await self.memory.save_cookies(url, res.cookies)
                return self._pack_result(url, res.html, "layer4_stealth", start, attempts, res=res)

            html = res.html or html

        if not allow_browser or not allow_layer5:
            diag = _summarize_attempts(attempts, html)
            return self._pack_best_capture(url, captures, start, attempts, diag)

        if time.time() >= deadline - _BUDGET_STOP_FLOOR:
            return self._budget_stop(url, start, attempts, captures, html)

        fp_full = make_identity(chrome_only=True)
        res = await self._admitted_browser_call(
            full_scrape,
            url,
            deadline=deadline,
            fingerprint=fp_full,
            solve_challenges=solve_challenges,
        )
        attempt = _attempt_from_result("layer5_full", res)
        attempts.append(attempt)
        _remember_capture(captures, "layer5_full", res)
        if res.success and not attempt["retry_recommended"]:
            if getattr(res, "cookies", None):
                await self.memory.save_cookies(url, res.cookies)
            return self._pack_result(url, res.html, "layer5_full", start, attempts, res=res)
        if _protected_retry_capture(attempt, solve_challenges=solve_challenges):
            return self._pack_result(url, res.html, "layer5_full", start, attempts, res=res)

        html = res.html or html
        diag = _summarize_attempts(attempts, res.html or html)
        return self._pack_best_capture(url, captures, start, attempts, diag)

    async def _try_layer1(self, url, start, attempts):
        res = await _bounded_http(raw_scrape(url), url, "Raw request", deadline=start + _scrape_budget())
        attempts.append(_attempt_from_result("layer1_simple", res))
        if res.success:
            return self._pack_result(url, res.html, "layer1_simple", start, attempts, res=res)
        diag = _summarize_attempts(attempts, res.html)
        capture = _capture_from_result("layer1_simple", res)
        return self._pack_best_capture(
            url,
            [capture] if capture else [],
            start,
            attempts,
            diag,
            fallback_layer="layer1_simple",
        )

    async def _try_layer1c(self, url, start, attempts):
        res = await _bounded_http(
            advanced_waf_scrape(url),
            url,
            "Advanced WAF request",
            deadline=start + _scrape_budget(),
        )
        attempts.append(_attempt_from_result("layer1c_advanced_waf", res))
        if res.success:
            return self._pack_result(url, res.html, "layer1c_advanced_waf", start, attempts, res=res)
        diag = _summarize_attempts(attempts, res.html)
        capture = _capture_from_result("layer1c_advanced_waf", res)
        return self._pack_best_capture(
            url,
            [capture] if capture else [],
            start,
            attempts,
            diag,
            fallback_layer="layer1c_advanced_waf",
        )

    async def _try_layer6(self, url, start, attempts):
        res = await _bounded_http(
            scrape_ultimate(url),
            url,
            "Layer 6 TLS impersonation",
            deadline=start + _scrape_budget(),
        )
        attempts.append(_attempt_from_result("layer6_ultimate", res))
        if res.success:
            if getattr(res, "cookies", None):
                await self.memory.save_cookies(url, res.cookies)
            return self._pack_result(url, res.html, "layer6_ultimate", start, attempts, res=res)
        diag = _summarize_attempts(attempts, res.html)
        capture = _capture_from_result("layer6_ultimate", res)
        return self._pack_best_capture(
            url,
            [capture] if capture else [],
            start,
            attempts,
            diag,
            fallback_layer="layer6_ultimate",
        )

    async def _try_source(self, url, start, attempts, solve_challenges=False, allow_browser=True):
        source = source_name(url)
        if not source:
            return self._pack_best_capture(
                url,
                [],
                start,
                attempts,
                {"summary": "No named source route for URL"},
                fallback_layer="source_none",
            )

        if not allow_browser and source not in HTTP_ONLY_SOURCE_NAMES and source not in STATIC_HTTP_SOURCE_NAMES:
            raise ValueError(f"Browser-backed source mode '{source}' is disabled because allow_browser is False")

        kwargs = {"cookies": self.memory.get_valid_cookies(url)}
        source_parameters = inspect.signature(source_scrape).parameters
        if "structured" in source_parameters:
            kwargs["structured"] = True
        if "solve_challenges" in source_parameters:
            kwargs["solve_challenges"] = solve_challenges
        if "memory" in source_parameters:
            kwargs["memory"] = self.memory
        if "allow_browser" in source_parameters:
            kwargs["allow_browser"] = allow_browser
        layer = f"source_{source}"
        try:
            res = await self._source_call(source, source_scrape, url, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed = QuickResult(
                success=False,
                status_code=0,
                needs_browser=False,
                reason=f"Named source raised {type(exc).__name__}",
                response_time=time.time() - start,
                final_url=url,
            )
            attempts.append(_attempt_from_result(layer, failed))
            diag = _summarize_attempts(attempts)
            return self._pack_best_capture(
                url,
                [],
                start,
                attempts,
                diag,
                fallback_layer=layer,
            )
        if res is None:
            return self._pack_best_capture(
                url,
                [],
                start,
                attempts,
                {"summary": f"No structured source implementation for {source}"},
                fallback_layer=layer,
            )

        attempt = _attempt_from_result(layer, res)
        attempts.append(attempt)
        if res.success and not attempt["retry_recommended"]:
            if getattr(res, "cookies", None):
                await self.memory.save_cookies(url, res.cookies)
            return self._pack_result(url, res.html, layer, start, attempts, res=res)

        capture = _capture_from_result(layer, res)
        diag = _summarize_attempts(attempts, res.html)
        return self._pack_best_capture(
            url,
            [capture] if capture else [],
            start,
            attempts,
            diag,
            fallback_layer=layer,
        )

    async def _try_mobile(self, url, start, attempts):
        res = await _bounded_http(mobile_scrape(url), url, "Mobile request", deadline=start + _scrape_budget())
        attempts.append(_attempt_from_result("layer1b_mobile", res))
        if res.success:
            return self._pack_result(url, res.html, "layer1b_mobile", start, attempts, res=res)
        diag = _summarize_attempts(attempts, res.html)
        capture = _capture_from_result("layer1b_mobile", res)
        return self._pack_best_capture(
            url,
            [capture] if capture else [],
            start,
            attempts,
            diag,
            fallback_layer="layer1b_mobile",
        )

    async def _try_layer2(self, url, fp, start, attempts):
        valid_cookies = self.memory.get_valid_cookies(url)
        res = await _bounded_http(quick_scrape(url, fingerprint=fp, cookies=valid_cookies), url, "Static request", deadline=start + _scrape_budget())
        attempts.append(_attempt_from_result("layer2_cookie", res))
        if res.success:
            return self._pack_result(url, res.html, "layer2_cookie", start, attempts, res=res)
        diag = _summarize_attempts(attempts, res.html)
        capture = _capture_from_result("layer2_cookie", res)
        return self._pack_best_capture(
            url,
            [capture] if capture else [],
            start,
            attempts,
            diag,
            fallback_layer="layer2_cookie",
        )

    async def _try_layer3(self, url, start, attempts):
        captures = []
        packed, html = await self._try_crawler_personas(url, start, attempts, captures=captures)
        if packed:
            return packed

        diag = _summarize_attempts(attempts, html)
        return self._pack_best_capture(url, captures, start, attempts, diag, fallback_layer="layer3_crawler")

    async def _try_crawler_personas(
        self,
        url,
        start,
        attempts,
        skip_layers=None,
        html=None,
        captures=None,
        solve_challenges=False,
    ):
        skip_layers = set(skip_layers or [])
        captures = captures if captures is not None else []
        deadline = start + _scrape_budget()
        for bot_type, layer in CRAWLER_PERSONAS:
            if layer in skip_layers:
                continue
            if time.time() >= deadline - _BUDGET_STOP_FLOOR:
                break

            res = await _bounded_http(crawler_scrape(url, bot_type=bot_type), url, f"{bot_type} request", deadline=deadline)
            attempt = _attempt_from_result(layer, res)
            attempts.append(attempt)
            _remember_capture(captures, layer, res)
            if res.success and not attempt["retry_recommended"]:
                return self._pack_result(url, res.html, layer, start, attempts, res=res), html
            if _protected_retry_capture(attempt, solve_challenges=solve_challenges):
                return self._pack_result(url, res.html, layer, start, attempts, res=res), html
            html = res.html or html

        return None, html

    async def _try_layer4(self, url, fp, start, attempts, solve_challenges=False):
        res = await self._admitted_browser_call(
            stealth_scrape,
            url,
            deadline=start + _scrape_budget(),
            fingerprint=fp,
            solve_challenges=solve_challenges,
        )
        attempt = _attempt_from_result("layer4_stealth", res)
        attempts.append(attempt)
        if res.success:
            if getattr(res, "cookies", None):
                await self.memory.save_cookies(url, res.cookies)
            return self._pack_result(url, res.html, "layer4_stealth", start, attempts, res=res)

        diag = _summarize_attempts(attempts, res.html)
        capture = _capture_from_result("layer4_stealth", res)
        return self._pack_best_capture(url, [capture] if capture else [], start, attempts, diag, fallback_layer="layer4_stealth")

    async def warmup_layer4(self, url, solve_challenges=False):
        url = self._rewrite_reddit_url(url)
        start = time.time()
        attempts = []
        fp = make_identity(chrome_only=True)
        res = await self._try_layer4(url, fp, start, attempts, solve_challenges=solve_challenges)
        if res.ok:
            self._warmup_fingerprints[self.memory._get_domain(url)] = fp
        await self.memory.record_attempt(
            url,
            res.tries,
            success_layer=res.winning_layer if res.ok else None,
        )
        return res

    async def scrape_layer2_after_warmup(self, url):
        url = self._rewrite_reddit_url(url)
        start = time.time()
        attempts = []
        fp = self._warmup_fingerprints.get(self.memory._get_domain(url))
        if fp is None:
            fp = make_identity(chrome_only=True)
        res = await self._try_layer2(url, fp, start, attempts)
        await self.memory.record_attempt(
            url,
            res.tries,
            success_layer=res.winning_layer if res.ok else None,
        )
        return res

    async def scrape_source_bulk(self, urls, solve_challenges=False, allow_browser=True):
        urls = list(urls or [])
        if not urls:
            return []

        source = source_name(urls[0])
        if not allow_browser and source not in HTTP_ONLY_SOURCE_NAMES and source not in STATIC_HTTP_SOURCE_NAMES:
            return None

        seed_cookies = self.memory.get_valid_cookies(urls[0])

        bulk_parameters = inspect.signature(source_bulk_scrape).parameters
        bulk_kwargs = {
            "cookies": seed_cookies,
            "solve_challenges": solve_challenges,
        }
        if "memory" in bulk_parameters:
            bulk_kwargs["memory"] = self.memory
        if "allow_browser" in bulk_parameters:
            bulk_kwargs["allow_browser"] = allow_browser
        bulk_deadline = time.time() + _scrape_budget()
        try:
            raw_results = await self._source_call(
                source,
                source_bulk_scrape,
                urls,
                deadline=bulk_deadline,
                **bulk_kwargs,
            )
        except Exception:
            import traceback
            traceback.print_exc()
            return None
        if raw_results is None:
            return None

        raw_results = list(raw_results)
        packed = []
        for index, url in enumerate(urls):
            if index < len(raw_results) and raw_results[index] is not None:
                raw = raw_results[index]
            else:
                raw = QuickResult(
                    success=False,
                    status_code=0,
                    needs_browser=False,
                    reason="Source bulk returned no result for URL",
                    final_url=url,
                )
            layer = f"source_{source_name(url) or 'custom'}"
            attempt = _attempt_from_result(layer, raw)
            start = time.time() - (getattr(raw, "response_time", 0.0) or 0.0)

            if raw.success and not attempt["retry_recommended"]:
                result = self._pack_result(url, raw.html, layer, start, [attempt], res=raw)
            else:
                capture = _capture_from_result(layer, raw)
                diag = _summarize_attempts([attempt], raw.html)
                result = self._pack_best_capture(
                    url,
                    [capture] if capture else [],
                    start,
                    [attempt],
                    diag,
                    fallback_layer=layer,
                )
                if not raw.success:
                    result.ok = False
                    result.err = getattr(raw, "reason", "") or "source bulk did not produce usable content"
                    if result.best_attempt:
                        result.best_attempt["retry_recommended"] = True

            await self.memory.record_attempt(
                url,
                result.tries,
                success_layer=result.winning_layer if result.ok else None,
            )
            if result.ok and getattr(raw, "cookies", None):
                await self.memory.save_cookies(url, raw.cookies)
            packed.append(result)

        return packed

    async def _try_layer5(self, url, fp, start, attempts, solve_challenges=False):
        res = await self._admitted_browser_call(
            full_scrape,
            url,
            deadline=start + _scrape_budget(),
            fingerprint=fp,
            solve_challenges=solve_challenges,
        )
        attempt = _attempt_from_result("layer5_full", res)
        attempts.append(attempt)
        if res.success:
            if getattr(res, "cookies", None):
                await self.memory.save_cookies(url, res.cookies)
            return self._pack_result(url, res.html, "layer5_full", start, attempts, res=res)

        diag = _summarize_attempts(attempts, res.html)
        capture = _capture_from_result("layer5_full", res)
        return self._pack_best_capture(url, [capture] if capture else [], start, attempts, diag, fallback_layer="layer5_full")

    @staticmethod
    async def _admitted_browser_call(call, *args, deadline=None, slot_wait=12.0, **kwargs):
        url = kwargs.get("url") or (args[0] if args else "")
        wait = None
        if deadline is not None:
            remaining = deadline - time.time()
            if remaining <= 0:
                return QuickResult(
                    False,
                    status_code=0,
                    needs_browser=True,
                    reason="Browser call skipped: scrape budget exhausted",
                    response_time=0.0,
                    final_url=url,
                )
            # Never wait longer for a free slot than we could then use: at
            # most 12s, and never into the floor where the call itself
            # could not finish.
            wait = min(slot_wait, max(0.1, remaining - _BUDGET_STOP_FLOOR))
        entered = time.time()
        try:
            async with browser_admission.slot(timeout=wait):
                try:
                    if deadline is not None:
                        return await asyncio.wait_for(
                            call(*args, **kwargs),
                            timeout=max(0.1, deadline - time.time()),
                        )
                    return await call(*args, **kwargs)
                except TimeoutError:
                    return QuickResult(
                        False,
                        status_code=0,
                        needs_browser=True,
                        reason="Browser call exceeded remaining scrape budget",
                        response_time=time.time() - entered,
                        final_url=url,
                    )
        except TimeoutError as exc:
            # Slot never freed within the wait: honest failed layer, never
            # an unhandled exception out of _try_layer4/5.
            return QuickResult(
                False,
                status_code=0,
                needs_browser=True,
                reason=f"Browser admission queue unavailable: {exc}",
                response_time=time.time() - entered,
                final_url=url,
            )

    @staticmethod
    async def _source_call(source, call, *args, deadline=None, **kwargs):
        budget = _source_call_timeout()
        if deadline is not None:
            budget = min(budget, max(0.5, deadline - time.time()))
        if source in HTTP_ONLY_SOURCE_NAMES:
            return await asyncio.wait_for(call(*args, **kwargs), timeout=budget)
        return await asyncio.wait_for(
            ScraperEngine._admitted_browser_call(call, *args, deadline=deadline, **kwargs),
            timeout=budget,
        )

    def _budget_stop(self, url, start, attempts, captures, html):
        print(
            f"[Engine] Scrape budget exhausted after {time.time() - start:.1f}s for {url}"
            " - returning best capture"
        )
        diag = _summarize_attempts(attempts, html)
        return self._pack_best_capture(url, captures, start, attempts, diag)

    def _pack_result(self, url, html, layer, start, attempts, res=None):
        final_url = getattr(res, "final_url", None) if res else None
        status_code = getattr(res, "status_code", 0) if res else 0
        is_empty_skeleton = (
            status_code == 0
            and (
                final_url == "about:blank"
                or (html or "").strip() in {
                    "<html><head></head><body></body></html>",
                    "<html><body></body></html>",
                }
            )
        )
        if (
            (final_url and (final_url.startswith("chrome-error://") or "chromewebdata" in final_url))
            or ("chrome-error://" in (html or ""))
            or is_empty_skeleton
        ):
            html = None
        html, truncation = _bounded_html(html)
        if not html:
            status_code = getattr(res, "status_code", 0) if res else 0
            final_url = getattr(res, "final_url", None) if res else None
            reason = getattr(res, "reason", "") if res else ""
            diagnostics = _diagnostics_for_capture(
                "",
                status_code,
                reason,
                cookies=getattr(res, "cookies", None) if res else None,
                url=final_url or url,
            )
            return ScrapeResult(
                False,
                url,
                data=None,
                winning_layer=layer,
                elapsed=time.time() - start,
                tries=attempts,
                err=reason or "no capture",
                captured=False,
                capture_state="no_capture",
                diagnostics=diagnostics,
                best_attempt=None,
                status_code=status_code,
                final_url=final_url or url,
            )
        data = {"html": html}
        status_code = getattr(res, "status_code", 0) if res else 0
        final_url = getattr(res, "final_url", None) if res else None
        reason = getattr(res, "reason", "") if res else ""
        diagnostics = _diagnostics_for_capture(
            html,
            status_code,
            reason,
            cookies=getattr(res, "cookies", None) if res else None,
            url=final_url or url,
        )
        if truncation and "content_truncated" not in diagnostics:
            diagnostics.append("content_truncated")
        text_len = len(_text_from_html(html))
        best_attempt = {
            "layer": layer,
            "captured": bool(html),
            "capture_state": _capture_state(bool(html), diagnostics),
            "retry_recommended": _retry_recommended(html, status_code, diagnostics, reason, text_len),
            "status_code": status_code,
            "reason": reason,
            "html_length": len(html or ""),
            "text_length": text_len,
            "diagnostics": diagnostics,
            "final_url": final_url or url,
        }
        if truncation:
            best_attempt.update(truncation)

        if status_code >= 400:
            # A captured error/block page (403 Access Denied etc.) is not
            # usable content.  Report the failure honestly instead of
            # returning it as success; the body is kept for diagnostics and
            # the caller decides whether to keep escalating.
            return ScrapeResult(
                False,
                url,
                data={"html": html},
                winning_layer=layer,
                elapsed=time.time() - start,
                tries=attempts,
                err=reason or f"HTTP {status_code}",
                captured=bool(html),
                capture_state=_capture_state(bool(html), diagnostics),
                diagnostics=diagnostics,
                best_attempt=best_attempt,
                html_length=len(html or ""),
                text_length=text_len,
                status_code=status_code,
                final_url=final_url or url,
            )

        ctype = find_captcha(html)
        notes = None
        if ctype:
            notes = {"captcha": ctype, "note": "Captured content also contains CAPTCHA markers."}

        return ScrapeResult(
            True,
            url,
            data=data,
            winning_layer=layer,
            elapsed=time.time()-start,
            tries=attempts,
            notes=notes,
            captured=bool(html),
            capture_state=_capture_state(bool(html), diagnostics),
            diagnostics=diagnostics,
            best_attempt=best_attempt,
            html_length=len(html or ""),
            text_length=text_len,
            status_code=status_code,
            final_url=final_url or url,
            content_truncation=truncation,
        )

    def _pack_best_capture(self, url, captures, start, attempts, diag, fallback_layer="none"):
        captures = [c for c in captures if c]
        viable = [c for c in captures if (c.get("status_code") or 0) < 400]
        if viable:
            best = max(viable, key=_capture_score)
            html = best.pop("html", "")
            best.pop("score", None)
            html, truncation = _bounded_html(html)
            diagnostics = list(best.get("diagnostics", []))
            if truncation and "content_truncated" not in diagnostics:
                diagnostics.append("content_truncated")
            best["diagnostics"] = diagnostics
            best["html_length"] = len(html)
            best["text_length"] = len(_text_from_html(html))
            if truncation:
                best.update(truncation)
            notes = dict(diag or {})
            notes["diagnostics"] = diagnostics
            return ScrapeResult(
                True,
                url,
                data={"html": html},
                winning_layer=best.get("layer", fallback_layer),
                elapsed=time.time()-start,
                tries=attempts,
                notes=notes,
                captured=True,
                capture_state=_capture_state(True, diagnostics),
                diagnostics=diagnostics,
                best_attempt=best,
                html_length=len(html),
                text_length=len(_text_from_html(html)),
                status_code=best.get("status_code", 0),
                final_url=best.get("final_url") or url,
                content_truncation=truncation,
            )

        if captures:
            # Every capture is an error/block page (status >= 400): fail
            # honestly with the best blocked attempt kept for diagnostics.
            # The body is still preserved so consumers can inspect it.
            best_blocked = dict(max(captures, key=_capture_score))
            blocked_html = best_blocked.pop("html", "")
            best_blocked.pop("score", None)
            diagnostics = list(best_blocked.get("diagnostics") or [])
            notes = dict(diag or {})
            notes["diagnostics"] = diagnostics
            return ScrapeResult(
                False,
                url,
                data={"html": blocked_html},
                winning_layer=best_blocked.get("layer", fallback_layer),
                elapsed=time.time() - start,
                tries=attempts,
                err=f"HTTP {best_blocked.get('status_code') or 0} blocked capture",
                notes=notes,
                captured=True,
                capture_state=_capture_state(True, diagnostics),
                diagnostics=diagnostics,
                best_attempt=best_blocked,
                html_length=len(blocked_html or ""),
                text_length=len(_text_from_html(blocked_html or "")),
                status_code=best_blocked.get("status_code", 0),
                final_url=best_blocked.get("final_url") or url,
            )

        notes = dict(diag or {})
        diagnostics = notes.get("diagnostics", [])
        return ScrapeResult(
            False,
            url,
            data=None,
            winning_layer=fallback_layer,
            elapsed=time.time()-start,
            tries=attempts,
            err=notes.get("summary", "no capture"),
            notes=notes,
            captured=False,
            capture_state="no_capture",
            diagnostics=diagnostics,
            best_attempt=None,
            html_length=0,
            text_length=0,
            status_code=0,
            final_url=url,
        )


