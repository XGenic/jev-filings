"""Synthetic HTTP responses only: these tests never contact SEC."""

import logging
import os
from email.utils import formatdate

import httpx
import pytest

from radar.config import Settings
from radar.sec import cache as cache_module
from radar.sec import client as client_module
from radar.sec.cache import CacheCorruptionError, CacheMissError, DiskCache, atomic_write
from radar.sec.client import RequestLimiter, SecClient, SecRequestError

METADATA_URL = "https://data.sec.gov/submissions/CIK0000000042.json"
RAW_URL = "https://www.sec.gov/Archives/edgar/data/42/000000004224000001/synthetic.htm"


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(client_module, "_REQUEST_LIMITER", RequestLimiter(clock, clock.sleep))
    monkeypatch.setattr(client_module, "sleep", clock.sleep)
    return clock


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, sec_user_agent="Synthetic Radar tests@example.com")


@pytest.mark.parametrize(
    "user_agent",
    ["", "Anonymous crawler", "tests@example.com", "Radar tests@example.com\r\nInjected: yes"],
)
def test_online_requires_identifying_user_agent(settings, user_agent):
    with pytest.raises(ValueError):
        SecClient(settings.model_copy(update={"sec_user_agent": user_agent}))


def test_limiter_is_shared_across_clients_and_caps_rate(settings, clock):
    request_times = []

    def handle(request):
        assert request.headers["User-Agent"] == settings.sec_user_agent
        request_times.append(clock())
        return httpx.Response(200, json={"synthetic": True})

    settings = settings.model_copy(update={"sec_requests_per_second": 5})
    with (
        SecClient(settings, transport=httpx.MockTransport(handle)) as first,
        SecClient(settings, transport=httpx.MockTransport(handle)) as second,
    ):
        first.get_json(METADATA_URL)
        second.get_json(METADATA_URL.replace("42", "43"))
        first.get_json(METADATA_URL.replace("42", "44"))
    assert request_times == pytest.approx([0.0, 0.2, 0.4])
    limiter = RequestLimiter(clock, clock.sleep)
    limiter.acquire(100)
    limiter.acquire(100)
    assert clock() == pytest.approx(0.6)


def test_retries_honor_retry_after_then_exponential_backoff(settings, clock):
    request_times = []

    def handle(request):
        request_times.append(clock())
        if len(request_times) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        if len(request_times) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"ready": True})

    with SecClient(settings, transport=httpx.MockTransport(handle)) as client:
        assert client.get_json(METADATA_URL) == {"ready": True}
    assert request_times == [0.0, 3.0, 5.0]


def test_retry_after_http_date_and_transport_failure(settings, clock, monkeypatch):
    epoch = 1_700_000_000
    monkeypatch.setattr(client_module.time, "time", lambda: epoch + clock())
    request_times = []

    def handle(request):
        request_times.append(clock())
        if len(request_times) == 1:
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if len(request_times) == 2:
            return httpx.Response(429, headers={"Retry-After": formatdate(epoch + 6, usegmt=True)})
        return httpx.Response(200, json={"recovered": True})

    with SecClient(settings, transport=httpx.MockTransport(handle)) as client:
        assert client.get_json(METADATA_URL) == {"recovered": True}
    assert request_times == [0.0, 1.0, 6.0]


