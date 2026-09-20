"""Inline synthetic SEC-shaped metadata; no real issuer or filing provenance is claimed."""

import logging
from datetime import date

import httpx
import pytest

from radar.config import Settings
from radar.sec import client as client_module
from radar.sec.client import RequestLimiter, SecClient
from radar.sec.filings import (
    ARCHIVES_BASE,
    SUBMISSIONS_BASE,
    TICKERS_URL,
    InvalidSecMetadataError,
    NoFilingError,
    NoPriorFilingError,
    UnknownTickerError,
    discover_pair,
    fetch_pair,
    normalize_accession,
    normalize_cik,
    normalize_ticker,
)

CIK = "0000000042"
SUBMISSIONS_URL = f"{SUBMISSIONS_BASE}CIK{CIK}.json"


def synthetic_row(form, filed, serial, period="2024-06-30", primary="synthetic.htm"):
    return {
        "form": form,
        "filingDate": filed,
        "accessionNumber": f"{CIK}-{filed[2:4]}-{serial:06d}",
        "reportDate": period,
        "primaryDocument": primary,
    }


def arrays(rows):
    fields = ("form", "filingDate", "accessionNumber", "reportDate", "primaryDocument")
    return {field: [row[field] for row in rows] for field in fields}


def history_file(number, start, end):
    return {
        "name": f"CIK{CIK}-submissions-{number:03d}.json",
        "filingFrom": start,
        "filingTo": end,
    }


def source_url(row):
    return f"{ARCHIVES_BASE}42/{row['accessionNumber'].replace('-', '')}/{row['primaryDocument']}"


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(client_module, "_REQUEST_LIMITER", RequestLimiter(lambda: now[0], sleep))

    def factory(recent, files=(), pages=None, documents=None):
        responses = {
            TICKERS_URL: {
                "0": {"ticker": "SYN-A", "cik_str": 42, "title": "Synthetic Example Corp"}
            },
            SUBMISSIONS_URL: {
                "cik": 42,
                "name": "SYNTHETIC EXAMPLE CORPORATION",
                "filings": {"recent": arrays(recent), "files": list(files)},
            },
        }
        responses.update(
            {f"{SUBMISSIONS_BASE}{name}": arrays(rows) for name, rows in (pages or {}).items()}
        )
        responses.update(documents or {})
        requests = []

        def handle(request):
            url = str(request.url)
            requests.append(url)
            if url not in responses:
                pytest.fail(f"Unexpected synthetic SEC request: {url}")
            payload = responses[url]
            if isinstance(payload, bytes):
                return httpx.Response(200, content=payload)
            return httpx.Response(200, json=payload)

        settings = Settings(data_dir=tmp_path, sec_user_agent="Synthetic Radar tests@example.com")
        return SecClient(settings, transport=httpx.MockTransport(handle)), requests, responses

    return factory


def test_ticker_cik_and_accession_normalization():
    assert normalize_ticker(" syn.a ") == "SYN-A"
    assert normalize_cik(42) == CIK
    assert normalize_cik("0000000042") == CIK
    assert normalize_accession("000000004224000007") == "0000000042-24-000007"
    assert normalize_accession("0000000042-24-000007") == "0000000042-24-000007"


@pytest.mark.parametrize(
    ("normalizer", "value"),
    [
        (normalize_ticker, "../SYN"),
        (normalize_ticker, "ß"),
        (normalize_cik, True),
        (normalize_cik, 0),
        (normalize_cik, "12345678901"),
        (normalize_cik, "42/../43"),
        (normalize_accession, "42-24-7"),
        (normalize_accession, "0000000042-24-000007/evil"),
    ],
)
def test_invalid_identifiers_are_rejected(normalizer, value):
    with pytest.raises(ValueError):
        normalizer(value)


