"""Lossless reference encoding and explicit whole-record bounds for optional evidence."""

import json
from collections.abc import Callable
from typing import Any

from radar.models import ComparisonEvidence, FilingParagraph, SourceFact

MAX_PACKET_BYTES = 24_000
_BOUND_NOTICE = (
    "Optional context records were omitted to keep the supplemental packet within 24,000 UTF-8 "
    "bytes. Original target passages are unchanged. Omitted context is not evidence of absence; "
    "consult the full source filings and retained filing-evidence diagnostics."
)


def semantic_evidence(evidence: ComparisonEvidence) -> dict[str, Any]:
    """Preserve every selected value/quote without repeating long source paragraphs."""
    facts: dict[str, dict] = {}
    originals: dict[str, SourceFact] = {}
    quotes: dict[str, str] = {}
    quote_ids: dict[str, str] = {}

    def reference(fact: SourceFact) -> str:
        if fact.fact_id in facts:
            if originals[fact.fact_id] != fact:
                raise ValueError("One source fact ID cannot identify contradictory records")
            return fact.fact_id
        quote_id = quote_ids.get(fact.quote)
        if quote_id is None:
            quote_id = f"quote-{len(quotes) + 1}"
            quote_ids[fact.quote] = quote_id
            quotes[quote_id] = fact.quote
        facts[fact.fact_id] = fact.model_dump(mode="json", exclude={"quote"})
        facts[fact.fact_id]["quote_ref"] = quote_id
        originals[fact.fact_id] = fact
        return fact.fact_id

    state = evidence.model_dump(
        mode="json", exclude={"changes", "context_facts", "tables", "counterparts"}
    )
    state["encoding_version"] = "source-reference-1"
    state["encoding"] = (
        "Fact IDs resolve in source_facts; quote_ref resolves in source_quotes. Counterpart IDs "
        "resolve in source_context. Table geometry uses zero-based excerpt row/cell coordinates. "
        "Within span_shape/header_shape, unspecified spans are 1x1 and headers false; an empty "
        "shape means unavailable geometry. row_indices preserve original source rows."
    )
    state["changes"] = [
        {
            **change.model_dump(mode="json", exclude={"previous", "current"}),
            "previous": reference(change.previous),
            "current": reference(change.current),
        }
        for change in evidence.changes
    ]
    state["context_facts"] = {
        side: [reference(fact) for fact in values]
        for side, values in sorted(
            evidence.context_facts.items(), key=lambda item: (item[0] != "previous", item[0])
        )
    }
    state["source_facts"] = facts
    state["source_quotes"] = quotes
    state["tables"] = {}
    for side, tables in evidence.tables.items():
        state["tables"][side] = []
        for table in tables:
            value = table.model_dump(mode="json", exclude={"cell_spans", "header_cells"})
            value["span_shape"] = [len(row) for row in table.cell_spans]
            value["header_shape"] = [len(row) for row in table.header_cells]
            value["spans"] = [
                [row, column, *span]
                for row, cells in enumerate(table.cell_spans)
                for column, span in enumerate(cells)
                if span != (1, 1)
            ]
            value["headers"] = [
                [row, column]
                for row, cells in enumerate(table.header_cells)
                for column, flag in enumerate(cells)
                if flag
            ]
            state["tables"][side].append(value)
    state["counterparts"] = {
        side: [paragraph.paragraph_id for paragraph in values]
        for side, values in evidence.counterparts.items()
    }
    return state


def packet_bytes(context: dict[str, list[FilingParagraph]], evidence: ComparisonEvidence) -> int:
    state = {
        "source_context": {
            side: [p.model_dump(mode="json", exclude={"normalized_text"}) for p in values]
            for side, values in context.items()
        },
        "comparison_evidence": semantic_evidence(evidence),
    }
    return len(json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def bound_packet(
    context: dict[str, list[FilingParagraph]], evidence: ComparisonEvidence
) -> tuple[dict[str, list[FilingParagraph]], ComparisonEvidence]:
    """Keep whole source records; never clip targets, a number, quote or table row."""
    if packet_bytes(context, evidence) <= MAX_PACKET_BYTES:
        return context, evidence
    context = {side: list(values) for side, values in context.items()}
    evidence = evidence.model_copy(
        update={
            "changes": list(evidence.changes),
            "context_facts": {
                side: list(values) for side, values in evidence.context_facts.items()
            },
            "tables": {side: list(values) for side, values in evidence.tables.items()},
            "counterparts": {side: list(values) for side, values in evidence.counterparts.items()},
            "changed_spans": {
                side: list(values) for side, values in evidence.changed_spans.items()
            },
            "uncertainties": [*evidence.uncertainties, _BOUND_NOTICE],
        }
    )

    def drop(
        mapping: dict[str, list], minimum: int = 0, eligible: Callable = lambda _: True
    ) -> bool:
        candidates = [
            (side, index, value)
            for side, values in mapping.items()
            for index, value in enumerate(values)
            if index >= minimum and eligible(value)
        ]
        if not candidates:
            return False
        # Lower-ranked tail records go first; ties prefer the larger source atom.
        side, index, _ = max(candidates, key=lambda row: (row[1], len(str(row[2])), row[0]))
        del mapping[side][index]
        return True

    while packet_bytes(context, evidence) > MAX_PACKET_BYTES:
        source_ids = {
            fact.fact_id
            for change in evidence.changes
            for fact in (change.previous, change.current)
        }
        if drop(context, minimum=2):
            pass
        elif drop(evidence.tables, minimum=1):
            pass
        elif drop(evidence.context_facts, eligible=lambda fact: fact.fact_id not in source_ids):
            pass
        elif drop(evidence.tables):
            pass
        elif drop(context):
            pass
        elif drop(evidence.context_facts):
            pass
        elif evidence.changes:
            evidence.changes.pop()
            evidence.uncertainties.append(
                "A computed numeric pair was omitted by the packet bound."
            )
        elif drop(evidence.changed_spans):
            pass
        elif evidence.possible_channels:
            evidence.possible_channels = []
        elif evidence.uncertainties == [_BOUND_NOTICE]:
            raise ValueError("Irreducible supplemental evidence exceeds the packet byte bound")
        else:
            evidence.uncertainties = [_BOUND_NOTICE]
        # Counterparts refer only to source paragraphs actually supplied to the evaluator.
        for side, values in evidence.counterparts.items():
            kept = {p.paragraph_id for p in context.get(side, [])}
            evidence.counterparts[side] = [p for p in values if p.paragraph_id in kept]
        bases = {change.basis for change in evidence.changes}
        evidence.basis = next(iter(bases)) if len(bases) == 1 else "mixed" if bases else "unclear"
        evidence.basis_reason = (
            "Numeric basis describes only retained attributed measurements, not every target claim."
            if bases
            else "No computed numeric pair is retained; inspect the original target passages."
        )
    return context, evidence
