"""All provider calls are fixtures; no test needs credentials or network access."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from radar.db import Database
from radar.evidence_packet import bound_packet
from radar.jev import questions
from radar.jev.client import JevEvaluator, JevUnavailableError
from radar.jev.schemas import JevResponseError
from radar.models import (
    AlignedPair,
    ComparableFilingPair,
    ComparisonEvidence,
    Filing,
    FilingParagraph,
    JevSemanticSignals,
    RankedDelta,
    SourceFact,
)


def complete_response(definitions):
    answers = {}
    for name, question in definitions.items():
        if question["type"] == "noul":
            answers[name] = {"type": "noul", "noul": 0.5}
        else:
            labels = list(question["criteria"])
            answers[name] = {
                "type": "choice",
                "choice": "unclear",
                "confidence": 0.45,
                "probabilities": {
                    label: (0.7 if label == "unclear" else 0.3 / (len(labels) - 1))
                    for label in labels
                },
            }
    return {
        "model": "fixture-resolved-model",
        "usage": {"input_tokens": 80, "output_tokens": 20},
        "answers": answers,
        "future_metadata": {"preserve": True},
    }


class FixtureProvider:
    identity = "fixture:jev-v1"

    def __init__(self, transform=None):
        self.calls = []
        self.transform = transform

    def system_one(self, state, definitions, *, model):
        self.calls.append((deepcopy(state), deepcopy(definitions), model))
        response = complete_response(definitions)
        return self.transform(response) if self.transform else response


@pytest.fixture
def inputs(tmp_path):
    def filing(accession, year):
        return Filing(
            ticker="TEST",
            cik="0000000011",
            company_name="Synthetic Company",
            form="10-K",
            accession=accession,
            filed_date=date(year, 2, 1),
            period_of_report=date(year - 1, 12, 31),
            primary_document="synthetic.htm",
            source_url=f"https://example.invalid/{accession}",
            local_path=Path("unused"),
        )

    comparison = ComparableFilingPair(current=filing("new", 2026), previous=filing("old", 2025))

    def paragraph(accession, text):
        return FilingParagraph(
            filing_accession=accession,
            paragraph_id=f"{accession}:1",
            section="Liquidity",
            item="Item 7",
            ordinal=1,
            text=text,
            normalized_text=text,
            text_hash=sha256(text.encode()).hexdigest(),
        )

    pair = AlignedPair(
        old=paragraph("old", "We have sufficient financing."),
        new=paragraph("new", "We do not have sufficient financing."),
        relation="matched",
    )
    return Database(tmp_path / "radar.sqlite"), pair, comparison


def test_repeated_evaluation_preserves_provenance_without_another_call(inputs):
    db, pair, comparison = inputs
    provider = FixtureProvider()
    first = JevEvaluator(db, provider=provider).evaluate(pair, comparison)
    second = JevEvaluator(db, provider=provider).evaluate(pair, comparison)
    assert first == second
    assert len(provider.calls) == 1
    assert first.raw_response["usage"]["input_tokens"] == 80
    assert first.raw_response["future_metadata"] == {"preserve": True}
    assert first.raw_response["answers"]["business_impact"]["confidence"] == 0.45
    assert first.signals.same_underlying_meaning == 0.5
    for name, definition in questions.BUSINESS_QUESTIONS.items():
        assessment = getattr(first.signals.assessment, name)
        assert assessment.choice == "unclear"
        assert set(assessment.probabilities) == set(definition["criteria"])
        assert assessment.probabilities["unclear"] == pytest.approx(0.7)
    assert first.requested_at.tzinfo is not None
    assert first.latency_seconds >= 0


def test_alignment_evidence_changes_do_not_invalidate_semantic_cache(inputs):
    from radar.models import AlignmentEvidence, ParagraphContext

    db, pair, comparison = inputs
    provider = FixtureProvider()
    evaluator = JevEvaluator(db, provider=provider)
    first = evaluator.evaluate(pair, comparison)
    pair.alignment = AlignmentEvidence(
        status="review",
        old_context=ParagraphContext(subsection_key="liquidity", reporting_scope="quarter"),
        new_context=ParagraphContext(subsection_key="liquidity", reporting_scope="quarter"),
        candidate_tier="subsection",
        assignment_margin=0.01,
        review_reasons=["near_tied_assignment"],
    )
    pair.warnings = ["A different alignment explanation must not change the semantic state."]
    second = evaluator.evaluate(pair, comparison)
    assert second.cache_key == first.cache_key
    assert second.raw_response == first.raw_response
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        "schema",
        "definitions",
        "company",
        "period",
        "item",
        "model",
        "relation",
        "hash",
        "text",
        "provider",
    ],
)
def test_cache_invalidates_semantic_inputs(inputs, monkeypatch, change):
    db, pair, comparison = inputs
    provider = FixtureProvider()
    evaluator = JevEvaluator(db, model="model-a", provider=provider)
    first = evaluator.evaluate(pair, comparison)
    if change == "schema":
        monkeypatch.setattr(questions, "QUESTION_SCHEMA_VERSION", "changed-schema")
    elif change == "definitions":
        definitions = deepcopy(questions.MATCHED_QUESTIONS)
        definitions["same_underlying_meaning"]["instructions"] += " Revised rubric."
        monkeypatch.setitem(questions.QUESTIONS_BY_RELATION, "matched", definitions)
    elif change == "company":
        comparison = comparison.model_copy(deep=True)
        comparison.current.cik = "0000000022"
        comparison.current.company_name = "Other Synthetic Company"
    elif change == "period":
        comparison = comparison.model_copy(deep=True)
        comparison.current.period_of_report = date(2025, 9, 30)
    elif change == "item":
        pair = pair.model_copy(deep=True)
        pair.new.item = "Item 1A"
    elif change == "model":
        evaluator = JevEvaluator(db, model="model-b", provider=provider)
    elif change == "relation":
        pair = AlignedPair(old=None, new=pair.new, relation="added")
    elif change == "hash":
        pair = pair.model_copy(deep=True)
        pair.new.text_hash = "different-hash"
    elif change == "text":
        pair = pair.model_copy(deep=True)
        pair.new.text = "Changed text even if a caller supplied a stale hash."
    else:
        provider.identity = "fixture:other-provider"
        evaluator = JevEvaluator(db, model="model-a", provider=provider)
    changed = evaluator.evaluate(pair, comparison)
    assert changed.cache_key != first.cache_key
    assert len(provider.calls) == 2


def test_unmatched_assesses_disclosed_matter_without_old_to_new_binary_judgments(inputs):
    db, pair, comparison = inputs
    provider = FixtureProvider()
    evaluator = JevEvaluator(db, provider=provider)
    matched = evaluator.evaluate(pair, comparison)
    added = evaluator.evaluate(AlignedPair(old=None, new=pair.new, relation="added"), comparison)
    deleted = evaluator.evaluate(
        AlignedPair(old=pair.old, new=None, relation="deleted"), comparison
    )
    assert len(provider.calls) == 3
    for evaluation in (added, deleted):
        assert evaluation.signals.assessment == matched.signals.assessment
        assert evaluation.signals.same_underlying_meaning is None
        assert evaluation.signals.introduces_new_substantive_information is None
        assert evaluation.signals.mostly_boilerplate_or_rephrasing is None
        assert evaluation.signals.substantive_disclosure == 0.5
    assert added.signals.removal_consistent_with_resolution is None
    assert deleted.signals.removal_consistent_with_resolution is None


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "missing_dimension",
        "type",
        "noul_boolean",
        "noul_range",
        "nan",
        "labels",
        "sum",
        "choice",
        "argmax",
        "confidence",
    ],
)
def test_invalid_answers_are_rejected_and_never_successfully_cached(inputs, failure):
    db, pair, comparison = inputs

    def damage(response):
        noul = response["answers"]["same_underlying_meaning"]
        choice = response["answers"]["business_impact"]
        if failure == "missing":
            del response["answers"]["same_underlying_meaning"]
        elif failure == "missing_dimension":
            del response["answers"]["business_impact"]
        elif failure == "type":
            noul["type"] = "bool"
        elif failure == "noul_boolean":
            noul["noul"] = True
        elif failure == "noul_range":
            noul["noul"] = 1.1
        elif failure == "nan":
            noul["noul"] = float("nan")
        elif failure == "labels":
            del choice["probabilities"]["unclear"]
        elif failure == "sum":
            choice["probabilities"] = dict.fromkeys(choice["probabilities"], 0.1)
        elif failure == "choice":
            choice["choice"] = "invented-label"
        elif failure == "argmax":
            choice["choice"] = "high"
        else:
            del choice["confidence"]
        return response

    provider = FixtureProvider(damage)
    evaluator = JevEvaluator(db, provider=provider)
    with pytest.raises(JevResponseError):
        evaluator.evaluate(pair, comparison)
    provider.transform = None
    valid = evaluator.evaluate(pair, comparison)
    assert len(provider.calls) == 2
    assert valid.signals.same_underlying_meaning == 0.5


def test_injected_provider_must_have_explicit_identity(inputs):
    db, _, _ = inputs
    with pytest.raises(ValueError, match="identity"):
        JevEvaluator(db, provider=object())


def test_missing_key_is_explicit_and_does_not_import_sdk(inputs, monkeypatch):
    import sys

    db, pair, comparison = inputs
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)
    with pytest.raises(JevUnavailableError, match="TYPESAFE_API_KEY"):
        JevEvaluator(db).evaluate(pair, comparison)


def test_real_sdk_shapes_and_raw_metadata_with_mock_transport(inputs, monkeypatch):
    sdk = pytest.importorskip("typesafe_sdk")
    httpx2 = pytest.importorskip("httpx2")
    db, pair, comparison = inputs
    calls = []

    def handler(request):
        import json

        payload = json.loads(request.content)
        calls.append(payload)
        return httpx2.Response(
            200,
            json=complete_response(payload["questions"]),
            headers={"x-typesafe-request-id": "fixture-request"},
        )

    original_client = sdk.TypeSafeClient
    monkeypatch.setenv("TYPESAFE_API_KEY", "fixture-not-a-real-key")
    monkeypatch.setattr(
        sdk,
        "TypeSafeClient",
        lambda **kwargs: original_client(
            **kwargs,
            transport=httpx2.MockTransport(handler),
        ),
    )
    evaluator = JevEvaluator(db)
    evaluation = evaluator.evaluate(pair, comparison)
    assert len(calls) == 1
    assert evaluation.raw_response["future_metadata"] == {"preserve": True}
    assert evaluation.raw_response["usage"]["output_tokens"] == 20
    assert evaluation.signals.assessment.business_impact.probabilities["unclear"] == pytest.approx(
        0.7
    )
    monkeypatch.delenv("TYPESAFE_API_KEY")
    monkeypatch.setitem(__import__("sys").modules, "typesafe_sdk", None)
    assert JevEvaluator(db).evaluate(pair, comparison) == evaluation
    assert len(calls) == 1


def test_injected_sdk_response_object_is_supported(inputs):
    sdk = pytest.importorskip("typesafe_sdk")
    db, pair, comparison = inputs
    provider = FixtureProvider(lambda raw: sdk.SystemOneResponse.model_validate(raw))
    result = JevEvaluator(db, provider=provider).evaluate(pair, comparison)
    assert result.signals.same_underlying_meaning == 0.5
    assert result.signals.assessment.business_impact.choice == "unclear"
    assert result.raw_response["answers"]["business_impact"]["confidence"] == 0.45


def test_source_context_changes_invalidate_cache_even_with_stale_text_hash(inputs):
    db, pair, comparison = inputs
    provider = FixtureProvider()
    evaluator = JevEvaluator(db, provider=provider)
    context = {
        "previous": [
            pair.old.model_copy(
                update={
                    "paragraph_id": "old:0",
                    "ordinal": 0,
                    "text": "The following amounts relate to annual company-wide cash flows.",
                }
            )
        ],
        "current": [
            pair.new.model_copy(
                update={
                    "paragraph_id": "new:0",
                    "ordinal": 0,
                    "text": "The following amounts relate to annual company-wide cash flows.",
                }
            )
        ],
    }
    without_context = evaluator.evaluate(pair, comparison)
    first = evaluator.evaluate(pair, comparison, source_context=context)
    assert first.cache_key != without_context.cache_key
    assert evaluator.evaluate(pair, comparison, source_context=context) == first
    context["current"][0].text = "The following amounts relate to quarterly segment cash flows."
    changed = evaluator.evaluate(pair, comparison, source_context=context)
    assert changed.cache_key != first.cache_key
    assert len(provider.calls) == 3
    assert first.state["source_context"]["current"][0]["text"] == (
        "The following amounts relate to annual company-wide cash flows."
    )
    assert changed.state["source_context"]["current"][0]["text"] == (
        "The following amounts relate to quarterly segment cash flows."
    )
    assert first.state["source_context"]["current"][0]["paragraph_id"] == "new:0"
    assert first.state["source_context"]["current"][0]["filing_accession"] == "new"


@pytest.mark.parametrize(
    ("side", "paragraph_side"),
    [("previous", "new"), ("current", "old"), ("other", "old")],
)
def test_wrong_side_or_unknown_source_context_is_rejected_before_provider_call(
    inputs, side, paragraph_side
):
    db, pair, comparison = inputs
    provider = FixtureProvider()
    evaluator = JevEvaluator(db, provider=provider)
    with pytest.raises(ValueError, match="source_context"):
        evaluator.evaluate(pair, comparison, source_context={side: [getattr(pair, paragraph_side)]})
    assert provider.calls == []


def test_historical_signals_load_without_inventing_business_assessments(inputs):
    _, pair, _ = inputs
    historical = {
        "question_schema_version": "filing-delta-1",
        "same_underlying_meaning": 0.2,
        "removal_consistent_with_resolution": 0.7,
        "liquidity_or_financing_direction": "introduced_or_increased",
        "liquidity_or_financing_probabilities": {
            "introduced_or_increased": 0.8,
            "reduced_or_resolved": 0.05,
            "unchanged_or_not_present": 0.1,
            "unclear": 0.05,
        },
    }
    signals = JevSemanticSignals.model_validate(historical)
    restored = RankedDelta.model_validate(
        {"pair": pair.model_dump(), "score": 0.72, "components": {}, "signals": historical}
    )
    assert signals.assessment is None
    assert signals.removal_consistent_with_resolution == 0.7
    assert signals.liquidity_or_financing_probabilities["introduced_or_increased"] == 0.8
    assert restored.signals == signals
    assert restored.score == 0.72
    assert restored.impact_band is None
    assert restored.priority_band is None
    assert restored.comparison_reliability is None


def test_numeric_evidence_change_invalidates_cached_business_judgment(inputs):
    db, pair, comparison = inputs
    provider = FixtureProvider()
    evaluator = JevEvaluator(db, provider=provider)
    evidence = ComparisonEvidence(
        basis="unclear",
        basis_reason="Tagged liquidity context is not the target's matched metric.",
        context_facts={
            "current": [
                SourceFact(
                    fact_id="new:fact:1",
                    filing_accession="new",
                    concept="us-gaap:CashAndCashEquivalentsAtCarryingValue",
                    label="Cash and cash equivalents",
                    value="100",
                    unit="USD",
                    entity=comparison.current.cik,
                    period_end=comparison.current.period_of_report,
                    quote="Cash and cash equivalents 100",
                )
            ]
        },
    )
    original = evaluator.evaluate(pair, comparison, comparison_evidence=evidence)
    assert evaluator.evaluate(pair, comparison, comparison_evidence=evidence) == original
    assert len(provider.calls) == 1
    evidence.context_facts["current"][0].value *= 2
    evidence.context_facts["current"][0].quote = "Cash and cash equivalents 200"
    corrected = evaluator.evaluate(pair, comparison, comparison_evidence=evidence)
    assert corrected.cache_key != original.cache_key
    assert len(provider.calls) == 2
    # The previous judgment retains the financial evidence actually used at the time.
    assert db.get_evaluation(original.cache_key) == original
    for evaluation, value, quote in (
        (original, Decimal("100"), "Cash and cash equivalents 100"),
        (corrected, Decimal("200"), "Cash and cash equivalents 200"),
    ):
        packet = evaluation.state["comparison_evidence"]
        reference = packet["context_facts"]["current"][0]
        record = packet["source_facts"][reference]
        assert Decimal(record["value"]) == value
        assert packet["source_quotes"][record["quote_ref"]] == quote
        assert record["filing_accession"] == "new"
        assert record["period_end"] == comparison.current.period_of_report.isoformat()
    evidence.context_facts["current"][0].filing_accession = "old"
    with pytest.raises(ValueError, match="wrong-side"):
        evaluator.evaluate(pair, comparison, comparison_evidence=evidence)
    assert len(provider.calls) == 2


def test_bounded_supplemental_context_does_not_truncate_original_targets(inputs):
    db, pair, comparison = inputs
    provider = FixtureProvider()
    for target in (pair.old, pair.new):
        target.text = "完整原文" * 3000
        target.normalized_text = target.text
        target.text_hash = sha256(target.text.encode()).hexdigest()
    before_pair = pair.model_copy(deep=True)
    optional = pair.new.model_copy(update={"paragraph_id": "new:optional", "ordinal": 2})
    source_context = {"current": [optional]}
    evidence = ComparisonEvidence(
        basis="unclear",
        basis_reason="No numeric pair is asserted.",
        counterparts={"current": [optional]},
    )
    bounded_context, bounded_evidence = bound_packet(source_context, evidence)
    evaluation = JevEvaluator(db, provider=provider).evaluate(
        pair,
        comparison,
        source_context=bounded_context,
        comparison_evidence=bounded_evidence,
    )
    assert pair == before_pair
    assert source_context == {"current": [optional]}
    assert evidence.counterparts["current"] == [optional]
    assert evaluation.state["source_context"]["current"] == []
    assert evaluation.state["comparison_evidence"]["counterparts"]["current"] == []
    for side, target in (("old", before_pair.old), ("new", before_pair.new)):
        delivered = provider.calls[0][0][side]
        assert delivered["text"] == target.text
        assert delivered["text_hash"] == sha256(target.text.encode()).hexdigest()
        assert delivered["paragraph_id"] == target.paragraph_id
        assert delivered["filing_accession"] == target.filing_accession
    assert db.get_evaluation(evaluation.cache_key) == evaluation


def test_reordered_evidence_mappings_reuse_the_same_judgment(inputs):
    db, pair, comparison = inputs
    provider = FixtureProvider()
    evaluator = JevEvaluator(db, provider=provider)
    evidence = ComparisonEvidence(
        basis="unclear",
        basis_reason="Numeric context only.",
        context_facts={
            side: [
                SourceFact(
                    fact_id=f"{filing.accession}:cash",
                    filing_accession=filing.accession,
                    concept="us-gaap:CashAndCashEquivalentsAtCarryingValue",
                    label="Cash and cash equivalents",
                    value=value,
                    unit="USD",
                    entity=filing.cik,
                    period_end=filing.period_of_report,
                    quote=f"Cash and cash equivalents {value}",
                )
            ]
            for side, filing, value in [
                ("previous", comparison.previous, "100"),
                ("current", comparison.current, "200"),
            ]
        },
    )
    original = evaluator.evaluate(pair, comparison, comparison_evidence=evidence)
    # Canonical JSON persistence changes mapping order, not source meaning.
    restored = evidence.model_copy(
        update={"context_facts": dict(reversed(evidence.context_facts.items()))}
    )
    replay = evaluator.evaluate(pair, comparison, comparison_evidence=restored)
    assert replay == original
    assert len(provider.calls) == 1
