"""Small synchronous TypeSafe boundary with persistent, context-complete provenance."""

import json
import os
import time
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Protocol

from radar.db import Database, canonical_json, content_key
from radar.jev import questions as question_definitions
from radar.jev.schemas import JevResponseError, normalize_response
from radar.models import AlignedPair, ComparableFilingPair, Filing, JevEvaluation


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

    def evaluate(self, pair: AlignedPair, comparison: ComparableFilingPair) -> JevEvaluation:
        schema_version = question_definitions.QUESTION_SCHEMA_VERSION
        questions = question_definitions.questions_for(pair.relation)
        state = {
            "question_schema_version": schema_version,
            "requested_model": self.model,
            "relation": pair.relation,
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