def test_latest_form_is_deterministic_and_amendments_are_excluded(make_client, caplog):
    prior = synthetic_row("10-Q", "2024-05-08", 2, "2024-03-31")
    latest = synthetic_row("10-Q", "2024-08-08", 3, "2024-06-30")
    client, requests, _ = make_client(
        [
            prior,
            synthetic_row("10-K", "2024-03-01", 1, "2023-12-31"),
            synthetic_row("10-Q/A", "2024-11-01", 4),
            latest,
            synthetic_row("10-K", "2023-03-01", 1, "2022-12-31"),
        ]
    )
    with client, caplog.at_level(logging.INFO):
        pair = discover_pair(client, "syn.a")
        annual = discover_pair(client, "SYN-A", "10-K")
    assert (pair.current.accession, pair.previous.accession) == (
        latest["accessionNumber"],
        prior["accessionNumber"],
    )
    assert pair.current.form == pair.previous.form == "10-Q"
    assert pair.current.ticker == "SYN-A"
    assert pair.current.cik == CIK
    assert pair.current.company_name == "SYNTHETIC EXAMPLE CORPORATION"
    assert pair.current.period_of_report == date(2024, 6, 30)
    assert pair.previous.period_of_report == date(2024, 3, 31)
    assert pair.strategy == "previous_same_form"
    assert annual.current.filed_date == date(2024, 3, 1)
    assert annual.previous.filed_date == date(2023, 3, 1)
    assert requests == [TICKERS_URL, SUBMISSIONS_URL]
    assert any(
        pair.current.accession in record.message and pair.previous.accession in record.message
        for record in caplog.records
    )


def test_history_is_newest_first_deduplicated_and_stops_when_pair_is_known(make_client):
    latest = synthetic_row("10-Q", "2024-08-08", 3)
    prior = synthetic_row("10-Q", "2024-05-08", 2, "2024-03-31")
    near = history_file(1, "2024-01-01", "2024-08-08")
    old = history_file(2, "2020-01-01", "2023-12-31")
    client, requests, _ = make_client(
        [latest],
        files=[old, near],
        pages={near["name"]: [latest, synthetic_row("10-Q/A", "2024-06-01", 9), prior]},
    )
    with client:
        pair = discover_pair(client, "SYN-A")
    assert pair.current.accession == latest["accessionNumber"]
    assert pair.previous.accession == prior["accessionNumber"]
    assert requests == [TICKERS_URL, SUBMISSIONS_URL, f"{SUBMISSIONS_BASE}{near['name']}"]


def test_empty_recent_paginates_without_switching_from_latest_form(make_client):
    latest = synthetic_row("10-Q", "2024-06-01", 3)
    prior = synthetic_row("10-Q", "2024-02-01", 1)
    newer = history_file(1, "2023-01-01", "2024-06-01")
    older = history_file(2, "2023-01-01", "2024-05-01")
    client, requests, _ = make_client(
        [],
        files=[older, newer],
        pages={
            newer["name"]: [
                synthetic_row("10-K", "2023-03-01", 1),
                synthetic_row("10-K", "2024-03-01", 2),
                latest,
            ],
            older["name"]: [prior],
        },
    )
    with client:
        pair = discover_pair(client, "SYN-A")
    assert pair.current.accession == latest["accessionNumber"]
    assert pair.previous.accession == prior["accessionNumber"]
    assert requests[2:] == [
        f"{SUBMISSIONS_BASE}{newer['name']}",
        f"{SUBMISSIONS_BASE}{older['name']}",
    ]


def test_history_same_date_boundary_can_replace_previous_accession(make_client):
    current = synthetic_row("10-Q", "2024-08-01", 3)
    earlier = synthetic_row("10-Q", "2024-05-01", 1)
    later = synthetic_row("10-Q", "2024-05-01", 2)
    page = history_file(1, "2024-01-01", "2024-05-01")
    client, _, _ = make_client([earlier, current], files=[page], pages={page["name"]: [later]})
    with client:
        pair = discover_pair(client, "SYN-A")
    assert pair.previous.accession == later["accessionNumber"]


