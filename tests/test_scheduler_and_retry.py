from __future__ import annotations

import pytest

from scheduler.scheduler import SingleInstanceError, SingleInstanceGuard
from utils.retry import retry_with_backoff


def test_single_instance_guard_blocks_second_acquire(tmp_path):
    lock_path = tmp_path / "app.lock"

    first = SingleInstanceGuard(lock_path)
    first.acquire()
    try:
        second = SingleInstanceGuard(lock_path)
        with pytest.raises(SingleInstanceError):
            second.acquire()
    finally:
        first.release()


def test_single_instance_guard_allows_reacquire_after_release(tmp_path):
    lock_path = tmp_path / "app.lock"

    first = SingleInstanceGuard(lock_path)
    first.acquire()
    first.release()

    second = SingleInstanceGuard(lock_path)
    second.acquire()  # should not raise
    second.release()


@pytest.mark.asyncio
async def test_retry_succeeds_after_transient_failures():
    attempts = {"count": 0}

    @retry_with_backoff(max_attempts=3, base_delay=0.01)
    async def flaky():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ConnectionError("temporary")
        return "ok"

    result = await flaky()
    assert result == "ok"
    assert attempts["count"] == 3


@pytest.mark.asyncio
async def test_retry_raises_after_max_attempts_exhausted():
    attempts = {"count": 0}

    @retry_with_backoff(max_attempts=2, base_delay=0.01)
    async def always_fails():
        attempts["count"] += 1
        raise ConnectionError("permanent")

    with pytest.raises(ConnectionError):
        await always_fails()

    assert attempts["count"] == 2  # never retries forever, per TZ v4 §31
