import asyncio
import time



CANVAS_SPOOF_JS = """
(() => {
    const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
    const originalToBlob = HTMLCanvasElement.prototype.toBlob;
    const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;

    // Stable seed per session. Repeated reads should not drift.
    const NOISE_SEED = Math.floor(Math.random() * 256);

    function copyWithNoise(data) {
        const out = new Uint8ClampedArray(data);
        for (let i = 0; i < data.length; i += 4) {
            out[i] = (out[i] + ((NOISE_SEED + i) % 2)) % 256;
        }
        return out;
    }

    HTMLCanvasElement.prototype.toDataURL = function(...args) {
        const ctx = this.getContext('2d');
        if (ctx) {
            try {
                const imageData = originalGetImageData.call(ctx, 0, 0, this.width, this.height);
                imageData.data.set(copyWithNoise(imageData.data));
                const copy = document.createElement('canvas');
                copy.width = this.width;
                copy.height = this.height;
                copy.getContext('2d').putImageData(imageData, 0, 0);
                return originalToDataURL.apply(copy, args);
            } catch(e) {}  // CORS canvas - ignore
        }
        return originalToDataURL.apply(this, args);
    };

    HTMLCanvasElement.prototype.toBlob = function(callback, ...args) {
        const ctx = this.getContext('2d');
        if (ctx) {
            try {
                const imageData = originalGetImageData.call(ctx, 0, 0, this.width, this.height);
                imageData.data.set(copyWithNoise(imageData.data));
                const copy = document.createElement('canvas');
                copy.width = this.width;
                copy.height = this.height;
                copy.getContext('2d').putImageData(imageData, 0, 0);
                return originalToBlob.call(copy, callback, ...args);
            } catch(e) {}
        }
        return originalToBlob.call(this, callback, ...args);
    };

    CanvasRenderingContext2D.prototype.getImageData = function(...args) {
        const imageData = originalGetImageData.apply(this, args);
        imageData.data.set(copyWithNoise(imageData.data));
        return imageData;
    };
})();
"""




_WEBGL_SPOOF_TEMPLATE = """
(() => {
    const GPU_CONFIGS = __GPU_CONFIGS__;
    const GPU = GPU_CONFIGS[Math.floor(Math.random() * GPU_CONFIGS.length)];

    const getParameterProxy = new Proxy(WebGLRenderingContext.prototype.getParameter, {
        apply(target, thisArg, args) {
            const param = args[0];
            if (param === 0x9245) return GPU.vendor;
            if (param === 0x9246) return GPU.renderer;
            return Reflect.apply(target, thisArg, args);
        }
    });

    WebGLRenderingContext.prototype.getParameter = getParameterProxy;

    if (typeof WebGL2RenderingContext !== 'undefined') {
        const getParameter2Proxy = new Proxy(WebGL2RenderingContext.prototype.getParameter, {
            apply(target, thisArg, args) {
                const param = args[0];
                if (param === 0x9245) return GPU.vendor;
                if (param === 0x9246) return GPU.renderer;
                return Reflect.apply(target, thisArg, args);
            }
        });
        WebGL2RenderingContext.prototype.getParameter = getParameter2Proxy;
    }
})();
"""

_GPU_CONFIGS_BY_OS = {
    "Windows": """[
        { vendor: 'Google Inc. (NVIDIA)', renderer: 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)' },
        { vendor: 'Google Inc. (NVIDIA)', renderer: 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)' },
        { vendor: 'Google Inc. (NVIDIA)', renderer: 'ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)' },
        { vendor: 'Google Inc. (AMD)', renderer: 'ANGLE (AMD, AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0, D3D11)' },
        { vendor: 'Google Inc. (Intel)', renderer: 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)' },
        { vendor: 'Google Inc. (Intel)', renderer: 'ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)' },
    ]""",
    "macOS": """[
        { vendor: 'Google Inc. (Apple)', renderer: 'ANGLE (Apple, Apple M1, OpenGL 4.1)' },
        { vendor: 'Google Inc. (Apple)', renderer: 'ANGLE (Apple, Apple M2, OpenGL 4.1)' },
        { vendor: 'Google Inc. (Apple)', renderer: 'ANGLE (Apple, Apple M3, OpenGL 4.1)' },
        { vendor: 'Google Inc. (Apple)', renderer: 'ANGLE (Apple, Apple M1 Pro, OpenGL 4.1)' },
    ]""",
    "Linux": """[
        { vendor: 'Google Inc. (Intel)', renderer: 'ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)' },
        { vendor: 'Google Inc. (NVIDIA)', renderer: 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060/PCIe/SSE2, OpenGL 4.6)' },
        { vendor: 'Google Inc. (AMD)', renderer: 'ANGLE (AMD, AMD Radeon RX 6700 XT, OpenGL 4.6)' },
    ]""",
}

