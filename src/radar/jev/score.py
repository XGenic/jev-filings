"""Heuristic contributions, not probabilities. Missing semantics never become guessed signals."""

import re

from radar.config import RankWeights
from radar.models import AlignedPair, Form, JevSemanticSignals, RankedDelta

DOMAINS = (
    "liquidity_or_financing",
    "supply_or_capacity",
    "customer_concentration",
    "regulatory_or_government",
    "outlook_or_guidance",
    "uncertainty",
)


def rank_pair(
    pair: AlignedPair,
    signals: JevSemanticSignals | None,
    weights: RankWeights,
    evaluation_key: str | None = None,
    semantic_error: str | None = None,
    form: Form | None = None,
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
    )
