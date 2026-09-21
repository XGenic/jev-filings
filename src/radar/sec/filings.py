"""Resolve tickers and select the immediately previous non-amended same-form filing."""

import logging
import re
from datetime import date

from radar.models import Company, ComparableFilingPair, Filing, Form
from radar.sec.client import SecClient, SecError

logger = logging.getLogger(__name__)
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_BASE = "https://data.sec.gov/submissions/"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data/"


class InvalidSecMetadataError(SecError):
    """SEC metadata cannot safely and unambiguously identify a filing."""


class UnknownTickerError(SecError):
    """The canonical ticker is absent from SEC's company ticker mapping."""


class NoFilingError(SecError):
    """No non-amended 10-K/10-Q exists for the requested issuer/form."""


class NoPriorFilingError(SecError):
    """The latest filing has no immediately preceding filing of the same form."""


def normalize_ticker(value: str) -> str:
    if not isinstance(value, str) or not value.isascii():
        raise ValueError("Ticker must be an ASCII stock symbol")
    ticker = value.strip().upper().replace(".", "-")
    if len(ticker) > 20 or not re.fullmatch(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*", ticker):
        raise ValueError(f"Invalid ticker: {value!r}")
    return ticker


def normalize_cik(value: str | int) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("CIK must be a positive number of at most ten digits")
    cik = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,10}", cik) or int(cik) == 0:
        raise ValueError(f"Invalid CIK: {value!r}")
    return cik.zfill(10)


def normalize_accession(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Accession must be an 18-digit SEC accession number")
    accession = value.strip()
    if re.fullmatch(r"[0-9]{18}", accession):
        return f"{accession[:10]}-{accession[10:12]}-{accession[12:]}"
    if not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession):
        raise ValueError(f"Invalid accession: {value!r}")
    return accession


def _primary_document(value: object) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:htm|html|xhtml)", value, re.I)
        or ".." in value
    ):
        raise InvalidSecMetadataError(f"Invalid primary filing HTML filename: {value!r}")
    return value


def _date(value: object, field: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise InvalidSecMetadataError(f"Invalid SEC {field}: {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidSecMetadataError(f"Invalid SEC {field}: {value!r}") from exc


def resolve_company(client: SecClient, ticker: str) -> Company:
    ticker = normalize_ticker(ticker)
    mapping = client.get_json(TICKERS_URL)
    matches = []
    for entry in mapping.values():
        if not isinstance(entry, dict) or not isinstance(entry.get("ticker"), str):
            raise InvalidSecMetadataError("SEC ticker mapping contains a malformed company row")
        if entry["ticker"].strip().upper().replace(".", "-") != ticker:
            continue
        try:
            cik = normalize_cik(entry["cik_str"])
            name = entry["title"]
        except (KeyError, ValueError) as exc:
            raise InvalidSecMetadataError(f"Invalid SEC company metadata for {ticker}") from exc
        if not isinstance(name, str) or not name.strip():
            raise InvalidSecMetadataError(f"Missing SEC company name for {ticker}")
        matches.append(Company(ticker=ticker, cik=cik, name=name.strip()))
    if not matches:
        raise UnknownTickerError(f"Ticker {ticker} is not present in SEC company_tickers.json")
    if any(company != matches[0] for company in matches[1:]):
        raise InvalidSecMetadataError(f"SEC ticker {ticker} maps to conflicting company records")
    return matches[0]


def _filings_from_arrays(client: SecClient, company: Company, arrays: dict) -> list[Filing]:
    forms = arrays.get("form")
    if not isinstance(forms, list):
        raise InvalidSecMetadataError("SEC submissions must contain a form array")
    required = ("accessionNumber", "filingDate", "primaryDocument")
    for field in (*required, "reportDate"):
        values = arrays.get(field)
        if field == "reportDate" and values is None:
            continue
        if not isinstance(values, list) or len(values) != len(forms):
            raise InvalidSecMetadataError(
                f"SEC submissions array {field} does not match form array"
            )
    periods = arrays.get("reportDate")
    filings = []
    for index, form in enumerate(forms):
        if form not in {"10-K", "10-Q"}:
            continue
        try:
            accession = normalize_accession(arrays["accessionNumber"][index])
        except ValueError as exc:
            raise InvalidSecMetadataError("SEC submissions contain an invalid accession") from exc
        # Validate document paths only after selection: legacy rows can have text
        # documents or no filename, without making the latest HTML pair unusable.
        primary_document = arrays["primaryDocument"][index]
        if (
            not isinstance(primary_document, str)
            or not re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9_.-]*)?", primary_document)
            or ".." in primary_document
        ):
            raise InvalidSecMetadataError(f"Invalid primary filing filename: {primary_document!r}")
        period = periods[index] if periods is not None else None
        source_url = (
            f"{ARCHIVES_BASE}{int(company.cik)}/{accession.replace('-', '')}/{primary_document}"
        )
        filings.append(
            Filing(
                ticker=company.ticker,
                cik=company.cik,
                company_name=company.name,
                form=form,
                accession=accession,
                filed_date=_date(arrays["filingDate"][index], "filingDate"),
                period_of_report=_date(period, "reportDate") if period not in (None, "") else None,
                primary_document=primary_document,
                source_url=source_url,
                local_path=client.cache_path(source_url, permanent=True),
            )
        )
    return filings


