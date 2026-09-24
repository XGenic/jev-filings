import re
from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from radar.evidence import EvidenceIndex
from radar.models import (
    AlignedPair,
    ComparableFilingPair,
    Filing,
    FilingEvidence,
    FilingParagraph,
    SourceFact,
    SourceTable,
)


def filing(accession, period):
    return Filing(
        ticker="EXAMPLE",
        cik="0000000042",
        company_name="Example Company",
        form="10-Q",
        accession=accession,
        filed_date=date(2026, 8, 1),
        period_of_report=date.fromisoformat(period),
        primary_document="filing.htm",
        source_url=f"https://www.sec.gov/Archives/{accession}/filing.htm",
        local_path=Path(f"{accession}.htm"),
    )


def paragraph(side, ordinal, text, section="Part I / Item 2 / Results of Operations"):
    return FilingParagraph(
        filing_accession=side,
        paragraph_id=f"{side}:{ordinal}",
        ordinal=ordinal,
        section=section,
        item="Part I / Item 2",
        text=text,
        normalized_text=text,
        text_hash=sha256(text.encode()).hexdigest(),
    )


def fact(side, identifier, value, start="2026-01-01", end="2026-03-31", **updates):
    fields = {
        "fact_id": f"{side}:{identifier}",
        "filing_accession": side,
        "concept": "us-gaap:NetIncomeLoss",
        "label": "Net income (loss)",
        "value": Decimal(value),
        "unit": "USD",
        "entity": "0000000042",
        "period_start": date.fromisoformat(start) if start else None,
        "period_end": date.fromisoformat(end),
        "quote": f"Net income (loss) {value}",
        "source_anchor": identifier,
    }
    fields.update(updates)
    return SourceFact(**fields)


def index(previous, current, old_facts=(), new_facts=(), old_tables=(), new_tables=(), **periods):
    comparison = ComparableFilingPair(
        previous=filing("previous", periods.get("old_period", "2026-03-31")),
        current=filing("current", periods.get("new_period", "2026-06-30")),
    )
    return EvidenceIndex(
        comparison,
        previous,
        current,
        FilingEvidence(
            filing_accession="previous",
            raw_sha256="a" * 64,
            facts=list(old_facts),
            tables=list(old_tables),
        ),
        FilingEvidence(
            filing_accession="current",
            raw_sha256="b" * 64,
            facts=list(new_facts),
            tables=list(new_tables),
        ),
    )


def test_sequential_losses_do_not_subtract_within_filing_yoy_comparators():
    old = paragraph(
        "previous",
        0,
        "Net loss was $317.0 million for Q1 2026 versus $162.0 million for Q1 2025.",
    )
    new = paragraph(
        "current",
        0,
        "Net loss was $95.9 million for Q2 2026 versus $147.9 million for Q2 2025.",
    )
    old_current = fact("previous", "q1", "-317000000", paragraph_ids=[old.paragraph_id])
    new_current = fact(
        "current",
        "q2",
        "-95900000",
        "2026-04-01",
        "2026-06-30",
        paragraph_ids=[new.paragraph_id],
    )
    old_comparative = fact("previous", "prior-q1", "-162000000", "2025-01-01", "2025-03-31")
    new_comparative = fact("current", "prior-q2", "-147900000", "2025-04-01", "2025-06-30")
    evidence_index = index(
        [old], [new], [old_current, old_comparative], [new_current, new_comparative]
    )
    _, packet = evidence_index.build(AlignedPair(old=old, new=new, relation="matched"))
    assert packet.basis == "sequential"
    assert len(packet.changes) == 1
    change = packet.changes[0]
    assert change.previous == old_current
    assert change.current == new_current
    assert change.absolute_change == Decimal("221100000")
    assert change.percent_change is None
    assert old_comparative in packet.context_facts["previous"]
    assert new_comparative in packet.context_facts["current"]


