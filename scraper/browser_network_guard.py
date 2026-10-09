"""Fail-closed network egress guard for Playwright browser contexts.

The API boundary validates the initial URL, but a public page can redirect to or
embed a private-network URL.  This context-level route validates every network
origin before Chromium is allowed to issue the request.
"""

import asyncio
from urllib.parse import urlsplit

from .url_safety import normalize_public_url


_BROWSER_INTERNAL_SCHEMES = {
    "about",
    "blob",
    "chrome",
    "chrome-extension",
    "data",
    "devtools",
}


def service_worker_context_options():
    """Options that keep service workers from bypassing Playwright routing."""
    return {"service_workers": "block"}


async def _route_action(action, *args):
    """Ignore only normal teardown races; preserve unexpected route failures."""
    try:
        await action(*args)
    except asyncio.CancelledError:
        raise
    except TypeError:
        # Test doubles and older Playwright releases may not accept an abort code.
        if args:
            await action()
        else:
            raise
    except Exception as exc:
        message = str(exc).lower()
        expected_teardown = (
            "has been closed",
            "execution context was destroyed",
            "target page, context or browser has been closed",
            "most likely because of a navigation",
            "route is already handled",
        )
        if not any(marker in message for marker in expected_teardown):
            raise


async def _abort_quietly(route):
    try:
        await _route_action(route.abort, "blockedbyclient")
    except (asyncio.CancelledError, Exception):
        # This is cleanup after validation/cancellation; the request must never
        # be continued merely because the page was concurrently torn down.
        return


async def install_browser_network_guard(context, request_handler=None):
    """Install one context route that composes SSRF and optional content policy.

    ``request_handler`` is invoked only after the target is known-safe. This is
    how tracker filtering remains active without a page-level route taking
    precedence over (and bypassing) the context-level SSRF guard.
    """
    async def guarded_route(route, request):
        target = getattr(request, "url", "")
        try:
            scheme = urlsplit(target).scheme.lower()
        except (TypeError, ValueError):
            await _abort_quietly(route)
            return

        if scheme not in _BROWSER_INTERNAL_SCHEMES:
            if scheme not in {"http", "https"}:
                await _abort_quietly(route)
                return
            try:
                # Validate the complete request target, not only its origin, so
                # URL length, credentials, ports and DNS are all rechecked on
                # redirects as well as every subresource request.
                await normalize_public_url(target)
            except asyncio.CancelledError:
                await asyncio.shield(_abort_quietly(route))
                raise
            except Exception:
                await _abort_quietly(route)
                return

        if request_handler is not None:
            await request_handler(route, request)
        else:
            await _route_action(route.continue_)

    await context.route("**/*", guarded_route)
    return guarded_route