def get_webgl_spoof_js(os_name="Windows"):
    """Generate WebGL spoofing JS with GPU configs matching the given OS."""
    configs = _GPU_CONFIGS_BY_OS.get(os_name, _GPU_CONFIGS_BY_OS["Windows"])
    return _WEBGL_SPOOF_TEMPLATE.replace("__GPU_CONFIGS__", configs)



AUDIO_SPOOF_JS = """
(() => {
    const originalGetFloatFrequencyData = AnalyserNode.prototype.getFloatFrequencyData;
    const originalGetByteFrequencyData = AnalyserNode.prototype.getByteFrequencyData;
    const AUDIO_NOISE = Math.random() * 0.0001;

    AnalyserNode.prototype.getFloatFrequencyData = function(array) {
        originalGetFloatFrequencyData.call(this, array);
        for (let i = 0; i < array.length; i++) {
            array[i] = array[i] + AUDIO_NOISE * ((i % 7) - 3);
        }
    };

    AnalyserNode.prototype.getByteFrequencyData = function(array) {
        originalGetByteFrequencyData.call(this, array);
        for (let i = 0; i < array.length; i++) {
            array[i] = Math.max(0, Math.min(255,
                array[i] + ((i % 3) - 1)
            ));
        }
    };
})();
"""



FONT_SPOOF_JS = """
(() => {
    const originalMeasureText = CanvasRenderingContext2D.prototype.measureText;
    const FONT_NOISE = 1 + (Math.random() * 0.002 - 0.001);

    CanvasRenderingContext2D.prototype.measureText = function(text) {
        const metrics = originalMeasureText.call(this, text);

        return new Proxy(metrics, {
            get(target, prop) {
                if (prop === 'width') {
                    return target.width * FONT_NOISE;
                }
                if (prop === 'actualBoundingBoxLeft' ||
                    prop === 'actualBoundingBoxRight') {
                    const val = target[prop];
                    return typeof val === 'number' ? val * FONT_NOISE : val;
                }
                const val = target[prop];
                return typeof val === 'function' ? val.bind(target) : val;
            }
        });
    };
})();
"""



NAVIGATOR_SPOOF_JS = """
(() => {
    // randomize cpu cores
    const CORES = [4, 8, 12, 16][Math.floor(Math.random() * 4)];
    Object.defineProperty(navigator, 'hardwareConcurrency', {
        get: () => CORES,
        configurable: true,
    });

    // device memory
    const MEMORY = [4, 8, 16][Math.floor(Math.random() * 3)];
    Object.defineProperty(navigator, 'deviceMemory', {
        get: () => MEMORY,
        configurable: true,
    });

    // no touch for desktop
    Object.defineProperty(navigator, 'maxTouchPoints', {
        get: () => 0,
        configurable: true,
    });

    // network info
    const DOWNLINK = 10 + Math.floor(Math.random() * 30);
    if (navigator.connection) {
        Object.defineProperty(navigator.connection, 'effectiveType', {
            get: () => '4g',
            configurable: true,
        });
        Object.defineProperty(navigator.connection, 'downlink', {
            get: () => DOWNLINK,
            configurable: true,
        });
    }

    // fake battery api
    const BATTERY_LEVEL = 0.85 + Math.random() * 0.15;
    if (navigator.getBattery) {
        navigator.getBattery = async () => ({
            charging: true,
            chargingTime: 0,
            dischargingTime: Infinity,
            level: BATTERY_LEVEL,
            addEventListener: () => {},
        });
    }

    // permissions api
    const originalQuery = Permissions.prototype.query;
    Permissions.prototype.query = function(desc) {
        if (desc.name === 'notifications') {
            return Promise.resolve({ state: 'prompt', addEventListener: () => {} });
        }
        return originalQuery.call(this, desc);
    };
})();
"""



