"""Behavioral regression cases use synthetic HTML, never fabricated SEC records."""

from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest

from radar.models import Filing
from radar.parse import extract_paragraphs, parse_filing

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_paragraphs(name: str):
    return extract_paragraphs((FIXTURES / name).read_text(encoding="utf-8"), "synthetic-accession")


def test_nested_and_malformed_layout_does_not_duplicate_or_drop_narrative():
    paragraphs = fixture_paragraphs("synthetic_layout.html")
    texts = [paragraph.normalized_text for paragraph in paragraphs]
    assert texts == [
        "Direct layout-container narrative remains visible without a paragraph wrapper.",
        "Nested paragraph narrative must appear once, not again in its parent container.",
        "Trailing layout-container narrative remains visible after its nested child.",
        "Malformed opening paragraph remains separate from the following layout block.",
        "Malformed nested block remains a distinct narrative after HTML repair.",
        "Visible inline XBRL disclosure remains in the paragraph, with supply intact.",
        (
            "Single line breaks preserve the entire sentence rather than dropping its short opening"
            " fragment."
        ),
        "Double line breaks begin another independent narrative paragraph in the layout.",
        "Repeated disclosure in distinct source blocks must retain both source positions.",
        "Repeated disclosure in distinct source blocks must retain both source positions.",
    ]
    assert paragraphs[-1].paragraph_id != paragraphs[-2].paragraph_id
    assert paragraphs[-1].text_hash == paragraphs[-2].text_hash


def test_hidden_metadata_and_navigation_are_not_visible_but_inline_xbrl_is():
    paragraphs = fixture_paragraphs("synthetic_layout.html")
    text = " ".join(paragraph.text for paragraph in paragraphs)
    assert "inline XBRL disclosure" in text
    for forbidden in (
        "Navigation",
        "Script payload",
        "Stylesheet-hidden",
        "Attribute-hidden",
        "Visibility-hidden",
        "ARIA-hidden",
        "Hidden XBRL",
    ):
        assert forbidden not in text


def test_part_item_and_subheading_boundaries_exclude_toc_and_financial_tables():
    paragraphs = fixture_paragraphs("synthetic_sections.html")
    by_text = {paragraph.normalized_text: paragraph for paragraph in paragraphs}
    intro = by_text[
        "Introductory narrative before the real headings must not inherit a contents entry."
    ]
    assert intro.item is None
    assert intro.section is None
    statements = by_text[
        "Our interim financial statements reflect the reporting period and accompanying notes."
    ]
    assert statements.item == "Part I / Item 1"
    assert statements.section == "Part I / Item 1. Financial Statements"
    liquidity = by_text[
        "We have sufficient liquidity to fund operating needs throughout the next fiscal year."
    ]
    assert (
        liquidity.section
        == "Part I / Item 2. Management's Discussion and Analysis / Liquidity and Capital Resources"
    )
    assert (
        by_text[
            "We renewed our revolving credit facility and extended its maturity by three years."
        ].section
        == liquidity.section
    )
    reference = by_text[
        "Item 1 of Part I contains our financial statements and the accompanying accounting notes."
    ]
    assert reference.item == "Part I / Item 2"
    proceedings = by_text[
        "We are defending litigation that could result in additional settlement expenses."
    ]
    assert proceedings.item == "Part II / Item 1"
    assert proceedings.item != statements.item
    customer = by_text[
        "Our largest customer represents a substantial portion of consolidated annual revenue."
    ]
    assert customer.section == "Part II / Item 1A. RISK FACTORS / Customer Concentration"
    final = by_text[
        "No material unregistered equity transactions occurred during this reporting period."
    ]
    assert final.section == "Part II / Item 2. Unregistered Sales of Equity Securities"
    assert set(by_text) == {
        intro.text,
        statements.text,
        liquidity.text,
        reference.text,
        proceedings.text,
        customer.text,
        final.text,
        "We renewed our revolving credit facility and extended its maturity by three years.",
    }


def test_unicode_equivalence_has_stable_hashes_but_preserves_original_text():
    html = (FIXTURES / "synthetic_unicode.html").read_text(encoding="utf-8")
    paragraphs = extract_paragraphs(html, "first")
    expected = "Our financing at Café faces supplychain pressures and uncertain costs."
    assert [paragraph.normalized_text for paragraph in paragraphs] == [expected, expected]
    assert "ﬁnancing" in paragraphs[0].text
    assert "\u00a0" in paragraphs[0].text
    assert "Cafe\u0301" in paragraphs[0].text
    assert paragraphs[0].text != paragraphs[1].text
    assert (
        paragraphs[0].text_hash
        == paragraphs[1].text_hash
        == sha256(expected.encode("utf-8")).hexdigest()
    )
    assert paragraphs == extract_paragraphs(html, "first")
    other_filing = extract_paragraphs(html, "second")
    assert [paragraph.text_hash for paragraph in paragraphs] == [
        paragraph.text_hash for paragraph in other_filing
    ]
    assert {paragraph.paragraph_id for paragraph in paragraphs}.isdisjoint(
        paragraph.paragraph_id for paragraph in other_filing
    )


def test_configured_threshold_does_not_drop_short_section_headings():
    html = "<h2>Item 1A. Risk Factors</h2><p>Short risk.</p><p>A longer risk disclosure.</p>"
    paragraphs = extract_paragraphs(html, "fixture", min_chars=20)
    assert [paragraph.text for paragraph in paragraphs] == ["A longer risk disclosure."]
    assert paragraphs[0].item == "Item 1A"
    assert [paragraph.text for paragraph in extract_paragraphs(html, "fixture", min_chars=1)] == [
        "Short risk.",
        "A longer risk disclosure.",
    ]
    with pytest.raises(ValueError, match="min_chars"):
        extract_paragraphs(html, "fixture", min_chars=0)


def test_parse_cached_filing_honors_encoding_and_never_rewrites_raw_bytes(tmp_path):
    raw = (
        '<html><head><meta charset="windows-1252"></head><body>'
        "<h2>Item 1A. Risk Factors</h2><p>Our café operations face higher costs and uncertain"
        " demand.</p>"
        "<table><tr><td>2025</td><td>2024</td></tr><tr><td>1200</td><td>1100</td></tr></table>"
        "</body></html>"
    ).encode("cp1252")
    path = tmp_path / "synthetic_cached.html"
    path.write_bytes(raw)
    filing = Filing(
        ticker="SYNTHETIC",
        cik="0000000000",
        company_name="Synthetic fixture",
        form="10-Q",
        accession="synthetic-accession",
        filed_date=date(2025, 1, 1),
        period_of_report=date(2024, 12, 31),
        primary_document=path.name,
        source_url="https://example.invalid/synthetic_cached.html",
        local_path=path,
    )
    paragraphs = parse_filing(filing)
    assert [paragraph.text for paragraph in paragraphs] == [
        "Our café operations face higher costs and uncertain demand."
    ]
    assert paragraphs[0].item == "Item 1A"
    assert path.read_bytes() == raw
