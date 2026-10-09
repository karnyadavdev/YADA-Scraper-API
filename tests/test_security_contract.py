import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

import yada_scraper as api_mod
from scraper import url_safety
from scraper.site_memory import SiteMemory

_real_resolve_addresses = url_safety._resolve_addresses



@pytest.mark.asyncio
async def test_url_guard_normalizes_public_http_and_rejects_private_targets(monkeypatch):
    async def public_dns(host, port):
        assert (host, port) == ("example.com", 443)
        return {"93.184.216.34"}

    monkeypatch.setattr(url_safety, "_resolve_addresses", public_dns)
    assert await url_safety.normalize_public_url("example.com/path?q=1") == (
        "https://example.com/path?q=1"
    )

    async def private_dns(host, port):
        return {"10.0.0.7"}

    monkeypatch.setattr(url_safety, "_resolve_addresses", private_dns)
    with pytest.raises(url_safety.UnsafeUrlError, match="non-public"):
        await url_safety.normalize_public_url("https://internal.example/")

    for target in (
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://localhost/",
    ):
        with pytest.raises(url_safety.UnsafeUrlError):
            await url_safety.normalize_public_url(target)


@pytest.mark.asyncio
async def test_url_guard_rejects_scheme_credentials_port_and_oversize(monkeypatch):
    monkeypatch.setenv("YADA_MAX_URL_LENGTH", "256")
    for target in (
        "file:///etc/passwd",
        "https://user:secret@example.com/",
        "https://example.com:22/",
        "https://example.com:0/",
        "https://example.com:65536/",
        "https://example.com:abc/",
        "https://example.com/" + ("a" * 300),
    ):
        with pytest.raises(url_safety.UnsafeUrlError):
            await url_safety.normalize_public_url(target)


