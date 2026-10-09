import asyncio
import copy
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

DEFAULT_SAVE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scratch",
    "site_memory.json",
)

_env_path = os.getenv("YADA_MEMORY_PATH", "").strip()
if _env_path:
    DEFAULT_SAVE_PATH = _env_path


@dataclass
class SiteInfo:
    domain: str
    best_layer: Optional[str] = None
    failed_layers: list = field(default_factory=list)
    layer_fail_counts: dict = field(default_factory=dict)
    success_count: int = 0
    fail_count: int = 0
    avg_response_time: float = 0.0
    last_scrape_time: float = 0.0
    captcha_detected: Optional[str] = None
    notes: str = ""
    cookies: list = field(default_factory=list)
    cookies_last_updated: float = 0.0

    def to_dict(self):
        return {
            "domain": self.domain,
            "best_layer": self.best_layer,
            "failed_layers": list(self.failed_layers),
            "layer_fail_counts": dict(self.layer_fail_counts),
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "avg_response_time": round(self.avg_response_time, 3),
            "last_scrape_time": self.last_scrape_time,
            "captcha_detected": self.captcha_detected,
            "notes": self.notes,
            "cookies": copy.deepcopy(self.cookies),
            "cookies_last_updated": self.cookies_last_updated,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            domain=data.get("domain", ""),
            best_layer=data.get("best_layer"),
            failed_layers=data.get("failed_layers", []),
            layer_fail_counts=data.get("layer_fail_counts", {}),
            success_count=data.get("success_count", 0),
            fail_count=data.get("fail_count", 0),
            avg_response_time=data.get("avg_response_time", 0.0),
            last_scrape_time=data.get("last_scrape_time", 0.0),
            captcha_detected=data.get("captcha_detected"),
            notes=data.get("notes", ""),
            cookies=data.get("cookies", []),
            cookies_last_updated=data.get("cookies_last_updated", 0.0),
        )


class _ThreadSafeMemoryLock:
    """An async and sync reentrant lock backed by threading.RLock, allowing
    safe synchronization across any OS thread and any event loop without binding
    to a single asyncio loop or raising RuntimeError: bound to a different event loop."""
    def __init__(self):
        self._lock = threading.RLock()

    async def __aenter__(self):
        self._lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._lock.release()

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._lock.release()

    def acquire(self, blocking=True, timeout=-1):
        return self._lock.acquire(blocking, timeout)

    def release(self):
        return self._lock.release()


