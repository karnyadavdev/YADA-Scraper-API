import logging
import random
from dataclasses import dataclass
from typing import Optional

logging.getLogger("fake_useragent").setLevel(logging.ERROR)

# fake_useragent is disabled because it can silently return the same static
# Edge UA for every request. Use the curated fallback pool instead.
_ua_gen = None
has_fake_ua = False

FALLBACK_AGENTS = [
    ("chrome", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    ("chrome", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    ("chrome", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
    ("chrome", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    ("chrome", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
    ("chrome", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),

    ("firefox", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"),
    ("firefox", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0"),
    ("firefox", "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0"),
    ("safari", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15"),
    ("safari", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15"),

    ("edge", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"),
]


def _pick_browser(ua):
    ua_lower = ua.lower()
    if "edg/" in ua_lower or "edge/" in ua_lower:
        return "edge"
    elif "firefox/" in ua_lower:
        return "firefox"
    elif "safari/" in ua_lower and "chrome/" not in ua_lower:
        return "safari"
    else:
        return "chrome"


def _get_ua(chrome_only=False):
    if has_fake_ua and _ua_gen:
        try:
            for _ in range(5):
                ua = _ua_gen.random
                fam = _pick_browser(ua)
                if fam in ("chrome", "edge"):
                    return (fam, ua)
        except Exception:
            pass


    pool = [
        x for x in FALLBACK_AGENTS
        if x[0] in ("chrome", "edge")
    ]
    fam, ua = random.choice(pool)
    return (fam, ua)



BROWSER_VERSIONS = {
    "chrome": [
        "chrome110", "chrome116", "chrome119", "chrome120", "chrome123",
        "chrome124", "chrome131",
    ],
    "safari": ["safari15_3", "safari15_5", "safari17_0"],
    "edge": ["edge101", "edge99"],
    "firefox": [],
}



SCREEN_SIZES = [
    (1920, 1080, 50),
    (1366, 768, 20),
    (1536, 864, 10),
    (1440, 900, 8),
    (1680, 1050, 5),
    (2560, 1440, 4),
    (1280, 720, 3),
]


LOCALES = [
    ("en-US", "America/New_York"),
    ("en-US", "America/Chicago"),
    ("en-US", "America/Los_Angeles"),
    ("en-GB", "Europe/London"),
    ("en-AU", "Australia/Sydney"),
    ("en-CA", "America/Toronto"),
]


TRACKER_DOMAINS = [
    # Pure analytics/tracking - safe to block, never gates access
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "hotjar.com",
    "fullstory.com",
    "cdn.fp.measure.office.com",
    # NOTE: Do NOT block anti-bot verification services here!
    # DataDome, PerimeterX, Kasada, and FingerprintJS require their JS
    # to load and verify the user. Blocking them CAUSES blocks.
    # Instead, we rely on our stealth scripts to pass their checks.
]


@dataclass
class FakeIdentity:
    browser_type: str
    ua_string: str
    headers: dict
    scr_w: int
    scr_h: int
    lang: str
    tz: str
    impersonate_id: Optional[str] = None
    os_name: str = ""

    def __post_init__(self):
        if not self.os_name:
            if "Windows" in self.ua_string:
                self.os_name = "Windows"
            elif "Macintosh" in self.ua_string:
                self.os_name = "macOS"
            else:
                self.os_name = "Linux"


def _get_chrome_ver(ua):
    import re
    m = re.search(r"Chrome/(\d+)", ua)
    return m.group(1) if m else "120"


def _chrome_headers(ua):
    ver = _get_chrome_ver(ua)

    if "Windows" in ua:
        plat = '"Windows"'
    elif "Macintosh" in ua:
        plat = '"macOS"'
    else:
        plat = '"Linux"'

    grease = [
        '"Not A(Brand";v="99"',
        '"Not A;Brand";v="8"',
        '"Not_A Brand";v="24"',
        '"Not)A;Brand";v="99"',
    ]
    grease_str = random.choice(grease)

    is_edge = "Edg/" in ua
    if is_edge:
        brand = f'"Microsoft Edge";v="{ver}", "Chromium";v="{ver}", {grease_str}'
    else:
        brand = f'"Google Chrome";v="{ver}", "Chromium";v="{ver}", {grease_str}'

    # Cache-Control trade-off, A/B tested over 36 sites / 144 hits per
    # variant (scratch/cache_control_ab_*.json): keeping it wins on
    # glassdoor/amazon/medium, losing only on reddit + tripadvisor raw
    # fetches - and both of those route through dedicated source paths
    # in the ladder before generic headers are ever sent. Net 106/144
    # vs 105/144 with structural protection for the losers. Keep it.
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Ch-Ua": brand,
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": plat,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
        "Connection": "keep-alive",
    }
    # Only ~10% of real users enable DNT; sending it every time is a fingerprint
    if random.random() < 0.10:
        headers["DNT"] = "1"
    return headers


def _firefox_headers(ua):
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Upgrade-Insecure-Requests": "1",
        "DNT": "1",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }


def _safari_headers(ua):
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }



def make_identity(chrome_only=False):
    (b_type, ua) = _get_ua(chrome_only=chrome_only)




    if b_type in ("chrome", "edge"):
        hdrs = _chrome_headers(ua)
    elif b_type == "firefox":
        hdrs = _firefox_headers(ua)
    else:
        hdrs = _safari_headers(ua)


    choices = [(w, h) for w, h, _ in SCREEN_SIZES]
    weights = [wt for _, _, wt in SCREEN_SIZES]
    w, h = random.choices(choices, weights=weights, k=1)[0]


    # Choose standard physical screen resolutions without jitter (avoid flagging by anti-bot checks)



    loc, timezone = random.choice(LOCALES)
    hdrs["Accept-Language"] = f"{loc},{loc.split('-')[0]};q=0.9"


    # Map TLS impersonation based on the actual browser family.
    # UA131 -> chrome131 was A/B tested against chrome120 on 15 targets
    # (scratch/tls_fingerprint_abc_benchmark.py, results JSON alongside):
    # matched chrome131 turns Quora 0/4 -> 3/4 and Reddit 8KB bot-shell
    # -> 167KB real content with zero regressions elsewhere. Older UA
    # versions keep the battle-tested chrome120 pin until tested.
    if b_type == "chrome":
        chrome_ver = _get_chrome_ver(ua)
        _TLS_MAP = {"131": "chrome131", "130": "chrome120", "128": "chrome120", "120": "chrome120"}
        imp = _TLS_MAP.get(chrome_ver, "chrome120")
    elif b_type == "edge":
        # Modern Edge is Chromium: Edg/131's real TLS fingerprint tracks
        # chrome131, not curl_cffi's stale edge101. A/B tested on 11
        # targets (scratch/edge_ab_benchmark.py): chrome131 TLS turns
        # HomeDepot 0/4 -> 4/4, Quora 0/4 -> 3/4, Reddit bot-shell ->
        # 167KB, Glassdoor 0/4 -> 2/4, zero regressions.
        imp = "chrome131"
    elif b_type == "safari":
        imp = random.choice(["safari15_3", "safari15_5", "safari17_0"])
    elif b_type == "firefox":
        imp = random.choice(["firefox110", "firefox117"])
    else:
        imp = "chrome120"

    return FakeIdentity(
        browser_type=b_type,
        ua_string=ua,
        headers=hdrs,
        scr_w=w,
        scr_h=h,
        lang=loc,
        tz=timezone,
        impersonate_id=imp,
    )


def random_sleep(base=1.5, jitter=0.7):
    """Gaussian delay to simulate human timing."""
    t = random.gauss(base, jitter)
    return max(0.2, t)


def small_pause():
    return random.uniform(0.04, 0.25)



def fake_mouse_move(start, end, num=15):

    x1, y1 = start
    x2, y2 = end


    m1x = x1 + random.randint(-80, 180)
    m1y = y1 + random.randint(-80, 180)
    m2x = x2 + random.randint(-180, 80)
    m2y = y2 + random.randint(-180, 80)

    pts = []
    for i in range(num + 1):
        t = i / num
        inv = 1 - t
        px = int(inv**3 * x1 + 3 * inv**2 * t * m1x + 3 * inv * t**2 * m2x + t**3 * x2)
        py = int(inv**3 * y1 + 3 * inv**2 * t * m1y + 3 * inv * t**2 * m2y + t**3 * y2)


        px += random.randint(-1, 1)
        py += random.randint(-1, 1)
        pts.append((px, py))

    return pts


def rand_point(max_w, max_h):
    return (
        random.randint(10, max(11, max_w - 10)),
        random.randint(10, max(11, max_h - 10)),
    )
