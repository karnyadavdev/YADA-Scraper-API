import time

import pytest

import yada_scraper as api_mod
from scraper import engine as engine_mod


async def _run_limited_request(headers, messages):
    downstream = {"called": False, "body": None}
    sent = []
    pending = list(messages)

    async def receive():
        if pending:
            return pending.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    async def endpoint(scope, receive_body, send_response):
        downstream["called"] = True
        request = await receive_body()
        downstream["body"] = request.get("body", b"")
        await send_response({"type": "http.response.start", "status": 204, "headers": []})
        await send_response({"type": "http.response.body", "body": b""})

    middleware = api_mod.RequestBodyLimitMiddleware(endpoint)
    await middleware(
        {"type": "http", "method": "POST", "path": "/scrape/bulk", "headers": headers},
        receive,
        send,
    )
    return downstream, sent


@pytest.mark.asyncio
async def test_request_limit_rejects_oversized_content_length_before_endpoint(monkeypatch):
    monkeypatch.setenv("YADA_MAX_REQUEST_BYTES", "10")
    downstream, sent = await _run_limited_request(
        [(b"content-length", b"11")],
        [{"type": "http.request", "body": b"not-read", "more_body": False}],
    )

    assert downstream["called"] is False
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_request_limit_counts_chunked_body_and_replays_allowed_body(monkeypatch):
    monkeypatch.setenv("YADA_MAX_REQUEST_BYTES", "10")
    rejected, rejected_sent = await _run_limited_request(
        [],
        [
            {"type": "http.request", "body": b"123456", "more_body": True},
            {"type": "http.request", "body": b"78901", "more_body": False},
        ],
    )
    allowed, allowed_sent = await _run_limited_request(
        [],
        [
            {"type": "http.request", "body": b"12345", "more_body": True},
            {"type": "http.request", "body": b"67890", "more_body": False},
        ],
    )

    assert rejected["called"] is False
    assert rejected_sent[0]["status"] == 413
    assert allowed["called"] is True
    assert allowed["body"] == b"1234567890"
    assert allowed_sent[0]["status"] == 204


def test_pack_result_truncates_at_utf8_boundary_with_metadata(monkeypatch):
    monkeypatch.setenv("YADA_MAX_CONTENT_BYTES", "5")
    engine = engine_mod.ScraperEngine()

    result = engine._pack_result(
        "https://example.com/",
        "ééé",
        "source_test",
        time.time(),
        [],
    )
    packed = result.to_dict()

    assert packed["content"]["html"] == "éé"
    assert len(packed["content"]["html"].encode("utf-8")) <= 5
    assert packed["content_truncated"] is True
    assert packed["content_original_bytes"] == 6
    assert packed["content_returned_bytes"] == 4
    assert packed["content_limit_bytes"] == 5
    assert "content_truncated" in packed["diagnostics"]
    assert packed["best_attempt"]["content_truncated"] is True
    assert packed["html_length"] == 2


def test_best_capture_path_applies_same_content_limit(monkeypatch):
    monkeypatch.setenv("YADA_MAX_CONTENT_BYTES", "7")
    engine = engine_mod.ScraperEngine()
    capture = {
        "html": "abc😀def",
        "score": 1,
        "layer": "layer_test",
        "diagnostics": [],
        "status_code": 200,
        "final_url": "https://example.com/final",
    }

    result = engine._pack_best_capture(
        "https://example.com/",
        [capture],
        time.time(),
        [],
        {},
    ).to_dict()

    assert result["content"]["html"] == "abc😀"
    assert len(result["content"]["html"].encode("utf-8")) == 7
    assert result["content_original_bytes"] == 10
    assert result["content_returned_bytes"] == 7
    assert result["best_attempt"]["html_length"] == 4
    assert "content_truncated" in result["diagnostics"]


def test_content_bytes_env_is_clamped_to_a_safe_upper_bound(monkeypatch):
    monkeypatch.setenv("YADA_MAX_CONTENT_BYTES", "999999999999")
    assert engine_mod._max_content_bytes() == 64 * 1024 * 1024

    monkeypatch.setenv("YADA_MAX_CONTENT_BYTES", str(8 * 1024 * 1024))
    assert engine_mod._max_content_bytes() == 8 * 1024 * 1024
