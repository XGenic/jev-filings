"""Persistent, provider-independent pipeline contracts."""

from datetime import date, datetime
from decimal import Decimal
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


class SourceFact(BaseModel):
    """An explicitly tagged numeric disclosure, not an inferred business consequence."""

    fact_id: str
    filing_accession: str
    concept: str
    label: str
    value: Decimal
    unit: str
    entity: str
    period_start: date | None = None
    period_end: date
    dimensions: dict[str, str] = Field(default_factory=dict)
    quote: str
    source_anchor: str | None = None
    paragraph_ids: list[str] = Field(default_factory=list)
    table_id: str | None = None
    scale: int = 0
    sign: str | None = None
    format: str | None = None


class SourceTable(BaseModel):
    """Visible cells in source order; selected row indices refer to the original table."""

    table_id: str
    filing_accession: str
    caption: str
    rows: list[list[str]]
    row_indices: list[int] = Field(default_factory=list)
    cell_spans: list[list[tuple[int, int]]] = Field(default_factory=list)
    header_cells: list[list[bool]] = Field(default_factory=list)
    source_anchor: str | None = None
    section: str | None = None
    item: str | None = None


class FilingEvidence(BaseModel):
    filing_accession: str
    raw_sha256: str
    facts: list[SourceFact] = Field(default_factory=list)
    tables: list[SourceTable] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class FactChange(BaseModel):
    metric: str
    previous: SourceFact
    current: SourceFact
    basis: Literal["sequential", "year_over_year", "point_in_time"]
    absolute_change: Decimal
    percent_change: Decimal | None = None


class ComparisonEvidence(BaseModel):
    schema_version: Literal["comparison-evidence-1"] = "comparison-evidence-1"
    basis: Literal["sequential", "year_over_year", "point_in_time", "mixed", "unclear"]
    basis_reason: str
    changed_spans: dict[str, list[str]] = Field(default_factory=dict)
    changes: list[FactChange] = Field(default_factory=list)
    context_facts: dict[str, list[SourceFact]] = Field(default_factory=dict)
    tables: dict[str, list[SourceTable]] = Field(default_factory=dict)
    counterparts: dict[str, list[FilingParagraph]] = Field(default_factory=dict)
    possible_channels: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


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


class CategoricalAssessment(BaseModel):
    choice: str
    probabilities: dict[str, float]


class BusinessAssessment(BaseModel):
    business_subject: CategoricalAssessment
    change_nature: CategoricalAssessment
    business_direction: CategoricalAssessment
    impact_magnitude: CategoricalAssessment
    impact_duration: CategoricalAssessment
    impact_timing: CategoricalAssessment
    impact_evidence: CategoricalAssessment
    comparison_validity: CategoricalAssessment
    business_impact: CategoricalAssessment


class JevSemanticSignals(BaseModel):
    question_schema_version: str
    assessment: BusinessAssessment | None = None
    same_underlying_meaning: float | None = Field(default=None, ge=0, le=1)
    introduces_new_substantive_information: float | None = Field(default=None, ge=0, le=1)
    plausibly_economically_consequential: float | None = Field(default=None, ge=0, le=1)
    mostly_boilerplate_or_rephrasing: float | None = Field(default=None, ge=0, le=1)
    substantive_disclosure: float | None = Field(default=None, ge=0, le=1)
    # Retained only for deserializing historical evaluations and their original rankings.
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
    impact_band: Literal["high", "medium", "low", "unclear"] | None = None
    priority_band: Literal["high", "medium", "low", "review", "unavailable"] | None = None
    comparison_reliability: (
        Literal["supported", "needs_review", "unmatched", "unavailable"] | None
    ) = None
    impact_explanation: str | None = None
    source_context: dict[str, list[FilingParagraph]] = Field(default_factory=dict)
    comparison_evidence: ComparisonEvidence | None = None


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
