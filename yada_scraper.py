import asyncio
import inspect
import os
import sys
import secrets
from contextlib import asynccontextmanager
from enum import Enum
from typing import Optional
from collections import defaultdict
from urllib.parse import urlparse
from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv()


if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from scraper import ScraperEngine
from scraper.engine import _http_attempt_timeout, _scrape_budget
from scraper.proxy_config import proxy_enabled, proxy_insecure, proxy_timeout_bonus
from scraper.safe_http import request_timeout_seconds
from scraper.source_fast_paths import hard_source_status, source_name
from scraper.url_safety import UnsafeUrlError, normalize_public_url


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_env_bytes(name: str, default: int) -> int:
    try:
        configured = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return configured if configured > 0 else default


class RequestBodyLimitMiddleware:
    """Reject oversized request bodies before JSON parsing or endpoint execution."""

    def __init__(self, app):
        self.app = app

    @staticmethod
    async def _reject(scope, receive, send, status_code, detail):
        response = JSONResponse({"detail": detail}, status_code=status_code)
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        limit = _positive_env_bytes("YADA_MAX_REQUEST_BYTES", 256 * 1024)
        content_lengths = []
        for key, value in scope.get("headers", []):
            if key.lower() != b"content-length":
                continue
            try:
                content_lengths.extend(
                    int(part.strip()) for part in value.decode("ascii").split(",")
                )
            except (UnicodeDecodeError, ValueError):
                await self._reject(scope, receive, send, 400, "Invalid Content-Length")
                return

        if content_lengths:
            if any(length < 0 for length in content_lengths) or len(set(content_lengths)) != 1:
                await self._reject(scope, receive, send, 400, "Invalid Content-Length")
                return
            if content_lengths[0] > limit:
                await self._reject(
                    scope,
                    receive,
                    send,
                    413,
                    f"Request body exceeds the {limit}-byte limit",
                )
                return

        body = bytearray()
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") != "http.request":
                continue
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > limit:
                await self._reject(
                    scope,
                    receive,
                    send,
                    413,
                    f"Request body exceeds the {limit}-byte limit",
                )
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)


def _validate_runtime_security_config():
    if _truthy_env("YADA_REQUIRE_API_KEY") and not os.getenv("YADA_API_KEY", "").strip():
        raise RuntimeError("YADA_REQUIRE_API_KEY is enabled but YADA_API_KEY is not set")


@asynccontextmanager
async def _lifespan(_app):
    _validate_runtime_security_config()
    yield


app = FastAPI(
    title="YADA Scraper API",
    version="1.0.0",
    lifespan=_lifespan,
)


@app.exception_handler(RequestValidationError)
async def _bounded_validation_error(_request: Request, exc: RequestValidationError):
    """Return useful validation details without echoing an arbitrarily large body."""
    errors = []
    for error in exc.errors():
        sanitized = dict(error)
        sanitized.pop("input", None)
        errors.append(sanitized)
    return JSONResponse({"detail": errors}, status_code=422)

_cors_origins = [
    origin.strip()
    for origin in os.getenv("YADA_CORS_ORIGINS", "").split(",")
    if origin.strip()
]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-API-Key"],
    )

app.add_middleware(RequestBodyLimitMiddleware)

engine = ScraperEngine()


async def _engine_scrape(**kwargs):
    """Call the active engine while preserving compatibility with test/custom engines."""
    parameters = inspect.signature(engine.scrape).parameters
    supported = {key: value for key, value in kwargs.items() if key in parameters}
    return await engine.scrape(**supported)


async def _engine_source_bulk(urls, allow_browser=True):
    parameters = inspect.signature(engine.scrape_source_bulk).parameters if hasattr(engine, "scrape_source_bulk") else {}
    if "allow_browser" in parameters:
        return await engine.scrape_source_bulk(urls, allow_browser=allow_browser)
    return await engine.scrape_source_bulk(urls)


async def _engine_compare(url):
    return await engine.compare(url)

def _key_matches(provided: Optional[str], required: str) -> bool:
    if not provided:
        return False
    try:
        return secrets.compare_digest(
            provided.encode("utf-8"),
            required.encode("utf-8"),
        )
    except (AttributeError, UnicodeError):
        return False


