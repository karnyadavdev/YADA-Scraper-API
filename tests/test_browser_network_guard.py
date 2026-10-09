import asyncio
from types import SimpleNamespace

import pytest

from scraper import browser_network_guard as guard_mod
from scraper.url_safety import UnsafeUrlError


class FakeRoute:
    def __init__(self):
        self.aborted = 0
        self.continued = 0
        self.abort_code = None

    async def abort(self, code=None):
        self.aborted += 1
        self.abort_code = code

    async def continue_(self):
        self.continued += 1


class FakeContext:
    async def route(self, pattern, handler):
        self.pattern = pattern
        self.handler = handler


@pytest.mark.asyncio
async def test_public_requests_are_validated_then_run_composed_policy(monkeypatch):
    checked = []
    policy_calls = []

    async def validate(url):
        checked.append(url)
        return url

    async def policy(route, request):
        policy_calls.append(request.url)
        await route.continue_()

    monkeypatch.setattr(guard_mod, "normalize_public_url", validate)
    context = FakeContext()
    await guard_mod.install_browser_network_guard(context, policy)

    first = FakeRoute()
    second = FakeRoute()
    await context.handler(first, SimpleNamespace(url="https://example.com/a.js"))
    await context.handler(second, SimpleNamespace(url="https://example.com/b.css"))

    assert context.pattern == "**/*"
    assert checked == ["https://example.com/a.js", "https://example.com/b.css"]
    assert policy_calls == ["https://example.com/a.js", "https://example.com/b.css"]
    assert first.continued == second.continued == 1
    assert first.aborted == second.aborted == 0


@pytest.mark.asyncio
async def test_private_redirect_and_subresource_are_aborted_fail_closed(monkeypatch):
    policy_calls = []

    async def reject(url):
        raise UnsafeUrlError("private")

    async def policy(route, request):
        policy_calls.append(request.url)
        await route.continue_()

    monkeypatch.setattr(guard_mod, "normalize_public_url", reject)
    context = FakeContext()
    await guard_mod.install_browser_network_guard(context, policy)

    for target in (
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "https://internal.example/secret",
    ):
        route = FakeRoute()
        await context.handler(route, SimpleNamespace(url=target))
        assert route.aborted == 1
        assert route.continued == 0
        assert route.abort_code == "blockedbyclient"

    assert policy_calls == []


@pytest.mark.asyncio
async def test_internal_page_schemes_bypass_dns_but_still_run_content_policy(monkeypatch):
    async def should_not_validate(url):
        pytest.fail(f"internal URL was sent to public DNS validation: {url}")

    monkeypatch.setattr(guard_mod, "normalize_public_url", should_not_validate)
    context = FakeContext()
    await guard_mod.install_browser_network_guard(context)

    for target in ("about:blank", "data:text/plain,ok", "blob:https://example.com/id"):
        route = FakeRoute()
        await context.handler(route, SimpleNamespace(url=target))
        assert route.continued == 1
        assert route.aborted == 0


@pytest.mark.asyncio
async def test_non_web_schemes_and_validator_errors_abort(monkeypatch):
    async def broken_validator(url):
        raise RuntimeError("resolver failure")

    monkeypatch.setattr(guard_mod, "normalize_public_url", broken_validator)
    context = FakeContext()
    await guard_mod.install_browser_network_guard(context)

    for target in ("file:///etc/passwd", "ftp://example.com/file", "https://example.com/"):
        route = FakeRoute()
        await context.handler(route, SimpleNamespace(url=target))
        assert route.aborted == 1
        assert route.continued == 0


@pytest.mark.asyncio
async def test_route_cancellation_aborts_request_and_propagates(monkeypatch):
    async def cancelled_validator(url):
        raise asyncio.CancelledError

    monkeypatch.setattr(guard_mod, "normalize_public_url", cancelled_validator)
    context = FakeContext()
    await guard_mod.install_browser_network_guard(context)
    route = FakeRoute()

    with pytest.raises(asyncio.CancelledError):
        await context.handler(route, SimpleNamespace(url="https://example.com/"))

    assert route.aborted == 1
    assert route.continued == 0


def test_service_workers_are_blocked_at_context_construction_boundary():
    assert guard_mod.service_worker_context_options() == {"service_workers": "block"}
