"""Source-visible numeric boundaries, not inferred financial interpretations."""

from datetime import date
from decimal import Decimal, localcontext
from hashlib import sha256

import pytest

from radar.models import Filing
from radar.parse import parse_evidence, parse_filing

_HEADER = """<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
 xmlns:xbrli="http://www.xbrl.org/2003/instance"
 xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
 xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
 xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2022-02-16">
 <head><style>.concealed {display: none}</style></head><body>
 <ix:header><ix:resources>
 <xbrli:context id="duration"><xbrli:entity>
 <xbrli:identifier scheme="http://www.sec.gov/CIK">0000000001</xbrli:identifier>
 <xbrli:segment>
 <xbrldi:explicitMember dimension="sample:RegionAxis">sample:USMember</xbrldi:explicitMember>
 </xbrli:segment>
 </xbrli:entity><xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period></xbrli:context>
 <xbrli:context id="instant"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000000001</xbrli:identifier></xbrli:entity>
 <xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period></xbrli:context>
 <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
 </ix:resources></ix:header>"""


def _filing(tmp_path, body, header=_HEADER):
    path = tmp_path / "evidence.html"
    path.write_text(header + body + "</body></html>", encoding="utf-8")
    return Filing(
        ticker="SYNTHETIC",
        cik="0000000001",
        company_name="Synthetic evidence",
        form="10-Q",
        accession="source-fixture",
        filed_date=date(2026, 8, 1),
        period_of_report=date(2026, 6, 30),
        primary_document=path.name,
        source_url="https://example.invalid/evidence.html",
        local_path=path,
    )


def test_visible_signed_scaled_fact_keeps_exact_digits_period_and_source(tmp_path):
    filing = _filing(
        tmp_path,
        """<h2>Item 2. Results</h2><p>Our regional loss was
    <ix:nonFraction id="loss" name="sample:Loss" contextRef="duration" unitRef="usd"
    format="ixt:num-dot-decimal" scale="-2" sign="-"
    >1,234.5678901234567890123456789</ix:nonFraction>
    dollars during the first six months of the year.</p>""",
    )
    raw = filing.local_path.read_bytes()
    paragraphs = parse_filing(filing)
    with localcontext() as context:
        context.prec = 6
        evidence = parse_evidence(filing, paragraphs)
    (fact,) = evidence.facts
    assert fact.value == Decimal("-12.345678901234567890123456789")
    assert (fact.period_start, fact.period_end) == (date(2026, 1, 1), date(2026, 6, 30))
    assert fact.entity == "0000000001"
    assert fact.unit == "USD"
    assert fact.dimensions == {"sample:RegionAxis": "sample:USMember"}
    assert fact.paragraph_ids == [paragraphs[0].paragraph_id]
    assert fact.quote == paragraphs[0].text
    assert fact.source_anchor == "loss"
    assert evidence.raw_sha256 == sha256(raw).hexdigest()
    assert filing.local_path.read_bytes() == raw


def test_hidden_facts_stay_hidden_but_their_unit_and_context_metadata_are_used(tmp_path):
    numeric = (
        '<ix:nonFraction name="sample:Cash" contextRef="instant" unitRef="usd">{}</ix:nonFraction>'
    )
    filing = _filing(
        tmp_path,
        (
            "<ix:hidden>" + numeric.format("91") + "</ix:hidden>"
            '<div class="concealed">' + numeric.format("92") + "</div>"
            '<div style="visibility:hidden">' + numeric.format("93") + "</div>"
            "<div hidden>" + numeric.format("94") + "</div>"
            "<p>Our available cash at the end of the reporting period was "
            + numeric.format("25.50")
            + " dollars.</p>"
        ),
    )
    evidence = parse_evidence(filing, parse_filing(filing))
    assert [fact.value for fact in evidence.facts] == [Decimal("25.50")]
    assert evidence.facts[0].period_start is None
    assert evidence.facts[0].period_end == date(2026, 6, 30)


def test_unsupported_numeric_transform_preserves_table_headers_spans_units_and_claim(tmp_path):
    filing = _filing(
        tmp_path,
        """<h2>Item 1. Financial Statements</h2>
    <p>Amounts in thousands, except per-share data.</p>
    <table id="balances"><caption>Cash balances</caption>
    <tr><th rowspan="2">Metric</th><th colspan="2">June 30</th></tr>
    <tr><th>2026</th><th>2025</th></tr>
    <tr><td>Cash</td><td><ix:nonFraction id="unsupported" name="sample:Cash" contextRef="instant"
    unitRef="usd" format="ixt:unknown">1,23</ix:nonFraction></td><td>45</td></tr>
    <tr><td>Reserve</td><td><ix:nonFraction name="sample:Reserve" contextRef="instant"
    unitRef="usd" format="ixt:num-dot-decimal" scale="3">2</ix:nonFraction></td><td>3</td></tr>
    </table><p>Cash remains subject to restrictions under the existing loan agreement.</p>""",
    )
    paragraphs = parse_filing(filing)
    evidence = parse_evidence(filing, paragraphs)
    (table,) = evidence.tables
    assert table.rows == [
        ["Metric", "June 30"],
        ["2026", "2025"],
        ["Cash", "1,23", "45"],
        ["Reserve", "2", "3"],
    ]
    assert table.cell_spans[:2] == [[(2, 1), (1, 2)], [(1, 1), (1, 1)]]
    assert table.header_cells[:2] == [[True, True], [True, True]]
    assert "Amounts in thousands" in table.caption
    assert "Cash balances" in table.caption
    assert table.source_anchor == "balances"
    assert table.item == "Item 1"
    assert [fact.value for fact in evidence.facts] == [Decimal("2000")]
    assert evidence.facts[0].table_id == table.table_id
    assert evidence.facts[0].paragraph_ids == []
    assert any("unsupported" in warning for warning in evidence.warnings)
    assert all(
        "1,23" not in paragraph.text and "Reserve" not in paragraph.text for paragraph in paragraphs
    )


@pytest.mark.parametrize(
    ("attributes", "text", "metadata"),
    [
        ('format="ixt:num-dot-decimal"', "1,23", ""),
        ('sign="-"', "-12", ""),
        ('format="unknown:num-dot-decimal"', "12", ""),
        (
            'contextRef="bad"',
            "12",
            """<xbrli:context id="bad"><xbrli:entity>
         <xbrli:identifier>1</xbrli:identifier></xbrli:entity><xbrli:period>
         <xbrli:startDate>2026-07-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate>
         </xbrli:period></xbrli:context>""",
        ),
        (
            'contextRef="bad"',
            "12",
            """<xbrli:context id="bad"><xbrli:entity>
         <xbrli:identifier>1</xbrli:identifier></xbrli:entity><xbrli:period>
         <xbrli:instant>2026-06-30</xbrli:instant><xbrli:endDate>2026-06-30</xbrli:endDate>
         </xbrli:period></xbrli:context>""",
        ),
    ],
)
def test_ambiguous_numeric_values_or_periods_are_not_invented(tmp_path, attributes, text, metadata):
    context = "" if "contextRef" in attributes else 'contextRef="instant"'
    filing = _filing(
        tmp_path,
        metadata
        + f"""<table><tr><th>Metric</th><th>Amount</th></tr>
    <tr><td>Cash</td><td><ix:nonFraction name="sample:Cash" {context} unitRef="usd"
    {attributes}>{text}</ix:nonFraction></td></tr></table>""",
    )
    evidence = parse_evidence(filing, parse_filing(filing))
    assert evidence.facts == []
    assert evidence.warnings
    assert evidence.tables[0].rows[-1] == ["Cash", text]
