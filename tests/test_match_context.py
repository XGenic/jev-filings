from hashlib import sha256

import pytest

from radar.match.context import paragraph_contexts, scopes_compatible
from radar.models import FilingParagraph

TITLE = "Part I / Item 2. MANAGEMENT’S DISCUSSION AND ANALYSIS"


def paragraph(text, section=None, ordinal=0, item="Part I / Item 2"):
    return FilingParagraph(
        filing_accession="source",
        paragraph_id=f"source:{ordinal}",
        section=section,
        item=item,
        ordinal=ordinal,
        text=text,
        normalized_text=text,
        text_hash=sha256(text.encode()).hexdigest(),
    )


def context(text, heading=None):
    section = f"{TITLE} / {heading}" if heading else TITLE
    return paragraph_contexts([paragraph(text, section)])[0]


def test_date_varying_headings_keep_scope_out_of_subsection_identity():
    old = context("Sales increased.", "Financial Summary – Three Months Ended November 29, 2025")
    new = context("Sales increased.", "FINANCIAL  SUMMARY: Nine Months Ended February 28, 2026")
    assert old.subsection_key == new.subsection_key == "financial summary"
    assert old.reporting_scope == "quarter"
    assert new.reporting_scope == "year_to_date"
    assert not scopes_compatible(old, new)


def test_six_to_nine_months_are_comparable_without_date_or_fiscal_year_keys():
    old = context("Net sales increased for the first six months of fiscal 2026.", "Net Sales")
    new = context("Net sales increased for the first nine months of fiscal 2026.", "Net Sales")
    assert old.reporting_scope == new.reporting_scope == "year_to_date"
    assert scopes_compatible(old, new)
    old_heading = context("", "Net Sales for Six Months Ended 11/29/2025")
    new_heading = context("", "Net Sales for Nine Months Ended 2026-02-28")
    assert old_heading.subsection_key == new_heading.subsection_key == "net sales"
    month_heading = context("", "Net Sales for Nine Months Ended February 2026")
    assert month_heading.subsection_key == new_heading.subsection_key


def test_summary_and_detail_paths_are_not_collapsed():
    summary = context("Sales grew during the second quarter.", "Financial Summary / Net Sales")
    detail = context("Sales grew during the third quarter.", "Results of Operations / Net Sales")
    assert summary.subsection_key == "financial summary / net sales"
    assert detail.subsection_key == "results of operations / net sales"
    assert summary.reporting_scope == detail.reporting_scope == "quarter"


def test_structural_titles_do_not_invent_subsections():
    paragraphs = [
        paragraph("Business description.", TITLE),
        paragraph("Business description.", "Item 7. Management’s Discussion and Analysis"),
        paragraph("Business description.", "Part II"),
        paragraph("Business description."),
    ]
    assert [entry.subsection_key for entry in paragraph_contexts(paragraphs)] == [None] * 4


def test_unicode_spacing_and_fiscal_dates_normalize_without_erasing_numeric_names():
    old = context("", "ＬＥＡＳＥＳ — ASC 842: Fiscal Years 2025 and 2026")
    new = context("", "Leases - ASC 842: Fiscal Years 2026 and 2027")
    different_standard = context("", "Leases - ASC 840: Fiscal Year 2027")
    assert old.subsection_key == new.subsection_key == "leases asc 842"
    assert different_standard.subsection_key != old.subsection_key


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Net sales during the second quarter of fiscal 2026 were $52.3 million.", "quarter"),
        ("Gross margin increased to 31.9% during the third quarter of fiscal 2026.", "quarter"),
        ("Revenue for the three months ended May 31, 2026 was $12.9 million.", "quarter"),
        ("Revenue increased 3.6% for the nine months ended February 28, 2026.", "year_to_date"),
        ("Year-to-date net income was $3 million.", "year_to_date"),
        ("Revenue for fiscal 2026 was $120 million.", "annual"),
        ("Full-year results included a net loss.", "annual"),
        ("Revenue for the twelve months ended May 31, 2026 was $100 million.", "annual"),
        ("Twelve-month year results included a net loss.", "annual"),
        ("Cash and cash equivalents were $20 million as of May 31, 2026.", "point_in_time"),
        ("At February 28, 2026, our inventory balance was $20 million.", "point_in_time"),
        ("Our cash runway is twelve months.", "unknown"),
        ("Our $20 million debt matures at February 28, 2027.", "unknown"),
        ("Our contract expires in fiscal year 2027.", "unknown"),
        ("Founded in 1986, we began selling this product in 2025.", "unknown"),
        ("We do not expect revenue to increase during the third quarter.", "unknown"),
        ("Revenue may increase for the next twelve months.", "unknown"),
        ("Revenue did not increase during the third quarter.", "quarter"),
        ("Our debt is not due for nine months.", "unknown"),
        (
            "We reported revenue for three months ended February 28, 2026, not for nine months.",
            "quarter",
        ),
        ("Revenue rose during the third quarter, and our cash runway is twelve months.", "quarter"),
    ],
)
def test_scope_tracks_reporting_subject_not_numbers_dates_or_negation(text, expected):
    assert context(text).reporting_scope == expected


