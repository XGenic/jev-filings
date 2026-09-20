"""Validate SDK answer shapes before any successful result enters the cache."""

import math
from typing import Any

from radar.models import JevSemanticSignals


class JevResponseError(ValueError):
    """The provider did not supply a complete, coherent probabilistic answer."""


def _probability(value: Any, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JevResponseError(f"{location}: expected a numeric probability")
    if not math.isfinite(value) or not 0 <= value <= 1:
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
    signals: dict[str, Any] = {"question_schema_version": schema_version}
    for name, question in questions.items():
        answer = answers.get(name)
        if not isinstance(answer, dict) or answer.get("type") != question["type"]:
            raise JevResponseError(f"{name}: missing answer or incorrect answer type")
        if question["type"] == "noul":
            signals[name] = _probability(answer.get("noul"), f"{name}.noul")
            continue
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
        signals[name] = choice
        signals[name.removesuffix("_direction") + "_probabilities"] = {
            label: value / total for label, value in values.items()
        }
    return JevSemanticSignals.model_validate(signals)
