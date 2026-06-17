import logging
import time
from dataclasses import dataclass

import httpx

from archiver.config import Config
from archiver.utils.rate_limiter import TokenBucketRateLimiter
from archiver.utils.retry import RetryExhausted, retry_request

logger = logging.getLogger(__name__)


class MaxRequestsReached(Exception):
    pass


@dataclass
class FetchResult:
    url: str
    status_code: int
    content: bytes
    response_time: float
    final_url: str | None = None
    error: str | None = None


class HttpClient:
    def __init__(self, config: Config):
        self.config = config
        self.rate_limiter = TokenBucketRateLimiter(
            rate=config.rate, burst=config.burst
        )
        self._client: httpx.AsyncClient | None = None
        self._request_count = 0
        self._consecutive_errors = 0

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": self.config.user_agent},
            timeout=httpx.Timeout(self.config.request_timeout),
            follow_redirects=True,
            http2=True,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        assert self._client is not None, "Client not started"
        return self._client

    @property
    def request_count(self) -> int:
        return self._request_count

    async def fetch(self, url: str) -> FetchResult:
        await self.rate_limiter.acquire()

        if (
            self.config.max_requests
            and self._request_count >= self.config.max_requests
        ):
            raise MaxRequestsReached()

        start = time.monotonic()
        try:
            response = await retry_request(self.client.get, url)
            elapsed = time.monotonic() - start

            self._request_count += 1
            self._consecutive_errors = 0

            # Adaptive throttling
            if elapsed > self.config.adaptive_threshold:
                logger.warning(
                    f"Slow response ({elapsed:.1f}s), slowing down"
                )
                self.rate_limiter.slowdown()
            elif elapsed < 2.0:
                self.rate_limiter.speed_up()

            return FetchResult(
                url=url,
                status_code=response.status_code,
                content=response.content,
                response_time=elapsed,
                final_url=str(response.url),
            )

        except RetryExhausted as e:
            elapsed = time.monotonic() - start
            self._request_count += 1
            self._consecutive_errors += 1

            if self._consecutive_errors >= self.config.error_budget:
                logger.error(
                    f"Error budget exhausted ({self._consecutive_errors} consecutive errors)"
                )
                raise

            return FetchResult(
                url=url,
                status_code=0,
                content=b"",
                response_time=elapsed,
                error=str(e),
            )

    def thread_url(
        self, thread_id: int, page: int = 1, forum_id: int | None = None
    ) -> str:
        # We default the URL prefix to /msg/7/ (the largest forum). When a
        # thread actually lives in a different forum the server 301-redirects
        # to /msg/<real-forum>/<thread_id> -- but crucially that redirect
        # STRIPS the `?pg=N` query parameter. So for page 1 the default is
        # fine (page 1 has no pg=), but for pages 2+ we MUST send the real
        # forum_id or we'd silently get page 1 saved under page_N.html.
        # This bug corrupted ~142K pages on the original live crawl before
        # the forum_id arg was added (commit 2026-06-17).
        fid = forum_id if forum_id is not None else 7
        url = f"{self.config.base_url}/msg/{fid}/{thread_id}"
        if page > 1:
            url += f"?&pg={page}"
        return url

    def events_url(self, year: int, month: int) -> str:
        return f"{self.config.base_url}/events/{year}/{month}"

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.close()