@pytest.mark.parametrize(
    ("new_start", "new_end", "dimensions", "entity", "unit"),
    [
        ("2026-01-01", "2026-06-30", {}, "0000000042", "USD"),
        ("2026-04-01", "2026-06-30", {"SegmentAxis": "AutomotiveMember"}, "0000000042", "USD"),
        ("2026-04-01", "2026-06-30", {}, "0000000043", "USD"),
        ("2026-04-01", "2026-06-30", {}, "0000000042", "EUR"),
    ],
)
def test_incompatible_duration_dimensions_entity_or_unit_never_produce_arithmetic(
    new_start, new_end, dimensions, entity, unit
):
    old = paragraph("previous", 0, "Automotive net income was 20.")
    new = paragraph("current", 0, "Automotive net income was 30.")
    old_fact = fact("previous", "income", "20", paragraph_ids=[old.paragraph_id])
    new_fact = fact(
        "current",
        "income",
        "30",
        new_start,
        new_end,
        dimensions=dimensions,
        entity=entity,
        unit=unit,
        paragraph_ids=[new.paragraph_id],
    )
    _, packet = index([old], [new], [old_fact], [new_fact]).build(
        AlignedPair(old=old, new=new, relation="matched")
    )
    assert packet.changes == []
    assert packet.basis == "unclear"


def test_quarter_selection_ignores_overlapping_ytd_without_deriving_a_quarter():
    old = paragraph("previous", 0, "Net income for the first quarter was $20.")
    new = paragraph("current", 0, "Net income for the second quarter was $30.")
    old_fact = fact("previous", "quarter", "20", paragraph_ids=[old.paragraph_id])
    quarter = fact(
        "current",
        "quarter",
        "30",
        "2026-04-01",
        "2026-06-30",
        paragraph_ids=[new.paragraph_id],
    )
    ytd = fact(
        "current",
        "ytd",
        "50",
        "2026-01-01",
        "2026-06-30",
        paragraph_ids=[new.paragraph_id],
    )
    _, packet = index([old], [new], [old_fact], [quarter, ytd]).build(
        AlignedPair(old=old, new=new, relation="matched")
    )
    assert [(change.current.fact_id, change.absolute_change) for change in packet.changes] == [
        (quarter.fact_id, Decimal("10"))
    ]
    assert packet.changes[0].percent_change == Decimal("50")
    _, missing_quarter = index([old], [new], [old_fact], [ytd]).build(
        AlignedPair(old=old, new=new, relation="matched")
    )
    assert missing_quarter.changes == []


def test_yoy_requires_both_explicit_period_endpoints_not_a_year_label():
    old = paragraph("previous", 0, "Net income for six months ended June 30, 2025 was $20.")
    new = paragraph("current", 0, "Net income for six months ended June 30, 2026 was $30.")
    old_fact = fact(
        "previous",
        "ytd",
        "20",
        "2025-01-01",
        "2025-06-30",
        paragraph_ids=[old.paragraph_id],
    )
    new_fact = fact(
        "current",
        "ytd",
        "30",
        "2026-01-01",
        "2026-06-30",
        paragraph_ids=[new.paragraph_id],
    )
    _, packet = index([old], [new], [old_fact], [new_fact], old_period="2025-06-30").build(
        AlignedPair(old=old, new=new, relation="matched")
    )
    assert packet.basis == "year_over_year"
    assert packet.changes[0].absolute_change == Decimal("10")
    shortened = old_fact.model_copy(update={"period_start": date(2025, 4, 1)})
    _, invalid = index([old], [new], [shortened], [new_fact], old_period="2025-06-30").build(
        AlignedPair(old=old, new=new, relation="matched")
    )
    assert invalid.changes == []


def test_unmatched_counterpart_remains_context_even_with_compatible_facts():
    old = paragraph("previous", 40, "Net income from the specialty factory was $20.")
    new = paragraph("current", 2, "Net income from the specialty factory was $30.")
    old_fact = fact("previous", "income", "20", paragraph_ids=[old.paragraph_id])
    new_fact = fact(
        "current",
        "income",
        "30",
        "2026-04-01",
        "2026-06-30",
        paragraph_ids=[new.paragraph_id],
    )
    pair = AlignedPair(old=None, new=new, relation="added")
    source_context, packet = index([old], [new], [old_fact], [new_fact]).build(pair)
    assert old in packet.counterparts["previous"]
    assert old in source_context["previous"]
    assert packet.changes == []
    assert packet.basis == "unclear"
    assert pair.old is None and pair.relation == "added"


