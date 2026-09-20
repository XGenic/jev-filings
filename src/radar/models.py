"""Persistent, provider-independent pipeline contracts."""

from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Form = Literal["10-K", "10-Q"]


class Company(BaseModel):
    ticker: str
    cik: str
    name: str


class Filing(BaseModel):
    ticker: str
    cik: str
    company_name: str
    form: Form
    accession: str
    filed_date: date
    period_of_report: date | None
    primary_document: str
    source_url: str
    local_path: Path


class ComparableFilingPair(BaseModel):
    current: Filing
    previous: Filing
    strategy: str = "previous_same_form"


class FilingParagraph(BaseModel):
    filing_accession: str
    paragraph_id: str
    section: str | None = None
    item: str | None = None
    ordinal: int
    text: str
    normalized_text: str
    text_hash: str


class ParagraphContext(BaseModel):
    subsection_key: str | None = None
    reporting_scope: Literal[
        "quarter", "year_to_date", "annual", "point_in_time", "mixed", "unknown"
    ] = "unknown"
    scope_evidence: list[str] = Field(default_factory=list)
    subsection_position: float = Field(default=0.5, ge=0, le=1)


class AlignmentAlternative(BaseModel):
    old: FilingParagraph | None = None
    new: FilingParagraph | None = None
    score: float | None = None
    cosine_similarity: float | None = None


class AlignmentEvidence(BaseModel):
    method: str = "subsection_joint_v1"
    status: Literal["exact", "cosmetic", "aligned", "review", "unmatched"]
    old_context: ParagraphContext | None = None
    new_context: ParagraphContext | None = None
    candidate_tier: Literal["subsection", "item", "filing", "none"] = "none"
    score: float | None = None
    components: dict[str, float] = Field(default_factory=dict)
    assignment_margin: float | None = None
    review_reasons: list[
        Literal["near_tied_assignment", "scope_unclear", "cross_subsection", "possible_split_merge"]
    ] = Field(default_factory=list)
    alternatives: list[AlignmentAlternative] = Field(default_factory=list)


class AlignedPair(BaseModel):
    old: FilingParagraph | None
    new: FilingParagraph | None
    relation: Literal["matched", "added", "deleted"]
    cosine_similarity: float | None = None
    lexical_similarity: float | None = None
    lexical_diff_html: str | None = None
    skip_reason: Literal["exact", "cosmetic"] | None = None
    warnings: list[str] = Field(default_factory=list)
    alignment: AlignmentEvidence | None = None

    @model_validator(mode="after")
    def valid_relation(self):
        expected = {"matched": (True, True), "added": (False, True), "deleted": (True, False)}
        if (self.old is not None, self.new is not None) != expected[self.relation]:
            raise ValueError("Paragraph presence must agree with relation")
        return self


class AlignmentResult(BaseModel):
    pairs: list[AlignedPair]
    embedding_model: str
    warnings: list[str] = Field(default_factory=list)


class JevSemanticSignals(BaseModel):
    question_schema_version: str
    same_underlying_meaning: float | None = Field(default=None, ge=0, le=1)
    introduces_new_substantive_information: float | None = Field(default=None, ge=0, le=1)
    plausibly_economically_consequential: float | None = Field(default=None, ge=0, le=1)
    mostly_boilerplate_or_rephrasing: float | None = Field(default=None, ge=0, le=1)
    substantive_disclosure: float | None = Field(default=None, ge=0, le=1)
    removal_consistent_with_resolution: float | None = Field(default=None, ge=0, le=1)
    liquidity_or_financing_direction: str | None = None
    liquidity_or_financing_probabilities: dict[str, float] | None = None
    supply_or_capacity_direction: str | None = None
    supply_or_capacity_probabilities: dict[str, float] | None = None
    customer_concentration_direction: str | None = None
    customer_concentration_probabilities: dict[str, float] | None = None
    regulatory_or_government_direction: str | None = None
    regulatory_or_government_probabilities: dict[str, float] | None = None
    outlook_or_guidance_direction: str | None = None
    outlook_or_guidance_probabilities: dict[str, float] | None = None
    uncertainty_direction: str | None = None
    uncertainty_probabilities: dict[str, float] | None = None


class JevEvaluation(BaseModel):
    cache_key: str
    provider: str
    state: dict[str, Any]
    questions: dict[str, Any]
    raw_response: dict[str, Any]
    signals: JevSemanticSignals
    requested_at: datetime
    latency_seconds: float


class RankedDelta(BaseModel):
    pair: AlignedPair
    score: float
    components: dict[str, float]
    signals: JevSemanticSignals | None = None
    evaluation_key: str | None = None
    semantic_error: str | None = None


class CompanyAnalysis(BaseModel):
    comparison: ComparableFilingPair
    counts: dict[str, int]
    deltas: list[RankedDelta]
    warnings: list[str] = Field(default_factory=list)


class AnalysisRun(BaseModel):
    run_id: str
    generated_at: datetime
    tickers: list[str]
    form: Form | None
    question_schema_version: str
    embedding_model: str
    strategy: str = "previous_same_form"
    companies: list[CompanyAnalysis] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)