def test_no_prior_is_explicit_instead_of_falling_back_to_annual_pair(make_client):
    client, requests, _ = make_client(
        [
            synthetic_row("10-Q", "2024-08-01", 3),
            synthetic_row("10-Q/A", "2024-05-01", 2),
            synthetic_row("10-K", "2024-03-01", 1),
            synthetic_row("10-K", "2023-03-01", 1),
        ]
    )
    with client, pytest.raises(NoPriorFilingError):
        discover_pair(client, "SYN-A")
    assert requests == [TICKERS_URL, SUBMISSIONS_URL]


def test_only_amendments_are_not_eligible_filings(make_client):
    client, _, _ = make_client([synthetic_row("10-K/A", "2024-03-01", 1)])
    with client, pytest.raises(NoFilingError):
        discover_pair(client, "SYN-A")


def test_unknown_ticker_does_not_request_submissions(make_client):
    client, requests, _ = make_client([])
    with client, pytest.raises(UnknownTickerError):
        discover_pair(client, "UNKNOWN")
    assert requests == [TICKERS_URL]


@pytest.mark.parametrize("filename", ["../evil.htm", "evil%2fdocument.htm", "evil.htm?redirect=1"])
def test_primary_document_paths_are_validated_before_download(make_client, filename):
    client, requests, _ = make_client([synthetic_row("10-Q", "2024-08-01", 3, primary=filename)])
    with client, pytest.raises(InvalidSecMetadataError):
        discover_pair(client, "SYN-A")
    assert requests == [TICKERS_URL, SUBMISSIONS_URL]


def test_history_path_cannot_escape_issuer_or_submissions_directory(make_client):
    page = history_file(1, "2023-01-01", "2024-01-01")
    page["name"] = "../CIK0000000099-submissions-001.json"
    client, requests, _ = make_client([], files=[page])
    with client, pytest.raises(InvalidSecMetadataError):
        discover_pair(client, "SYN-A")
    assert requests == [TICKERS_URL, SUBMISSIONS_URL]


def test_mismatched_arrays_fail_instead_of_pairing_wrong_metadata(make_client):
    client, _, responses = make_client([synthetic_row("10-Q", "2024-08-01", 3)])
    responses[SUBMISSIONS_URL]["filings"]["recent"]["filingDate"] = []
    with client, pytest.raises(InvalidSecMetadataError):
        discover_pair(client, "SYN-A")


def test_conflicting_duplicate_accessions_are_not_silently_overwritten(make_client):
    latest = synthetic_row("10-Q", "2024-08-01", 3)
    changed = {**latest, "reportDate": "2024-03-31"}
    page = history_file(1, "2024-01-01", "2024-08-01")
    client, _, _ = make_client([latest], files=[page], pages={page["name"]: [changed]})
    with client, pytest.raises(InvalidSecMetadataError):
        discover_pair(client, "SYN-A")


def test_fetches_exactly_two_primary_documents_and_replays_offline(make_client):
    current = synthetic_row("10-Q", "2024-08-01", 3)
    previous = synthetic_row("10-Q", "2024-05-01", 2, "2024-03-31")
    documents = {
        source_url(current): b"<html><p>Synthetic current disclosure</p></html>",
        source_url(previous): b"<html><p>Synthetic previous disclosure</p></html>",
    }
    client, requests, _ = make_client(
        [previous, synthetic_row("8-K", "2024-08-02", 4), current], documents=documents
    )
    with client:
        pair = fetch_pair(client, discover_pair(client, "SYN-A"))
        cached_pair = fetch_pair(client, pair)
    assert requests == [TICKERS_URL, SUBMISSIONS_URL, source_url(current), source_url(previous)]
    assert cached_pair == pair
    for filing in (pair.current, pair.previous):
        assert filing.local_path.is_relative_to(client.settings.data_dir / "raw")
        assert filing.local_path.read_bytes() == documents[filing.source_url]
        assert filing.accession.replace("-", "") in filing.source_url

    def forbidden_request(request):
        pytest.fail("Offline filing replay attempted network access")

    with SecClient(
        client.settings, offline=True, transport=httpx.MockTransport(forbidden_request)
    ) as offline:
        assert fetch_pair(offline, discover_pair(offline, "SYN-A")) == pair
