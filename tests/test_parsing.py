"""Behavioral regressions use synthetic HTML and explicitly sourced SEC excerpts."""

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


def test_empty_destination_anchor_does_not_hide_actual_item_boundary():
    # NPK uses <a href="#" id="mda"></a> before the actual MD&A heading.
    html = (
        '<p><a href="#mda">Item 2. Management discussion</a></p>'
        "<h2>Item 1. Financial Statements</h2>"
        "<p>Our interim financial statements include the accompanying notes.</p>"
        '<p><a href="#" id="mda"></a>ITEM 2. MANAGEMENT DISCUSSION</p>'
        "<p>Operating margins improved following the production expansion.</p>"
    )
    paragraphs = extract_paragraphs(html, "anchor-fixture")
    assert [paragraph.item for paragraph in paragraphs] == ["Item 1", "Item 2"]
    assert paragraphs[1].text == ("Operating margins improved following the production expansion.")


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


def test_source_product_revenue_continuation_survives_as_one_paragraph():
    paragraphs = fixture_paragraphs("bfly_product_revenue_pagebreak.html")
    expected = (
        "Product revenue increased by $0.5 million, or 3.5%, for the three months ended"
        " March 31, 2026 compared to the three months ended March 31, 2025."
        " This increase was driven by higher probe sales volume in our international"
        " distributor and veterinary sales channels, with veterinary sales positively"
        " impacted by the launch of our iQ3 Vet probe in the United States and some"
        " international markets in the fourth quarter of 2025. We also experienced a"
        " favorable shift year-over-year in our product sales mix with a higher proportion"
        " of sales of our current-generation iQ3 probes that have a higher selling price"
        " than our previous-generation iQ+ probes."
    )
    services = (
        "Software and other services revenue increased by $4.8 million, or 68.2%, for the"
        " three months ended March 31, 2026 compared to the three months ended March 31,"
        " 2025. This increase was primarily driven by increases in software and other"
        " services revenue generated by our Embedded partnerships."
    )
    assert [paragraph.normalized_text for paragraph in paragraphs] == [expected, services]
    assert paragraphs[0].text.count("March\u00a031") == 2
    assert paragraphs[0].text_hash == sha256(expected.encode("utf-8")).hexdigest()
    assert paragraphs[0].filing_accession == "synthetic-accession"
    assert paragraphs[0].item == "Item 2"
    assert paragraphs[0].section == (
        "Item 2. Management’s Discussion and Analysis of Financial Condition"
        " and Results of Operations / Revenue"
    )


@pytest.mark.parametrize(
    ("before", "separator", "after", "ending_section"),
    [
        ("Demand increased in international markets", "", "and remains strong.", "Revenue"),
        (
            "Demand increased in international markets.",
            "{furniture}",
            "and a separate paragraph remains separate.",
            "Revenue",
        ),
        (
            "Demand increased in international markets",
            "{furniture}<h2>Liquidity</h2>",
            "and cash balances remain available.",
            "Liquidity",
        ),
        (
            "Demand increased in international markets",
            "{furniture}{table}",
            "and the table presents the financial results.",
            "Revenue",
        ),
        (
            "Demand increased in international markets",
            "{table}{furniture}",
            "and the table presents the financial results.",
            "Revenue",
        ),
    ],
)
def test_page_recovery_does_not_cross_paragraph_heading_or_table_boundaries(
    before, separator, after, ending_section
):
    furniture = (
        '<div style="height:45pt;position:relative;width:100%">'
        '<div style="bottom:0;position:absolute;width:100%">'
        '<div style="text-align:center"><span>21</span></div></div></div>'
        '<hr style="page-break-after:always"/>'
        '<div style="min-height:45pt;width:100%"><div>'
        '<a href="#contents">Table of Contents</a></div></div>'
    )
    table = "<table><tr><td>2026</td><td>2025</td></tr><tr><td>1200</td><td>1100</td></tr></table>"
    separator = separator.format(furniture=furniture, table=table)
    html = (
        f'<h2>Revenue</h2><div style="text-align:justify">{before}</div>'
        f'{separator}<div style="text-align:justify">{after}</div>'
    )
    paragraphs = extract_paragraphs(html, "boundary", min_chars=1)
    assert [paragraph.text for paragraph in paragraphs] == [before, after]
    assert paragraphs[0].section == "Revenue"
    assert paragraphs[1].section == ending_section