def test_nonadjacent_financing_context_and_current_company_denominators_are_source_exact():
    old = paragraph("previous", 0, "Our revolving credit facility carries restrictive covenants.")
    target = paragraph(
        "current", 0, "Our revolving credit facility requires additional collateral."
    )
    filler = [
        paragraph("current", number, "An unrelated accounting policy applies.")
        for number in range(1, 10)
    ]
    financing = paragraph(
        "current",
        50,
        "Revolving credit borrowings were $70 million; liquidity included $30 million cash.",
        section="Part I / Item 1 / Financing Arrangements",
    )
    cash = fact(
        "current",
        "cash",
        "30000000",
        None,
        "2026-06-30",
        concept="us-gaap:CashAndCashEquivalentsAtCarryingValue",
        label="Cash and cash equivalents",
    )
    stale = cash.model_copy(update={"fact_id": "current:stale", "period_end": date(2025, 12, 31)})
    subsidiary = cash.model_copy(update={"fact_id": "current:subsidiary", "entity": "0000000077"})
    source = [target, *filler, financing]
    context, packet = index([old], source, new_facts=[stale, subsidiary, cash]).build(
        AlignedPair(old=old, new=target, relation="matched")
    )
    assert financing in context["current"]
    assert all(value in source for value in context["current"])
    assert packet.context_facts["current"] == [cash]
    assert packet.changes == []
    assert "liquidity_and_financing" in packet.possible_channels


def test_numeric_only_coincidence_and_unmentioned_segments_are_not_target_changes():
    old = paragraph("previous", 0, "Specialty factory production expanded.")
    new = paragraph("current", 0, "Specialty factory production reached 30 units.")
    unrelated_old = fact("previous", "income", "20", quote="20")
    unrelated_new = fact("current", "income", "30", "2026-04-01", "2026-06-30", quote="30")
    _, packet = index([old], [new], [unrelated_old], [unrelated_new]).build(
        AlignedPair(old=old, new=new, relation="matched")
    )
    assert packet.changes == []
    old_revenue = paragraph("previous", 0, "Specialty revenue expanded.")
    new_revenue = paragraph("current", 0, "Specialty revenue reached 30.")
    automotive = {"SegmentAxis": "AutomotiveMember"}
    fields = {"concept": "us-gaap:Revenues", "label": "Revenue", "dimensions": automotive}
    _, irrelevant_segment = index(
        [old_revenue],
        [new_revenue],
        [fact("previous", "revenue", "20", **fields)],
        [fact("current", "revenue", "30", "2026-04-01", "2026-06-30", **fields)],
    ).build(AlignedPair(old=old_revenue, new=new_revenue, relation="matched"))
    assert irrelevant_segment.changes == []
    assert irrelevant_segment.context_facts["current"] == []


def test_fragment_packet_retains_original_table_rows_headers_and_geometry_with_bounds():
    target = paragraph("current", 0, "Specialty gross profit (loss)")
    rows = [
        ["Segment gross profit", "Three months ended June 30"],
        ["", "2026"],
        ["", "USD millions"],
    ]
    rows += [[f"Unrelated depreciation category {number}", str(number)] for number in range(24)]
    rows += [["Specialty gross profit (loss)", "31.8"]]
    table = SourceTable(
        table_id="current:table-1",
        filing_accession="current",
        caption="Segment profitability",
        rows=rows,
        row_indices=[number * 2 + 100 for number in range(len(rows))],
        cell_spans=[[(1, 1), (1, 1)] for _ in rows],
        header_cells=[[True, True] for _ in rows[:3]] + [[False, False] for _ in rows[3:]],
    )
    other_tables = [
        table.model_copy(update={"table_id": f"current:table-{number}"}) for number in range(2, 6)
    ]
    paragraphs = [target] + [
        paragraph("current", n, f"Specialty gross profit detail {n}.") for n in range(1, 30)
    ]
    context, packet = index([], paragraphs, new_tables=[table, *other_tables]).build(
        AlignedPair(old=None, new=target, relation="added")
    )
    assert len(context["current"]) <= 6
    assert len(packet.tables["current"]) <= 2
    excerpt = packet.tables["current"][0]
    assert len(excerpt.rows) <= 12
    assert excerpt.rows[:3] == rows[:3]
    assert rows[-1] in excerpt.rows
    for offset, original_index in enumerate(excerpt.row_indices):
        source_index = table.row_indices.index(original_index)
        assert excerpt.rows[offset] == table.rows[source_index]
        assert excerpt.cell_spans[offset] == table.cell_spans[source_index]
        assert excerpt.header_cells[offset] == table.header_cells[source_index]
    assert table.rows == rows
    assert packet.changes == []


