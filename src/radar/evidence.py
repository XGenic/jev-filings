"""Bounded, source-preserving retrieval and deliberately conservative fact arithmetic.

The index is local and reusable across all paragraph pairs for two filings. Retrieved
counterparts and company denominators are context, never new alignment decisions.
"""

import re
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, localcontext
from difflib import SequenceMatcher
from math import sqrt
from typing import Literal

from radar.evidence_packet import bound_packet
from radar.match.context import paragraph_contexts, scopes_compatible
from radar.models import (
    AlignedPair,
    ComparableFilingPair,
    ComparisonEvidence,
    FactChange,
    Filing,
    FilingEvidence,
    FilingParagraph,
    SourceFact,
    SourceTable,
)

MAX_PARAGRAPHS = 6
MAX_FACTS = 12
MAX_TABLES = 2
MAX_TABLE_ROWS = 12
MAX_COUNTERPARTS = 2
MAX_CHANGED_SPANS = 8

_STOP = frozenset(
    "a an and are as at be been being by company companies corporation could did do does "
    "during ended ending fiscal for from had has have in inc include included includes "
    "including is it its may month months not of on or our quarter quarters reported "
    "respectively shall should that the their these this those three to two under us "
    "was we were which will with would year years january february march april may june "
    "july august september october november december first second third fourth six nine "
    "twelve million millions billion billions thousand thousands part item management "
    "discussion analysis financial statement statements unaudited condensed consolidated "
    "note notes certain following pursuant information period periods related other "
    "current previous total amount amounts approximately".split()
)
_CHANNEL_TERMS = {
    "demand_and_customers": "customer demand order backlog contract revenue sale volume",
    "pricing_and_margins": "price pricing margin profit loss cost expense income ebitda",
    "operations_and_capacity": "operation production capacity facility supply plant staffing",
    "liquidity_and_financing": "cash liquidity debt credit covenant borrow loan financing",
    "capital_allocation": "acquisition acquire merger divestiture investment dividend repurchase",
    "legal_and_regulatory": (
        "litigation lawsuit regulation regulatory license settlement compliance"
    ),
    "accounting_and_controls": "accounting amortization impairment audit control intangible",
    "outlook_and_strategy": "outlook guidance strategy forecast expect anticipate",
}
# Exact local concept names avoid promoting an arbitrary large figure to a denominator.
_DENOMINATORS = {
    "cash": {
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    },
    "assets": {"Assets"},
    "revenue": {
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
    },
    "cash_flow": {"NetCashProvidedByUsedInOperatingActivities"},
    "debt": {"LongTermDebtCurrent", "LongTermDebtNoncurrent", "LongTermDebt"},
    "income": {"NetIncomeLoss", "ProfitLoss", "OperatingIncomeLoss"},
    "shares": {"CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding"},
}
_WORDS = re.compile(r"[^\W\d_]+", re.UNICODE)