def test_retry_exhaustion_is_bounded_and_does_not_cache_errors(settings):
    attempts = []

    def handle(request):
        attempts.append(request)
        return httpx.Response(503)

    settings = settings.model_copy(update={"sec_retries": 2})
    with SecClient(settings, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(SecRequestError):
            client.get_json(METADATA_URL)
        assert client.cache.read(METADATA_URL) is None
    assert len(attempts) == 3


@pytest.mark.parametrize(
    ("status", "headers"),
    [(404, {}), (429, {"Retry-After": "120"}), (302, {"Location": "https://example.com/"})],
)
def test_nonretryable_or_unbounded_delay_never_sends_another_request(
    settings, clock, status, headers
):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, headers=headers)

    with SecClient(settings, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(SecRequestError):
            client.get_json(METADATA_URL)
    assert len(requests) == 1
    assert clock.sleeps == []


def test_fresh_metadata_and_permanent_raw_documents_avoid_network(settings):
    requests = []

    def handle(request):
        requests.append(str(request.url))
        if str(request.url) == RAW_URL:
            return httpx.Response(200, content=b"<html>Synthetic filing</html>")
        return httpx.Response(200, json={"version": 1})

    with SecClient(settings, transport=httpx.MockTransport(handle)) as client:
        assert client.get_json(METADATA_URL) == client.get_json(METADATA_URL) == {"version": 1}
        original = client.get_bytes(RAW_URL, permanent=True)
        raw_path = client.cache_path(RAW_URL, permanent=True)
        os.utime(raw_path, (1, 1))
        assert client.get_bytes(RAW_URL, permanent=True) == original
        assert raw_path.parent == settings.data_dir / "raw"
        assert raw_path.read_bytes() == original
    assert requests == [METADATA_URL, RAW_URL]


def test_stale_fallback_warns_and_preserves_cache(settings, caplog):
    cache = DiskCache(settings.data_dir)
    original = b'{"synthetic_version": 7}'
    path = cache.write(METADATA_URL, original)
    os.utime(path, (1, 1))
    settings = settings.model_copy(update={"sec_retries": 0})
    with SecClient(
        settings, transport=httpx.MockTransport(lambda request: httpx.Response(503))
    ) as client:
        with caplog.at_level(logging.WARNING):
            assert client.get_json(METADATA_URL) == {"synthetic_version": 7}
    assert path.read_bytes() == original
    assert path.stat().st_mtime == 1
    assert any(
        record.levelno >= logging.WARNING and METADATA_URL in record.message
        for record in caplog.records
    )


def test_invalid_refresh_does_not_poison_valid_stale_json(settings):
    cache = DiskCache(settings.data_dir)
    path = cache.write(METADATA_URL, b'{"synthetic": "old"}')
    os.utime(path, (1, 1))
    with SecClient(
        settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="not json")),
    ) as client:
        assert client.get_json(METADATA_URL) == {"synthetic": "old"}
    assert path.read_bytes() == b'{"synthetic": "old"}'


def test_offline_uses_expired_cache_without_identity_and_never_requests(settings):
    def forbidden_request(request):
        pytest.fail("An offline client attempted a network request")

    cache = DiskCache(settings.data_dir)
    path = cache.write(METADATA_URL, b'{"cached": true}')
    os.utime(path, (1, 1))
    cache.write(RAW_URL, b"<html>Synthetic offline filing</html>", permanent=True)
    settings = settings.model_copy(update={"sec_user_agent": ""})
    with SecClient(
        settings, offline=True, transport=httpx.MockTransport(forbidden_request)
    ) as client:
        assert client.get_json(METADATA_URL) == {"cached": True}
        assert client.get_bytes(RAW_URL, permanent=True) == b"<html>Synthetic offline filing</html>"
        with pytest.raises(CacheMissError):
            client.get_json(METADATA_URL.replace("42", "99"))
        with pytest.raises(CacheMissError):
            client.get_bytes(RAW_URL.replace("synthetic", "missing"), permanent=True)


def test_corrupt_metadata_requires_network_repair_or_fails_offline(settings):
    cache = DiskCache(settings.data_dir)
    cache.write(METADATA_URL, b"broken")
    with SecClient(settings, offline=True) as client:
        with pytest.raises(CacheCorruptionError):
            client.get_json(METADATA_URL)
    with SecClient(
        settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"fixed": True})),
    ) as client:
        assert client.get_json(METADATA_URL) == {"fixed": True}
    with SecClient(settings, offline=True) as client:
        assert client.get_json(METADATA_URL) == {"fixed": True}


def test_atomic_failure_preserves_existing_entry_and_removes_temporary_file(tmp_path, monkeypatch):
    target = tmp_path / "entry.json"
    atomic_write(target, b"old complete document")

    def failed_replace(source, destination):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(cache_module.os, "replace", failed_replace)
    with pytest.raises(OSError):
        atomic_write(target, b"new complete document")
    assert target.read_bytes() == b"old complete document"
    assert list(tmp_path.iterdir()) == [target]