def _history_pages(files: object, cik: str) -> list[tuple[date, str]]:
    if not isinstance(files, list):
        raise InvalidSecMetadataError("SEC submissions files must be an array")
    pages = {}
    for entry in files:
        if not isinstance(entry, dict):
            raise InvalidSecMetadataError("SEC submissions contains a malformed historical file")
        name = entry.get("name")
        if not isinstance(name, str) or not re.fullmatch(
            rf"CIK{cik}-submissions-[0-9]+\.json", name
        ):
            raise InvalidSecMetadataError(f"Invalid SEC historical submissions filename: {name!r}")
        start = _date(entry.get("filingFrom"), "filingFrom")
        end = _date(entry.get("filingTo"), "filingTo")
        if start > end:
            raise InvalidSecMetadataError(f"Reversed SEC historical filing date range for {name}")
        if name in pages and pages[name] != (start, end):
            raise InvalidSecMetadataError(f"Conflicting SEC historical file ranges for {name}")
        pages[name] = (start, end)
    return sorted(((bounds[1], name) for name, bounds in pages.items()), reverse=True)


def _merge_filings(filings: dict[str, Filing], additions: list[Filing]) -> None:
    for filing in additions:
        prior = filings.get(filing.accession)
        if prior is not None and prior != filing:
            raise InvalidSecMetadataError(f"Conflicting SEC filing metadata for {filing.accession}")
        filings[filing.accession] = filing


def _latest_same_form(filings: dict[str, Filing], form: Form | None) -> list[Filing]:
    ordered = sorted(
        (filing for filing in filings.values() if form is None or filing.form == form),
        key=lambda filing: (filing.filed_date, filing.accession),
        reverse=True,
    )
    if not ordered:
        return []
    selected_form = form or ordered[0].form
    return [filing for filing in ordered if filing.form == selected_form][:2]


def discover_pair(client: SecClient, ticker: str, form: Form | None = None) -> ComparableFilingPair:
    if form is not None and form not in {"10-K", "10-Q"}:
        raise ValueError("Comparable filing form must be 10-K or 10-Q (not an amendment)")
    company = resolve_company(client, ticker)
    submissions = client.get_json(f"{SUBMISSIONS_BASE}CIK{company.cik}.json")
    if "cik" in submissions:
        try:
            issuer_cik = normalize_cik(submissions["cik"])
        except ValueError as exc:
            raise InvalidSecMetadataError("Invalid issuer CIK in SEC submissions") from exc
        if issuer_cik != company.cik:
            raise InvalidSecMetadataError("SEC submissions CIK does not match the resolved issuer")
    name = submissions.get("name")
    if isinstance(name, str) and name.strip():
        company = company.model_copy(update={"name": name.strip()})
    metadata = submissions.get("filings")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("recent"), dict):
        raise InvalidSecMetadataError("SEC submissions lack a recent filings object")
    filings: dict[str, Filing] = {}
    _merge_filings(filings, _filings_from_arrays(client, company, metadata["recent"]))
    for latest_date, filename in _history_pages(metadata.get("files", []), company.cik):
        selected = _latest_same_form(filings, form)
        if len(selected) == 2 and latest_date < selected[1].filed_date:
            break
        historical = client.get_json(f"{SUBMISSIONS_BASE}{filename}")
        _merge_filings(filings, _filings_from_arrays(client, company, historical))
    selected = _latest_same_form(filings, form)
    if not selected:
        raise NoFilingError(
            f"No non-amended {form or '10-K or 10-Q'} filing found for {company.ticker}"
        )
    for filing in selected:
        _primary_document(filing.primary_document)
    if len(selected) < 2:
        latest = selected[0]
        raise NoPriorFilingError(
            f"No prior non-amended {latest.form} for {company.ticker}; "
            f"latest accession is {latest.accession}. Strategy remains previous_same_form."
        )
    pair = ComparableFilingPair(current=selected[0], previous=selected[1])
    logger.info(
        "Selected %s %s pair using %s: current=%s filed=%s period=%s url=%s; "
        "previous=%s filed=%s period=%s url=%s",
        company.ticker,
        pair.current.form,
        pair.strategy,
        pair.current.accession,
        pair.current.filed_date,
        pair.current.period_of_report,
        pair.current.source_url,
        pair.previous.accession,
        pair.previous.filed_date,
        pair.previous.period_of_report,
        pair.previous.source_url,
    )
    return pair


def fetch_pair(client: SecClient, pair: ComparableFilingPair) -> ComparableFilingPair:
    """Fetch only these two primary documents, using the immutable raw cache."""
    fetched = []
    for filing in (pair.current, pair.previous):
        cik = normalize_cik(filing.cik)
        accession = normalize_accession(filing.accession)
        primary_document = _primary_document(filing.primary_document)
        expected_url = f"{ARCHIVES_BASE}{int(cik)}/{accession.replace('-', '')}/{primary_document}"
        if filing.source_url != expected_url:
            raise InvalidSecMetadataError(
                "Filing source URL does not match its SEC accession metadata"
            )
        client.get_bytes(expected_url, permanent=True)
        fetched.append(
            filing.model_copy(
                update={"local_path": client.cache_path(expected_url, permanent=True)}
            )
        )
    return ComparableFilingPair(current=fetched[0], previous=fetched[1], strategy=pair.strategy)