def _tokens(text: str) -> frozenset[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    words = _WORDS.findall(text.casefold())
    aliases = {
        "losses": "loss",
        "operating": "operation",
        "operations": "operation",
        "borrowings": "borrow",
        "borrowed": "borrow",
        "acquired": "acquire",
        "pricing": "price",
        "sales": "sale",
    }
    return frozenset(
        aliases.get(word, word[:-1] if word.endswith("s") and not word.endswith("ss") else word)
        for word in words
        if len(word) > 2 and word not in _STOP
    )


_CHANNEL_TOKENS = {name: _tokens(text) for name, text in _CHANNEL_TERMS.items()}


class _Lookup:
    def __init__(self, texts: list[str]):
        self.tokens = [_tokens(text) for text in texts]
        self.postings: dict[str, list[int]] = defaultdict(list)
        for index, tokens in enumerate(self.tokens):
            for token in sorted(tokens):
                self.postings[token].append(index)

    def scores(self, query: frozenset[str]) -> dict[int, float]:
        scores: dict[int, float] = defaultdict(float)
        for token in sorted(query):
            positions = self.postings.get(token, ())
            weight = 1 / (1 + len(positions))
            for index in positions:
                scores[index] += weight
        return {
            index: score / sqrt(max(1, len(self.tokens[index]))) for index, score in scores.items()
        }


def _fact_key(fact: SourceFact) -> tuple:
    return fact.concept, fact.entity, fact.unit, tuple(sorted(fact.dimensions.items()))


def _observation_key(fact: SourceFact) -> tuple:
    return _fact_key(fact), fact.period_start, fact.period_end


def _metric_tokens(fact: SourceFact) -> frozenset[str]:
    generic = {"net", "value", "carrying", "provided", "used", "activitie", "activity"}
    return _tokens(fact.concept.rsplit(":", 1)[-1] + " " + fact.label) - generic


def _period_basis(
    previous: SourceFact, current: SourceFact
) -> Literal["sequential", "year_over_year", "point_in_time"] | None:
    """Use context dates, never number order, table position, or filing date alone."""
    if _fact_key(previous) != _fact_key(current) or current.period_end <= previous.period_end:
        return None
    if previous.period_start is None or current.period_start is None:
        return "point_in_time" if previous.period_start is current.period_start is None else None
    old_days = (previous.period_end - previous.period_start).days + 1
    new_days = (current.period_end - current.period_start).days + 1
    if min(old_days, new_days) <= 0 or abs(old_days - new_days) > 8:
        return None
    # Calendar and 52/53-week fiscal years can differ by a week. Both endpoints
    # must advance a year; a quarter versus YTD cannot pass the duration gate.
    starts = (current.period_start - previous.period_start).days
    ends = (current.period_end - previous.period_end).days
    if 350 <= starts <= 380 and 350 <= ends <= 380:
        return "year_over_year"
    quarter = 80 <= old_days <= 100 and 80 <= new_days <= 100
    annual = 350 <= old_days <= 380 and 350 <= new_days <= 380
    if (quarter or annual) and current.period_start == previous.period_end + timedelta(days=1):
        return "sequential"
    return None


def _changed_spans(pair: AlignedPair) -> tuple[dict[str, list[str]], bool]:
    spans: dict[str, list[str]] = {"previous": [], "current": []}
    truncated = False

    def append(side: str, paragraph: FilingParagraph, start: int, end: int):
        nonlocal truncated
        if start == end:
            return
        if len(spans[side]) == MAX_CHANGED_SPANS:
            truncated = True
            return
        spans[side].append(
            f"[{paragraph.filing_accession} | {paragraph.paragraph_id} | chars {start}:{end}] "
            + paragraph.text[start:end]
        )

    if pair.old is None or pair.new is None:
        for side, paragraph in (("previous", pair.old), ("current", pair.new)):
            if paragraph is not None:
                append(side, paragraph, 0, len(paragraph.text))
        return spans, truncated
    old_tokens = list(re.finditer(r"\S+", pair.old.text))
    new_tokens = list(re.finditer(r"\S+", pair.new.text))
    matcher = SequenceMatcher(
        None, [word[0] for word in old_tokens], [word[0] for word in new_tokens], autojunk=False
    )
    for operation, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if operation == "equal":
            continue
        for side, paragraph, tokens, start, end in (
            ("previous", pair.old, old_tokens, old_start, old_end),
            ("current", pair.new, new_tokens, new_start, new_end),
        ):
            if start < end:
                append(side, paragraph, tokens[start].start(), tokens[end - 1].end())
    return spans, truncated


class _FilingIndex:
    def __init__(self, filing: Filing, paragraphs: list[FilingParagraph], evidence: FilingEvidence):
        if evidence.filing_accession != filing.accession or any(
            value.filing_accession != filing.accession
            for value in [*paragraphs, *evidence.facts, *evidence.tables]
        ):
            raise ValueError("Evidence and paragraphs must belong to their indicated filing")
        self.filing = filing
        self.evidence = evidence
        self.paragraphs = sorted(paragraphs, key=lambda value: (value.ordinal, value.paragraph_id))
        self.positions = {value.paragraph_id: index for index, value in enumerate(self.paragraphs)}
        self.contexts = dict(
            zip(
                (value.paragraph_id for value in self.paragraphs),
                paragraph_contexts(self.paragraphs),
            )
        )
        self.paragraph_lookup = _Lookup(
            [value.text + " " + (value.section or "") for value in self.paragraphs]
        )
        self.facts = sorted(evidence.facts, key=lambda value: value.fact_id)
        observation_values: dict[tuple, set[Decimal]] = defaultdict(set)
        for fact in self.facts:
            if fact.value.is_finite():
                observation_values[_observation_key(fact)].add(fact.value)
        self.conflicting_observations = {
            key for key, values in observation_values.items() if len(values) > 1
        }
        self.fact_lookup = _Lookup(
            [
                value.concept.rsplit(":", 1)[-1]
                + " "
                + value.label
                + " "
                + " ".join(value.dimensions.values())
                for value in self.facts
            ]
        )
        self.fact_metrics = [_metric_tokens(value) for value in self.facts]
        self.paragraph_facts: dict[str, list[int]] = defaultdict(list)
        for index, fact in enumerate(self.facts):
            for paragraph_id in fact.paragraph_ids:
                self.paragraph_facts[paragraph_id].append(index)
        self.tables = sorted(evidence.tables, key=lambda value: value.table_id)
        self.table_lookup = _Lookup(
            [
                value.caption + " " + " ".join(" ".join(row) for row in value.rows)
                for value in self.tables
            ]
        )
        self.row_lookups = [_Lookup([" ".join(row) for row in value.rows]) for value in self.tables]
        self.denominators: dict[str, list[SourceFact]] = defaultdict(list)
        for fact in self.facts:
            if (
                fact.dimensions
                or _observation_key(fact) in self.conflicting_observations
                or not filing.cik.isdigit()
                or not fact.entity.isdigit()
                or int(fact.entity) != int(filing.cik)
                or filing.period_of_report is None
                or fact.period_end != filing.period_of_report
                or not fact.value.is_finite()
                or not fact.quote.strip()
            ):
                continue
            for category, concepts in _DENOMINATORS.items():
                if fact.concept.rsplit(":", 1)[-1] in concepts:
                    self.denominators[category].append(fact)
        for facts in self.denominators.values():
            # A short reported period is more useful than an overlapping YTD total;
            # the dates remain explicit, and denominators never enter arithmetic.
            facts.sort(
                key=lambda fact: (
                    (fact.period_end - fact.period_start).days if fact.period_start else 0,
                    fact.fact_id,
                )
            )

    def neighbors(self, target: FilingParagraph | None) -> list[FilingParagraph]:
        position = self.positions.get(target.paragraph_id) if target else None
        if position is None:
            return []
        return (
            self.paragraphs[max(0, position - 1) : position]
            + self.paragraphs[position + 1 : position + 2]
        )

    def related(
        self, query: frozenset[str], target: FilingParagraph | None
    ) -> list[FilingParagraph]:
        scores = self.paragraph_lookup.scores(query)
        target_context = self.contexts.get(target.paragraph_id) if target else None
        for index in list(scores):
            paragraph = self.paragraphs[index]
            overlap = query & self.paragraph_lookup.tokens[index]
            if len(overlap) < 2 and not any(
                len(token) >= 5 and len(self.paragraph_lookup.postings[token]) <= 2
                for token in overlap
            ):
                del scores[index]
                continue
            context = self.contexts[paragraph.paragraph_id]
            if (
                target_context
                and target_context.subsection_key
                and (context.subsection_key == target_context.subsection_key)
            ):
                scores[index] *= 1.5
        return [
            self.paragraphs[index]
            for index in sorted(scores, key=lambda index: (-scores[index], index))
            if target is None or self.paragraphs[index].paragraph_id != target.paragraph_id
        ]

    def select_facts(
        self, query: frozenset[str], target: FilingParagraph | None, channels: list[str]
    ) -> tuple[list[SourceFact], set[str]]:
        scores = self.fact_lookup.scores(query)
        direct = set(self.paragraph_facts.get(target.paragraph_id, ())) if target else set()

        def relevant_dimensions(fact: SourceFact, tokens: frozenset[str]) -> bool:
            return all(
                (_tokens(member.rsplit(":", 1)[-1]) - {"member", "domain", "segment"}) & tokens
                for member in fact.dimensions.values()
            )

        # Lexical overlap retrieves context, but cannot attribute a measurement to
        # the target (e.g. a cash forecast is not a changed cash balance). Only
        # explicit source containment may create automatic target arithmetic.
        target_relevant = direct
        # Numeric-only table quotes and a shared dimension alone do not establish
        # metric relevance. True source containment can establish direct relevance.
        relevant = {
            index
            for index in scores.keys() | direct
            if index in direct
            or (self.fact_metrics[index] & query and relevant_dimensions(self.facts[index], query))
        }
        ranked = sorted(
            relevant,
            key=lambda index: (
                index not in direct,
                index not in target_relevant,
                self.facts[index].period_end != self.filing.period_of_report,
                -scores.get(index, 0),
                self.facts[index].fact_id,
            ),
        )
        selected: list[SourceFact] = []
        seen = set()
        eligible = set()

        def add(fact: SourceFact, *, target_fact: bool):
            identity = (_observation_key(fact), fact.value)
            if (
                identity not in seen
                and fact.value.is_finite()
                and fact.quote.strip()
                and fact.period_end <= self.filing.filed_date
            ):
                seen.add(identity)
                selected.append(fact)
                if target_fact:
                    eligible.add(fact.fact_id)

        for index in ranked:
            add(self.facts[index], target_fact=index in target_relevant)
            if len(selected) == MAX_FACTS - 4:
                break
        if "liquidity_and_financing" in channels:
            categories = ["cash", "cash_flow", "debt", "assets"]
        elif "capital_allocation" in channels:
            categories = ["cash", "assets", "revenue", "shares"]
        else:
            categories = ["revenue", "assets", "cash", "income"]
        if query & {"share", "stock", "director", "trading"}:
            categories = ["shares", *[category for category in categories if category != "shares"]][
                :4
            ]
        for category in categories:
            for fact in self.denominators.get(category, ()):
                before = len(selected)
                add(fact, target_fact=False)
                if len(selected) > before:
                    break
            if len(selected) == MAX_FACTS:
                break
        for index in ranked:
            if len(selected) == MAX_FACTS:
                break
            add(self.facts[index], target_fact=index in target_relevant)
        return selected, eligible

    def select_tables(
        self, query: frozenset[str], facts: list[SourceFact], relevant_ids: set[str]
    ) -> tuple[list[SourceTable], list[str]]:
        scores = self.table_lookup.scores(query - {"net", "value"})
        relevant_table_ids = {fact.table_id for fact in facts if fact.fact_id in relevant_ids}
        for index, table in enumerate(self.tables):
            if table.table_id in relevant_table_ids:
                scores[index] = scores.get(index, 0) + 10
        selected, warnings = [], []
        for index in sorted(
            scores, key=lambda index: (-scores[index], self.tables[index].table_id)
        )[:MAX_TABLES]:
            table = self.tables[index]
            row_scores = self.row_lookups[index].scores(query)
            fact_quotes = {
                " ".join(fact.quote.split()) for fact in facts if fact.table_id == table.table_id
            }
            for row_index, row in enumerate(table.rows):
                if " ".join(" | ".join(row).split()) in fact_quotes:
                    row_scores[row_index] = row_scores.get(row_index, 0) + 10
            # Keep the leading source rows and explicitly marked column headers.
            # No positional inference of periods or units is used for arithmetic.
            headers = {
                row_index
                for row_index, flags in enumerate(table.header_cells)
                if any(flags)
                and all(
                    flag or not cell.strip() for flag, cell in zip(flags, table.rows[row_index])
                )
            }
            preserved = set(range(min(3, len(table.rows)))) | headers
            row_indices = sorted(preserved)[: MAX_TABLE_ROWS // 2]
            for row_index in sorted(row_scores, key=lambda value: (-row_scores[value], value)):
                if row_index not in row_indices:
                    row_indices.append(row_index)
                if len(row_indices) == MAX_TABLE_ROWS:
                    break
            if len(table.rows) <= MAX_TABLE_ROWS:
                row_indices = list(range(len(table.rows)))
            row_indices.sort()
            original_indices = table.row_indices or list(range(len(table.rows)))
            updates = {
                "rows": [table.rows[row_index] for row_index in row_indices],
                "row_indices": [original_indices[row_index] for row_index in row_indices],
                "cell_spans": [table.cell_spans[row_index] for row_index in row_indices]
                if table.cell_spans
                else [],
                "header_cells": [table.header_cells[row_index] for row_index in row_indices]
                if table.header_cells
                else [],
            }
            selected.append(table.model_copy(update=updates))
            if len(row_indices) < len(table.rows):
                warnings.append(
                    f"{table.table_id}: omitted source rows may contain qualifications. "
                    "Noncontiguous rows do not establish column or rowspan associations."
                )
            if headers - set(row_indices):
                warnings.append(
                    f"{table.table_id}: not all source header rows fit in the bounded excerpt."
                )
            if not headers:
                warnings.append(
                    f"{table.table_id}: source column-header associations are unverified; "
                    "numeric periods must come from explicit fact contexts, not cell positions."
                )
        return selected, warnings


class EvidenceIndex:
    """Prepare once per filing pair, then build bounded packets without provider calls."""

    def __init__(
        self,
        comparison: ComparableFilingPair,
        previous_paragraphs: list[FilingParagraph],
        current_paragraphs: list[FilingParagraph],
        previous_evidence: FilingEvidence,
        current_evidence: FilingEvidence,
    ):
        self.comparison = comparison
        self.sides = {
            "previous": _FilingIndex(comparison.previous, previous_paragraphs, previous_evidence),
            "current": _FilingIndex(comparison.current, current_paragraphs, current_evidence),
        }

    def build(
        self, pair: AlignedPair
    ) -> tuple[dict[str, list[FilingParagraph]], ComparisonEvidence]:
        targets = {"previous": pair.old, "current": pair.new}
        queries = {}
        for side, target in targets.items():
            if target is None:
                continue
            index = self.sides[side]
            position = index.positions.get(target.paragraph_id)
            if position is None or index.paragraphs[position] != target:
                raise ValueError(
                    "Target must be an unchanged source paragraph from its indicated filing"
                )
            query = _tokens(target.text)
            context = index.contexts[target.paragraph_id]
            if context.subsection_key:
                query |= _tokens(context.subsection_key)
            # Expand genuinely fragmentary labels, not short complete sentences:
            # unrelated neighbors must not crowd out the target's own mechanism.
            if len(_tokens(target.text)) < 12 and not re.search(
                r"""[.!?]["'’”)\]]*$""", target.text.rstrip()
            ):
                query |= frozenset().union(
                    *(_tokens(value.text) for value in index.neighbors(target))
                )
            queries[side] = query
        shared_query = frozenset().union(*queries.values())
        channels = [name for name, terms in _CHANNEL_TOKENS.items() if terms & shared_query]
        source_context: dict[str, list[FilingParagraph]] = {}
        facts, tables, counterparts, eligible = {}, {}, {}, {}
        uncertainties = [
            "Arithmetic alone does not establish causation, duration, direction or impact.",
            "This bounded packet is not the complete filings; absence is not a zero value. "
            "Company denominators retain their source dates and entity scope.",
            "Unavailable automatic arithmetic does not establish an invalid qualitative "
            "comparison or the absence of a substantive change; inspect the original targets.",
        ]
        for side, index in self.sides.items():
            target = targets[side]
            query = queries.get(side, shared_query)
            related = index.related(query, target)
            counterparts[side] = related[:MAX_COUNTERPARTS] if target is None else []
            selected = []
            seen = {target.paragraph_id} if target else set()
            for paragraph in index.neighbors(target) + counterparts[side] + related:
                if paragraph.paragraph_id not in seen:
                    selected.append(paragraph)
                    seen.add(paragraph.paragraph_id)
                if len(selected) == MAX_PARAGRAPHS:
                    break
            source_context[side] = selected
            facts[side], eligible[side] = index.select_facts(query, target, channels)
            tables[side], table_warnings = index.select_tables(query, facts[side], eligible[side])
            uncertainties.extend(table_warnings)
            uncertainties.extend(f"{side}: {warning}" for warning in index.evidence.warnings[:4])
            if len(index.evidence.warnings) > 4:
                uncertainties.append(
                    f"{side}: additional extraction warnings are retained in filing evidence."
                )
            if any(fact.period_end != index.filing.period_of_report for fact in facts[side]):
                uncertainties.append(
                    f"{side}: historical/comparative facts are within-filing context, not the "
                    "across-filing current-period endpoints; do not confuse a disclosed YoY "
                    "comparison with a sequential change."
                )
            if not facts[side]:
                uncertainties.append(
                    f"{side}: no attributable relevant numeric facts are available in this packet."
                )
        changes = []
        if pair.relation != "matched":
            uncertainties.append(
                "Counterparts are candidates, not matched targets or proof of change, "
                "novelty, removal or resolution."
            )
        elif not scopes_compatible(
            self.sides["previous"].contexts[pair.old.paragraph_id],
            self.sides["current"].contexts[pair.new.paragraph_id],
        ):
            uncertainties.append(
                "Target reporting scopes differ; no cross-scope arithmetic was constructed."
            )
        else:
            changes = self._changes(facts, eligible, targets, uncertainties)
        bases = {change.basis for change in changes}
        if len(bases) == 1:
            basis = next(iter(bases))
            reason = (
                "Same-concept, entity, unit and dimension facts have compatible report periods. "
                "This numeric basis applies to those facts, not every claim in the target."
            )
        elif bases:
            basis = "mixed"
            reason = (
                "The attributable fact comparisons use multiple bases: "
                + ", ".join(sorted(bases))
                + "."
            )
        else:
            basis = "unclear"
            reason = (
                "No verified target comparison exists."
                if pair.relation != "matched"
                else "No target-attributed numeric pair has compatible periods and scope."
            )
            uncertainties.append(
                "Unpaired measurements are unavailable; no zero or inferred value was substituted."
            )
        spans, truncated = _changed_spans(pair)
        if truncated:
            uncertainties.append(
                "Changed spans are bounded; the complete targets preserve all edits."
            )
        evidence = ComparisonEvidence(
            basis=basis,
            basis_reason=reason,
            changed_spans=spans,
            changes=changes,
            context_facts=facts,
            tables=tables,
            counterparts=counterparts,
            possible_channels=channels,
            uncertainties=list(dict.fromkeys(uncertainties)),
        )
        return bound_packet(source_context, evidence)

    def _changes(
        self,
        facts: dict[str, list[SourceFact]],
        eligible: dict[str, set[str]],
        targets: dict[str, FilingParagraph | None],
        uncertainties: list[str],
    ) -> list[FactChange]:
        endpoints: dict[str, list[SourceFact]] = {}
        for side, values in facts.items():
            grouped: dict[tuple, list[SourceFact]] = defaultdict(list)
            target = targets[side]
            scope = self.sides[side].contexts[target.paragraph_id].reporting_scope
            for fact in values:
                if (
                    fact.fact_id not in eligible[side]
                    or self.sides[side].filing.period_of_report is None
                    or fact.period_end != self.sides[side].filing.period_of_report
                ):
                    continue
                days = (fact.period_end - fact.period_start).days + 1 if fact.period_start else None
                if (
                    (scope == "quarter" and (days is None or not 80 <= days <= 100))
                    or (scope == "annual" and (days is None or not 350 <= days <= 380))
                    or (scope == "year_to_date" and (days is None or not 80 <= days <= 380))
                    or (scope == "point_in_time" and days is not None)
                ):
                    continue
                grouped[_observation_key(fact)].append(fact)
            endpoints[side] = []
            for group in grouped.values():
                if _observation_key(group[0]) in self.sides[side].conflicting_observations:
                    uncertainties.append(
                        f"{side}: {group[0].concept} has conflicting values for the same context; "
                        "no arbitrary endpoint was selected."
                    )
                else:
                    endpoints[side].append(group[0])
        candidates = [
            (previous, current, basis)
            for previous in endpoints["previous"]
            for current in endpoints["current"]
            if (basis := _period_basis(previous, current)) is not None
        ]
        old_matches: dict[str, int] = defaultdict(int)
        new_matches: dict[str, int] = defaultdict(int)
        for previous, current, _ in candidates:
            old_matches[previous.fact_id] += 1
            new_matches[current.fact_id] += 1
        changes = []
        for previous, current, basis in candidates:
            if old_matches[previous.fact_id] != 1 or new_matches[current.fact_id] != 1:
                uncertainties.append(
                    f"{previous.concept}: multiple compatible endpoints remain ambiguous."
                )
                continue
            meaningful_base = previous.value > 0 and previous.unit.casefold() not in {
                "pure",
                "percent",
                "%",
                "ratio",
            }
            with localcontext() as context:
                # Enough significant digits for an exact difference even when source
                # scales differ substantially. Only division can require rounding.
                context.prec = max(
                    38,
                    max(previous.value.adjusted(), current.value.adjusted())
                    - min(previous.value.as_tuple().exponent, current.value.as_tuple().exponent)
                    + 3,
                )
                absolute = current.value - previous.value
                percent = absolute / previous.value * Decimal(100) if meaningful_base else None
            changes.append(
                FactChange(
                    metric=current.concept,
                    previous=previous,
                    current=current,
                    basis=basis,
                    absolute_change=absolute,
                    percent_change=percent,
                )
            )
            if not meaningful_base:
                uncertainties.append(
                    f"{previous.concept}: percentage change is unavailable for a non-positive "
                    "base or a dimensionless rate."
                )
        if not changes and endpoints["previous"] and endpoints["current"]:
            uncertainties.append(
                "Available facts lack unambiguous compatible periods, metrics, entities, units "
                "or dimensions; overlapping YTD and different-duration figures were not subtracted."
            )
        return changes
