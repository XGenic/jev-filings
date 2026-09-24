"""Report regressions protect untrusted content and missing-evidence review."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from radar.jev.questions import BUSINESS_QUESTIONS, QUESTION_SCHEMA_VERSION
from radar.models import (
    AlignedPair,
    AlignmentAlternative,
    AlignmentEvidence,
    AnalysisRun,
    BusinessAssessment,
    CompanyAnalysis,
    ComparableFilingPair,
    ComparisonEvidence,
    FactChange,
    Filing,
    FilingParagraph,
    JevSemanticSignals,
    ParagraphContext,
    RankedDelta,
    SourceFact,
    SourceTable,
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


def assessed_delta(name, band="low", reliability="supported", direction="positive"):
    judgments = {
        key: {"choice": "unclear", "probabilities": {"unclear": 1.0}} for key in BUSINESS_QUESTIONS
    }
    judgments["business_direction"] = {"choice": direction, "probabilities": {direction: 1.0}}
    judgments["business_impact"] = {"choice": band, "probabilities": {band: 1.0}}
    judgments["comparison_validity"] = {
        "choice": "comparable" if reliability == "supported" else "unclear",
        "probabilities": {"comparable" if reliability == "supported" else "unclear": 1.0},
    }
    return delta(
        old=f"Previous {name}",
        new=f"Current {name}",
        signals=JevSemanticSignals(
            question_schema_version=QUESTION_SCHEMA_VERSION,
            assessment=BusinessAssessment(**judgments),
        ),
        impact_band=band,
        priority_band=band if reliability == "supported" else "review",
        comparison_reliability=reliability,
        evaluation_key=name,
    )


def numeric_evidence(period_end=date(2025, 12, 31), entity="Synthetic company"):
    previous = SourceFact(
        fact_id="old-revenue",
        filing_accession="old",
        concept="us-gaap:Revenues",
        label="Revenue",
        value=Decimal("100"),
        unit="USD",
        entity=entity,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        quote="Revenue was $100.",
        source_anchor="old-fact",
    )
    current = previous.model_copy(
        update={
            "fact_id": "new-revenue",
            "filing_accession": "new",
            "value": Decimal("120"),
            "period_start": date(2025, 1, 1),
            "period_end": period_end,
            "quote": "Revenue was $120.",
            "source_anchor": "new-fact",
        }
    )
    return ComparisonEvidence(
        basis="year_over_year",
        basis_reason="Same entity, metric, unit and annual scope.",
        changes=[
            FactChange(
                metric="Revenue",
                previous=previous,
                current=current,
                basis="year_over_year",
                absolute_change=Decimal("20"),
                percent_change=Decimal("20"),
            )
        ],
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
    context = paragraph(f"Surrounding {attack}", section=attack)
    context.item = attack
    context.paragraph_id = attack
    context.filing_accession = attack
    changed.source_context = {"previous": [context], "current": [context]}
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
    assert [passage.get_text() for passage in soup.select(".source-context .paragraph")] == [
        f"Surrounding {attack}",
        f"Surrounding {attack}",
    ]
    assert attack in soup.select_one(".source-context .small").find_next("p").get_text()


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


def test_business_assessment_renders_nested_judgments_without_trusting_model_text(tmp_path):
    attack = '"><img src=x onerror="alert(1)"><script>alert(2)</script>'
    judgments = {
        name: {
            "choice": "unclear",
            "probabilities": dict.fromkeys(question["criteria"], 0.0) | {"unclear": 1.0},
        }
        for name, question in BUSINESS_QUESTIONS.items()
    }
    judgments["business_subject"] = {
        "choice": attack,
        "probabilities": {attack: 0.75, "unclear": 0.25},
    }
    changed = delta(
        signals=JevSemanticSignals(
            question_schema_version=QUESTION_SCHEMA_VERSION,
            assessment=BusinessAssessment(**judgments),
            plausibly_economically_consequential=0.82,
        ),
        impact_band="unclear",
        priority_band="review",
        comparison_reliability="needs_review",
        impact_explanation=attack,
    )
    soup = rendered(run_with([changed]), tmp_path)

    assert soup.select("img, svg, iframe, object") == []
    assert len(soup.find_all("script")) == 1
    assert all(not key.lower().startswith("on") for tag in soup.find_all() for key in tag.attrs)
    assert attack in soup.select_one(".impact-explanation").get_text()
    assert attack.capitalize() in soup.select_one(".impact-summary").get_text()
    assert soup.select_one(".delta")["data-business_subject"] == attack
    assert (
        attack
        in soup.select_one('[data-lane="review"] [data-control="business_subject"]').get_text()
    )
    dimensions = soup.select(".impact-dimension")
    assert len(dimensions) == len(BUSINESS_QUESTIONS)
    assert "0.7500" in dimensions[0].get_text()
    assert "0.2500" in dimensions[0].get_text()
    assert "0.8200" in soup.select_one(".ranking-details").get_text()
    assert "0.2500" not in soup.select_one(".delta-heading").get_text()


def test_historical_and_missing_provider_results_do_not_infer_business_impact(tmp_path):
    historical = delta(
        signals=JevSemanticSignals(
            question_schema_version="filing-delta-1",
            plausibly_economically_consequential=0.99,
            liquidity_or_financing_direction="introduced_or_increased",
        )
    )
    missing = delta(semantic_error="Provider unavailable")
    soup = rendered(run_with([historical, missing]), tmp_path)

    for card in soup.select(".delta"):
        assert "Unavailable" in card.select_one(".delta-heading").get_text()
        assert card.select(".impact-details, .impact-explanation") == []
        assert card.select_one(".impact-unavailable") is not None
        assert "0.2500" in card.select_one(".ranking-details").get_text()
    assert "introduced_or_increased" in soup.select_one(".ranking-details").get_text()
    assert "0.9900" in soup.select_one(".ranking-details").get_text()


def test_full_priority_counts_include_review_candidates_outside_report_subset(tmp_path):
    highest = delta(priority_band="high", impact_band="high", comparison_reliability="supported")
    review = delta(priority_band="review", impact_band="high", comparison_reliability="unmatched")
    unavailable = delta()
    skipped = delta(priority_band="review")
    skipped.pair.skip_reason = "exact"
    soup = rendered(run_with([highest, review, unavailable, skipped]), tmp_path, top_n=1)

    assert len(soup.select(".delta")) == 1
    assert soup.select_one('[data-priority-count="high"]').get_text().endswith(": 1")
    assert soup.select_one('[data-priority-count="review"]').get_text().endswith(": 1")
    assert soup.select_one('[data-priority-count="unavailable"]').get_text().endswith(": 1")


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


def test_review_reservation_is_total_budget_and_yields_to_supported_material_changes(tmp_path):
    low = [assessed_delta(f"low-{index}") for index in range(10)]
    review = assessed_delta("unmatched-review", band="high", reliability="unmatched")
    soup = rendered(run_with([*low, review]), tmp_path, top_n=4)
    assert len(soup.select(".delta")) == 4
    assert len(soup.select('[data-lane="supported"] .delta')) == 3
    assert "unmatched-review" in soup.select_one('[data-lane="review"] .delta').get_text()
    assert "Current low-3" not in soup.get_text()
    counts = soup.select_one(".selection-counts")
    assert counts["data-candidate-passages"] == counts["data-total-groups"] == "11"
    assert counts["data-included-passages"] == "4"
    assert counts["data-omitted-groups"] == "7"

    material = [
        assessed_delta("medium", band="medium"),
        assessed_delta("high", band="high"),
    ]
    soup = rendered(run_with([*low, review, *material]), tmp_path, top_n=2)
    assert len(soup.select(".delta")) == 2
    assert soup.select('[data-lane="review"] .delta') == []
    references = [node.get_text() for node in soup.select(".reference code")]
    assert references == ["medium", "high"]  # Original order within the supported lane.


def test_unavailable_semantics_never_enter_supported_lane_even_with_saved_labels(tmp_path):
    missing = delta(
        comparison_evidence=ComparisonEvidence(basis="unclear", basis_reason="No facts."),
        priority_band="high",
        impact_band="high",
        comparison_reliability="supported",
        semantic_error="Provider unavailable",
    )
    soup = rendered(run_with([missing]), tmp_path)
    assert soup.select('[data-lane="supported"] .delta') == []
    card = soup.select_one('[data-lane="review"] .delta')
    assert card["data-group-reliability"] == "unavailable"
    assert card["data-comparison_reliability"] == card["data-impact_band"] == "unavailable"
    assert card.select_one(".impact-unavailable") is not None


def test_fact_groups_preserve_members_and_do_not_merge_periods_entities_or_transactions(tmp_path):
    first = assessed_delta("revenue-table", band="high")
    first.comparison_evidence = numeric_evidence()
    duplicate = assessed_delta(
        "revenue-narrative", band="high", reliability="needs_review", direction="negative"
    )
    duplicate.comparison_evidence = numeric_evidence()
    other_period = assessed_delta("another-period", band="high")
    other_period.comparison_evidence = numeric_evidence(period_end=date(2025, 9, 30))
    other_entity = assessed_delta("another-entity", band="high")
    other_entity.comparison_evidence = numeric_evidence(entity="Separate subsidiary")
    first_deal = assessed_delta("acquisition of Alpha", band="high")
    first_deal.comparison_evidence = numeric_evidence()
    second_deal = assessed_delta("acquisition of Beta", band="high")
    second_deal.comparison_evidence = numeric_evidence()
    soup = rendered(
        run_with([first, duplicate, other_period, other_entity, first_deal, second_deal]),
        tmp_path,
    )
    assert len(soup.select(".delta")) == 5
    grouped = soup.select_one('[data-lane="review"] .delta')
    assert grouped["data-group-reliability"] == "needs_review"
    assert "Negative" in grouped.select_one(".group-summary").get_text()
    assert "Supported" in grouped.select_one(".group-summary").get_text()
    assert "Needs review" in grouped.select_one(".group-summary").get_text()
    assert grouped.select_one(".group-members .group-member") is not None
    for changed in [first, duplicate]:
        assert changed.pair.old.text in grouped.get_text()
        assert changed.pair.new.text in grouped.get_text()
        assert changed.evaluation_key in grouped.get_text()
    assert len(grouped.select(".impact-details")) == 2
    assert len(grouped.select(".comparison-evidence")) == 2
    assert soup.select_one(".selection-counts")["data-included-passages"] == "6"
    for name in ["another-period", "another-entity", "acquisition of Alpha", "acquisition of Beta"]:
        assert name in soup.select_one('[data-lane="supported"]').get_text()


@pytest.mark.parametrize("difference", ["qualification", "untagged_amount"])
def test_equal_tagged_totals_do_not_hide_other_economic_changes(tmp_path, difference):
    first = assessed_delta("revenue", band="high")
    first.comparison_evidence = numeric_evidence()
    first.pair.old.text = "Revenue was $100 and capital spending was $10."
    first.pair.new.text = "Revenue was $120 and capital spending was $10."
    other = first.model_copy(deep=True)
    if difference == "qualification":
        other.pair.new.text += " The principal customer cannot settle its receivables."
    else:
        other.pair.new.text = "Revenue was $120 and capital spending was $30."
    soup = rendered(run_with([first, other]), tmp_path)
    assert len(soup.select(".delta")) == 2
    assert not soup.select(".group-member")


def test_exact_event_duplicates_require_matching_dates_and_scope(tmp_path):
    first = assessed_delta("unused")
    first.pair.old.text = (
        "During 2024 we signed an agreement to purchase the Alpha facility for $10 million."
    )
    first.pair.new.text = (
        "During 2025 we completed the agreement to purchase the Alpha facility for $10 million."
    )
    first.pair.alignment = AlignmentEvidence(
        status="aligned",
        old_context=ParagraphContext(reporting_scope="annual", scope_evidence=["Year 2024"]),
        new_context=ParagraphContext(reporting_scope="annual", scope_evidence=["Year 2025"]),
    )
    duplicate = first.model_copy(deep=True)
    duplicate.evaluation_key = "duplicate-event"
    different_date = first.model_copy(deep=True)
    different_date.pair.new.text = different_date.pair.new.text.replace("2025", "2026")
    different_scope = first.model_copy(deep=True)
    different_scope.pair.alignment.new_context.reporting_scope = "year_to_date"
    soup = rendered(run_with([first, duplicate, different_date, different_scope]), tmp_path)
    assert len(soup.select(".delta")) == 3
    assert len(soup.select(".group-member")) == 1
    assert "2026" in soup.get_text()
    assert "duplicate-event" in soup.select_one(".group-members").get_text()


def test_historical_ranking_remains_flat_without_grouping_or_band_inference(tmp_path):
    first = delta(old="Annual sales in 2024 were $100", new="Annual sales in 2025 were $120")
    first.score = 0.1
    first.evaluation_key = "first-persisted"
    duplicate = first.model_copy(deep=True)
    duplicate.score = 0.9
    duplicate.evaluation_key = "second-persisted"
    soup = rendered(run_with([first, duplicate]), tmp_path, top_n=2)
    assert len(soup.select('[data-lane="historical"] .delta')) == 2
    assert soup.select(".group-members") == []
    assert [node.get_text() for node in soup.select(".reference code")] == [
        "first-persisted",
        "second-persisted",
    ]
    assert [node["data-rank"] for node in soup.select(".delta")] == ["1", "2"]
    assert len(soup.select(".impact-unavailable")) == 2


def test_previous_rubric_is_not_replaced_with_current_criteria(tmp_path):
    changed = assessed_delta("older-rubric", band="high")
    changed.signals.question_schema_version = "filing-delta-2"
    changed.impact_explanation = "Persisted explanation from the original evaluation."
    soup = rendered(run_with([changed]), tmp_path)
    detail = soup.select_one(".impact-details").get_text()
    assert "filing-delta-2" in detail
    assert all(
        criterion not in detail
        for question in BUSINESS_QUESTIONS.values()
        for criterion in question["criteria"].values()
    )
    assert changed.impact_explanation in soup.select_one(".impact-explanation").get_text()
    assert "High" in soup.select_one(".delta-heading").get_text()
    assert "1.0000" in detail


def test_comparison_evidence_is_source_cited_escaped_and_keeps_noncontiguous_rows(tmp_path):
    attack = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    changed = assessed_delta("numeric-review")
    evidence = numeric_evidence()
    evidence.basis_reason = attack
    evidence.changes[0].current.quote = attack
    evidence.changes[0].current.source_anchor = attack
    evidence.changed_spans = {"previous": ["$100"], "current": [attack]}
    evidence.context_facts = {"current": [evidence.changes[0].current]}
    evidence.tables = {
        "current": [
            SourceTable(
                table_id="revenue-table",
                filing_accession="new",
                caption=attack,
                rows=[["Fiscal year", "2025", "2024"], [attack, "120", "100"]],
                row_indices=[0, 8],
                cell_spans=[[(2, 1), (1, 2), (1, 1)], [(1, 1)] * 3],
                header_cells=[[True, True, True], [False, False, False]],
                source_anchor=attack,
            )
        ]
    }
    evidence.counterparts = {"previous": [paragraph(attack)]}
    evidence.uncertainties = [attack]
    evidence.possible_channels = [attack]
    changed.comparison_evidence = evidence
    soup = rendered(run_with([changed]), tmp_path)
    assert soup.select("img, svg, iframe, object") == []
    assert len(soup.select("script")) == 1
    assert all(not key.lower().startswith("on") for tag in soup.find_all() for key in tag.attrs)
    panel = soup.select_one(".comparison-evidence")
    assert "120 − (100) = 20 USD" in panel.select_one(".arithmetic").get_text()
    for value in ["2024-01-01", "2024-12-31", "2025-01-01", "2025-12-31", "USD"]:
        assert value in panel.select_one(".fact-changes").get_text()
    assert attack in panel.select_one(".changed-spans").get_text()
    assert attack in panel.select_one(".counterpart-candidates").get_text()
    table = panel.select_one(".source-table table")
    assert [cell.get_text() for cell in table.select("tbody th")] == ["0", "8"]
    assert table.select("[rowspan], [colspan]") == []  # Never bridge omitted source rows.
    assert "120" in table.get_text() and "100" in table.get_text()
    assert all(
        link["href"].startswith("https://www.sec.gov/Archives/") for link in panel.select("a[href]")
    )
    assert "%3Cimg" in panel.select("a[href]")[1]["href"]