def _enforce_api_key(x_api_key: Optional[str]):
    required = os.getenv("YADA_API_KEY", "").strip()
    if not required:
        if _truthy_env("YADA_REQUIRE_API_KEY"):
            raise HTTPException(
                status_code=503,
                detail="API key authentication is required but not configured",
            )
        return
    if not _key_matches(x_api_key, required):
        raise HTTPException(status_code=401, detail="Missing or invalid API key")


def _enforce_admin_key(x_api_key: Optional[str]):
    required = (
        os.getenv("YADA_ADMIN_API_KEY", "").strip()
        or os.getenv("YADA_API_KEY", "").strip()
    )
    if not required:
        raise HTTPException(status_code=404, detail="Memory administration is disabled")
    if not _key_matches(x_api_key, required):
        raise HTTPException(status_code=401, detail="Missing or invalid admin API key")


async def _validated_url(url: str) -> str:
    try:
        return await normalize_public_url(url)
    except UnsafeUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _browser_source_concurrency(limit=10):
    try:
        requested = int(os.getenv("YADA_BROWSER_SOURCE_CONCURRENCY", "2") or "2")
    except ValueError:
        requested = 1
    return max(1, min(limit, requested))


class ScrapeMode(str, Enum):
    auto = "auto"
    source = "source"
    structured = "structured"
    layer1 = "layer1"
    layer1b = "layer1b"
    layer1c = "layer1c"
    mobile = "mobile"
    layer2 = "layer2"
    layer3 = "layer3"
    layer4 = "layer4"
    layer5 = "layer5"
    layer6 = "layer6"

class BulkRequest(BaseModel):
    urls: list[str] = Field(max_length=100)
    mode: ScrapeMode = ScrapeMode.auto
    max_concurrency: int = Field(default=3, ge=1, le=20)
    same_domain_fanout: int = Field(default=5, ge=1, le=8)
    allow_layer5: bool = False
    allow_browser: bool = True
    timeout_seconds: float = Field(default=120.0, ge=1.0, le=600.0)


class BulkResponse(BaseModel):
    total: int
    succeeded: int
    failed: int
    results: list[dict]


class ScrapeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    url: Optional[str] = None
    mode: ScrapeMode = ScrapeMode.auto
    allow_layer5: bool = False
    allow_browser: bool = True
    timeout_seconds: float = Field(default=120.0, ge=1.0, le=600.0)


class ScrapeResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    success: bool = Field(description="True if an HTTP response was obtained and captured from the target.")
    usable: bool = Field(default=False, description="True if captured content is valid and usable (not a bot challenge or block requiring retry). Check this before parsing.")
    url: str
    content: Optional[dict] = None
    layer_used: str = ""
    total_time: float = 0.0
    layer_attempts: list[dict] = Field(default_factory=list)
    error: str = ""
    captured: bool = False
    capture_state: str = "no_capture"
    diagnostics: list[str] = Field(default_factory=list)
    best_attempt: Optional[dict] = None
    html_length: int = 0
    text_length: int = 0
    status_code: int = 0
    final_url: Optional[str] = None


class CompareResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    url: str
    total_time: float = 0.0
    layers: list[dict] = Field(default_factory=list)
    fastest_successful: Optional[str] = None
    recommendation: str = ""




