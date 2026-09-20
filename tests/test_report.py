"""Report regressions protect untrusted content and missing-evidence review."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from radar.models import (
    AlignedPair,
    AlignmentAlternative,
    AlignmentEvidence,
    AnalysisRun,
    CompanyAnalysis,
    ComparableFilingPair,
    Filing,
    FilingParagraph,
    JevSemanticSignals,
    ParagraphContext,
    RankedDelta,
)
from radar.report import render_report


def filing(accession, url="https://www.sec.gov/Archives/example.htm"):
    return Filing(
        ticker="TEST",
        cik="0000000001",
        company_name="Synthetic company",
        form="10-K",
        accession=accession,
        filed_date=date(2025, 2, 1),
        period_of_report=date(2024, 12, 31),
        primary_document="example.htm",
        source_url=url,
        local_path=Path("unused.htm"),
    )


def paragraph(text, section="Risk factors"):
    return FilingParagraph(
        filing_accession="fixture",
        paragraph_id="paragraph",
        section=section,
        item="1A",
        ordinal=1,
        text=text,
        normalized_text=text,
        text_hash="fixture",
    )


def delta(old="Previous disclosure", new="Current disclosure", **kwargs):
    return RankedDelta(
        pair=AlignedPair(old=paragraph(old), new=paragraph(new), relation="matched"),
        score=0.25,
        components={"embedding_drift": 0.25},
        **kwargs,
    )


def run_with(deltas):
    return AnalysisRun(
        run_id="synthetic-run",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        tickers=["TEST", "FAIL"],
        form="10-K",
        question_schema_version="fixture-v1",
        embedding_model="disabled",
        settings={"embedding_enabled": False, "jev_enabled": False},
        companies=[
            CompanyAnalysis(
                comparison=ComparableFilingPair(current=filing("new"), previous=filing("old")),
                counts={"matched_changes": 1, "additions": 0, "deletions": 0},
                deltas=deltas,
            )
        ],
        errors={"FAIL": "No comparable filings"},
    )


def rendered(run, tmp_path, **kwargs):
    output = tmp_path / "nested" / "report.html"
    assert render_report(run, output, **kwargs) == output
    return BeautifulSoup(output.read_text(encoding="utf-8"), "html.parser")


def test_section_transition_follows_old_to_new_not_alphabetical_order(tmp_path):
    changed = delta()
    changed.pair.old.section = "Financial Summary – November 2025"
    changed.pair.new.section = "Financial Summary – February 2026"
    soup = rendered(run_with([changed]), tmp_path)
    heading = soup.select_one(".delta h3").get_text()
    assert heading.index("November 2025") < heading.index("February 2026")


def test_report_escapes_untrusted_text_attributes_and_rebuilds_diff(tmp_path):
    attack = '"><img src=x onerror="alert(1)"><script>alert(2)</script>'
    changed = delta(old=f"Before {attack}", new=f"After {attack}")
    changed.pair.old.section = attack
    changed.pair.new.section = attack
    changed.pair.lexical_diff_html = '<svg onload="alert(3)"></svg>'
    changed.evaluation_key = attack
    changed.semantic_error = attack
    changed.pair.warnings = [attack]
    alternative_old = paragraph(f"Competing old {attack}", section=attack)
    alternative_old.paragraph_id = f"old-{attack}"
    alternative_old.item = attack
    alternative_new = paragraph(f"Competing new {attack}", section=attack)
    alternative_new.paragraph_id = f"new-{attack}"
    changed.pair.alignment = AlignmentEvidence(
        status="review",
        method=attack,
        old_context=ParagraphContext(subsection_key=attack, scope_evidence=[attack]),
        new_context=ParagraphContext(subsection_key=attack, scope_evidence=[attack]),
        components={attack: 0.12},
        alternatives=[AlignmentAlternative(old=alternative_old, new=alternative_new)],
    )
    run = run_with([changed])
    run.run_id = attack
    run.companies[0].comparison.current.company_name = attack
    run.companies[0].warnings = [attack]
    run.errors = {attack: attack}
    soup = rendered(run, tmp_path)

    assert soup.select("img, svg, iframe, object") == []
    assert len(soup.find_all("script")) == 1  # Only the static, local interaction script.
    assert all(not key.lower().startswith("on") for tag in soup.find_all() for key in tag.attrs)
    assert soup.select_one(".paragraph").get_text() == f"Before {attack}"
    assert json.loads(soup.select_one(".delta")["data-sections"]) == [attack]
    assert soup.select_one(".diff del").get_text() == "Before"
    assert soup.select_one(".diff ins").get_text() == "After"
    assert attack in soup.select_one(".diff").get_text()
    assert attack in soup.select_one(".reference").get_text()
    alignment_text = soup.select_one(".alignment").get_text()
    assert attack in alignment_text
    assert soup.select_one(".scope-evidence li").get_text() == attack
    assert soup.select_one(".alignment-evidence tbody th").get_text() == attack.capitalize()
    alternative = soup.select_one(".alignment-alternative")
    assert [excerpt.get_text() for excerpt in alternative.select(".alternative-excerpt")] == [
        f"Competing old {attack}",
        f"Competing new {attack}",
    ]
    assert f"old-{attack}" in alternative.get_text()
    assert f"new-{attack}" in alternative.get_text()


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "http://www.sec.gov/file.htm",
        "https://sec.gov.evil.test/file",
        "https://sec.gov@evil.test/file",
        "https://evil.test@sec.gov/file",
        "https://www.sec.gov:444/file",
        "https://www.sec.gov/\nfile",
        "https://www.sec.gov\\@evil.test/file",
        "https://[invalid/file",
    ],
)
def test_non_sec_or_unsafe_source_urls_are_not_clickable(tmp_path, url):
    run = run_with([delta()])
    run.companies[0].comparison.current.source_url = url
    soup = rendered(run, tmp_path)
    assert [link["href"] for link in soup.select("a[href]")] == [
        run.companies[0].comparison.previous.source_url,
    ]


def test_valid_sec_source_url_is_escaped_without_losing_query(tmp_path):
    run = run_with([delta()])
    url = 'https://www.sec.gov/Archives/file.htm?x="&y=<review>'
    run.companies[0].comparison.current.source_url = url
    soup = rendered(run, tmp_path)
    assert soup.select("a[href]")[-1]["href"] == url
    assert soup.select("review") == []


def test_missing_semantics_and_skipped_pairs_preserve_review_counts(tmp_path):
    exact = delta(old="Unchanged", new="Unchanged")
    exact.pair.skip_reason = "exact"
    changed = delta(semantic_error="Fixture provider unavailable")
    added = RankedDelta(
        pair=AlignedPair(old=None, new=paragraph("New capacity disclosure"), relation="added"),
        score=0.1,
        components={"section_boost": 0.1},
    )
    run = run_with([exact, changed, added])
    run.companies[0].counts = {"matched_changes": 1, "additions": 1, "deletions": 0}
    soup = rendered(run, tmp_path, top_n=1)
    cards = soup.select(".delta")
    assert len(cards) == 1
    assert cards[0]["data-rank"] == "1"
    assert cards[0]["data-cosine"] == cards[0]["data-economic"] == ""
    assert [text.get_text() for text in cards[0].select(".paragraph")] == [
        "Previous disclosure",
        "Current disclosure",
    ]
    assert "Fixture provider unavailable" in cards[0].get_text()
    assert "0.0000" not in cards[0].select_one(".metrics").get_text()
    assert soup.select_one("[data-visible-count]").get_text() == "1"
    counts = soup.select_one(".counts").get_text()
    assert "Eligible deltas: 2" in counts
    assert "Additions: 1" in counts
    assert "FAIL" in soup.select_one(".run-errors").get_text()
    assert all(soup.find("label", attrs={"for": select["id"]}) for select in soup.select("select"))


def test_normalized_signals_and_probability_labels_are_visible_and_safe(tmp_path):
    attack = '<img src=x onerror="alert(1)">'
    signals = JevSemanticSignals(
        question_schema_version="fixture-v1",
        same_underlying_meaning=0.15,
        plausibly_economically_consequential=0.82,
        liquidity_or_financing_direction=attack,
        liquidity_or_financing_probabilities={attack: 0.71, "unchanged": 0.29},
    )
    run = run_with([delta(signals=signals, evaluation_key="evaluation-fixture")])
    soup = rendered(run, tmp_path)
    card = soup.select_one(".delta")
    assert card["data-economic"] == "0.82"
    assert soup.select("img") == []
    text = card.get_text()
    for value in ["0.1500", "0.8200", "0.7100", "0.2900", attack, "evaluation-fixture"]:
        assert value in text


def test_domain_filters_only_flag_affirmative_presence_or_directional_change(tmp_path):
    signals = JevSemanticSignals(
        question_schema_version="fixture-v1",
        liquidity_or_financing_direction="introduced_or_increased",
        supply_or_capacity_direction="unchanged_or_not_present",
        customer_concentration_direction="not_present",
        regulatory_or_government_direction="unclear",
    )
    soup = rendered(run_with([delta(signals=signals)]), tmp_path)
    assert json.loads(soup.select_one(".delta")["data-domains"]) == ["liquidity_or_financing"]
    options = soup.select('[data-control="domain"] option')
    assert [option["value"] for option in options] == ["", "liquidity_or_financing"]


def test_alignment_evidence_exposes_counterfactual_and_unmatched_sides(tmp_path):
    changed = delta()
    changed.pair.alignment = AlignmentEvidence(
        status="review",
        candidate_tier="item",
        score=0.8123,
        components={"lexical": 0.6123, "position": 0.2},
        assignment_margin=0.003214,
        old_context=ParagraphContext(
            subsection_key="results-of-operations",
            reporting_scope="quarter",
            scope_evidence=["For the three months ended November 30, 2025"],
            subsection_position=0.3,
        ),
        new_context=ParagraphContext(
            subsection_key="results-of-operations",
            reporting_scope="year_to_date",
            scope_evidence=["For the nine months ended November 30, 2026"],
            subsection_position=0.4,
        ),
        review_reasons=["near_tied_assignment", "scope_unclear"],
        alternatives=[
            AlignmentAlternative(
                old=paragraph("Other old disclosure"),
                new=paragraph("Other new disclosure"),
                score=0.8091,
                cosine_similarity=0.9123,
            ),
            AlignmentAlternative(old=paragraph("Unmatched old disclosure")),
            AlignmentAlternative(new=paragraph("Unmatched new disclosure")),
        ],
    )
    soup = rendered(run_with([changed]), tmp_path)
    alignment = soup.select_one(".alignment")
    evidence = alignment.select_one(".alignment-evidence").get_text()
    assert "Risk factors" in evidence
    assert "results-of-operations" in evidence
    assert "Quarter" in evidence and "Year to date" in evidence
    assert "For the three months ended November 30, 2025" in evidence
    assert "For the nine months ended November 30, 2026" in evidence
    assert "+0.612300" in evidence and "+0.200000" in evidence
    assert "0.8123" in alignment.get_text() and "0.003214" in alignment.get_text()
    reasons = alignment.select(".alignment-reasons li")
    assert "competing assignment" in reasons[0].get_text()
    assert "scope" in reasons[1].get_text()
    assert len(reasons) == 2  # Do not invent a split/merge explanation.
    alternatives = alignment.select(".alignment-alternative")
    assert len(alternatives) == 3
    assert [part.get_text() for part in alternatives[0].select(".alternative-excerpt")] == [
        "Other old disclosure",
        "Other new disclosure",
    ]
    assert "0.8091" in alternatives[0].get_text()
    assert "0.9123" in alternatives[0].get_text()
    assert len(alternatives[1].select(".absent")) == 1
    assert len(alternatives[2].select(".absent")) == 1
    assert "0.0000" not in alternatives[1].get_text()


def test_alignment_filter_includes_review_unmatched_and_unavailable_per_company(tmp_path):
    review = delta()
    review.pair.alignment = AlignmentEvidence(status="review")
    aligned = delta()
    aligned.pair.alignment = AlignmentEvidence(status="aligned")
    unmatched = RankedDelta(
        pair=AlignedPair(
            old=None,
            new=paragraph("Added disclosure"),
            relation="added",
            alignment=AlignmentEvidence(status="unmatched"),
        ),
        score=0.1,
        components={"section_boost": 0.1},
    )
    historical = delta()
    historical.pair.cosine_similarity = 0.99
    run = run_with([review, aligned, unmatched, historical])
    other_company = run.companies[0].model_copy(deep=True)
    other_company.deltas = [historical]
    run.companies.append(other_company)
    soup = rendered(run, tmp_path)
    companies = soup.select(".company")
    assert [
        [card["data-alignment"] for card in company.select(".delta")] for company in companies
    ] == [
        ["review", "aligned", "unmatched", "unavailable"],
        ["unavailable"],
    ]
    assert not any(card.has_attr("hidden") for card in soup.select(".delta"))
    controls = soup.select('[data-control="alignment"]')
    assert controls[0]["id"] != controls[1]["id"]
    for company, control in zip(companies, controls, strict=True):
        assert company.find("label", attrs={"for": control["id"]}) is not None
        assert [option["value"] for option in control.select("option")] == [
            "",
            "review",
            "aligned",
            "unmatched",
            "unavailable",
        ]
        assert company.select_one("[data-no-results]").has_attr("hidden")
    historical_card = companies[1].select_one(".delta")
    assert "Unavailable" in historical_card.select_one(".alignment h4").get_text()
    assert historical_card.select(".alignment-evidence, .alignment-alternative") == []
    assert "0.99" not in historical_card.select_one(".alignment").get_text()
    assert [value.get_text() for value in soup.select("[data-visible-count]")] == ["4", "1"]


def test_no_comparisons_still_writes_complete_error_report(tmp_path):
    run = run_with([])
    run.companies = []
    soup = rendered(run, tmp_path)
    assert soup.select(".company") == []
    assert "No comparable filings" in soup.select_one(".run-errors").get_text()
    assert soup.html is not None and soup.body is not None and soup.footer is not None


def test_invalid_top_n_does_not_replace_existing_report(tmp_path):
    output = tmp_path / "report.html"
    output.write_text("previous report", encoding="utf-8")
    with pytest.raises(ValueError):
        render_report(run_with([delta()]), output, top_n=0)
    assert output.read_text(encoding="utf-8") == "previous report"
