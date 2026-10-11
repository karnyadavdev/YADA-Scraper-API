import asyncio
import json
import os
import time

import pytest

from scraper.site_memory import SiteMemory, _CrossThreadAsyncLock


@pytest.mark.asyncio
async def test_overlapping_deferred_bulk_scopes_flush_only_after_last_exit(tmp_path):
    path = tmp_path / "memory.json"
    memory = SiteMemory(str(path))

    await memory.begin_deferred_writes()
    await memory.record_success("https://one.example/page", "layer1_simple", 0.1)

    await memory.begin_deferred_writes()
    await memory.record_success("https://two.example/page", "layer2_cookie", 0.2)

    await memory.end_deferred_writes()
    assert memory.persistence_status()["deferred_requests"] == 1
    assert not path.exists()

    await memory.end_deferred_writes()
    assert memory.persistence_status()["deferred_requests"] == 0
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert set(saved) == {"one.example", "two.example"}


@pytest.mark.asyncio
async def test_success_clears_prior_failed_layer_and_persists(tmp_path):
    path = tmp_path / "memory.json"
    memory = SiteMemory(str(path))
    url = "https://example.com/page"

    # 1st and 2nd failure do not blacklist yet under the 3-strikes rule
    await memory.record_failure(url, "layer1_simple", "HTTP 403 blocked")
    assert "layer1_simple" not in memory.get_failed_layers(url)
    await memory.record_failure(url, "layer1_simple", "HTTP 403 blocked")
    assert "layer1_simple" not in memory.get_failed_layers(url)

    # 3rd consecutive failure enters failed_layers
    await memory.record_failure(url, "layer1_simple", "HTTP 403 blocked")
    assert "layer1_simple" in memory.get_failed_layers(url)

    await memory.record_success(url, "layer1_simple", 0.1)
    assert "layer1_simple" not in memory.get_failed_layers(url)

    reloaded = SiteMemory(str(path))
    assert "layer1_simple" not in reloaded.get_failed_layers(url)


@pytest.mark.asyncio
async def test_deferred_scope_survives_interleaved_task_exit_order(tmp_path):
    path = tmp_path / "memory.json"
    memory = SiteMemory(str(path))
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    allow_first_exit = asyncio.Event()
    first_exited = asyncio.Event()

    async def first():
        await memory.begin_deferred_writes()
        first_entered.set()
        await second_entered.wait()
        await memory.record_success("https://first.test", "layer1_simple", 0.1)
        await allow_first_exit.wait()
        await memory.end_deferred_writes()
        first_exited.set()

    async def second():
        await first_entered.wait()
        await memory.begin_deferred_writes()
        second_entered.set()
        await memory.record_success("https://second.test", "layer2_cookie", 0.1)
        allow_first_exit.set()
        await first_exited.wait()
        assert memory.persistence_status()["deferred_requests"] == 1
        assert not path.exists()
        await memory.end_deferred_writes()

    await asyncio.gather(first(), second())
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert set(saved) == {"first.test", "second.test"}


@pytest.mark.asyncio
async def test_save_failure_remains_dirty_and_later_success_clears_error(monkeypatch, tmp_path):
    path = tmp_path / "memory.json"
    memory = SiteMemory(str(path))
    real_write = memory._write_file

    def fail_write(data, is_clear=False):
        raise PermissionError("read-only target")

    monkeypatch.setattr(memory, "_write_file", fail_write)
    await memory.record_success("https://example.com/one", "layer1_simple", 0.1)
    assert memory.persistence_status()["dirty"]
    assert "PermissionError" in memory.persistence_status()["last_error"]

    monkeypatch.setattr(memory, "_write_file", real_write)
    await memory.record_success("https://example.com/two", "layer1_simple", 0.1)
    assert not memory.persistence_status()["dirty"]
    assert memory.persistence_status()["last_error"] is None
    assert path.exists()