def test_body_subject_overrides_inherited_heading_but_preserves_both_sources():
    entry = context(
        "Net sales for the first nine months of fiscal 2026 increased by 4.2%.",
        "Financial Summary – Three Months Ended February 28, 2026",
    )
    assert entry.reporting_scope == "year_to_date"
    assert any(value.startswith("heading: quarter:") for value in entry.scope_evidence)
    assert any(value.startswith("body: year_to_date:") for value in entry.scope_evidence)


@pytest.mark.parametrize(
    ("text", "expected_scopes"),
    [
        (
            "Revenue for the three and nine months ended February 28, 2026 increased.",
            {"quarter", "year_to_date"},
        ),
        (
            "Net sales rose in the third quarter. Net income for the first nine months decreased.",
            {"quarter", "year_to_date"},
        ),
        (
            (
                "Revenue increased for three months ended February 28, 2026. "
                "Cash was $5 million as of February 28, 2026."
            ),
            {"quarter", "point_in_time"},
        ),
    ],
)
def test_conflicting_reporting_subjects_remain_inspectable(text, expected_scopes):
    entry = context(text, "Financial Summary – Three Months Ended February 28, 2026")
    assert entry.reporting_scope == "mixed"
    body_scopes = {
        value.split(": ")[1] for value in entry.scope_evidence if value.startswith("body:")
    }
    assert body_scopes == expected_scopes
    assert scopes_compatible(entry, context("Full-year results included a net loss."))


def test_future_statement_does_not_erase_observed_reporting_subject():
    entry = context(
        "Revenue increased 3.6% during the third quarter, and we expect growth for the next twelve"
        " months."
    )
    assert entry.reporting_scope == "quarter"
    assert all("annual" not in value for value in entry.scope_evidence)


def test_unknown_scope_does_not_block_known_scope():
    unknown = context("We manufacture electronic components.")
    annual = context("Revenue for fiscal 2026 was $120 million.")
    quarter = context("Sales rose during the third quarter.")
    assert scopes_compatible(unknown, annual)
    assert scopes_compatible(annual, unknown)
    assert not scopes_compatible(annual, quarter)
    assert not scopes_compatible(quarter, annual)


def test_positions_are_within_item_and_normalized_subsection_and_do_not_mutate_sources():
    source = [
        paragraph("First summary.", f"{TITLE} / Summary – Three Months Ended May 31, 2025", 10),
        paragraph("Other item.", "Item 7 / Summary", 3, "Item 7"),
        paragraph("Separate detail.", f"{TITLE} / Net Sales", 20),
        paragraph("Second summary.", f"{TITLE} / Summary – Nine Months Ended May 31, 2026", 2),
        paragraph("Final summary.", f"{TITLE} / Summary", 1),
    ]
    original = [entry.model_dump() for entry in source]
    contexts = paragraph_contexts(source)
    assert [entry.subsection_position for entry in contexts] == [0.0, 0.5, 0.5, 0.5, 1.0]
    assert paragraph_contexts(source) == contexts
    assert [entry.model_dump() for entry in source] == original
    contexts[0].scope_evidence.append("local context edit")
    assert [entry.model_dump() for entry in source] == original
    assert paragraph_contexts([]) == []


def test_historical_transaction_date_does_not_change_reporting_subject():
    entry = context(
        "PMT net sales decreased during the first six months of fiscal 2026. "
        "The decline in healthcare sales was due to the sale of assets in the third quarter "
        "of fiscal 2025."
    )
    assert entry.reporting_scope == "year_to_date"
    tax = context(
        "The difference in tax rate during the first nine months of fiscal 2026 reflects "
        "the nonrecurring asset sale loss in the third quarter of fiscal 2025."
    )
    assert tax.reporting_scope == "year_to_date"
    mixed = context(
        "Sales increased during the first nine months due to an asset sale, "
        "and gross margins increased during the third quarter."
    )
    assert mixed.reporting_scope == "mixed"


def test_fiscal_year_reference_is_not_an_annual_reporting_period():
    entry = context(
        "Other income during the first nine months of fiscal 2026 totaled $0.7 million, "
        "compared to other expense for the first nine months of fiscal 2025."
        "The increase from fiscal 2025 was due to a non-recurring gain."
    )
    assert entry.reporting_scope == "year_to_date"
    actual_annual_comparison = context(
        "Revenue for the third quarter and for fiscal 2026 increased."
    )
    assert actual_annual_comparison.reporting_scope == "mixed"
