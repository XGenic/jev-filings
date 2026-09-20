import pytest

from radar.config import RankWeights
from radar.jev.score import rank_pair
from radar.models import AlignedPair, FilingParagraph, JevSemanticSignals


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
