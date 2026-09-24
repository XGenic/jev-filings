import pytest

from radar.config import RankWeights
from radar.jev.questions import BUSINESS_QUESTIONS
from radar.jev.score import rank_pair, ranking_key
from radar.models import (
    AlignedPair,
    AlignmentEvidence,
    BusinessAssessment,
    FilingParagraph,
    JevSemanticSignals,
)


def pair(relation="matched"):
    paragraph = FilingParagraph(
        filing_accession="test",
        paragraph_id="test:0",
        ordinal=0,
        text="A substantive business disclosure.",
        normalized_text="same",
        text_hash="test-hash",
        item="Item 7",
    )
    return AlignedPair(
        old=None if relation == "added" else paragraph,
        new=None if relation == "deleted" else paragraph,
        relation=relation,
        cosine_similarity=0.8 if relation == "matched" else None,
        lexical_similarity=0.5 if relation == "matched" else None,
    )


def test_ranking_matches_documented_formula_and_visible_contributions():
    signals = JevSemanticSignals(
        question_schema_version="test",
        same_underlying_meaning=0.2,
        introduces_new_substantive_information=0.8,
        plausibly_economically_consequential=0.9,
        mostly_boilerplate_or_rephrasing=0.1,
    )
    ranked = rank_pair(pair(), signals, RankWeights())
    assert ranked.score == pytest.approx(
        0.25 * 0.2 + 0.25 * 0.8 + 0.20 * 0.8 + 0.20 * 0.9 + 0.10 * 0.9 + 0.03
    )
    assert sum(ranked.components.values()) == ranked.score
    reranked = rank_pair(pair(), signals, RankWeights(economic_relevance=0))
    assert ranked.score - reranked.score == pytest.approx(0.18)


def test_baseline_does_not_impute_missing_semantic_probabilities():
    baseline = rank_pair(pair(), None, RankWeights())
    assert baseline.signals is None
    assert baseline.score == pytest.approx(0.25 * 0.2 + 0.03)
    assert "semantic_drift" not in baseline.components


def test_removed_risk_does_not_automatically_gain_resolution_boost():
    signals = JevSemanticSignals(
        question_schema_version="test",
        substantive_disclosure=0.9,
        removal_consistent_with_resolution=0.9,
        liquidity_or_financing_direction="present",
        liquidity_or_financing_probabilities={"present": 1},
    )
    removed = rank_pair(pair("deleted"), signals, RankWeights())
    added = rank_pair(pair("added"), signals, RankWeights())
    assert removed.components["domain_boost"] == 0
    assert added.components["domain_boost"] == pytest.approx(0.05)


def test_section_priority_distinguishes_annual_business_from_quarterly_statements():
    disclosure = pair()
    disclosure.old.item = disclosure.new.item = "Part I / Item 1"
    annual = rank_pair(disclosure, None, RankWeights(), form="10-K")
    quarterly = rank_pair(disclosure, None, RankWeights(), form="10-Q")
    assert annual.score - quarterly.score == pytest.approx(0.03)
    disclosure.old.item = disclosure.new.item = "Part II / Item 7"
    assert (
        rank_pair(disclosure, None, RankWeights(), form="10-K").components["section_boost"] == 0.03
    )


def impact_signals(**choices):
    selected = dict.fromkeys(BUSINESS_QUESTIONS, "unclear")
    selected.update(comparison_validity="comparable", business_impact="high")
    selected.update(choices)
    return JevSemanticSignals(
        question_schema_version="filing-delta-2",
        assessment=BusinessAssessment.model_validate(
            {
                name: {
                    "choice": choice,
                    "probabilities": {
                        label: float(label == choice)
                        for label in BUSINESS_QUESTIONS[name]["criteria"]
                    },
                }
                for name, choice in selected.items()
            }
        ),
    )


def test_impact_priority_is_direction_neutral_and_precedes_drift():
    comparable = pair()
    comparable.alignment = AlignmentEvidence(status="aligned")
    adverse = rank_pair(comparable, impact_signals(business_direction="negative"), RankWeights())
    favorable = rank_pair(comparable, impact_signals(business_direction="positive"), RankWeights())
    routine = rank_pair(
        comparable,
        impact_signals(business_impact="low", change_nature="routine_update"),
        RankWeights(embedding_drift=100),
    )
    assert adverse.impact_band == favorable.impact_band == "high"
    assert adverse.priority_band == favorable.priority_band == "high"
    assert ranking_key(adverse) == ranking_key(favorable)
    assert routine.score > adverse.score
    assert sorted([routine, adverse], key=ranking_key) == [adverse, routine]


@pytest.mark.parametrize(
    ("relation", "alignment", "validity", "expected"),
    [
        ("matched", AlignmentEvidence(status="aligned"), "comparable", "supported"),
        ("matched", AlignmentEvidence(status="review"), "comparable", "needs_review"),
        ("matched", AlignmentEvidence(status="aligned"), "not_comparable", "needs_review"),
        ("matched", AlignmentEvidence(status="aligned"), "unclear", "needs_review"),
        ("matched", None, "comparable", "unavailable"),
        ("added", AlignmentEvidence(status="unmatched"), "comparable", "unmatched"),
        ("deleted", AlignmentEvidence(status="unmatched"), "comparable", "unmatched"),
    ],
)
def test_high_impact_does_not_establish_comparison_validity(
    relation, alignment, validity, expected
):
    disclosure = pair(relation)
    disclosure.alignment = alignment
    ranked = rank_pair(disclosure, impact_signals(comparison_validity=validity), RankWeights())
    assert ranked.impact_band == "high"
    assert ranked.comparison_reliability == expected
    assert ranked.priority_band == ("high" if expected == "supported" else "review")


def test_weak_or_missing_assessments_never_become_definite_impact():
    disclosure = pair()
    disclosure.alignment = AlignmentEvidence(status="aligned")
    signals = impact_signals()
    signals.assessment.business_impact.probabilities = {
        "high": 0.599,
        "medium": 0.201,
        "low": 0.1,
        "unclear": 0.1,
    }
    ranked = rank_pair(disclosure, signals, RankWeights())
    assert ranked.impact_band == "unclear"
    assert ranked.priority_band == "review"
    signals.assessment.business_impact.probabilities = {
        "high": 0.6,
        "medium": 0.2,
        "low": 0.1,
        "unclear": 0.1,
    }
    assert rank_pair(disclosure, signals, RankWeights()).priority_band == "high"
    signals.assessment.comparison_validity.probabilities = {
        "comparable": 0.59,
        "not_comparable": 0.21,
        "unclear": 0.2,
    }
    assert rank_pair(disclosure, signals, RankWeights()).priority_band == "review"
    for missing in (None, JevSemanticSignals(question_schema_version="filing-delta-1")):
        unavailable = rank_pair(disclosure, missing, RankWeights())
        assert unavailable.impact_band is None
        assert unavailable.priority_band == "unavailable"
