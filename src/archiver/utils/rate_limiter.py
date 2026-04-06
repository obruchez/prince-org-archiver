import asyncio
import time


class TokenBucketRateLimiter:
    """Token bucket rate limiter with async support.

    Args:
        rate: Tokens added per second (e.g., 0.5 = 1 token every 2 seconds).
        burst: Maximum tokens in the bucket.
    """

    def __init__(self, rate: float = 0.5, burst: int = 5):
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._slowdown_factor = 1.0

    def slowdown(self, factor: float = 2.0) -> None:
        self._slowdown_factor = min(self._slowdown_factor * factor, 10.0)

    def speed_up(self) -> None:
        self._slowdown_factor = max(self._slowdown_factor / 1.5, 1.0)

    @property
    def effective_rate(self) -> float:
        return self.rate / self._slowdown_factor

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(
                    self.burst,
                    self._tokens + elapsed * self.effective_rate,
                )
                self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

                wait_time = (1.0 - self._tokens) / self.effective_rate
                await asyncio.sleep(wait_time)
