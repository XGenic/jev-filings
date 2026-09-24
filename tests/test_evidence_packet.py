import json
from copy import deepcopy
from datetime import date
from decimal import Decimal
from hashlib import sha256

import pytest

from radar.evidence_packet import bound_packet, packet_bytes, semantic_evidence
from radar.models import ComparisonEvidence, FactChange, FilingParagraph, SourceFact, SourceTable


def paragraph(side, ordinal, text):
    return FilingParagraph(
        filing_accession=side,
        paragraph_id=f"{side}:{ordinal}",
        ordinal=ordinal,
        section="Liquidity",
        item="Item 2",
        text=text,
        normalized_text=text.casefold(),
        text_hash=sha256(text.encode()).hexdigest(),
    )


@pytest.fixture
def source_packet():
    quote = (
        "Cash — current / previous\n9,007,199,254,740,993.000000000000000002 / "
        "9,007,199,254,740,993.000000000000000001 (美元)"
    )
    context = {side: [paragraph(side, 0, quote)] for side in ("previous", "current")}
    facts = {}
    for side, year, value in (
        ("previous", 2025, "9007199254740993.000000000000000001"),
        ("current", 2026, "9007199254740993.000000000000000002"),
    ):
        facts[side] = SourceFact(
            fact_id=f"{side}:cash",
            filing_accession=side,
            concept="us-gaap:CashAndCashEquivalentsAtCarryingValue",
            label="Cash and cash equivalents",
            value=Decimal(value),
            unit="USD",
            entity="0000000042",
            period_end=date(year, 6, 30),
            dimensions={"StatementBusinessSegmentsAxis": "InternationalMember"},
            quote=quote,
            source_anchor="cash-disclosure",
            paragraph_ids=[context[side][0].paragraph_id],
            table_id=f"{side}:table:0",
            scale=-18,
            format="ixt:num-dot-decimal",
        )
    table = SourceTable(
        table_id="current:table:0",
        filing_accession="current",
        caption="Cash — USD, not rounded",
        rows=[["Cash balances"], ["2026 / 2025", "International"], ["Reported amount"]],
        row_indices=[0, 7, 11],
        cell_spans=[[(1, 2)], [(2, 1), (1, 1)], [(1, 1)]],
        header_cells=[[True], [True, False], [False]],
        source_anchor="cash-table",
        section="Liquidity",
        item="Item 2",
    )
    evidence = ComparisonEvidence(
        basis="point_in_time",
        basis_reason="Compatible tagged balances at the two reporting dates.",
        changes=[
            FactChange(
                metric=facts["current"].concept,
                previous=facts["previous"],
                current=facts["current"],
                basis="point_in_time",
                absolute_change=Decimal("0.000000000000000001"),
            )
        ],
        context_facts={side: [fact.model_copy(deep=True)] for side, fact in facts.items()},
        tables={"current": [table]},
        counterparts={side: list(values) for side, values in context.items()},
        changed_spans={"previous": ["previous balance"], "current": ["current balance"]},
        uncertainties=["Amounts are segment-specific, not consolidated."],
    )
    return context, evidence


def resolve_fact(packet, fact_id):
    record = dict(packet["source_facts"][fact_id])
    record["quote"] = packet["source_quotes"][record.pop("quote_ref")]
    return SourceFact.model_validate(record)