@pytest.mark.parametrize(
    ("before", "after", "inline_wrappers"),
    [
        (
            "We operate principally through our subsidiaries. "
            "ASP Isotopes UK Ltd is the owner of our",
            "technology.",
            False,
        ),
        (
            "The Company had noncurrent restricted cash related to Renergen's obligation to manage"
            " the negative environmental impact associated",
            "with its operational activities and $0.5 million related to electricity payments and"
            " early termination guaranties with a public utility company.",
            True,
        ),
    ],
)
def test_source_wrapped_page_edges_reunite_short_and_xbrl_continuations(
    before, after, inline_wrappers
):
    # The current ASPI 2026-06-30 source has these numbered-footer/blank-header
    # boundaries at pages 37/38 and 12/13. Paragraph indentation and top margins
    # differ across the break; the restricted-cash policy has XBRL wrappers too.
    # Source: sec.gov/Archives/edgar/data/1921865/000119312526352603/aspi-20260630.htm
    previous = (
        '<p style="text-indent:4.533%;font-size:10pt;margin-top:0;'
        f'font-family:Times New Roman;text-align:justify;">{before}</p>'
    )
    current = (
        '<p style="font-size:10pt;margin-top:6pt;font-family:Times New Roman;'
        f'text-align:justify;">{after}</p>'
    )
    if inline_wrappers:
        previous = (
            '<ix:continuation><div><ix:nonNumeric continuedAt="policy-tail">'
            f"{previous}</ix:nonNumeric></div></ix:continuation>"
        )
        current = (
            '<div><ix:continuation><div><ix:continuation id="policy-tail">'
            f"{current}</ix:continuation></div></ix:continuation></div>"
        )
    html = (
        "<h2>Item 2. Management discussion</h2>"
        f'<div class="main-content-container">{previous}</div>'
        '<div style="min-height:0.5in;justify-content:flex-end"><p>12</p></div>'
        '<hr style="page-break-after:always"/>'
        '<div style="padding-top:0.5in;min-height:0.5in"><p><span>&nbsp;</span></p></div>'
        f'<div class="main-content-container">{current}'
        "<h2>Separate disclosure</h2>"
        "<p>Other operations remain a separate disclosure after the continuation.</p></div>"
    )
    paragraphs = extract_paragraphs(html, "page-wrappers")
    assert [paragraph.text for paragraph in paragraphs] == [
        before + " " + after,
        "Other operations remain a separate disclosure after the continuation.",
    ]
    assert paragraphs[0].item == paragraphs[1].item == "Item 2"
    assert paragraphs[0].section != paragraphs[1].section


@pytest.mark.parametrize(
    ("before", "prefix", "style"),
    [
        (
            "Our production operations remain subject to extensive regulations.",
            "",
            "font-size:10pt",
        ),
        (
            "Our production operations remain subject to extensive regulations",
            "<h2>Liquidity</h2>",
            "font-size:10pt",
        ),
        (
            "Our production operations remain subject to extensive regulations",
            "<table><tr><td>2026</td><td>2025</td></tr><tr><td>100</td><td>90</td></tr></table>",
            "font-size:10pt",
        ),
        ("Our production operations remain subject to extensive regulations", "", "font-size:14pt"),
    ],
)
def test_wrapped_page_recovery_preserves_completed_heading_table_and_typography_boundaries(
    before, prefix, style
):
    after = "and this independent disclosure must not be swallowed by the previous paragraph."
    html = (
        f'<div><p style="font-size:10pt">{before}</p></div>'
        '<div><p>12</p></div><hr style="page-break-after:always"/>'
        '<div style="min-height:0.5in"><p>&nbsp;</p></div>'
        f'<div>{prefix}<p style="{style}">{after}</p></div>'
    )
    assert [paragraph.text for paragraph in extract_paragraphs(html, "boundaries")] == [
        before,
        after,
    ]
