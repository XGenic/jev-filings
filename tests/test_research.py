"""Research boundary regressions: blinding, source provenance and safe review."""

from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from radar.models import AlignedPair, AlignmentResult, ComparableFilingPair, Filing, FilingParagraph
from radar.research import (
    RESEARCH_SCHEMA_VERSION,
    build_packet,
    render_research_report,
    validate_screening,
)


def paragraph(accession, paragraph_id, text, ordinal=1):
    return FilingParagraph(
        filing_accession=accession,
        paragraph_id=paragraph_id,
        ordinal=ordinal,
        item="7",
        section="Operations",
        text=text,
        normalized_text=text,
        text_hash="fixture",
    )


def screening():
    def filing(accession):
        return Filing(
            ticker="TEST",
            cik="0000000001",
            company_name="Synthetic company",
            form="10-K",
            accession=accession,
            filed_date=date(2025, 2, 1),
            period_of_report=date(2024, 12, 31),
            primary_document="filing.htm",
            source_url="https://www.sec.gov/Archives/filing.htm",
            local_path=Path("private-cache") / accession,
        )

    comparison = ComparableFilingPair(previous=filing("old"), current=filing("new"))
    old = [paragraph("old", "shared-id", "We leased 10,000 square feet for existing operations.")]
    new = [paragraph("new", "shared-id", "We leased 20,000 square feet for capacity expansion.")]
    pair = AlignedPair(old=old[0], new=new[0], relation="matched")
    alignment = AlignmentResult(pairs=[pair], embedding_model="disabled")
    packet = build_packet(comparison, old, new, alignment)
    response = {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "assessments": [
            {
                "id": packet["candidates"][0]["id"],
                "category": "preparatory_development",
                "priority": "follow_up",
                "comparison": "sound",
                "observation": "The leased area increased from 10,000 to 20,000 square feet.",
                "why_it_matters": "Space could support capacity if equipped and staffed.",
                "alternative_explanation": "The lease could replace an expiring existing facility.",
                "uncertainty": "Equipment, staffing, utilization and customer demand are unknown.",
                "evidence": [
                    {"side": "previous", "paragraph_id": "shared-id", "quote": old[0].text},
                    {"side": "current", "paragraph_id": "shared-id", "quote": new[0].text},
                ],
            }
        ],
    }
    return comparison, old, new, alignment, packet, response


def test_packet_blinds_scores_and_paths_without_hiding_distant_counterparts():
    comparison, old, new, alignment, _, _ = screening()
    old.extend(
        [
            paragraph("old", "neighbor", "Operating expenses remained stable.", 2),
            paragraph("old", "distant", "We also leased 20,000 square feet in another region.", 3),
        ]
    )
    packet = build_packet(comparison, list(reversed(old)), new, alignment)
    alignment.pairs[0].cosine_similarity = 0.99
    alignment.pairs[0].lexical_similarity = 0.95
    alignment.pairs[0].lexical_diff_html = "<script>untrusted</script>"
    comparison.previous.local_path = Path("different-private-cache")
    changed = build_packet(comparison, old, new, alignment)
    assert changed == packet
    assert [p["paragraph_id"] for p in packet["paragraphs"]["previous"]] == [
        "shared-id",
        "neighbor",
        "distant",
    ]
    assert "local_path" not in packet["comparison"]["previous"]
    assert [p["paragraph_id"] for p in packet["candidates"][0]["source_context"]["previous"]] == [
        "shared-id",
        "neighbor",
    ]
    with pytest.raises(ValueError, match="wrong filing side"):
        build_packet(comparison, new, old, alignment)


@pytest.mark.parametrize("failure", ["missing", "duplicate", "extra"])
def test_screen_requires_exactly_one_assessment_per_candidate(failure):
    *_, packet, response = screening()
    if failure == "missing":
        response["assessments"].clear()
    else:
        extra = deepcopy(response["assessments"][0])
        if failure == "extra":
            extra["id"] = "not-in-packet"
        response["assessments"].append(extra)
    with pytest.raises(ValueError, match="candidate IDs"):
        validate_screening(packet, response)


def test_quotes_allow_unicode_whitespace_but_not_wrong_side_or_changed_numbers():
    *_, packet, response = screening()
    response["assessments"][0]["evidence"][1]["quote"] = (
        "We leased ２０,０００\u00a0square\nfeet for capacity expansion."
    )
    assert validate_screening(packet, response)["assessments"][0]["usable_lead"] is True
    for wrong_quote in (
        "We leased 30,000 square feet for capacity expansion.",
        packet["paragraphs"]["previous"][0]["text"],
    ):
        response["assessments"][0]["evidence"][1]["quote"] = wrong_quote
        with pytest.raises(ValueError, match="Evidence quote is not in current"):
            validate_screening(packet, response)


def test_follow_up_requires_endpoint_support_and_comparison_uncertainty_survives():
    *_, packet, response = screening()
    unrelated = paragraph("new", "other", "Cash and cash equivalents totaled $8 million.", 2)
    packet["paragraphs"]["current"].append(unrelated.model_dump(mode="json"))
    assessment = response["assessments"][0]
    original_evidence = deepcopy(assessment["evidence"])
    assessment["evidence"] = [{"side": "current", "paragraph_id": "other", "quote": unrelated.text}]
    with pytest.raises(ValueError, match="candidate endpoint"):
        validate_screening(packet, response)
    assessment["evidence"] = original_evidence[1:]
    with pytest.raises(ValueError, match="both filings"):
        validate_screening(packet, response)
    assessment["comparison"] = "uncertain"
    validated = validate_screening(packet, response)["assessments"][0]
    assert validated["priority"] == "follow_up"
    assert validated["comparison"] == "uncertain"
    assert validated["usable_lead"] is False
    assessment["observation"] = "  "
    with pytest.raises(ValueError, match="must not be blank"):
        validate_screening(packet, response)


