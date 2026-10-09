import pytest

from scraper import url_safety

collect_ignore_glob = ["* (1)*"]


@pytest.fixture(autouse=True)
def deterministic_public_dns(monkeypatch):
    """Keep contract tests offline; individual safety tests override as needed."""

    async def public_test_resolution(host, port):
        return {"93.184.216.34"}

    monkeypatch.setattr(url_safety, "_resolve_addresses", public_test_resolution)
