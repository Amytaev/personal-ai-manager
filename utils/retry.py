"""Retry/backoff decorator (TZ v4 §31).

Bounded exponential backoff with jitter — never retries forever, per
the spec. Intended for any external API call once Teams/Weather/
VALORANT agents exist (Phase 4/5/6); not used anywhere yet in Phase 2.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import random
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def retry_with_backoff(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
):
    """Decorator factory. Retries an async function up to ``max_attempts``
    times with exponential backoff (+ jitter) between attempts, then lets
    the final exception propagate."""

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            attempt = 0
            while True:
                attempt += 1
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    if attempt >= max_attempts:
                        logger.warning(
                            "%s failed after %s attempts: %s", func.__name__, attempt, exc
                        )
                        raise
                    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    delay += random.uniform(0, delay * 0.1)  # jitter
                    logger.info(
                        "%s attempt %s/%s failed (%s) - retrying in %.1fs",
                        func.__name__, attempt, max_attempts, exc, delay,
                    )
                    await asyncio.sleep(delay)

        return wrapper

    return decorator