HEADLESS_SPOOF_JS = """
(() => {
    // 1. window.chrome spoof
    if (!window.chrome) {
        window.chrome = {};
    }
    if (!window.chrome.app) {
        window.chrome.app = {
            InstallState: { DISABLED: 'disabled', ERROR: 'error', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
            isInstalled: false,
            getDetails: function() { return null; },
            getIsInstalled: function() { return false; },
            runningState: function() { return 'cannot_run'; }
        };
    }
    if (!window.chrome.runtime) {
        window.chrome.runtime = {};
    }
    if (!window.chrome.csi) {
        window.chrome.csi = function() { return { startE: Date.now(), onloadT: Date.now(), pageT: 0, tran: 15 }; };
    }
    if (!window.chrome.loadTimes) {
        window.chrome.loadTimes = function() {
            return {
                requestTime: Date.now() / 1000,
                startLoadTime: Date.now() / 1000,
                commitLoadTime: Date.now() / 1000,
                finishDocumentLoadTime: Date.now() / 1000,
                finishLoadTime: Date.now() / 1000,
                firstPaintTime: Date.now() / 1000,
                firstPaintAfterLoadTime: 0,
                navigationType: 'Other',
                wasFetchedViaSpdy: true,
                wasNpnNegotiated: true,
                npnNegotiatedProtocol: 'h2',
                wasAlternateProtocolAvailable: false,
                connectionInfo: 'h2'
            };
        };
    }

    // 2. navigator.plugins spoof
    if (navigator.plugins.length === 0) {
        const fakePluginArray = Object.create(PluginArray.prototype);
        const fakePlugin = Object.create(Plugin.prototype);
        const fakeMimeType = Object.create(MimeType.prototype);

        Object.defineProperties(fakeMimeType, {
            type: { get: () => 'application/pdf' },
            suffixes: { get: () => 'pdf' },
            description: { get: () => 'Portable Document Format' },
            enabledPlugin: { get: () => fakePlugin }
        });

        Object.defineProperties(fakePlugin, {
            name: { get: () => 'Chrome PDF Plugin' },
            filename: { get: () => 'internal-pdf-viewer' },
            description: { get: () => 'Portable Document Format' },
            length: { get: () => 1 },
            0: { get: () => fakeMimeType }
        });

        Object.defineProperties(fakePluginArray, {
            length: { get: () => 3 },
            0: { get: () => fakePlugin },
            1: { get: () => Object.create(Plugin.prototype, { name: { get: () => 'Chrome PDF Viewer' }, length: { get: () => 1 } }) },
            2: { get: () => Object.create(Plugin.prototype, { name: { get: () => 'Native Client' }, length: { get: () => 1 } }) },
            refresh: { value: () => {} },
            item: { value: function(i) { return this[i]; } },
            namedItem: { value: function(n) { return this[0]; } }
        });

        Object.defineProperty(navigator, 'plugins', {
            get: () => fakePluginArray
        });

        const fakeMimeTypeArray = Object.create(MimeTypeArray.prototype);
        Object.defineProperties(fakeMimeTypeArray, {
            length: { get: () => 1 },
            0: { get: () => fakeMimeType },
            item: { value: function(i) { return this[i]; } },
            namedItem: { value: function(n) { return this[0]; } }
        });

        Object.defineProperty(navigator, 'mimeTypes', {
            get: () => fakeMimeTypeArray
        });
    }

    // 3. Strict navigator.webdriver removal
    try {
        delete Object.getPrototypeOf(navigator).webdriver;
    } catch(e) {}

    Object.defineProperty(navigator, 'webdriver', {
        get: () => false,
        configurable: true
    });

    // 4. Notification permissions spoof
    if (window.Notification) {
        Object.defineProperty(Notification, 'permission', {
            get: () => 'default'
        });
    }
})();
"""



COOKIE_DISMISS_JS = """
(() => {
    function dismissCookies() {
        const selectors = [
            '[id*="cookie"] button[class*="accept"]',
            '[id*="cookie"] button[class*="agree"]',
            '[class*="cookie"] button[class*="accept"]',
            '[class*="cookie"] button[class*="agree"]',
            '[id*="consent"] button[class*="accept"]',
            '[class*="consent"] button[class*="accept"]',
            'button[id*="accept-cookies"]',
            'button[id*="cookie-accept"]',
            '#onetrust-accept-btn-handler',
            '.cc-accept',
            '.cc-btn.cc-dismiss',
            '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
            '[data-testid="cookie-policy-dialog-accept-button"]',
            'button[aria-label*="accept" i]',
            'button[aria-label*="agree" i]',
            'button[aria-label*="Allow" i]',
            // '.cmpboxbtn.cmpboxbtnyes',   // old one, not sure if still needed
        ];

        for (const sel of selectors) {
            try {
                const btn = document.querySelector(sel);
                if (btn && btn.offsetParent !== null) {
                    btn.click();
                    return true;
                }
            } catch(e) {}
        }

        // fallback - look for buttons with accept/agree text (including European translations)
        const buttons = document.querySelectorAll('button, a.button, [role="button"]');
        const keywords = [
            'accept', 'agree', 'got it', 'i understand', 'allow all', 'ok',
            'akzeptieren', 'zustimmen', 'verstanden', // German
            'aceptar', 'entendido', 'permitir',      // Spanish
            'accepter', "d'accord", 'autoriser',      // French
            'accetta', 'consenti'                     // Italian
        ];
        for (const btn of buttons) {
            const text = btn.textContent.toLowerCase().trim();
            const matches = keywords.some(kw => text.includes(kw));
            if (matches && text.length < 30 && btn.offsetParent !== null) {
                btn.click();
                return true;
            }
        }


        return false;
    }

    // try a few times since some load late
    setTimeout(dismissCookies, 1000);
    setTimeout(dismissCookies, 3000);
    setTimeout(dismissCookies, 5000);
})();
"""




