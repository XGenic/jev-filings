"""Validate SDK answer shapes before any successful result enters the cache."""

import math
from typing import Any

from radar.jev.questions import BUSINESS_QUESTIONS
from radar.models import BusinessAssessment, CategoricalAssessment, JevSemanticSignals


class JevResponseError(ValueError):
    """The provider did not supply a complete, coherent probabilistic answer."""


def _probability(value: Any, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JevResponseError(f"{location}: expected a numeric probability")
    if not 0 <= value <= 1 or not math.isfinite(value):
        raise JevResponseError(f"{location}: probability must be finite and between 0 and 1")
    return float(value)


def normalize_response(
    raw: dict[str, Any],
    questions: dict[str, Any],
    schema_version: str,
) -> JevSemanticSignals:
    """Require every answer and label; never fabricate probabilities from selected labels."""
    answers = raw.get("answers")
    if not isinstance(answers, dict):
        raise JevResponseError("Response is missing its answers mapping")
    if set(answers) != set(questions):
        raise JevResponseError("Answers must cover exactly the requested questions")
    if not set(BUSINESS_QUESTIONS).issubset(questions):
        raise JevResponseError("Questions must include every business assessment dimension")
    signals: dict[str, Any] = {"question_schema_version": schema_version}
    assessment: dict[str, CategoricalAssessment] = {}
    for name, question in questions.items():
        answer = answers.get(name)
        if not isinstance(answer, dict) or answer.get("type") != question["type"]:
            raise JevResponseError(f"{name}: missing answer or incorrect answer type")
        if name not in BUSINESS_QUESTIONS:
            if question["type"] != "noul" or name not in {
                "same_underlying_meaning",
                "introduces_new_substantive_information",
                "plausibly_economically_consequential",
                "mostly_boilerplate_or_rephrasing",
                "substantive_disclosure",
            }:
                raise JevResponseError(f"{name}: unsupported question")
            signals[name] = _probability(answer.get("noul"), f"{name}.noul")
            continue
        if question["type"] != "choice":
            raise JevResponseError(f"{name}: business assessments require choice answers")
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != set(question["criteria"]):
            raise JevResponseError(f"{name}: probabilities must cover exactly the requested labels")
        values = {
            label: _probability(value, f"{name}.{label}") for label, value in probabilities.items()
        }
        total = math.fsum(values.values())
        # The SDK specifies an approximate sum of one. Only correct small rounding error,
        # never turn arbitrary scores or an incomplete probability map into certainty.
        if not math.isclose(total, 1.0, rel_tol=0, abs_tol=0.001):
            raise JevResponseError(f"{name}: probabilities must sum to approximately 1")
        choice = answer.get("choice")
        if not isinstance(choice, str) or choice not in values:
            raise JevResponseError(f"{name}: selected choice is not a requested label")
        if values[choice] + 1e-9 < max(values.values()):
            raise JevResponseError(f"{name}: selected choice is inconsistent with probabilities")
        _probability(answer.get("confidence"), f"{name}.confidence")
        assessment[name] = CategoricalAssessment(
            choice=choice,
            probabilities={label: value / total for label, value in values.items()},
        )
    signals["assessment"] = BusinessAssessment.model_validate(assessment)
    return JevSemanticSignals.model_validate(signals)
