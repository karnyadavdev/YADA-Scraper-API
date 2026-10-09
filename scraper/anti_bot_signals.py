import re
from collections import defaultdict


# Real markers these vendors drop into cookies/headers/body during a
# challenge. Evidence beats vendor documentation, which goes stale fast.
VENDOR_PATTERNS = {
    "akamai": [
        re.compile(r"\b_abck\b", re.I),
        re.compile(r"\bbm_sz\b", re.I),
        re.compile(r"\bbm_sv\b", re.I),
        re.compile(r"\bbm_s\b", re.I),
        re.compile(r"\bsec_cpt\b", re.I),
        re.compile(r"\bsbsd\b", re.I),
        re.compile(r"akamai", re.I),
    ],
    "datadome": [
        re.compile(r"\bdatadome\b", re.I),
        re.compile(r"captcha-delivery\.com", re.I),
        re.compile(r"geo\.captcha-delivery\.com", re.I),
        re.compile(r"\bdd\s*=", re.I),
    ],
    "perimeterx": [
        re.compile(r"\b_px(?:2|3|hd|de|vid|uuid)\b", re.I),
        re.compile(r"\bpx-captcha\b", re.I),
        re.compile(r"\b_pxAppId\b", re.I),
        re.compile(r"\bauditor\.js\b", re.I),
        re.compile(r"fst-ec\.perimeterx\.net", re.I),
        re.compile(r"perimeterx", re.I),
        re.compile(r"px-(?:cdn|client|chk)\.net", re.I),
        re.compile(r"client\.perimeterx\.net", re.I),
        re.compile(r"/xhr/api/v2/collector", re.I),
    ],
    "cloudflare": [
        re.compile(r"\bcf_clearance\b", re.I),
        re.compile(r"\b__cf_bm\b", re.I),
        re.compile(r"challenges\.cloudflare\.com", re.I),
        re.compile(r"cf-turnstile", re.I),
        re.compile(r"cloudflare", re.I),
    ],
    "kasada": [
        re.compile(r"\bx-kpsdk-(?:ct|cd)\b", re.I),
        re.compile(r"\bKP_UIDz\b", re.I),
        re.compile(r"kasada", re.I),
    ],
}


def _cookie_names(cookies):
    # cookies arrive dict-shaped from curl_cffi but list-shaped from
    # playwright, and both show up here
    names = []
    if not cookies:
        return names

    if isinstance(cookies, dict):
        return [str(name) for name in cookies]

    for cookie in cookies:
        if isinstance(cookie, dict):
            name = cookie.get("name")
        else:
            name = getattr(cookie, "name", None)
        if name:
            names.append(str(name))
    return names


def anti_bot_signals(html="", cookies=None, headers=None, url=""):
    # vendors plant evidence in different layers (akamai in cookies,
    # cloudflare in the body), so all four go into one haystack
    haystack_parts = [url or "", html or ""]
    cookie_names = _cookie_names(cookies)
    if cookie_names:
        haystack_parts.append(" ".join(cookie_names))

    if headers and isinstance(headers, dict):
        haystack_parts.extend(str(k) for k in headers)
        haystack_parts.extend(str(v) for v in headers.values())

    haystack = "\n".join(haystack_parts)
    evidence = defaultdict(list)

    for vendor, patterns in VENDOR_PATTERNS.items():
        for pattern in patterns:
            if pattern.search(haystack):
                # keep the matched token so the report shows why a vendor was named
                token = pattern.pattern.replace("\\b", "")
                evidence[vendor].append(token)

    return {
        "vendors": sorted(evidence),
        "evidence": {vendor: sorted(set(items)) for vendor, items in evidence.items()},
        "cookies": cookie_names,
    }


def vendor_diagnostics(html="", cookies=None, headers=None, url=""):
    signals = anti_bot_signals(html=html, cookies=cookies, headers=headers, url=url)
    return [f"vendor_marker_seen:{vendor}" for vendor in signals["vendors"]]
