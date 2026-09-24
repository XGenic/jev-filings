"""Heuristic contributions, not probabilities. Missing semantics never become guessed signals."""

import re

from radar.config import RankWeights
from radar.jev.questions import BUSINESS_QUESTIONS, QUESTION_SCHEMA_VERSION
from radar.models import (
    AlignedPair,
    ComparisonEvidence,
    FilingParagraph,
    Form,
    JevSemanticSignals,
    RankedDelta,
)

DOMAINS = (
    "liquidity_or_financing",
    "supply_or_capacity",
    "customer_concentration",
    "regulatory_or_government",
    "outlook_or_guidance",
    "uncertainty",
)

# A decision threshold, not calibrated confidence or a probability of financial impact.
ASSESSMENT_DECISION_FLOOR = 0.6
RANKING_POLICY_VERSION = "business-impact-1"
_IMPACT_ORDER = {"high": 0, "medium": 1, "low": 2, "unclear": 3}
_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2, "review": 3, "unavailable": 4}


def _business_priority(pair: AlignedPair, signals: JevSemanticSignals | None) -> dict:
    assessment = signals.assessment if signals is not None else None
    if pair.skip_reason or assessment is None:
        return {"priority_band": "unavailable", "comparison_reliability": "unavailable"}
    impact = assessment.business_impact
    band = (
        impact.choice
        if impact.probabilities.get(impact.choice, 0) >= ASSESSMENT_DECISION_FLOOR
        else "unclear"
    )
    validity = assessment.comparison_validity
    alignment = pair.alignment
    if pair.relation != "matched":
        reliability = "unmatched"
        reason = (
            "This assesses the disclosed matter, not an established change. No counterpart "
            "was selected; novelty, disappearance and resolution are unverified."
        )
    elif alignment is None:
        reliability = "unavailable"
        reason = "Alignment evidence is unavailable; comparison validity needs review."
    elif (
        alignment.status not in {"aligned", "exact", "cosmetic"}
        or alignment.review_reasons
        or pair.warnings
        or validity.choice != "comparable"
        or validity.probabilities.get("comparable", 0) < ASSESSMENT_DECISION_FLOOR
    ):
        reliability = "needs_review"
        reason = (
            "Alignment warnings or an uncertain/incompatible semantic comparison prevent "
            "treating this as a supported change."
        )
    else:
        reliability = "supported"
        reason = (
            "The alignment and semantic comparison support reviewing this as a change; "
            "this is not independent verification."
        )
    priority = band if reliability == "supported" and band != "unclear" else "review"
    current_rubric = signals.question_schema_version == QUESTION_SCHEMA_VERSION
    explanation = [
        "Rubric assessment: " + BUSINESS_QUESTIONS["business_impact"]["criteria"][band]
        if current_rubric
        else f"Persisted {signals.question_schema_version} impact assessment: {band}. "
        "Original criteria are not substituted with the current rubric.",
        reason,
    ]
    if band == "unclear" and impact.choice != "unclear":
        explanation.append(
            "The selected impact label did not reach the 0.60 decision threshold; "
            "impact remains unclear."
        )
    if current_rubric:
        for name in ("impact_magnitude", "impact_evidence"):
            judgment = getattr(assessment, name)
            explanation.append(BUSINESS_QUESTIONS[name]["criteria"][judgment.choice])
    return {
        "impact_band": band,
        "priority_band": priority,
        "comparison_reliability": reliability,
        "impact_explanation": " ".join(explanation),
    }


def ranking_key(delta: RankedDelta) -> tuple[int, int, int, float]:
    """Supported impact bands first; review/unavailable queues never masquerade as leads.

    Within each queue, impact precedes the existing weighted heuristic. Direction is
    deliberately absent: equally significant adverse and favorable changes rank equally.
    Historical results have no bands and retain their weighted-score ordering.
    """
    return (
        int(delta.pair.skip_reason is not None),
        _PRIORITY_ORDER.get(delta.priority_band, 4),
        _IMPACT_ORDER.get(delta.impact_band, 4),
        -delta.score,
    )


def rank_pair(
    pair: AlignedPair,
    signals: JevSemanticSignals | None,
    weights: RankWeights,
    evaluation_key: str | None = None,
    semantic_error: str | None = None,
    form: Form | None = None,
    source_context: dict[str, list[FilingParagraph]] | None = None,
    comparison_evidence: ComparisonEvidence | None = None,
) -> RankedDelta:
    components: dict[str, float] = {}
    if not pair.skip_reason:
        if pair.cosine_similarity is not None:
            components["embedding_drift"] = weights.embedding_drift * (1 - pair.cosine_similarity)
        elif pair.lexical_similarity is not None:
            components["lexical_drift_fallback"] = weights.embedding_drift * (
                1 - pair.lexical_similarity
            )
        else:
            # Structural novelty is not evidence of semantic importance or resolved risk.
            components["unpaired_disclosure"] = weights.embedding_drift
        if signals is not None:
            if signals.same_underlying_meaning is not None:
                components["semantic_drift"] = weights.semantic_drift * (
                    1 - signals.same_underlying_meaning
                )
            substantive = signals.introduces_new_substantive_information
            if pair.relation != "matched":
                substantive = signals.substantive_disclosure
            if substantive is not None:
                components["substantive_information"] = (
                    weights.substantive_information * substantive
                )
            if signals.plausibly_economically_consequential is not None:
                components["economic_relevance"] = (
                    weights.economic_relevance * signals.plausibly_economically_consequential
                )
            if signals.mostly_boilerplate_or_rephrasing is not None:
                components["non_boilerplate"] = weights.non_boilerplate * (
                    1 - signals.mostly_boilerplate_or_rephrasing
                )
            domain_strength = 0.0
            for domain in DOMAINS:
                probabilities = getattr(signals, f"{domain}_probabilities") or {}
                if pair.relation == "matched":
                    directional = probabilities.get(
                        "introduced_or_increased", 0
                    ) + probabilities.get("reduced_or_resolved", 0)
                elif pair.relation == "added":
                    directional = probabilities.get("present", 0)
                else:
                    directional = 0.0
                domain_strength = max(domain_strength, min(1, max(0, directional)))
            components["domain_boost"] = weights.domain_boost * domain_strength
        paragraph = pair.new or pair.old
        item = (paragraph.item or "").casefold()
        section = (paragraph.section or "").casefold()
        item_number = re.search(r"\bitem\s+(\d+[a-z]?)\b", item)
        number = item_number.group(1) if item_number else ""
        if form == "10-Q":
            priority = ("part i /" in item and number == "2") or (
                "part ii" in item and number in {"1", "1a"}
            )
        else:
            priority = number in {"1", "1a", "3", "7"}
        priority = priority or "liquidity" in section or "capital resources" in section
        components["section_boost"] = weights.section_boost if priority else 0.0
    return RankedDelta(
        pair=pair,
        score=sum(components.values()),
        components=components,
        signals=signals,
        evaluation_key=evaluation_key,
        semantic_error=semantic_error,
        source_context=source_context or {},
        **_business_priority(pair, signals),
        comparison_evidence=comparison_evidence,
    )