@pytest.mark.asyncio
async def test_scrape_rejects_private_url_before_engine(monkeypatch):
    called = False

    async def should_not_run(**kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(api_mod, "_engine_scrape", should_not_run)
    with pytest.raises(HTTPException) as exc_info:
        await api_mod.do_scrape(
            url="http://127.0.0.1/",
            mode=api_mod.ScrapeMode.auto,
            allow_layer5=False,
            timeout_seconds=1,
            x_api_key=None,
        )

    assert exc_info.value.status_code == 400
    assert not called


@pytest.mark.asyncio
async def test_bulk_mixed_validation_preserves_one_result_per_input(monkeypatch):
    class FakeMemory:
        defer_writes = False

        def __init__(self):
            self.lookups = []

        def get_valid_cookies(self, url):
            self.lookups.append(url)
            return []

        async def flush(self):
            return None

    class FakeResult:
        ok = True
        diagnostics = []
        best_attempt = {"retry_recommended": False}

        def __init__(self, url):
            self.url = url

        def to_dict(self):
            return {
                "success": True,
                "url": self.url,
                "layer_used": "fake",
                "best_attempt": self.best_attempt,
            }

    class FakeEngine:
        def __init__(self):
            self.memory = FakeMemory()
            self.seen = []

        async def scrape(self, url, **kwargs):
            self.seen.append(url)
            return FakeResult(url)

        async def scrape_layer2_after_warmup(self, url):
            raise AssertionError("no fanout expected")

    async def public_dns(host, port):
        return {"93.184.216.34"}

    fake = FakeEngine()
    original = api_mod.engine
    monkeypatch.setattr(url_safety, "_resolve_addresses", public_dns)
    monkeypatch.setattr(api_mod, "engine", fake)
    monkeypatch.setattr(api_mod, "source_name", lambda url: None)
    try:
        response = await api_mod.do_bulk(
            api_mod.BulkRequest(urls=["example.com/ok", "http://127.0.0.1/private"]),
            x_api_key=None,
        )
    finally:
        api_mod.engine = original

    assert response.total == 2
    assert response.succeeded == 1
    assert response.failed == 1
    assert len(response.results) == 2
    assert response.results[0]["url"] == "https://example.com/ok"
    assert response.results[1]["url"] == "http://127.0.0.1/private"
    assert response.results[1]["layer_used"] == "rejected"
    assert response.results[1]["best_attempt"] is None
    assert fake.seen == ["https://example.com/ok"]
    assert fake.memory.lookups == ["https://example.com/ok"]


@pytest.mark.asyncio
async def test_memory_admin_is_disabled_without_key_and_omits_cookie_values(monkeypatch):
    monkeypatch.delenv("YADA_API_KEY", raising=False)
    monkeypatch.delenv("YADA_ADMIN_API_KEY", raising=False)
    with pytest.raises(HTTPException) as disabled:
        await api_mod.get_memory(x_api_key=None)
    assert disabled.value.status_code == 404

    monkeypatch.setenv("YADA_ADMIN_API_KEY", "admin-secret")
    profile = {
        "example.com": {
            "domain": "example.com",
            "cookies": [
                {"name": "session", "value": "top-secret", "domain": "example.com"}
            ],
        }
    }
    fake_engine = SimpleNamespace(
        memory=SimpleNamespace(get_all_profiles=lambda: profile)
    )
    original = api_mod.engine
    monkeypatch.setattr(api_mod, "engine", fake_engine)
    try:
        response = await api_mod.get_memory(x_api_key="admin-secret")
    finally:
        api_mod.engine = original

    returned_profile = response["profiles"][0]
    assert returned_profile["cookie_count"] == 1
    assert "cookies" not in returned_profile
    assert "top-secret" not in str(response)
    assert profile["example.com"]["cookies"][0]["value"] == "top-secret"


@pytest.mark.asyncio
async def test_memory_admin_works_with_real_site_memory(monkeypatch, tmp_path):
    monkeypatch.setenv("YADA_ADMIN_API_KEY", "admin-secret")
    memory = SiteMemory(str(tmp_path / "memory.json"))
    await memory.save_cookies(
        "https://example.com/page",
        [{"name": "session", "value": "top-secret", "domain": "example.com"}],
    )
    monkeypatch.setattr(api_mod, "engine", SimpleNamespace(memory=memory))

    response = await api_mod.get_memory(x_api_key="admin-secret")

    assert response["total_domains"] == 1
    assert response["profiles"][0]["domain"] == "example.com"
    assert response["profiles"][0]["cookie_count"] == 1
    assert "cookies" not in response["profiles"][0]
    assert "top-secret" not in str(response)


def test_required_api_key_config_fails_closed(monkeypatch):
    monkeypatch.setenv("YADA_REQUIRE_API_KEY", "1")
    monkeypatch.delenv("YADA_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="YADA_API_KEY is not set"):
        api_mod._validate_runtime_security_config()

    monkeypatch.setenv("YADA_API_KEY", "configured")
    api_mod._validate_runtime_security_config()


def test_required_api_key_also_fails_closed_at_request_time(monkeypatch):
    monkeypatch.setenv("YADA_REQUIRE_API_KEY", "1")
    monkeypatch.delenv("YADA_API_KEY", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        api_mod._enforce_api_key(None)

    assert exc_info.value.status_code == 503


def test_non_ascii_api_key_is_rejected_without_compare_digest_error(monkeypatch):
    monkeypatch.setenv("YADA_API_KEY", "configured")

    with pytest.raises(HTTPException) as exc_info:
        api_mod._enforce_api_key("é")

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_bulk_validation_is_bounded_and_openapi_declares_limit():
    with pytest.raises(ValidationError) as exc_info:
        api_mod.BulkRequest(urls=["https://example.com"] * 101)

    response = await api_mod._bounded_validation_error(
        None,
        RequestValidationError(exc_info.value.errors()),
    )
    assert response.status_code == 422
    assert len(response.body) < 2000
    assert b'"input"' not in response.body

    api_mod.app.openapi_schema = None
    schema = api_mod.app.openapi()
    urls_schema = schema["components"]["schemas"]["BulkRequest"]["properties"]["urls"]
    assert urls_schema["maxItems"] == 100
    assert schema["components"]["securitySchemes"]["ApiKeyAuth"]["name"] == "X-API-Key"
    assert schema["paths"]["/scrape"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/ScrapeResponse")


@pytest.mark.asyncio
async def test_explicit_bulk_cancellation_stops_child_scrapes(monkeypatch):
    started = 0
    completed = 0
    cancelled = 0
    all_started = asyncio.Event()

    class FakeEngine:
        memory = None

        async def scrape(self, url, mode="auto", allow_layer5=True, solve_challenges=False):
            nonlocal started, completed, cancelled
            started += 1
            if started == 2:
                all_started.set()
            try:
                await asyncio.sleep(10)
                completed += 1
            except asyncio.CancelledError:
                cancelled += 1
                raise

    async def allow_url(url):
        return url

    monkeypatch.delenv("YADA_API_KEY", raising=False)
    monkeypatch.delenv("YADA_REQUIRE_API_KEY", raising=False)
    monkeypatch.setattr(api_mod, "engine", FakeEngine())
    monkeypatch.setattr(api_mod, "normalize_public_url", allow_url)

    endpoint_task = asyncio.create_task(
        api_mod.do_bulk(
            api_mod.BulkRequest(
                urls=["https://one.example", "https://two.example"],
                mode=api_mod.ScrapeMode.layer1,
                max_concurrency=2,
            ),
            x_api_key=None,
        )
    )
    await asyncio.wait_for(all_started.wait(), timeout=1)
    endpoint_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await endpoint_task
    await asyncio.sleep(0)

    assert started == 2
    assert cancelled == 2
    assert completed == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "http://224.0.0.1/",
        "http://239.255.255.250/",
        "http://[ff02::1]/",
    ],
)
async def test_url_guard_rejects_multicast_literals(target):
    with pytest.raises(url_safety.UnsafeUrlError, match="Non-public"):
        await url_safety.normalize_public_url(target)


@pytest.mark.asyncio
async def test_url_guard_rejects_multicast_dns_answer(monkeypatch):
    async def multicast_dns(host, port):
        return {"239.255.255.250"}

    monkeypatch.setattr(url_safety, "_resolve_addresses", multicast_dns)
    with pytest.raises(url_safety.UnsafeUrlError, match="non-public"):
        await url_safety.normalize_public_url("https://multicast.example/")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://exam\u202eple.com/",      # BiDi override
        "https://example..com/",            # empty label
        "https://" + "a" * 64 + ".com/",    # label > 63 chars
        "http://[::1/",                     # urlsplit ValueError
        "https://example.com:99999/",       # .port ValueError
        "http://loc\u200balhost/",          # invisible character
    ],
)
async def test_malformed_hosts_rejected(url, monkeypatch):
    monkeypatch.setattr(url_safety, "_resolve_addresses", _real_resolve_addresses)
    with pytest.raises(url_safety.UnsafeUrlError):
        await url_safety.normalize_public_url(url)