def test_changed_spans_preserve_source_characters_and_coordinates():
    old = paragraph("previous", 0, "Cash was $200.1\u00a0million; unrelated words remain.")
    new = paragraph("current", 0, "Cash was $183.4\u00a0million; unrelated words remain.")
    _, packet = index([old], [new]).build(AlignedPair(old=old, new=new, relation="matched"))
    for side, source in (("previous", old), ("current", new)):
        assert packet.changed_spans[side]
        for cited in packet.changed_spans[side]:
            match = re.fullmatch(r"\[(.*?) \| (.*?) \| chars (\d+):(\d+)\] (.*)", cited, re.DOTALL)
            accession, paragraph_id, start, end, quote = match.groups()
            assert accession == source.filing_accession
            assert paragraph_id == source.paragraph_id
            assert quote == source.text[int(start) : int(end)]
    altered = new.model_copy(update={"text": "Fabricated target"})
    with pytest.raises(ValueError):
        index([old], [new]).build(AlignedPair(old=old, new=altered, relation="matched"))


def test_point_in_time_signed_difference_and_zero_base_are_not_a_zero_substitution():
    old = paragraph("previous", 0, "Cash was $0 as of March 31, 2026.")
    new = paragraph("current", 0, "Cash was $2.01 as of June 30, 2026.")
    fields = {"concept": "us-gaap:Cash", "label": "Cash"}
    old_fact = fact(
        "previous",
        "cash",
        "0",
        None,
        "2026-03-31",
        paragraph_ids=[old.paragraph_id],
        **fields,
    )
    new_fact = fact(
        "current",
        "cash",
        "2.01",
        None,
        "2026-06-30",
        paragraph_ids=[new.paragraph_id],
        **fields,
    )
    _, packet = index([old], [new], [old_fact], [new_fact]).build(
        AlignedPair(old=old, new=new, relation="matched")
    )
    assert packet.basis == "point_in_time"
    assert packet.changes[0].absolute_change == Decimal("2.01")
    assert packet.changes[0].percent_change is None
    _, missing = index([old], [new], [], [new_fact]).build(
        AlignedPair(old=old, new=new, relation="matched")
    )
    assert missing.changes == []


def test_fact_bound_cannot_hide_conflicting_source_values_and_input_order_is_irrelevant():
    old = paragraph("previous", 0, "Net income was $20.")
    new = paragraph("current", 0, "Net income and other reported income measurements were updated.")
    old_fact = fact("previous", "income", "20", paragraph_ids=[old.paragraph_id])
    new_fact = fact(
        "current",
        "income",
        "30",
        "2026-04-01",
        "2026-06-30",
        paragraph_ids=[new.paragraph_id],
    )
    distractors = [
        fact(
            "current",
            f"other-{number}",
            str(number + 1),
            "2026-04-01",
            "2026-06-30",
            concept=f"issuer:IncomeMetric{number}",
            paragraph_ids=[new.paragraph_id],
        )
        for number in range(14)
    ]
    conflicting = new_fact.model_copy(
        update={"fact_id": "current:conflicting", "value": Decimal("999"), "paragraph_ids": []}
    )
    source_facts = [new_fact, *distractors, conflicting]
    pair = AlignedPair(old=old, new=new, relation="matched")
    _, packet = index([old], [new], [old_fact], source_facts).build(pair)
    assert len(packet.context_facts["current"]) == 12
    assert conflicting not in packet.context_facts["current"]
    assert packet.changes == []
    _, reordered = index([old], [new], [old_fact], list(reversed(source_facts))).build(pair)
    assert packet == reordered


def test_topic_overlap_does_not_turn_a_forecast_into_a_balance_change():
    old = paragraph("previous", 0, "We expect our cash reserves to support the expansion plan.")
    new = paragraph("current", 0, "We expect our cash reserves to support a larger expansion plan.")
    fields = {
        "concept": "us-gaap:CashAndCashEquivalentsAtCarryingValue",
        "label": "Cash and cash equivalents",
    }
    previous = fact("previous", "cash", "100", None, "2026-03-31", **fields)
    current = fact("current", "cash", "200", None, "2026-06-30", **fields)
    _, packet = index([old], [new], [previous], [current]).build(
        AlignedPair(old=old, new=new, relation="matched")
    )
    assert previous in packet.context_facts["previous"]
    assert current in packet.context_facts["current"]
    assert packet.changes == []