def test_site_info_to_dict_returns_detached_copies():
    from scraper.site_memory import SiteInfo

    info = SiteInfo(
        domain="example.com",
        failed_layers=["layer1_simple"],
        cookies=[{"name": "sid", "value": "123"}],
    )
    serialized = info.to_dict()

    # Verify mutating serialized collections does not mutate internal SiteInfo state
    serialized["failed_layers"].append("layer2_cookie")
    serialized["cookies"].append({"name": "extra", "value": "456"})
    serialized["cookies"][0]["value"] = "tampered"

    assert info.failed_layers == ["layer1_simple"]
    assert len(info.cookies) == 1
    assert info.cookies[0]["value"] == "123"


@pytest.mark.asyncio
async def test_concurrent_save_and_flush_serialized_under_io_lock(tmp_path):
    path = tmp_path / "memory.json"
    memory = SiteMemory(str(path))
    concurrent_writes = 0
    max_concurrent_writes = 0

    real_write = memory._write_file

    def monitored_write(data, is_clear=False):
        nonlocal concurrent_writes, max_concurrent_writes
        concurrent_writes += 1
        max_concurrent_writes = max(max_concurrent_writes, concurrent_writes)
        try:
            time.sleep(0.02)
            real_write(data, is_clear)
        finally:
            concurrent_writes -= 1

    memory._write_file = monitored_write

    # Fire concurrent save_cookies and flush operations
    await asyncio.gather(
        memory.save_cookies("https://site-a.com", [{"name": "a", "value": "1"}]),
        memory.save_cookies("https://site-b.com", [{"name": "b", "value": "2"}]),
        memory.record_attempt("https://site-c.com", [{"layer": "l1", "success": True}], success_layer="l1"),
        memory.flush(),
    )

    assert max_concurrent_writes == 1
    assert memory.persistence_status()["dirty"] is False
    assert memory.persistence_status()["last_error"] is None
    assert path.exists()


@pytest.mark.asyncio
async def test_flush_error_does_not_deadlock_with_concurrent_mutations(tmp_path):
    path = tmp_path / "memory.json"
    memory = SiteMemory(str(path))

    def failing_write(data, is_clear=False):
        raise PermissionError("Simulated write failure")

    memory._write_file = failing_write
    await memory.save_cookies("https://example.com", [{"name": "s", "value": "1"}])

    done, pending = await asyncio.wait([
        asyncio.create_task(memory.flush()),
        asyncio.create_task(memory.record_success("https://example.com/2", "layer1_simple", 0.1)),
    ], timeout=2.0)

    assert len(pending) == 0, "Tasks deadlocked during flush write error!"
    assert memory.persistence_status()["dirty"] is True
    assert "PermissionError" in memory.persistence_status()["last_error"]


@pytest.mark.asyncio
async def test_save_cookies_copies_caller_list(tmp_path):
    path = tmp_path / "memory.json"
    memory = SiteMemory(str(path))
    cookies = [{"name": "sid", "value": "1"}]

    await memory.save_cookies("https://example.com", cookies)

    cookies.append({"name": "injected", "value": "2"})
    cookies[0]["value"] = "tampered"

    stored = memory.get_valid_cookies("https://example.com")
    assert stored == [{"name": "sid", "value": "1"}]


@pytest.mark.asyncio
async def test_second_instance_does_not_clobber(tmp_path):
    path = str(tmp_path / "site_memory.json")
    a, b = SiteMemory(path), SiteMemory(path)
    await a.save_cookies("https://example.com", [{"name": "a", "value": "1"}])
    await b.save_cookies("https://www.reddit.com", [{"name": "b", "value": "2"}])
    c = SiteMemory(path)
    assert "example.com" in c.profiles
    assert "reddit.com" in c.profiles


