"""Bounded, redirect-safe HTTP requests for untrusted scrape targets."""

from __future__ import annotations

import os
from urllib.parse import urljoin, urlsplit

from .proxy_config import proxy_timeout_bonus
from .url_safety import normalize_public_url


class ResponseTooLargeError(ValueError):
    """Raised when a response exceeds the configured decoded-body limit."""


class TooManyRedirectsError(ValueError):
    """Raised when a response exceeds the redirect hop limit."""


def max_response_bytes() -> int:
    """Return the response cap, clamped to a useful and safe range."""
    try:
        value = int(os.getenv("YADA_MAX_RESPONSE_BYTES", str(8 * 1024 * 1024)))
    except ValueError:
        value = 8 * 1024 * 1024
    return max(64 * 1024, min(value, 64 * 1024 * 1024))


def request_timeout_seconds() -> float:
    """Return a finite per-request timeout for every HTTP source path."""
    try:
        value = float(os.getenv("YADA_HTTP_TIMEOUT", "30"))
    except ValueError:
        value = 30.0
    return max(1.0, min(value, 120.0))


def resolve_request_timeout(session, declared=None) -> float:
    """Resolve the per-request timeout for one HTTP attempt.

    Priority:
    1. An explicit ``timeout=`` from the call site is honored as configured.
    2. The session's own configured timeout, floored at 20s so a tight
       session value can never undercut measured real-world fetch times.
    3. The global default from ``request_timeout_seconds()``.

    The proxy bonus (0 when direct) is added in every branch so proxy
    round-trip latency does not eat the budget.
    """
    bonus = proxy_timeout_bonus()
    if declared is not None:
        try:
            return max(1.0, float(declared)) + bonus
        except (TypeError, ValueError):
            return request_timeout_seconds() + bonus
    session_value = getattr(session, "timeout", None)
    if isinstance(session_value, (int, float)) and not isinstance(session_value, bool):
        return max(float(session_value), 20.0) + bonus
    return request_timeout_seconds() + bonus


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    port = (443 if parsed.scheme.lower() == "https" else 80) if parsed.port is None else parsed.port
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def _without_sensitive_headers(headers) -> dict:
    return {
        key: value
        for key, value in dict(headers or {}).items()
        if key.lower() not in {
            "authorization",
            "proxy-authorization",
            "cookie",
            "referer",
            "origin",
        }
    }


async def _discard_stream(response) -> None:
    """Abort a curl_cffi streaming response without downloading its body."""
    quit_now = getattr(response, "quit_now", None)
    if quit_now is not None:
        quit_now.set()
    await response.aclose()


async def _read_bounded(response, limit: int) -> bytes:
    declared = response.headers.get("content-length")
    if declared:
        try:
            declared_size = int(declared)
        except ValueError:
            declared_size = 0
        if declared_size > limit:
            await _discard_stream(response)
            raise ResponseTooLargeError(
                f"Response Content-Length exceeds the {limit}-byte limit"
            )

    chunks: list[bytes] = []
    received = 0
    try:
        async for chunk in response.aiter_content():
            received += len(chunk)
            if received > limit:
                raise ResponseTooLargeError(
                    f"Decoded response body exceeds the {limit}-byte limit"
                )
            chunks.append(chunk)
    except BaseException:
        # aiter_content normally closes only at EOF, so explicitly stop curl on
        # ANY failure (oversize body, network error, cancellation), but never
        # let cleanup replace the original exception.
        try:
            await _discard_stream(response)
        except BaseException:
            pass
        raise
    return b"".join(chunks)


async def safe_request(
    session,
    method: str,
    url: str,
    *,
    max_redirects: int = 5,
    response_limit: int | None = None,
    **kwargs,
):
    """Request a public URL while validating every redirect before following it.

    The returned value is the native curl_cffi Response object, populated with a
    bounded ``content`` body so existing ``.text``/``.json()`` consumers continue
    to work.
    """
    current_url = await normalize_public_url(url)
    current_method = method.upper()
    current_kwargs = dict(kwargs)
    current_kwargs.pop("allow_redirects", None)
    current_kwargs.pop("max_redirects", None)
    current_kwargs.pop("stream", None)
    current_kwargs["timeout"] = resolve_request_timeout(session, current_kwargs.get("timeout"))
    limit = max_response_bytes() if response_limit is None else response_limit
    history = []

    for hop in range(max_redirects + 1):
        response = await session.request(
            current_method,
            current_url,
            allow_redirects=False,
            stream=True,
            **current_kwargs,
        )
        response.url = str(response.url or current_url)
        location = response.headers.get("location")
        is_redirect = response.status_code in {301, 302, 303, 307, 308} and location
        if not is_redirect:
            response.content = await _read_bounded(response, limit)
            response.history = history
            return response

        if hop >= max_redirects:
            await _discard_stream(response)
            raise TooManyRedirectsError(f"Redirect limit of {max_redirects} exceeded")

        await _discard_stream(response)
        next_url = await normalize_public_url(urljoin(current_url, location))
        history.append(
            {
                "status_code": response.status_code,
                "url": response.url,
                "headers": response.headers,
            }
        )

        if _origin(current_url) != _origin(next_url):
            current_kwargs["headers"] = _without_sensitive_headers(
                current_kwargs.get("headers")
            )
            current_kwargs.pop("auth", None)
            # Explicit cookies must not be replayed to a different origin. The
            # session jar still applies its normal domain/path cookie scoping.
            current_kwargs.pop("cookies", None)

        # RFC 9110: 303 becomes GET (except HEAD); 307/308 preserve method and
        # body. Browsers conventionally rewrite POST on 301/302 to GET.
        if response.status_code == 303 and current_method != "HEAD":
            current_method = "GET"
        elif response.status_code in {301, 302} and current_method == "POST":
            current_method = "GET"
        if current_method in {"GET", "HEAD"}:
            for body_key in ("data", "json", "files", "multipart"):
                current_kwargs.pop(body_key, None)
        current_url = next_url

    raise TooManyRedirectsError(f"Redirect limit of {max_redirects} exceeded")


async def safe_get(session, url: str, **kwargs):
    return await safe_request(session, "GET", url, **kwargs)