@app.get("/scrape", response_model=ScrapeResponse)
async def do_scrape(
    url: str = Query(..., description="URL to scrape"),
    mode: ScrapeMode = Query(ScrapeMode.auto),
    allow_layer5: bool = Query(False, description="Allow full-browser layer 5 during auto mode"),
    allow_browser: bool = Query(True, description="Allow browser fallback layers (Layer 4/5)"),
    timeout_seconds: float = Query(120.0, ge=1.0, le=600.0),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    _enforce_api_key(x_api_key)
    url = await _validated_url(url)

    try:
        result = await asyncio.wait_for(
            _engine_scrape(
                url=url,
                mode=mode.value,
                allow_layer5=allow_layer5,
                allow_browser=allow_browser,
            ),
            timeout=max(1.0, min(float(timeout_seconds), 600.0)),
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"Scrape timed out after {timeout_seconds}s")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return result.to_dict()


@app.post("/scrape", response_model=ScrapeResponse)
async def do_scrape_post(
    request: Optional[ScrapeRequest] = Body(None),
    url: Optional[str] = Query(None, description="URL to scrape"),
    mode: Optional[ScrapeMode] = Query(None),
    allow_layer5: Optional[bool] = Query(None),
    allow_browser: Optional[bool] = Query(None),
    timeout_seconds: Optional[float] = Query(None, ge=1.0, le=600.0),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    _enforce_api_key(x_api_key)
    target_url = (request.url if request and request.url else None) or url
    if not target_url:
        raise HTTPException(status_code=422, detail="Field 'url' is required")
    target_mode = (request.mode if request and request.mode is not None else None) or mode or ScrapeMode.auto
    target_allow_layer5 = (request.allow_layer5 if request and request.allow_layer5 is not None else None)
    if target_allow_layer5 is None:
        target_allow_layer5 = allow_layer5 if allow_layer5 is not None else False
    target_allow_browser = (request.allow_browser if request and request.allow_browser is not None else None)
    if target_allow_browser is None:
        target_allow_browser = allow_browser if allow_browser is not None else True
    target_timeout = (request.timeout_seconds if request and request.timeout_seconds is not None else None)
    if target_timeout is None:
        target_timeout = timeout_seconds if timeout_seconds is not None else 120.0

    target_url = await _validated_url(target_url)

    try:
        result = await asyncio.wait_for(
            _engine_scrape(
                url=target_url,
                mode=target_mode.value if hasattr(target_mode, "value") else str(target_mode),
                allow_layer5=target_allow_layer5,
                allow_browser=target_allow_browser,
            ),
            timeout=max(1.0, min(float(target_timeout), 600.0)),
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"Scrape timed out after {target_timeout}s")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return result.to_dict()


@app.get("/scrape/compare", response_model=CompareResponse)
async def do_compare(
    url: str = Query(..., description="URL to compare all layers on"),
    timeout_seconds: float = Query(180.0, ge=1.0, le=600.0),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    _enforce_api_key(x_api_key)
    url = await _validated_url(url)

    try:
        result = await asyncio.wait_for(
            _engine_compare(url=url),
            timeout=max(1.0, min(float(timeout_seconds), 600.0)),
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"Compare timed out after {timeout_seconds}s")
    return result.to_dict()


@app.post("/scrape/bulk", response_model=BulkResponse)
async def do_bulk(
    request: BulkRequest,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    _enforce_api_key(x_api_key)
    if not request.urls:
        return BulkResponse(total=0, succeeded=0, failed=0, results=[])

    checked = await asyncio.gather(
        *(normalize_public_url(url) for url in request.urls),
        return_exceptions=True,
    )
    urls = []
    prevalidated_results = []
    valid_indices = []
    for index, (submitted_url, result) in enumerate(zip(request.urls, checked)):
        if isinstance(result, Exception):
            urls.append(submitted_url)
            prevalidated_results.append({
                "success": False,
                "url": submitted_url,
                "error": str(result).strip() or "URL validation failed",
                "layer_used": "rejected",
                "best_attempt": None,
            })
        else:
            urls.append(result)
            prevalidated_results.append(None)
            valid_indices.append(index)

    if not valid_indices:
        return BulkResponse(
            total=len(urls),
            succeeded=0,
            failed=len(urls),
            results=[{**result, "usable": False} for result in prevalidated_results],
        )

    memory = getattr(engine, "memory", None)
    using_deferred_context = bool(memory and hasattr(memory, "begin_deferred_writes"))
    old_defer_writes = getattr(memory, "defer_writes", False) if memory else False
    if using_deferred_context:
        await memory.begin_deferred_writes()
    elif memory and hasattr(memory, "defer_writes"):
        memory.defer_writes = True

    def _usable(item):
        if hasattr(item, "usable"):
            return bool(item.usable)
        if isinstance(item, dict):
            if "usable" in item:
                return bool(item["usable"])
            if not item.get("success"):
                return False
            best = item.get("best_attempt") or {}
            return not bool(best.get("retry_recommended"))
        return False

    try:
        if request.mode.value == "auto":
            indexed_urls = [(index, urls[index]) for index in valid_indices]
            grouped = defaultdict(list)
            for idx, url in indexed_urls:
                grouped[urlparse(url).netloc.lower()].append((idx, url))

            results = list(prevalidated_results)
            domain_limit = max(1, min(request.max_concurrency, 10))
            domain_sem = asyncio.Semaphore(domain_limit)
            browser_source_limit = _browser_source_concurrency(domain_limit)
            browser_source_sem = asyncio.Semaphore(browser_source_limit)
            browser_sources = {
                "bestbuy", "ebay", "leboncoin", "zillow", "homedepot",
                "bloomberg", "tripadvisor", "belk", "quora",
            }
            fanout_limit = max(1, min(request.same_domain_fanout, 8))
            url_timeout = max(1.0, min(float(request.timeout_seconds or 120.0), 600.0))

            async def _scrape_domain(items):
                async with domain_sem:
                    first_idx, first_url = items[0]
                    remaining = items
                    cookies = engine.memory.get_valid_cookies(first_url)
                    warmup_usable = True
                    source = source_name(first_url)

                    if source and hasattr(engine, "scrape_source_bulk"):
                        group_urls = [url for _, url in items]
                        try:
                            if source in browser_sources:
                                if not request.allow_browser:
                                    if source == "quora":
                                        # Static-only bulk exists; no browser slot needed.
                                        source_results = await _engine_source_bulk(group_urls, allow_browser=False)
                                    else:
                                        source_results = None
                                else:
                                    async with browser_source_sem:
                                        source_results = await _engine_source_bulk(group_urls, allow_browser=request.allow_browser)
                            else:
                                source_results = await _engine_source_bulk(group_urls, allow_browser=request.allow_browser)
                        except Exception:
                            source_results = []

                        if source_results is not None:
                            failed_source_items = []
                            for position, (idx, item_url) in enumerate(items):
                                result = (
                                    source_results[position]
                                    if position < len(source_results)
                                    else None
                                )
                                if result is None or isinstance(result, Exception):
                                    failed_source_items.append((idx, item_url))
                                    continue
                                out = result if isinstance(result, dict) else result.to_dict()
                                out["bulk_role"] = f"source_{source}_bulk"
                                if _usable(out):
                                    results[idx] = out
                                else:
                                    if source == "reddit":
                                        results[idx] = out
                                    else:
                                        failed_source_items.append((idx, urls[idx]))

                            if failed_source_items:
                                sem = asyncio.Semaphore(fanout_limit)

                                async def _source_fallback(idx, url):
                                    async with sem:
                                        fallback = await _engine_scrape(
                                            url=url,
                                            mode="auto",
                                            allow_layer5=request.allow_layer5,
                                            allow_browser=request.allow_browser,
                                            skip_source=True,
                                        )
                                        out = fallback.to_dict()
                                        out["bulk_role"] = f"source_{source}_fallback_auto"
                                        results[idx] = out

                                await asyncio.gather(*(_source_fallback(idx, url) for idx, url in failed_source_items))
                            return

                    if not cookies:
                        warmup = await _engine_scrape(
                            url=first_url,
                            mode="auto",
                            allow_layer5=request.allow_layer5,
                            allow_browser=request.allow_browser,
                        )
                        warmup_dict = warmup.to_dict()
                        warmup_dict["bulk_role"] = "domain_source_auto" if source else "domain_auto_warmup"
                        warmup_usable = _usable(warmup_dict)
                        results[first_idx] = warmup_dict
                        remaining = items[1:]

                    sem = asyncio.Semaphore(fanout_limit)

                    async def _layer2_fanout(idx, url):
                        async with sem:
                            result = await engine.scrape_layer2_after_warmup(url)
                            is_403 = "http_403" in result.diagnostics
                            needs_retry = bool(
                                result.best_attempt
                                and result.best_attempt.get("retry_recommended")
                            )
                            if not result.ok or is_403 or needs_retry:
                                if is_403:
                                    await engine.memory.clear_cookies(url)
                                if warmup_usable:
                                    result = await _engine_scrape(
                                        url=url,
                                        mode="auto",
                                        allow_layer5=request.allow_layer5,
                                        allow_browser=request.allow_browser,
                                    )
                                    out = result.to_dict()
                                    out["bulk_role"] = "cookie_fanout_fallback_auto"
                                else:
                                    out = result.to_dict()
                                    out["bulk_role"] = "cookie_fanout_after_bad_warmup"
                            else:
                                out = result.to_dict()
                                out["bulk_role"] = "cookie_fanout"
                            return idx, out

                    fanout = await asyncio.gather(
                        *[_layer2_fanout(idx, url) for idx, url in remaining],
                        return_exceptions=True,
                    )
                    for item in fanout:
                        if isinstance(item, Exception):
                            continue
                        idx, out = item
                        results[idx] = out

            async def _scrape_domain_with_timeout(items):
                timeout = url_timeout
                try:
                    await asyncio.wait_for(_scrape_domain(items), timeout=timeout)
                except asyncio.TimeoutError:
                    for idx, url in items:
                        if results[idx] is None:
                            results[idx] = {
                                "success": False,
                                "url": url,
                                "error": f"Bulk domain group timed out after {round(timeout, 3)}s",
                                "layer_used": "timeout",
                                "best_attempt": None,
                            }
                except Exception as exc:
                    for idx, url in items:
                        if results[idx] is None:
                            results[idx] = {
                                "success": False,
                                "url": url,
                                "error": f"Bulk domain group failed: {type(exc).__name__}",
                                "layer_used": "error",
                                "best_attempt": None,
                            }

            await asyncio.gather(
                *[_scrape_domain_with_timeout(items) for items in grouped.values()],
                return_exceptions=False,
            )
        else:
            is_browser_mode = request.mode.value in ("layer4", "layer5")
            max_allowed = 3 if is_browser_mode else 20
            concurrency = max(1, min(request.max_concurrency, max_allowed))
            sem = asyncio.Semaphore(concurrency)

            async def _scrape_with_sem(url):
                async with sem:
                    return await asyncio.wait_for(
                        _engine_scrape(
                            url=url,
                            mode=request.mode.value,
                            allow_layer5=request.allow_layer5,
                            allow_browser=request.allow_browser,
                        ),
                        timeout=max(1.0, min(float(request.timeout_seconds or 120.0), 600.0)),
                    )

            task_items = [
                (index, asyncio.create_task(_scrape_with_sem(urls[index])))
                for index in valid_indices
            ]
            tasks = [task for _, task in task_items]
            try:
                done, pending = await asyncio.wait(
                    tasks,
                    timeout=max(1.0, min(float(request.timeout_seconds or 120.0), 600.0)),
                )
            except BaseException:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            results = list(prevalidated_results)
            for (index, task) in task_items:
                url = urls[index]
                if task in pending:
                    results[index] = asyncio.TimeoutError(f"Bulk request deadline exceeded for {url}")
                else:
                    try:
                        results[index] = task.result()
                    except Exception as exc:
                        results[index] = exc

        output = []
        succeeded = 0
        failed = 0

        for r in results:
            if isinstance(r, Exception):
                message = str(r).strip() or type(r).__name__
                timed_out = isinstance(r, (asyncio.TimeoutError, TimeoutError))
                output.append({
                    "success": False,
                    "usable": False,
                    "url": urls[len(output)],
                    "error": message,
                    "layer_used": "timeout" if timed_out else "error",
                    "best_attempt": None,
                })
                failed += 1
            elif r is None:
                output.append({
                    "success": False,
                    "usable": False,
                    "url": urls[len(output)],
                    "error": "Bulk scrape did not return a result",
                    "layer_used": "error",
                    "best_attempt": None,
                })
                failed += 1
            elif isinstance(r, dict):
                r["usable"] = _usable(r)
                output.append(r)
                if r["usable"]:
                    succeeded += 1
                else:
                    failed += 1
            else:
                out = r.to_dict()
                out["usable"] = _usable(out)
                output.append(out)
                if out["usable"]:
                    succeeded += 1
                else:
                    failed += 1

        return BulkResponse(total=len(urls), succeeded=succeeded, failed=failed, results=output)
    finally:
        if using_deferred_context:
            await memory.end_deferred_writes()
        elif memory and hasattr(memory, "defer_writes"):
            memory.defer_writes = old_defer_writes
            if hasattr(memory, "flush"):
                await memory.flush()


@app.get("/health", response_model=dict)
async def health():
    return {
        "status": "healthy",
        "service": "yada-scraper-api",
        "version": app.version,
        "auth": "api_key" if os.getenv("YADA_API_KEY", "").strip() else "none",
        "proxy": (
            "off" if not proxy_enabled()
            else "on-insecure" if proxy_insecure()
            else "on"
        ),
        "http_timeout": request_timeout_seconds(),
        "http_attempt_timeout": _http_attempt_timeout(),
        "proxy_timeout_bonus": proxy_timeout_bonus(),
        "scrape_budget": _scrape_budget(),
        "memory_admin": "enabled" if (
            os.getenv("YADA_ADMIN_API_KEY", "").strip()
            or os.getenv("YADA_API_KEY", "").strip()
        ) else "disabled",
        "hard_sources": {
            **hard_source_status(),
            "browser_source_concurrency": _browser_source_concurrency(),
        },
        "features": [
            "Downloads coverage routes plus Current structured/API routes",
            "Bounded raw HTTP, browser-like HTTP, crawler personas, and Playwright",
            "Selective Android mobile HTTP with an explicit mobile mode",
            "WAF detection recorded as a hint without suppressing browser capture",
            "Atomic site memory with root-domain lookup and deferred bulk flush",
            "Guarded Layer 5 browser path and reusable Playwright context pool",
            "Optional compact structured source mode",
        ],
        "endpoints": {
            "/scrape": "Single URL with auto-fallback",
            "/scrape/compare": "All layers side-by-side",
            "/scrape/bulk": "Multiple URLs at once",
            "/memory": "View site memory",
            "/memory/clear": "Clear site memory",
            "/health": "This endpoint",
            "/docs": "Swagger docs",
        },
    }


@app.get("/memory", response_model=dict)
async def get_memory(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    _enforce_admin_key(x_api_key)

    profiles = engine.memory.get_all_profiles()
    profile_values = list(profiles.values()) if isinstance(profiles, dict) else list(profiles)
    redacted_profiles = []
    for profile in profile_values:
        redacted = dict(profile)
        redacted["cookie_count"] = len(profile.get("cookies", []))
        redacted.pop("cookies", None)
        redacted_profiles.append(redacted)
    return {
        "total_domains": len(profile_values),
        "profiles": redacted_profiles,
    }


@app.delete("/memory/clear", response_model=dict)
async def clear_memory(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    _enforce_admin_key(x_api_key)
    await engine.memory.clear()
    return {"status": "cleared", "message": "All site memory has been reset."}


def _openapi_with_api_key_contract():
    if app.openapi_schema:
        return app.openapi_schema

    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    security_schemes = schema.setdefault("components", {}).setdefault(
        "securitySchemes", {}
    )
    security_schemes["ApiKeyAuth"] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
        "description": (
            "Required when YADA_API_KEY/YADA_REQUIRE_API_KEY is configured; "
            "memory administration always requires a configured key."
        ),
    }

    optional_key = [{"ApiKeyAuth": []}, {}]
    protected_key = [{"ApiKeyAuth": []}]
    for path, method in (
        ("/scrape", "get"),
        ("/scrape", "post"),
        ("/scrape/compare", "get"),
        ("/scrape/bulk", "post"),
    ):
        schema["paths"][path][method]["security"] = optional_key
    schema["paths"]["/memory"]["get"]["security"] = protected_key
    schema["paths"]["/memory/clear"]["delete"]["security"] = protected_key

    app.openapi_schema = schema
    return schema


app.openapi = _openapi_with_api_key_contract



if __name__ == "__main__":
    import uvicorn

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    uvicorn.run(
        "yada_scraper:app",
        host="127.0.0.1",
        port=8000,

    )
