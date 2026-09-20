"""Conservative, cached access to SEC public endpoints."""

import json
import logging
import math
import re
import threading
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import sleep
from urllib.parse import urlsplit

import httpx

from radar.config import Settings
from radar.sec.cache import CacheCorruptionError, CacheMissError, DiskCache

logger = logging.getLogger(__name__)
MAX_BACKOFF_SECONDS = 60.0
RETRYABLE_STATUSES = {403, 408, 429, 500, 502, 503, 504}


class SecError(RuntimeError):
    """An explicit SEC ingestion failure."""


class SecRequestError(SecError):
    """The SEC response was unavailable or unusable."""


def validate_user_agent(value: str) -> str:
    """Require both a declared identity and an email contact before going online."""
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise ValueError("SEC_USER_AGENT must be a printable identity and contact email")
    value = value.strip()
    contact = re.search(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value)
    identity = (value[: contact.start()] + value[contact.end() :]) if contact else ""
    if contact is None or not re.search(r"[A-Za-z]", identity):
        raise ValueError(
            'Set SEC_USER_AGENT to an identifying name and email, e.g. "Radar me@example.com"'
        )
    return value


def validate_sec_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc not in {"www.sec.gov", "data.sec.gov"}
        or parsed.fragment
        or parsed.query
        or not parsed.path.startswith("/")
    ):
        raise ValueError(f"Not an allowed public SEC URL: {url!r}")


class RequestLimiter:
    """Space request starts; one instance is shared by all clients in this process."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._clock = clock
        self._sleep = sleeper
        self._lock = threading.Lock()
        self._last_request: float | None = None
        self._last_interval = 0.0

    def acquire(self, requests_per_second: float) -> None:
        if not math.isfinite(requests_per_second) or requests_per_second <= 0:
            raise ValueError("SEC request rate must be positive and finite")
        interval = 1.0 / min(requests_per_second, 5.0)
        with self._lock:
            if self._last_request is not None:
                earliest = self._last_request + max(interval, self._last_interval)
                remaining = earliest - self._clock()
                while remaining > 0:
                    self._sleep(remaining)
                    remaining = earliest - self._clock()
            self._last_request = self._clock()
            self._last_interval = interval


_REQUEST_LIMITER = RequestLimiter()


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        return float(value)
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return None
        return max(0.0, parsed.timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        return None


def _json_object(content: bytes, url: str) -> dict:
    try:
        decoded = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise SecRequestError(f"SEC returned invalid JSON for {url}") from exc
    if not isinstance(decoded, dict):
        raise SecRequestError(f"SEC returned a non-object JSON response for {url}")
    return decoded


class SecClient:
    def __init__(self, settings: Settings, offline: bool = False, transport=None):
        self.settings = settings
        self.offline = offline
        self.cache = DiskCache(settings.data_dir)
        self._http = None
        if not offline:
            user_agent = validate_user_agent(settings.sec_user_agent)
            self._http = httpx.Client(
                headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
                timeout=settings.sec_timeout,
                transport=transport,
                follow_redirects=False,
            )

    def __enter__(self) -> "SecClient":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        if self._http is not None:
            self._http.close()

    def cache_path(self, url: str, permanent: bool = False) -> Path:
        validate_sec_url(url)
        return self.cache.path_for(url, permanent)

    def get_json(self, url: str, permanent: bool = False) -> dict:
        value = self._get(url, permanent, expect_json=True)
        assert isinstance(value, dict)
        return value

    def get_bytes(self, url: str, permanent: bool = False) -> bytes:
        value = self._get(url, permanent, expect_json=False)
        assert isinstance(value, bytes)
        return value

    def _get(self, url: str, permanent: bool, expect_json: bool) -> bytes | dict:
        validate_sec_url(url)
        cached = self.cache.read(url, permanent)
        cached_value: bytes | dict = cached.content if cached is not None else b""
        if cached is not None and expect_json:
            try:
                cached_value = _json_object(cached.content, url)
            except SecRequestError as exc:
                if self.offline:
                    raise CacheCorruptionError(f"Invalid offline SEC cache for {url}") from exc
                logger.warning("Ignoring invalid SEC metadata cache for %s", url)
                cached = None
        if cached is not None and (
            self.offline or permanent or cached.is_fresh(self.settings.metadata_ttl_seconds)
        ):
            return cached_value
        if self.offline:
            raise CacheMissError(f"Offline SEC cache miss for {url}")
        try:
            content = self._request(url)
            value = _json_object(content, url) if expect_json else content
        except SecRequestError:
            if cached is None:
                raise
            logger.warning(
                "SEC request failed; using stale cached response for %s", url, exc_info=True
            )
            return cached_value
        self.cache.write(url, content, permanent)
        return value

    def _request(self, url: str) -> bytes:
        if self._http is None:
            raise SecRequestError("An offline SEC client cannot issue HTTP requests")
        for attempt in range(self.settings.sec_retries + 1):
            _REQUEST_LIMITER.acquire(self.settings.sec_requests_per_second)
            try:
                response = self._http.get(url)
                response.raise_for_status()
                return response.content
            except httpx.HTTPStatusError as exc:
                response = exc.response
                if response.status_code not in RETRYABLE_STATUSES:
                    raise SecRequestError(f"SEC HTTP {response.status_code} for {url}") from exc
                failure = exc
                retry_after = _retry_after(response)
            except httpx.TransportError as exc:
                failure = exc
                retry_after = None
            if attempt == self.settings.sec_retries:
                break
            delay = max(min(2.0**attempt, MAX_BACKOFF_SECONDS), retry_after or 0.0)
            # Never retry earlier than a server's long Retry-After. Fail/fall back
            # instead of turning a bounded local operation into a long sleep.
            if delay > MAX_BACKOFF_SECONDS:
                break
            logger.warning("Retrying SEC request to %s in %.2fs", url, delay)
            sleep(delay)
        raise SecRequestError(
            f"SEC request failed after bounded retries for {url}: {failure}"
        ) from failure