@pytest.mark.asyncio
async def test_stale_instance_does_not_overwrite_updated_domain(tmp_path):
    path = str(tmp_path / "site_memory.json")
    await SiteMemory(path).save_cookies("https://example.com", [{"name": "old", "value": "1"}])
    a, b = SiteMemory(path), SiteMemory(path)   # both load example.com
    await a.save_cookies("https://example.com", [{"name": "new", "value": "2"}])
    await b.save_cookies("https://www.reddit.com", [{"name": "r", "value": "3"}])
    c = SiteMemory(path)
    assert "new" in json.dumps(c.profiles["example.com"].to_dict())


def test_to_dict_isolated_from_live_state():
    from scraper.site_memory import SiteInfo

    def _poke(obj):
        """Mutate every container reachable from obj, in place."""
        if isinstance(obj, dict):
            for v in list(obj.values()):
                _poke(v)
            obj["__poked__"] = True
        elif isinstance(obj, list):
            for v in list(obj):
                _poke(v)
            obj.append("__poked__")

    fully_populated = SiteInfo(
        domain="example.com",
        best_layer="layer2_cookie",
        failed_layers=["layer1_simple"],
        success_count=5,
        fail_count=2,
        avg_response_time=0.456,
        last_scrape_time=1700000000.0,
        captcha_detected="cloudflare_turnstile",
        notes="special profile",
        cookies=[{"name": "session", "value": "xyz", "meta": {"nested": 123}}],
        cookies_last_updated=1700000000.0,
    )

    snap = fully_populated.to_dict()
    frozen = json.loads(json.dumps(snap))

    from dataclasses import fields
    assert set(vars(fully_populated).keys()) == {f.name for f in fields(SiteInfo)}, "Fixture missed a field on SiteInfo!"
    assert len(fully_populated.failed_layers) > 0, "Collection field failed_layers must have elements to prove isolation!"
    assert len(fully_populated.cookies) > 0 and isinstance(fully_populated.cookies[0], dict), "Collection field cookies must have nested dicts to prove isolation!"

    for v in vars(fully_populated).values():
        _poke(v)

    assert snap == frozen


@pytest.mark.asyncio
async def test_flush_cancellation_restores_dirty(tmp_path):
    path = tmp_path / "memory.json"
    memory = SiteMemory(str(path))
    await memory.save_cookies("https://example.com", [{"name": "sid", "value": "1"}])
    assert memory.persistence_status()["dirty"] is False

    # Mark dirty for flush
    memory._dirty = True

    # Block inside _write_file using an Event so we can cancel flush while in-flight
    write_entered = asyncio.Event()

    def blocked_write(data, is_clear=False):
        write_entered.set()
        # busy wait/sleep briefly until released
        time.sleep(0.05)

    memory._write_file = blocked_write

    flush_task = asyncio.create_task(memory.flush())
    await asyncio.sleep(0.01)
    flush_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await flush_task

    # Crucial assertion: cancellation must restore _dirty to True in finally block
    assert memory.persistence_status()["dirty"] is True


def test_mkstemp_target_dir_on_same_volume(tmp_path, monkeypatch):
    target_file = tmp_path / "subdir" / "memory.json"
    memory = SiteMemory(str(target_file))

    observed_src = None
    observed_dst = None

    orig_replace = os.replace

    def capture_replace(src, dst):
        nonlocal observed_src, observed_dst
        observed_src = src
        observed_dst = dst
        orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", capture_replace)
    memory._write_file("{}")

    assert observed_src is not None
    assert os.path.dirname(os.path.abspath(observed_src)) == os.path.dirname(os.path.abspath(observed_dst))


@pytest.mark.asyncio
async def test_cancelled_io_lock_waiter_does_not_leak_the_lock():
    lock = _CrossThreadAsyncLock()

    async def _waiter():
        async with lock:
            pass

    async with lock:
        task = asyncio.create_task(_waiter())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # On the old executor-based waiter, the cancelled task's background thread
    # still holds a blocked acquire() that hijacks the lock the moment it is
    # released, so this final acquire must succeed, proving no leak.
    await asyncio.sleep(0.05)
    assert lock._lock.acquire(timeout=0.2), "lock was leaked by a cancelled waiter"
    lock._lock.release()