def test_report_escapes_sources_rebuilds_diff_and_separates_hindsight():
    comparison, old, new, alignment, _, response = screening()
    attack = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    new[0].text += " " + attack
    alignment.pairs[0].lexical_diff_html = '<svg onload="alert(3)"></svg>'
    packet = build_packet(comparison, old, new, alignment)
    response["assessments"][0]["id"] = packet["candidates"][0]["id"]
    assessment = validate_screening(packet, response)["assessments"][0]
    case = {
        "id": "case-one",
        "cohort": "discovery",
        "ticker": "TEST",
        "extra_metadata": attack,
        "comparison": packet["comparison"],
        "pair": alignment.pairs[0].model_dump(mode="json"),
        "assessment": assessment,
        "source_context": packet["paragraphs"],
        "novelty": {
            "status": "not_checked",
            "finding": attack,
            "citations": [
                {
                    "title": "Unsafe link",
                    "url": "javascript:alert(4)",
                    "published_date": None,
                }
            ],
        },
        "historical_outcome": {
            "description": attack,
            "announced_date": "2026-01-01",
            "citations": [],
        },
        "baseline": {"label": attack},
    }
    second = deepcopy(case)
    second.update(id="case-two", cohort="hindsight")
    second["assessment"]["comparison"] = "broken"
    study = {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "title": "Source review",
        "generated_at": "now",
        "protocol": {"cutoff": "2025-02-01", "extra": attack},
        "cohorts": [{"id": "discovery"}, {"id": "hindsight"}],
        "cases": [case, second],
    }
    soup = BeautifulSoup(render_research_report(study), "html.parser")
    assert soup.select("img, svg, iframe, object") == []
    assert len(soup.find_all("script")) == 1
    assert all(not key.lower().startswith("on") for tag in soup.find_all() for key in tag.attrs)
    assert all(
        "open" not in details.attrs
        for details in soup.select("details.assessment, details.historical-outcome")
    )
    assert len(soup.select(".cohort")) == 2
    assert [len(cohort.select(".case")) for cohort in soup.select(".cohort")] == [1, 1]
    assert attack in soup.select_one(".diff").get_text()
    assert soup.select_one(".diff del").get_text() == "10"
    assert all(link["href"].startswith("https://www.sec.gov/") for link in soup.select("a[href]"))
    assert "20,000 square feet" in soup.select(".filing .paragraph")[1].get_text()
    assert [option.get("value") for option in soup.select("#company-filter option")] == ["", "TEST"]


def test_prior_announcement_evidence_cannot_leak_same_day_or_future_information():
    comparison, old, new, alignment, packet, response = screening()
    case = {
        "id": "timeline",
        "cohort": "fresh",
        "ticker": "TEST",
        "comparison": packet["comparison"],
        "pair": alignment.pairs[0].model_dump(mode="json"),
        "assessment": validate_screening(packet, response)["assessments"][0],
        "source_context": packet["paragraphs"],
        "novelty": {
            "status": "previously_announced",
            "finding": "The capacity lease was already announced.",
            "citations": [
                {
                    "title": "Company announcement",
                    "url": "https://example.com/announcement",
                    "published_date": "2025-01-31",
                }
            ],
        },
        "historical_outcome": None,
        "baseline": None,
    }
    study = {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "title": "Timeline",
        "generated_at": "now",
        "protocol": {},
        "cohorts": [{"id": "fresh"}],
        "cases": [case],
    }
    assert "Company announcement" in render_research_report(study)
    for published in ("2025-02-01", "2025-02-02", None):
        case["novelty"]["citations"][0]["published_date"] = published
        with pytest.raises(ValueError, match="precede"):
            render_research_report(study)
    case["novelty"]["citations"][0].update(
        published_date="2025-02-01", published_at="2025-02-01T07:00:00-05:00"
    )
    case["filing_published_at"] = "2025-02-01T20:00:00Z"
    assert "Company announcement" in render_research_report(study)
    for timestamp in ("2025-02-01T20:00:00Z", "2025-02-01T21:00:00Z", "2025-02-01T07:00:00"):
        case["novelty"]["citations"][0]["published_at"] = timestamp
        with pytest.raises(ValueError, match="precede"):
            render_research_report(study)
    # An earlier date label must not override a known post-cutoff instant.
    case["novelty"]["citations"][0].update(
        published_date="2025-01-31", published_at="2025-02-01T21:00:00Z"
    )
    with pytest.raises(ValueError):
        render_research_report(study)
    # SEC filing dates and publishers' local dates can differ from UTC dates.
    case["novelty"]["citations"][0].update(
        published_date="2025-02-02", published_at="2025-02-02T08:00:00+09:00"
    )
    case["filing_published_at"] = "2025-02-02T01:00:00Z"
    assert "Company announcement" in render_research_report(study)
    case["novelty"]["citations"] = []
    with pytest.raises(ValueError, match="requires dated"):
        render_research_report(study)
    case["novelty"]["status"] = "not_checked"
    case["historical_outcome"] = {
        "description": "Not actually a later outcome",
        "announced_date": "2025-01-31",
        "citations": [],
    }
    with pytest.raises(ValueError, match="later than"):
        render_research_report(study)