class _CrossThreadAsyncLock:
    """An async lock backed by threading.Lock that acquires via the current loop's
    executor, allowing coroutines across distinct OS worker threads and event loops
    to serialize I/O without blocking the event loops or raising cross-loop errors."""
    def __init__(self):
        self._lock = threading.Lock()

    async def __aenter__(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._lock.acquire()
            return self
        # Poll instead of parking a default-executor thread in acquire(): a
        # waiter occupying an executor thread while the lock holder waits for
        # another executor thread (asyncio.to_thread) can deadlock the pool.
        # Polling also makes cancellation safe: a cancelled waiter never holds
        # the lock, whereas a cancelled run_in_executor(acquire) leaks it.
        delay = 0.001
        while not self._lock.acquire(blocking=False):
            await asyncio.sleep(delay)
            delay = min(delay * 2, 0.025)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._lock.release()

    def acquire(self, blocking=True, timeout=-1):
        return self._lock.acquire(blocking, timeout)

    def release(self):
        return self._lock.release()


class SiteMemory:
    EXPIRY = int(os.getenv("YADA_MEMORY_EXPIRY", "3600"))
    FAILURES_BEFORE_BLACKLIST = int(os.getenv("YADA_FAILURES_BEFORE_BLACKLIST", "3"))

    def __init__(self, memory_path=DEFAULT_SAVE_PATH):
        self.memory_path = memory_path
        self.profiles = {}
        self.lock = _ThreadSafeMemoryLock()
        self._io_lock = _CrossThreadAsyncLock()
        self.defer_writes = False
        self._defer_count = 0
        self._dirty = False
        self.last_save_error = ""
        self._load()

    def get_failed_layers(self, url):
        domain = self._get_domain(url)
        info = self._get_info(domain)

        if not info:
            return []

        age = time.time() - info.last_scrape_time
        if age > self.EXPIRY:
            info.failed_layers = []
            if hasattr(info, "layer_fail_counts"):
                info.layer_fail_counts = {}
            return []

        failed_layers = list(info.failed_layers)
        if self.get_valid_cookies(url) and "layer2_cookie" in failed_layers:
            failed_layers.remove("layer2_cookie")

        failed_layers = [
            layer for layer in failed_layers
            if not layer.startswith("source_")
        ]

        return failed_layers


    def _record_success_internal(self, domain, layer, response_time, captcha=None):
        info = self.profiles.get(domain, SiteInfo(domain=domain))
        info.best_layer = layer
        if layer in info.failed_layers:
            info.failed_layers.remove(layer)
        if hasattr(info, "layer_fail_counts") and layer in info.layer_fail_counts:
            info.layer_fail_counts[layer] = 0
        info.success_count += 1
        info.last_scrape_time = time.time()

        if info.avg_response_time == 0:
            info.avg_response_time = response_time
        else:
            info.avg_response_time = (
                info.avg_response_time * 0.7 + response_time * 0.3
            )

        if captcha:
            info.captcha_detected = captcha

        self.profiles[domain] = info

    def _record_failure_internal(self, domain, layer, reason="", captcha=None, threshold=None):
        info = self.profiles.get(domain, SiteInfo(domain=domain))

        now = time.time()
        if now - info.last_scrape_time > self.EXPIRY:
            info.last_scrape_time = now
            info.failed_layers = []
            info.layer_fail_counts = {}
        elif info.last_scrape_time == 0:
            info.last_scrape_time = now

        cookie_request_failed = layer == "layer2_cookie" and bool(info.cookies)
        if cookie_request_failed:
            info.notes = "Layer 2 failed with existing cookies; cookies may need refresh."

        if not hasattr(info, "layer_fail_counts") or info.layer_fail_counts is None:
            info.layer_fail_counts = {}

        current_strikes = info.layer_fail_counts.get(layer, 0) + 1
        info.layer_fail_counts[layer] = current_strikes

        required_strikes = threshold if threshold is not None else self.FAILURES_BEFORE_BLACKLIST
        if current_strikes >= required_strikes:
            if layer not in info.failed_layers:
                info.failed_layers.append(layer)

        info.fail_count += 1
        if not cookie_request_failed and reason:
            info.notes = reason

        if captcha:
            info.captcha_detected = captcha

        self.profiles[domain] = info

    async def record_success(self, url, layer, response_time, captcha=None):
        domain = self._get_domain(url)
        async with self.lock:
            self._record_success_internal(domain, layer, response_time, captcha)
        await self._save_async()

    async def record_failure(self, url, layer, reason="", captcha=None, threshold=None):
        if self._is_transient_failure(status_code=0, reason=reason):
            return

        domain = self._get_domain(url)
        async with self.lock:
            self._record_failure_internal(domain, layer, reason, captcha, threshold=threshold)
        await self._save_async()

    async def record_attempt(self, url, layer_attempts, success_layer=None):
        domain = self._get_domain(url)
        async with self.lock:
            for attempt in layer_attempts:
                layer = attempt.get("layer", "")
                ok = attempt.get("success", False)
                t = attempt.get("time", 0.0)
                captcha = attempt.get("captcha_detected")
                status_code = attempt.get("status_code", 0)
                reason = attempt.get("reason", "")

                if self._is_transient_failure(status_code, reason):
                    continue

                if ok and layer == success_layer:
                    self._record_success_internal(domain, layer, t, captcha)
                elif not ok:
                    self._record_failure_internal(domain, layer, reason, captcha)

        await self._save_async()


    def get_valid_cookies(self, url):
        domain = self._get_domain(url)
        info = self._get_info(domain)
        if not info or not info.cookies:
            return []
        now = time.time()
        valid = []
        for c in info.cookies:
            expires = c.get("expires", -1)
            if isinstance(expires, (int, float)) and 0 < expires < now:
                continue
            valid.append(c)
        return valid

    def get_profile(self, url):
        domain = self._get_domain(url)
        info = self._get_info(domain)
        return info.to_dict() if info else None

    def get_all_profiles(self):
        return [p.to_dict() for p in self.profiles.values()]

    async def clear_cookies(self, url):
        domain = self._get_domain(url)
        async with self.lock:
            info = self.profiles.get(domain)
            if info:
                info.cookies = []
                info.cookies_last_updated = 0.0
                self.profiles[domain] = info
        await self._save_async()

    async def save_cookies(self, url, cookies):
        domain = self._get_domain(url)
        async with self.lock:
            info = self.profiles.get(domain)
            if not info:
                info = SiteInfo(domain=domain)
            info.cookies = cookies
            info.cookies_last_updated = time.time()
            if "layer2_cookie" in info.failed_layers:
                info.failed_layers.remove("layer2_cookie")
            if hasattr(info, "layer_fail_counts") and "layer2_cookie" in info.layer_fail_counts:
                info.layer_fail_counts["layer2_cookie"] = 0
            self.profiles[domain] = info
        await self._save_async()

    async def clear(self):
        async with self.lock:
            self.profiles = {}
            if self.defer_writes or self._defer_count > 0:
                self._dirty = True
                return
        await self._execute_write({}, is_clear=True)

    def _load(self):
        if os.path.exists(self.memory_path):
            try:
                with open(self.memory_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.profiles = {
                    domain: SiteInfo.from_dict(profile_data)
                    for domain, profile_data in data.items()
                }
            except (json.JSONDecodeError, AttributeError, KeyError, TypeError, OSError, PermissionError) as e:
                # Graceful startup recovery
                print(f"[WARNING] Failed to load site memory file: {e}. Starting with empty memory.")
                self.profiles = {}

    async def _execute_write(self, data, is_clear=False):
        ok = False
        try:
            async with self._io_lock:
                try:
                    merged = await asyncio.to_thread(self._write_file, data, is_clear)
                except TypeError:
                    merged = await asyncio.to_thread(self._write_file, data)
            ok = True
            self._dirty = False
            self.last_save_error = None
            if isinstance(merged, dict):
                for domain, pdata in merged.items():
                    if domain not in self.profiles:
                        self.profiles[domain] = SiteInfo.from_dict(pdata)
                    elif isinstance(pdata, dict):
                        curr = self.profiles[domain]
                        d_cookie_ts = float(pdata.get("cookies_last_updated", 0.0) or 0.0)
                        if d_cookie_ts > curr.cookies_last_updated:
                            curr.cookies = pdata.get("cookies", [])
                            curr.cookies_last_updated = d_cookie_ts
                        d_scrape_ts = float(pdata.get("last_scrape_time", 0.0) or 0.0)
                        if d_scrape_ts > curr.last_scrape_time:
                            curr.last_scrape_time = d_scrape_ts
                            curr.best_layer = pdata.get("best_layer")
                            curr.failed_layers = pdata.get("failed_layers", [])
                            curr.avg_response_time = float(pdata.get("avg_response_time", 0.0) or 0.0)
        except Exception as exc:
            self.last_save_error = f"{type(exc).__name__}: {exc}"
        finally:
            if not ok:
                # Restores _dirty synchronously on failure or task cancellation (CancelledError)
                self._dirty = True

    async def _save_async(self):
        async with self.lock:
            if self.defer_writes or self._defer_count > 0:
                self._dirty = True
                return
            data = {
                domain: info.to_dict()
                for domain, info in self.profiles.items()
            }
        await self._execute_write(data)

    async def begin_deferred_writes(self):
        async with self.lock:
            self._defer_count += 1

    async def end_deferred_writes(self):
        async with self.lock:
            self._defer_count = max(0, self._defer_count - 1)
            should_flush = self._defer_count == 0 and not self.defer_writes and self._dirty
        if should_flush:
            await self.flush()

    async def flush(self):
        # Invariants:
        # 1. Snapshot taken synchronously under self.lock without any await, so no other coroutine can interleave.
        #    Because to_dict() returns isolated structures (enforced by the poke test), data has
        #    zero live references into self.profiles once self.lock is released.
        # 2. _dirty is cleared in the same critical section as the snapshot.
        # 3. No await sits between releasing self.lock and queueing on _io_lock.
        #    With asyncio's FIFO locks, writes land in snapshot order, so an older snapshot
        #    cannot overwrite a newer one.
        # 4. json.dump() runs in the worker thread inside _write_file, so CPU-bound serialization
        #    does not block the asyncio event loop.
        async with self.lock:
            if self.defer_writes or self._defer_count > 0 or not self._dirty:
                return
            self._dirty = False
            data = {
                domain: info.to_dict()
                for domain, info in self.profiles.items()
            }
        await self._execute_write(data)

    def persistence_status(self):
        return {
            "path": self.memory_path,
            "dirty": self._dirty,
            "deferred_requests": self._defer_count,
            "last_error": self.last_save_error or None,
        }

    def _write_file(self, data, is_clear=False):
        target_dir = os.path.dirname(os.path.abspath(self.memory_path))
        os.makedirs(target_dir, exist_ok=True)

        merged_data = dict(data) if isinstance(data, dict) else {}
        if not is_clear and isinstance(data, dict) and os.path.exists(self.memory_path):
            disk_data = None
            for _ in range(3):
                try:
                    with open(self.memory_path, "r", encoding="utf-8") as f:
                        disk_data = json.load(f)
                    break
                except (PermissionError, FileNotFoundError):
                    time.sleep(0.02)
                except Exception:
                    break

            if isinstance(disk_data, dict):
                for domain, disk_profile in disk_data.items():
                    if not isinstance(disk_profile, dict):
                        continue
                    if domain not in merged_data:
                        merged_data[domain] = disk_profile
                    else:
                        curr_profile = merged_data[domain]
                        if not isinstance(curr_profile, dict):
                            continue
                        
                        disk_cookie_ts = float(disk_profile.get("cookies_last_updated", 0.0) or 0.0)
                        curr_cookie_ts = float(curr_profile.get("cookies_last_updated", 0.0) or 0.0)
                        if disk_cookie_ts > curr_cookie_ts:
                            curr_profile["cookies"] = disk_profile.get("cookies", [])
                            curr_profile["cookies_last_updated"] = disk_cookie_ts

                        disk_scrape_ts = float(disk_profile.get("last_scrape_time", 0.0) or 0.0)
                        curr_scrape_ts = float(curr_profile.get("last_scrape_time", 0.0) or 0.0)
                        if disk_scrape_ts > curr_scrape_ts:
                            curr_profile["last_scrape_time"] = disk_scrape_ts
                            curr_profile["best_layer"] = disk_profile.get("best_layer")
                            curr_profile["failed_layers"] = disk_profile.get("failed_layers", [])
                            curr_profile["avg_response_time"] = float(disk_profile.get("avg_response_time", 0.0) or 0.0)
                            curr_profile["notes"] = disk_profile.get("notes", "")
                            curr_profile["captcha_detected"] = disk_profile.get("captcha_detected")
                            curr_profile["layer_fail_counts"] = disk_profile.get("layer_fail_counts", {})

                        curr_profile["success_count"] = max(
                            int(curr_profile.get("success_count", 0) or 0),
                            int(disk_profile.get("success_count", 0) or 0),
                        )
                        curr_profile["fail_count"] = max(
                            int(curr_profile.get("fail_count", 0) or 0),
                            int(disk_profile.get("fail_count", 0) or 0),
                        )

        payload_to_write = merged_data if isinstance(data, dict) else data

        fd, tmp_path = tempfile.mkstemp(
            prefix=".site_memory.",
            suffix=".tmp",
            dir=target_dir,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                if isinstance(payload_to_write, str):
                    f.write(payload_to_write)
                else:
                    json.dump(payload_to_write, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(4):
                try:
                    os.replace(tmp_path, self.memory_path)
                    break
                except PermissionError:
                    if attempt == 3 or os.name != "nt":
                        raise
                    time.sleep(0.05 * (2 ** attempt))
            if os.name == "posix":
                os.chmod(self.memory_path, 0o600)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

        return merged_data

    @staticmethod
    def _get_domain(url):
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        # Cookies and learned strategy are host-scoped, not port-scoped. Keep
        # explicit :80/:443 URLs in the same profile as their canonical form.
        domain = (urlparse(url).hostname or "").lower().rstrip(".")
        if domain.startswith("www."):
            domain = domain[4:]
        return domain

    def _get_info(self, domain):
        return self.profiles.get(domain) or self.profiles.get(f"www.{domain}")

    @staticmethod
    def _is_transient_failure(status_code, reason=""):
        if status_code in (404, 410):
            return True

        if status_code in (500, 502, 504) or 520 <= status_code <= 524:
            return True

        if status_code == 0:
            reason_l = (reason or "").lower()
            transient_markers = (
                "timeout",
                "sslerror",
                "connectionerror",
                "httperror",
                "network",
                "connect",
                "reset",
                "temporary",
            )
            return any(marker in reason_l for marker in transient_markers)

        return False