CF_CHALLENGE_JS = """
(() => {
    function solveTurnstile() {
        // Note: contentDocument is not accessible due to cross-origin iframe security.
        // We handle the checkbox click natively using Playwright frame_locator instead.
        const checkboxes = document.querySelectorAll(
            '.cf-turnstile input, [data-action="managed-challenge"] input'
        );
        for (const cb of checkboxes) {
            try { cb.click(); } catch(e) {}
        }
    }

    setTimeout(solveTurnstile, 2000);
    setTimeout(solveTurnstile, 5000);
})();
"""


# Base spoofing scripts that don't need OS-specific config
_BASE_SPOOFING_SCRIPTS = "\n".join([
    CANVAS_SPOOF_JS,
    AUDIO_SPOOF_JS,
    FONT_SPOOF_JS,
    NAVIGATOR_SPOOF_JS,
    HEADLESS_SPOOF_JS,
])

def get_all_spoofing_scripts(os_name="Windows"):
    """Build the full spoofing JS bundle with OS-matched WebGL."""
    return _BASE_SPOOFING_SCRIPTS + "\n" + get_webgl_spoof_js(os_name)

ALL_HELPER_SCRIPTS = "\n".join([
    COOKIE_DISMISS_JS,
    CF_CHALLENGE_JS,
])

def get_full_stealth_injection(os_name="Windows"):
    """Build all spoofing + helper scripts for a given OS."""
    return get_all_spoofing_scripts(os_name) + "\n" + ALL_HELPER_SCRIPTS


# Backward compatibility variables
ALL_SPOOFING_SCRIPTS = get_all_spoofing_scripts("Windows")
FULL_STEALTH_INJECTION = get_full_stealth_injection("Windows")



async def inject_stealth_scripts(page, os_name="Windows"):
    """Inject all spoofing scripts with OS-matched WebGL GPU config."""
    await page.add_init_script(get_all_spoofing_scripts(os_name))


async def inject_helper_scripts(page):
    await page.evaluate(ALL_HELPER_SCRIPTS)


