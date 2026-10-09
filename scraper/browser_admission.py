"""Process-wide admission control for browser-capable engine operations."""

import asyncio
import os
import threading
import time
from contextlib import asynccontextmanager


# low default on purpose: browser jobs are the memory hogs, and the
# fallback chain rarely needs more than a pair in flight
DEFAULT_BROWSER_CONCURRENCY = 2
ENV_NAME = "YADA_BROWSER_SOURCE_CONCURRENCY"


def configured_browser_concurrency():
    """Read the process-wide browser limit, with a safe positive fallback."""
    try:
        requested = int(os.getenv(ENV_NAME, str(DEFAULT_BROWSER_CONCURRENCY)) or "")
    except (TypeError, ValueError):
        requested = DEFAULT_BROWSER_CONCURRENCY
    return max(1, requested)


class BrowserAdmission:
    """A cancellation-safe, event-loop-independent async admission gate.

    A small polling wait keeps the shared counter usable by multiple event loops
    while ensuring a cancelled waiter never consumes a permit in the background.
    """

    def __init__(self, limit=None, poll_interval=0.01):
        selected_limit = configured_browser_concurrency() if limit is None else limit
        self.limit = max(1, int(selected_limit))
        self.poll_interval = max(0.001, float(poll_interval))
        self._active = 0
        self._lock = threading.Lock()

    @property
    def active(self):
        with self._lock:
            return self._active

    async def acquire(self, timeout=None):
        start = time.time() if timeout else 0.0
        while True:
            with self._lock:
                if self._active < self.limit:
                    self._active += 1
                    return
            if timeout and (time.time() - start) > timeout:
                raise TimeoutError(f"Browser admission queue timed out after {timeout}s (active={self._active}/{self.limit})")
            await asyncio.sleep(self.poll_interval)

    def release(self):
        with self._lock:
            if self._active <= 0:
                raise RuntimeError("Browser admission slot released without acquisition")
            self._active -= 1

    @asynccontextmanager
    async def slot(self, timeout=None):
        acquired = False
        try:
            await self.acquire(timeout=timeout)
            acquired = True
            yield
        finally:
            if acquired:
                self.release()


browser_admission = BrowserAdmission()
