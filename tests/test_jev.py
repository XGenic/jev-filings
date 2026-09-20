"""All provider calls are fixtures; no test needs credentials or network access."""

from copy import deepcopy
from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest

from radar.db import Database
from radar.jev import questions
from radar.jev.client import JevEvaluator, JevUnavailableError
from radar.jev.schemas import JevResponseError
from radar.models import AlignedPair, ComparableFilingPair, Filing, FilingParagraph


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
    assert first.raw_response["answers"]["uncertainty_direction"]["confidence"] == 0.45
    assert first.signals.same_underlying_meaning == 0.5
    assert first.signals.uncertainty_probabilities["unclear"] == pytest.approx(0.7)
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


def test_relation_specific_judgments_do_not_infer_direction_from_absence(inputs):
    db, pair, comparison = inputs
    provider = FixtureProvider()
    evaluator = JevEvaluator(db, provider=provider)
    matched = evaluator.evaluate(pair, comparison)
    added = evaluator.evaluate(AlignedPair(old=None, new=pair.new, relation="added"), comparison)
    deleted = evaluator.evaluate(
        AlignedPair(old=pair.old, new=None, relation="deleted"), comparison
    )
    assert len(provider.calls) == 3
    assert len(matched.questions) == 10
    assert sum(q["type"] == "noul" for q in matched.questions.values()) == 4
    assert set(matched.signals.uncertainty_probabilities) == {
        "introduced_or_increased",
        "reduced_or_resolved",
        "unchanged_or_not_present",
        "unclear",
    }
    for evaluation in (added, deleted):
        assert evaluation.signals.same_underlying_meaning is None
        assert evaluation.signals.introduces_new_substantive_information is None
        assert evaluation.signals.mostly_boilerplate_or_rephrasing is None
        assert evaluation.signals.substantive_disclosure == 0.5
        assert set(evaluation.signals.uncertainty_probabilities) == {
            "present",
            "not_present",
            "unclear",
        }
    assert added.signals.removal_consistent_with_resolution is None
    assert deleted.signals.removal_consistent_with_resolution == 0.5


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
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
        choice = response["answers"]["uncertainty_direction"]
        if failure == "missing":
            del response["answers"]["same_underlying_meaning"]
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
            choice["choice"] = "introduced_or_increased"
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
    assert evaluation.signals.uncertainty_probabilities["unclear"] == pytest.approx(0.7)
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
    assert result.signals.uncertainty_direction == "unclear"
    assert result.raw_response["answers"]["uncertainty_direction"]["confidence"] == 0.45
