import asyncio
import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class RetryExhausted(Exception):
    def __init__(self, last_error: Exception, attempts: int):
        self.last_error = last_error
        self.attempts = attempts
        super().__init__(f"Retry exhausted after {attempts} attempts: {last_error}")


async def retry_request(
    func: Callable,
    *args: Any,
    max_retries: int = 5,
    base_delay: float = 5.0,
    max_delay: float = 80.0,
    **kwargs: Any,
) -> httpx.Response:
    """Execute an async function with exponential backoff retries."""
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            response = await func(*args, **kwargs)

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else 120.0
                logger.warning(f"Rate limited (429). Waiting {delay}s")
                await asyncio.sleep(delay)
                continue

            if response.status_code in RETRYABLE_STATUS_CODES:
                delay = min(base_delay * (2**attempt), max_delay)
                logger.warning(
                    f"HTTP {response.status_code}, retry {attempt + 1}/{max_retries} "
                    f"in {delay}s"
                )
                await asyncio.sleep(delay)
                continue

            return response

        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as e:
            last_error = e
            if attempt < max_retries:
                delay = min(base_delay * (2**attempt), max_delay)
                logger.warning(
                    f"{type(e).__name__}, retry {attempt + 1}/{max_retries} in {delay}s"
                )
                await asyncio.sleep(delay)
            else:
                raise RetryExhausted(e, max_retries + 1) from e

    raise RetryExhausted(
        last_error or Exception("Unknown error"), max_retries + 1
    )
