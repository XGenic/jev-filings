"""Small synchronous TypeSafe boundary with persistent, context-complete provenance."""

import json
import os
import time
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Protocol

from radar.db import Database, canonical_json, content_key
from radar.evidence_packet import semantic_evidence
from radar.jev import questions as question_definitions
from radar.jev.schemas import JevResponseError, normalize_response
from radar.models import (
    AlignedPair,
    ComparableFilingPair,
    ComparisonEvidence,
    Filing,
    FilingParagraph,
    JevEvaluation,
)


class JevUnavailableError(RuntimeError):
    """Semantic analysis is unavailable; callers may still produce a lexical report."""


class JevProvider(Protocol):
    """Fixture providers must identify themselves, never impersonate the production cache."""

    identity: str

    def system_one(self, state: dict, questions: dict, *, model: str) -> Any: ...


class _TypeSafeProvider:
    def __init__(self) -> None:
        self.base_url = os.environ.get("TYPESAFE_BASE_URL", "").strip() or "https://api.typesafe.ai"
        self.base_url = self.base_url.rstrip("/")
        self.identity = f"typesafe-sdk:{self.base_url}"

    def system_one(self, state: dict, questions: dict, *, model: str) -> dict:
        api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
        if not api_key:
            raise JevUnavailableError(
                "Set TYPESAFE_API_KEY to enable uncached Jev semantic analysis"
            )
        try:
            from typesafe_sdk import TypeSafeClient
        except ImportError as exc:
            raise JevUnavailableError(
                "Install the jev extra (typesafe-sdk) to enable uncached semantic analysis"
            ) from exc
        # Each invocation owns its client and transport, safe for concurrent evaluate calls.
        with TypeSafeClient(api_key=api_key, base_url=self.base_url) as client:
            response = client.system_one(state, questions, model=model)
            # model_dump drops unknown fields. Preserve the complete server payload instead,
            # including usage, confidence and metadata introduced by future server versions.
            return response.raw_http_response.json()


def _filing_context(filing: Filing) -> dict[str, Any]:
    return filing.model_dump(mode="json", exclude={"local_path", "primary_document"})


def _comparison_evidence_state(
    evidence: ComparisonEvidence | None, pair: AlignedPair, comparison: ComparableFilingPair
) -> dict[str, Any] | None:
    if evidence is None:
        return None
    if not isinstance(evidence, ComparisonEvidence):
        raise ValueError("comparison_evidence must be ComparisonEvidence")
    sides = {"previous", "current"}
    for name in ("changed_spans", "context_facts", "tables", "counterparts"):
        sources = getattr(evidence, name)
        if set(sources) - sides:
            raise ValueError(f"comparison_evidence.{name} contains an unknown filing side")
        if name != "changed_spans":
            for side, records in sources.items():
                accession = getattr(comparison, side).accession
                if any(record.filing_accession != accession for record in records):
                    raise ValueError(
                        f"comparison_evidence.{name}.{side} contains a wrong-side accession"
                    )
    if pair.relation != "matched" and evidence.changes:
        raise ValueError("Unmatched comparison_evidence cannot establish numeric changes")
    for change in evidence.changes:
        if (
            change.previous.filing_accession != comparison.previous.accession
            or change.current.filing_accession != comparison.current.accession
        ):
            raise ValueError("comparison_evidence change contains a wrong-side accession")
    return semantic_evidence(evidence)


class JevEvaluator:
    def __init__(self, db: Database, model: str | None = None, provider: JevProvider | None = None):
        self.db = db
        # Verified against typesafe_sdk.constants; do not import an optional SDK on cache hits.
        self.model = model or os.environ.get("TYPESAFE_DEFAULT_MODEL", "").strip() or "jev-latest"
        self.provider = provider if provider is not None else _TypeSafeProvider()
        identity = getattr(self.provider, "identity", None)
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError(
                "Injected Jev providers must supply an explicit nonempty identity string"
            )
        self.provider_identity = identity

    def evaluate(
        self,
        pair: AlignedPair,
        comparison: ComparableFilingPair,
        *,
        source_context: dict[str, list[FilingParagraph]] | None = None,
        comparison_evidence: ComparisonEvidence | None = None,
    ) -> JevEvaluation:
        context = {} if source_context is None else source_context
        if not isinstance(context, dict) or set(context) - {"previous", "current"}:
            raise ValueError("source_context allows only previous and current filing sides")
        context_state: dict[str, list[dict[str, Any]]] = {}
        for side, paragraphs in context.items():
            if not isinstance(paragraphs, list) or any(
                not isinstance(paragraph, FilingParagraph) for paragraph in paragraphs
            ):
                raise ValueError(f"source_context.{side} must be a list of FilingParagraph")
            accession = getattr(comparison, side).accession
            if any(paragraph.filing_accession != accession for paragraph in paragraphs):
                raise ValueError(f"source_context.{side} contains a wrong-side filing accession")
            context_state[side] = [
                paragraph.model_dump(mode="json", exclude={"normalized_text"})
                for paragraph in paragraphs
            ]
        for target, filing in ((pair.old, comparison.previous), (pair.new, comparison.current)):
            if target is not None and target.filing_accession != filing.accession:
                raise ValueError("Target paragraph contains a wrong-side filing accession")
        schema_version = question_definitions.QUESTION_SCHEMA_VERSION
        questions = question_definitions.questions_for(pair.relation)
        state = {
            "question_schema_version": schema_version,
            "requested_model": self.model,
            "relation": pair.relation,
            "source_context": context_state,
            "comparison_evidence": _comparison_evidence_state(
                comparison_evidence, pair, comparison
            ),
            "comparison": {
                "strategy": comparison.strategy,
                "previous": _filing_context(comparison.previous),
                "current": _filing_context(comparison.current),
            },
            "old": pair.old.model_dump(mode="json", exclude={"normalized_text"})
            if pair.old
            else None,
            "new": pair.new.model_dump(mode="json", exclude={"normalized_text"})
            if pair.new
            else None,
        }
        key = content_key(
            {
                "provider": self.provider_identity,
                "model": self.model,
                "schema_version": schema_version,
                "questions": questions,
                "state": state,
            }
        )
        cached = self.db.get_evaluation(key)
        if cached is not None:
            return cached
        requested_at = datetime.now(UTC)
        started = time.perf_counter()
        response = self.provider.system_one(deepcopy(state), deepcopy(questions), model=self.model)
        latency = time.perf_counter() - started
        if hasattr(response, "model_dump"):
            response = response.model_dump(mode="json")
        if not isinstance(response, dict):
            raise JevResponseError("Provider response must be an SDK response or a JSON object")
        # Detach provider-owned containers and reject non-JSON/non-finite metadata as well.
        try:
            raw = json.loads(canonical_json(response))
        except (TypeError, ValueError) as exc:
            raise JevResponseError("Provider response must contain finite JSON values") from exc
        signals = normalize_response(raw, questions, schema_version)
        evaluation = JevEvaluation(
            cache_key=key,
            provider=self.provider_identity,
            state=state,
            questions=questions,
            raw_response=raw,
            signals=signals,
            requested_at=requested_at,
            latency_seconds=latency,
        )
        self.db.save_evaluation(evaluation)
        return evaluation
