import os
from pathlib import Path


def find_chromium_binary():
    for env_name in (
        "YADA_BROWSER_EXECUTABLE",
        "YADA_CHROME_BINARY",
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH",
    ):
        value = os.environ.get(env_name, "").strip()
        if value and Path(value).is_file():
            return value

    roots = []
    playwright_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if playwright_root:
        roots.append(Path(playwright_root))
    roots.extend([
        Path("/ms-playwright"),
        Path.home() / "AppData" / "Local" / "ms-playwright",
        Path.home() / ".cache" / "ms-playwright",
        Path.home() / "Library" / "Caches" / "ms-playwright",
    ])

    patterns = (
        "chromium-*/chrome-win*/chrome.exe",
        "chromium-*/chrome-linux*/chrome",
        "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
    )
    matches = []
    for root in roots:
        if not root.exists():
            continue
        for pattern in patterns:
            matches.extend(path for path in root.glob(pattern) if path.is_file())
    if matches:
        return str(sorted(matches, reverse=True)[0])

    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ]
    for candidate in candidates:
        if str(candidate) and candidate.is_file():
            return str(candidate)

    return ""
