"""Presentation-only grouping and bounded selection of persisted judgments."""

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from radar.models import RankedDelta, SourceFact

REPORT_SELECTION_POLICY = "evidence-review-1"


@dataclass
class EvidenceGroup:
    members: list[tuple[int, RankedDelta]]
    historical: bool = False

    @property
    def source_rank(self) -> int:
        """One-based position in the persisted non-skipped candidate order."""
        return self.members[0][0]

    @property
    def lane(self) -> str:
        if self.historical:
            return "historical"
        return "supported" if self.priority in {"high", "medium", "low"} else "review"

    @property
    def leader(self) -> tuple[int, RankedDelta]:
        return min(self.members, key=_lead_order)

    @property
    def priority(self) -> str:
        if all(_supported(delta) for _, delta in self.members):
            return min(
                (delta.priority_band for _, delta in self.members),
                key={"high": 0, "medium": 1, "low": 2}.__getitem__,
            )
        if any(delta.signals and delta.signals.assessment for _, delta in self.members):
            return "review"
        return "unavailable"


@dataclass
class ReportSelection:
    groups: list[EvidenceGroup]
    selected: list[EvidenceGroup]


def _lead_order(member: tuple[int, RankedDelta]) -> tuple:
    rank, delta = member
    assessment = delta.signals.assessment if delta.signals else None
    return (
        {"high": 0, "medium": 1, "low": 2}.get(delta.impact_band, 3) if assessment else 3,
        assessment.business_direction.choice not in {"negative", "mixed"} if assessment else True,
        delta.comparison_reliability == "supported" if assessment else False,
        rank,
    )


def _supported(delta: RankedDelta) -> bool:
    return bool(
        delta.signals
        and delta.signals.assessment
        and not delta.semantic_error
        and delta.comparison_reliability == "supported"
        and delta.priority_band in {"high", "medium", "low"}
    )


def _normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def _fact_signature(fact: SourceFact) -> tuple:
    # Source locations can differ, but metric, entity, scope, period and value cannot.
    return (
        fact.filing_accession,
        fact.concept,
        _normalized(fact.entity),
        fact.unit,
        fact.period_start,
        fact.period_end,
        tuple(sorted(fact.dimensions.items())),
        fact.value,
    )


def _signature(delta: RankedDelta) -> tuple | None:
    evidence = delta.comparison_evidence
    if evidence and evidence.changes:
        if any(
            not fact.concept or not fact.entity or not fact.unit
            for change in evidence.changes
            for fact in (change.previous, change.current)
        ):
            return None
        text = tuple(
            _normalized(paragraph.text) if paragraph else None
            for paragraph in (delta.pair.old, delta.pair.new)
        )
        # Identical tagged totals do not account for a changed qualification or
        # an additional untagged amount elsewhere in either target. Preserve both
        # before treating two passages as repeated evidence for one change.
        words = [re.findall(r"[^\W\d_]+", side or "") for side in text]
        qualitative_edits = tuple(
            (tag, tuple(words[0][start:end]), tuple(words[1][new_start:new_end]))
            for tag, start, end, new_start, new_end in SequenceMatcher(
                None, words[0], words[1], autojunk=False
            ).get_opcodes()
            if tag != "equal"
        )
        numeric_text = tuple(
            tuple(re.findall(r"[-+]?\d+(?:[,.]\d+)*", side or "")) for side in text
        )
        # Identical aggregate amounts need not describe the same transaction.
        # Event-bearing passages additionally require identical event text.
        event_text = (
            text
            if any(
                re.search(
                    r"\b(?:acqui\w*|merg\w*|transaction\w*|divest\w*|dispos\w*|"
                    r"settlement\w*|agreement\w*|contract\w*|offering\w*|issuance\w*)\b",
                    side or "",
                )
                for side in text
            )
            else None
        )
        return (
            "facts",
            delta.pair.relation,
            event_text,
            qualitative_edits,
            numeric_text,
            frozenset(
                (
                    _normalized(change.metric),
                    change.basis,
                    _fact_signature(change.previous),
                    _fact_signature(change.current),
                    change.absolute_change,
                    change.percent_change,
                )
                for change in evidence.changes
            ),
        )

    # No fuzzy matching, subject-based merge or transitive similarity clustering.
    # Without tagged facts, require duplicated complete event text on every side,
    # explicit numeric/date evidence and matching recorded reporting scope.
    alignment = delta.pair.alignment
    if alignment is None:
        return None
    sides = []
    for paragraph, context in (
        (delta.pair.old, alignment.old_context),
        (delta.pair.new, alignment.new_context),
    ):
        if paragraph is None:
            sides.append(None)
            continue
        text = _normalized(paragraph.text)
        if (
            len(text.split()) < 12
            or not re.search(r"\b(?:19|20)\d{2}\b", text)
            or context is None
            or context.reporting_scope in {"unknown", "mixed"}
            or not context.scope_evidence
        ):
            return None
        sides.append(
            (
                paragraph.filing_accession,
                paragraph.item,
                text,
                context.reporting_scope,
                tuple(sorted(_normalized(value) for value in context.scope_evidence)),
            )
        )
    return ("text", delta.pair.relation, *sides)


def group_candidates(eligible: list[RankedDelta], historical: bool) -> list[EvidenceGroup]:
    """Group only exact evidence identities; never mutate stored deltas or ranks."""
    groups: list[EvidenceGroup] = []
    by_signature: dict[tuple, EvidenceGroup] = {}
    for rank, delta in enumerate(eligible, 1):
        signature = None if historical else _signature(delta)
        if signature is not None and signature in by_signature:
            by_signature[signature].members.append((rank, delta))
        else:
            group = EvidenceGroup(members=[(rank, delta)], historical=historical)
            groups.append(group)
            if signature is not None:
                by_signature[signature] = group
    return groups


def select_groups(groups: list[EvidenceGroup], top_n: int, historical: bool) -> list[EvidenceGroup]:
    """One total card budget: material supported groups outrank review reservation."""
    if historical:
        return groups[:top_n]
    buckets = {priority: [] for priority in ("high", "medium", "low", "review", "unavailable")}
    for group in groups:
        buckets[group.priority].append(group)
    selected = (buckets["high"] + buckets["medium"])[:top_n]
    remaining = top_n - len(selected)
    review = buckets["review"]
    reserved = min(max(1, top_n // 4), remaining, len(review))
    selected.extend(review[:reserved])
    remaining -= reserved
    low = buckets["low"][:remaining]
    selected.extend(low)
    remaining -= len(low)
    selected.extend((review[reserved:] + buckets["unavailable"])[:remaining])
    # Presentation lanes partition this subset; order within each lane follows
    # the original pipeline ranking, not an invented report-level score.
    return sorted(selected, key=lambda group: group.members[0][0])


def build_report_selection(
    deltas: list[RankedDelta], top_n: int, historical: bool
) -> ReportSelection:
    """Return full groups and selected groups without copying or judging deltas.

    Callers supply the run-wide historical flag (no assessments or comparison
    evidence anywhere in the run). Members and leaders are original objects,
    paired with one-based persisted ranks after skipped candidates are excluded.
    """
    if top_n < 1:
        raise ValueError("top_n must be positive")
    eligible = [delta for delta in deltas if delta.pair.skip_reason is None]
    groups = group_candidates(eligible, historical)
    return ReportSelection(groups=groups, selected=select_groups(groups, top_n, historical))