async def click_turnstile_native(page, timeout=45.0, max_clicks=4, verbose=False):
    deadline = time.time() + timeout
    clicks = 0

    selectors = [
        "input[type='checkbox']",
        "[role='checkbox']",
        "label:has-text('Verify you are human')",
        "div:has-text('Verify you are human')",
        "#challenge-stage",
    ]

    async def still_on_challenge():
        try:
            title = (await page.title()).lower()
        except Exception:
            title = ""
        try:
            url = page.url.lower()
        except Exception:
            url = ""
        try:
            body = (await page.locator("body").inner_text(timeout=1000)).lower()
        except Exception:
            body = ""

        markers = (
            "just a moment",
            "/__mc",
            "challenges.cloudflare.com",
            "safeguarding your website",
            "human verification is in progress",
            "verify you are human",
            "verification successful. waiting",
            "checking if the site connection is secure",
            "performance and security by cloudflare",
        )
        haystack = f"{title} {url} {body}"
        return any(marker in haystack for marker in markers)

    async def click_locator(locator):
        nonlocal clicks
        count = await locator.count()
        for i in range(min(count, 5)):
            candidate = locator.nth(i)
            try:
                if not await candidate.is_visible(timeout=500):
                    continue
                box = await candidate.bounding_box(timeout=1000)
                if box:
                    await page.mouse.move(
                        box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2,
                    )
                    await asyncio.sleep(0.15)
                    await page.mouse.click(
                        box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2,
                    )
                else:
                    await candidate.click(timeout=1500)
                clicks += 1
                if verbose:
                    print(f"[Turnstile] Clicked locator candidate; click #{clicks}")
                return True
            except Exception:
                continue
        return False

    async def click_visible_iframe_box():
        nonlocal clicks
        try:
            iframes = page.locator("iframe")
            count = await iframes.count()
        except Exception:
            return False

        for i in range(min(count, 10)):
            iframe = iframes.nth(i)
            try:
                if not await iframe.is_visible(timeout=500):
                    continue

                src = (await iframe.get_attribute("src", timeout=500) or "").lower()
                title = (await iframe.get_attribute("title", timeout=500) or "").lower()
                name = (await iframe.get_attribute("name", timeout=500) or "").lower()
                if not any(
                    marker in f"{src} {title} {name}"
                    for marker in ("cloudflare", "turnstile", "challenge")
                ):
                    continue

                box = await iframe.bounding_box(timeout=1000)
                if not box or box["width"] < 80 or box["height"] < 35:
                    continue

                # Cloudflare's checkbox sits near the left-center of the widget.
                x = box["x"] + min(28, box["width"] * 0.18)
                y = box["y"] + box["height"] / 2
                await page.mouse.move(x, y)
                await asyncio.sleep(0.2)
                await page.mouse.click(x, y)
                clicks += 1
                if verbose:
                    print(f"[Turnstile] Clicked iframe box at ({x:.1f}, {y:.1f}); click #{clicks}")
                return True
            except Exception:
                continue

        return False

    async def click_text_checkbox_area():
        nonlocal clicks
        text_locators = [
            page.get_by_text("Verify you are human", exact=False),
            page.locator("text=Verify you are human"),
        ]

        for locator in text_locators:
            try:
                count = await locator.count()
            except Exception:
                continue

            for i in range(min(count, 5)):
                candidate = locator.nth(i)
                try:
                    if not await candidate.is_visible(timeout=500):
                        continue
                    box = await candidate.bounding_box()
                    if not box:
                        continue

                    # The visual checkbox is directly left of the label text.
                    x = max(5, box["x"] - 25)
                    y = box["y"] + box["height"] / 2
                    await page.mouse.move(x, y)
                    await asyncio.sleep(0.2)
                    await page.mouse.click(x, y)
                    clicks += 1
                    if verbose:
                        print(f"[Turnstile] Clicked checkbox area left of text at ({x:.1f}, {y:.1f}); click #{clicks}")
                    return True
                except Exception:
                    continue

        return False

    async def click_visual_challenge_fallback():
        nonlocal clicks
        try:
            viewport = page.viewport_size or {}
            width = viewport.get("width") or await page.evaluate("window.innerWidth")
            height = viewport.get("height") or await page.evaluate("window.innerHeight")
        except Exception:
            width = 1365
            height = 900

        try:
            cards = page.locator("div, main, section")
            count = await cards.count()
        except Exception:
            count = 0

        best_box = None
        best_score = float("inf")
        for i in range(min(count, 80)):
            candidate = cards.nth(i)
            try:
                if not await candidate.is_visible(timeout=200):
                    continue
                text = (await candidate.inner_text(timeout=300)).lower()
                if "safeguarding your website" not in text and "human verification is in progress" not in text:
                    continue
                box = await candidate.bounding_box(timeout=500)
                if not box or box["width"] < 300 or box["height"] < 150:
                    continue
                if box["width"] > width * 0.8 or box["height"] > height * 0.95:
                    continue
                score = box["width"] * box["height"]
                if score < best_score:
                    best_score = score
                    best_box = box
            except Exception:
                continue

        if best_box:
            x = best_box["x"] + best_box["width"] * 0.335
            y = best_box["y"] + best_box["height"] * 0.515
        else:
            # BigScoots/Turnstile fallback based on the visible centered widget.
            x = width * 0.516
            y = height * 0.625

        await page.mouse.move(x, y)
        await asyncio.sleep(0.25)
        await page.mouse.click(x, y)
        clicks += 1
        if verbose:
            box_note = f" using card {best_box}" if best_box else " using viewport fallback"
            print(f"[Turnstile] Visual fallback click at ({x:.1f}, {y:.1f}); click #{clicks}{box_note}")
        return True

    while time.time() < deadline:
        if clicks and not await still_on_challenge():
            return True

        if clicks >= max_clicks:
            return False

        clicked = False
        frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame]
        for frame in frames:
            for selector in selectors:
                try:
                    if await click_locator(frame.locator(selector)):
                        clicked = True
                        break
                except Exception:
                    continue
            if clicked:
                break

        if not clicked:
            clicked = await click_visible_iframe_box()

        if not clicked:
            clicked = await click_text_checkbox_area()

        if not clicked and await still_on_challenge():
            clicked = await click_visual_challenge_fallback()

        if clicked:
            await asyncio.sleep(4.0)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            continue

        await asyncio.sleep(1.0)

    return clicks > 0 and not await still_on_challenge()