def serialized_packet(context, evidence):
    return json.dumps(
        {
            "source_context": {
                side: [p.model_dump(mode="json", exclude={"normalized_text"}) for p in values]
                for side, values in context.items()
            },
            "comparison_evidence": semantic_evidence(evidence),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def test_compact_references_reconstruct_exact_facts_quotes_and_sparse_table_geometry(source_packet):
    _, evidence = source_packet
    original_table = evidence.tables["current"][0]
    evidence.tables["current"].extend(
        [
            original_table.model_copy(
                update={
                    "table_id": "current:table:unknown",
                    "cell_spans": [],
                    "header_cells": [],
                }
            ),
            original_table.model_copy(
                update={
                    "table_id": "current:table:partial",
                    "cell_spans": [[], [(1, 1)]],
                    "header_cells": [[False]],
                }
            ),
        ]
    )
    before = evidence.model_copy(deep=True)
    # Exercise JSON transport, not only the in-memory dictionary.
    packet = json.loads(json.dumps(semantic_evidence(evidence), ensure_ascii=False))
    change = packet["changes"][0]
    for side in ("previous", "current"):
        expected = getattr(evidence.changes[0], side)
        restored = resolve_fact(packet, change[side])
        assert restored == expected
        assert restored.value.as_tuple() == expected.value.as_tuple()
        assert resolve_fact(packet, packet["context_facts"][side][0]) == expected
    assert Decimal(change["absolute_change"]) == Decimal("0.000000000000000001")
    assert change["percent_change"] is None

    for encoded, expected in zip(
        packet["tables"]["current"], evidence.tables["current"], strict=True
    ):
        encoded_table = dict(encoded)
        spans = [[(1, 1)] * size for size in encoded_table.pop("span_shape")]
        headers = [[False] * size for size in encoded_table.pop("header_shape")]
        for row, column, rowspan, colspan in encoded_table.pop("spans"):
            spans[row][column] = (rowspan, colspan)
        for row, column in encoded_table.pop("headers"):
            headers[row][column] = True
        encoded_table.update(cell_spans=spans, header_cells=headers)
        assert SourceTable.model_validate(encoded_table) == expected
    assert evidence == before


def test_multibyte_overflow_omits_whole_records_without_dangling_references(source_packet):
    context, evidence = source_packet
    retained = paragraph("current", 1, "The segment balance excludes restricted cash.")
    omitted = paragraph("current", 2, "額" * 4000)
    context["current"].extend([retained, omitted])
    evidence.counterparts["current"].extend([retained, omitted])
    oversized_fact = evidence.context_facts["current"][0].model_copy(
        update={
            "fact_id": "current:optional",
            "quote": "額" * 9000,
        }
    )
    evidence.context_facts["current"].append(oversized_fact)
    oversized_table = evidence.tables["current"][0].model_copy(
        update={
            "table_id": "current:table:optional",
            "rows": [["額" * 2000]],
            "row_indices": [53],
            "cell_spans": [[(1, 1)]],
            "header_cells": [[False]],
        }
    )
    evidence.tables["current"].append(oversized_table)
    before_context, before_evidence = deepcopy(context), evidence.model_copy(deep=True)
    unbounded = serialized_packet(context, evidence)
    # A character-count cap would incorrectly accept this genuinely multibyte packet.
    assert len(unbounded) < 24_000 < len(unbounded.encode("utf-8"))

    bounded_context, bounded_evidence = bound_packet(context, evidence)
    wire = serialized_packet(bounded_context, bounded_evidence)
    assert len(wire.encode("utf-8")) == packet_bytes(bounded_context, bounded_evidence) <= 24_000
    assert context == before_context
    assert evidence == before_evidence
    assert bounded_evidence.changes == before_evidence.changes
    assert retained in bounded_context["current"]
    assert omitted not in bounded_context["current"]
    assert oversized_fact not in bounded_evidence.context_facts["current"]
    assert oversized_table not in bounded_evidence.tables["current"]
    assert any("omitt" in notice.casefold() for notice in bounded_evidence.uncertainties)
    persisted = ComparisonEvidence.model_validate_json(bounded_evidence.model_dump_json())
    assert persisted.uncertainties == bounded_evidence.uncertainties

    packet = json.loads(wire)["comparison_evidence"]
    referenced_facts = set()
    for change in packet["changes"]:
        referenced_facts.update([change["previous"], change["current"]])
    for side, references in packet["context_facts"].items():
        referenced_facts.update(references)
        for fact_id in references:
            assert resolve_fact(packet, fact_id) in before_evidence.context_facts[side]
    assert referenced_facts == set(packet["source_facts"])
    assert {record["quote_ref"] for record in packet["source_facts"].values()} == set(
        packet["source_quotes"]
    )
    for side, references in packet["counterparts"].items():
        paragraphs = {p.paragraph_id: p for p in bounded_context[side]}
        assert all(paragraphs[ref] in before_evidence.counterparts[side] for ref in references)
        assert references == [p.paragraph_id for p in bounded_evidence.counterparts[side]]
    for side, paragraphs in bounded_context.items():
        assert all(p in before_context[side] for p in paragraphs)
    for side, tables in bounded_evidence.tables.items():
        assert all(table in before_evidence.tables[side] for table in tables)


def test_exact_utf8_boundary_keeps_record_and_one_extra_byte_omits_it():
    evidence = ComparisonEvidence(basis="unclear", basis_reason="No numeric comparison.")
    source = paragraph("current", 0, "額")
    context = {"current": [source]}
    padding = 24_000 - len(serialized_packet(context, evidence).encode("utf-8"))
    source.text += "x" * padding
    assert len(serialized_packet(context, evidence).encode("utf-8")) == 24_000
    kept_context, kept_evidence = bound_packet(context, evidence)
    assert kept_context == context
    assert kept_evidence == evidence

    oversized_context = {"current": [source.model_copy(update={"text": source.text + "x"})]}
    assert len(serialized_packet(oversized_context, evidence).encode("utf-8")) == 24_001
    bounded_context, bounded_evidence = bound_packet(oversized_context, evidence)
    assert bounded_context["current"] == []
    assert len(serialized_packet(bounded_context, bounded_evidence).encode("utf-8")) <= 24_000
    assert any("omitt" in notice.casefold() for notice in bounded_evidence.uncertainties)


@pytest.mark.parametrize(
    "contradiction",
    [
        {"value": Decimal("9007199254740993.000000000000000003")},
        {"quote": "Different source disclosure", "source_anchor": "another-disclosure"},
    ],
)
def test_same_fact_id_cannot_silently_overwrite_numeric_or_source_provenance(
    source_packet, contradiction
):
    _, evidence = source_packet
    evidence.context_facts["current"][0] = evidence.changes[0].current.model_copy(
        update=contradiction
    )
    with pytest.raises(ValueError, match="contradictory"):
        semantic_evidence(evidence)


def test_oversized_derived_channels_can_be_omitted_without_an_unbounded_retry():
    evidence = ComparisonEvidence(
        basis="unclear",
        basis_reason="No numeric comparison.",
        possible_channels=["推定" * 5000],
    )
    original = evidence.model_copy(deep=True)
    context, bounded = bound_packet({}, evidence)
    assert bounded.possible_channels == []
    assert any("omitt" in notice.casefold() for notice in bounded.uncertainties)
    assert packet_bytes(context, bounded) <= 24_000
    assert evidence == original
