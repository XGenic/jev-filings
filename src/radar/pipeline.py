"""Code owns the workflow; providers contribute signals, never control flow."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from radar.config import Settings
from radar.db import Database
from radar.evidence import EvidenceIndex
from radar.jev.score import RANKING_POLICY_VERSION, rank_pair, ranking_key
from radar.match.align import align_paragraphs
from radar.match.embeddings import EmbeddingProvider, SentenceTransformerEmbedder
from radar.models import AnalysisRun, CompanyAnalysis, Form
from radar.parse import parse_evidence, parse_filing
from radar.sec.cache import atomic_write
from radar.sec.client import SecClient
from radar.sec.filings import discover_pair, fetch_pair

logger = logging.getLogger(__name__)


def fetch_and_parse(client: SecClient, ticker: str, form: Form | None, settings: Settings):
    comparison = fetch_pair(client, discover_pair(client, ticker, form))
    db = Database(settings.data_dir / "radar.sqlite3")
    parsed = []
    for filing in (comparison.previous, comparison.current):
        paragraphs = parse_filing(filing, settings.min_paragraph_chars)
        if not paragraphs:
            raise ValueError(
                f"No narrative paragraphs extracted from {filing.accession}; raw HTML retained"
            )
        db.save_filing(filing, paragraphs)
        diagnostic = {
            "filing": filing.model_dump(mode="json"),
            "paragraphs": [p.model_dump(mode="json") for p in paragraphs],
        }
        atomic_write(
            settings.data_dir / "processed" / f"{filing.accession}.json",
            json.dumps(diagnostic, ensure_ascii=False, indent=2).encode(),
        )
        parsed.append(paragraphs)
    return comparison, parsed[0], parsed[1]


def analyze_tickers(
    tickers: list[str],
    settings: Settings,
    form: Form | None = None,
    recompute: bool = False,
    *,
    embedder: EmbeddingProvider | None = None,
    evaluator=None,
) -> AnalysisRun:
    from radar.jev.client import JevEvaluator
    from radar.jev.questions import QUESTION_SCHEMA_VERSION

    tickers = list(dict.fromkeys(ticker.strip().upper() for ticker in tickers))
    if not tickers or any(not ticker for ticker in tickers):
        raise ValueError("At least one non-empty ticker is required")
    db = Database(settings.data_dir / "radar.sqlite3")
    if settings.embedding_enabled and embedder is None:
        embedder = SentenceTransformerEmbedder(settings, db)
    if not settings.embedding_enabled:
        embedder = None
    if settings.jev_enabled and evaluator is None:
        evaluator = JevEvaluator(db)
    run = AnalysisRun(
        run_id=datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:10],
        generated_at=datetime.now(UTC),
        tickers=tickers,
        form=form,
        question_schema_version=QUESTION_SCHEMA_VERSION,
        embedding_model=embedder.name if embedder else "unavailable (lexical-only)",
        settings=settings.public_dict(),
    )
    run.settings["jev_provider"] = (
        getattr(evaluator, "provider_identity", "unrecorded injected provider")
        if settings.jev_enabled
        else "disabled"
    )
    run.settings["jev_model"] = getattr(evaluator, "model", None) if settings.jev_enabled else None
    run.settings["ranking_policy"] = RANKING_POLICY_VERSION
    run.settings["comparison_evidence_version"] = "comparison-evidence-1"
    with SecClient(settings, offline=recompute) as client:
        for ticker in tickers:
            try:
                comparison, old, new = fetch_and_parse(client, ticker, form, settings)
                alignment = align_paragraphs(old, new, settings, embedder)
                source_evidence = {
                    "previous": parse_evidence(comparison.previous, old),
                    "current": parse_evidence(comparison.current, new),
                }
                evidence_index = EvidenceIndex(
                    comparison, old, new, source_evidence["previous"], source_evidence["current"]
                )
                warnings = list(alignment.warnings)
                for evidence in source_evidence.values():
                    atomic_write(
                        settings.data_dir
                        / "processed"
                        / f"{evidence.filing_accession}.evidence.json",
                        evidence.model_dump_json(indent=2).encode(),
                    )
                    warnings.extend(
                        f"{evidence.filing_accession}: {warning}" for warning in evidence.warnings
                    )
                if alignment.embedding_model != run.embedding_model:
                    warnings.append(f"Actual alignment provider: {alignment.embedding_model}")

                def evaluate(pair):
                    if pair.skip_reason:
                        return rank_pair(pair, None, settings.weights, form=comparison.current.form)
                    source_context, comparison_evidence = evidence_index.build(pair)
                    if not settings.jev_enabled:
                        return rank_pair(
                            pair,
                            None,
                            settings.weights,
                            semantic_error="Jev disabled; baseline signals only.",
                            form=comparison.current.form,
                            source_context=source_context,
                            comparison_evidence=comparison_evidence,
                        )
                    try:
                        evaluation = evaluator.evaluate(
                            pair,
                            comparison,
                            source_context=source_context,
                            comparison_evidence=comparison_evidence,
                        )
                    except Exception as exc:
                        return rank_pair(
                            pair,
                            None,
                            settings.weights,
                            semantic_error=f"Jev unavailable: {type(exc).__name__}: {exc}",
                            form=comparison.current.form,
                            source_context=source_context,
                            comparison_evidence=comparison_evidence,
                        )
                    return rank_pair(
                        pair,
                        evaluation.signals,
                        settings.weights,
                        evaluation_key=evaluation.cache_key,
                        form=comparison.current.form,
                        source_context=source_context,
                        comparison_evidence=comparison_evidence,
                    )

                with ThreadPoolExecutor(max_workers=settings.jev_concurrency) as executor:
                    deltas = list(executor.map(evaluate, alignment.pairs))
                errors = sorted({d.semantic_error for d in deltas if d.semantic_error})
                warnings.extend(errors)
                for error in errors:
                    logger.warning("%s: %s", ticker, error)
                counts = {
                    "old_paragraphs": len(old),
                    "new_paragraphs": len(new),
                    "total_paragraphs": len(old) + len(new),
                    "unchanged_skipped": sum(p.skip_reason is not None for p in alignment.pairs),
                    "exact_skipped": sum(p.skip_reason == "exact" for p in alignment.pairs),
                    "cosmetic_skipped": sum(p.skip_reason == "cosmetic" for p in alignment.pairs),
                    "matched_changes": sum(
                        p.relation == "matched" and p.skip_reason is None for p in alignment.pairs
                    ),
                    "additions": sum(p.relation == "added" for p in alignment.pairs),
                    "deletions": sum(p.relation == "deleted" for p in alignment.pairs),
                    "jev_evaluated_pairs": sum(d.signals is not None for d in deltas),
                    "alignment_review_pairs": sum(
                        p.alignment is not None and p.alignment.status == "review"
                        for p in alignment.pairs
                    ),
                    "impact_assessed_pairs": sum(
                        d.signals is not None and d.signals.assessment is not None for d in deltas
                    ),
                    "supported_change_pairs": sum(
                        d.comparison_reliability == "supported" for d in deltas
                    ),
                    "comparison_review_pairs": sum(d.priority_band == "review" for d in deltas),
                }
                counts["source_numeric_facts"] = sum(
                    len(evidence.facts) for evidence in source_evidence.values()
                )
                counts["source_tables"] = sum(
                    len(evidence.tables) for evidence in source_evidence.values()
                )
                counts["computed_fact_changes"] = sum(
                    len(delta.comparison_evidence.changes)
                    for delta in deltas
                    if delta.comparison_evidence is not None
                )
                deltas.sort(key=ranking_key)
                run.companies.append(
                    CompanyAnalysis(
                        comparison=comparison, counts=counts, deltas=deltas, warnings=warnings
                    )
                )
            except Exception as exc:
                logger.exception("Comparison failed for %s", ticker)
                run.errors[ticker] = f"{type(exc).__name__}: {exc}"
            # Checkpoint completed companies: a later interruption cannot erase earlier results.
            db.save_run(run)
    return run


def rerank_run(run: AnalysisRun, settings: Settings) -> AnalysisRun:
    from radar.jev.questions import QUESTION_SCHEMA_VERSION

    result = run.model_copy(deep=True)
    result.settings["weights"] = settings.weights.model_dump()
    result.settings["top_n"] = settings.top_n
    if any(
        delta.signals is not None and delta.signals.assessment is not None
        for company in result.companies
        for delta in company.deltas
    ):
        result.settings["ranking_policy"] = RANKING_POLICY_VERSION
    for company in result.companies:
        original_deltas = company.deltas
        company.deltas = [
            rank_pair(
                delta.pair,
                delta.signals,
                settings.weights,
                delta.evaluation_key,
                delta.semantic_error,
                form=company.comparison.current.form,
                source_context=delta.source_context,
                comparison_evidence=delta.comparison_evidence,
            )
            for delta in company.deltas
        ]
        for original, reranked in zip(original_deltas, company.deltas):
            if (
                original.signals is not None
                and original.signals.question_schema_version != QUESTION_SCHEMA_VERSION
            ):
                reranked.impact_explanation = original.impact_explanation
        company.deltas.sort(key=ranking_key)
    return result


def write_report(run: AnalysisRun, settings: Settings, output: Path | None = None) -> Path:
    from radar.report.render import render_report

    output = output or settings.data_dir / "reports" / f"{run.run_id}.html"
    path = render_report(run, output, settings.top_n)
    Database(settings.data_dir / "radar.sqlite3").save_report(run.run_id, path)
    return path


def prepare_research(
    manifest: Path, output: Path, settings: Settings, *, offline: bool = True
) -> Path:
    """Prepare source-complete experimental packets without invoking a semantic provider."""
    import hashlib
    import re

    from radar.models import ComparableFilingPair
    from radar.research import (
        RESEARCH_SCHEMA_VERSION,
        SCREENING_INSTRUCTIONS,
        SCREENING_RESPONSE_SCHEMA,
        build_packet,
    )

    records = json.loads(manifest.read_text())
    if not isinstance(records, list) or not records:
        raise ValueError("Research manifest must be a nonempty list of comparisons")
    selected = []
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Every research manifest entry must be an object")
        identity = record.get("id")
        cohort = record.get("cohort")
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", identity)
            or identity in seen
        ):
            raise ValueError("Research comparison IDs must be unique, safe filenames")
        if not isinstance(cohort, str) or not cohort.strip():
            raise ValueError("Every research comparison needs a cohort")
        comparison = ComparableFilingPair.model_validate(record.get("comparison"))
        old, new = comparison.previous, comparison.current
        if (
            old.cik != new.cik
            or old.ticker != new.ticker
            or old.form != new.form
            or old.accession == new.accession
            or old.filed_date >= new.filed_date
        ):
            raise ValueError("Research pairs require ordered same-issuer, same-form filings")
        seen.add(identity)
        selected.append((identity, cohort, comparison))

    db = Database(settings.data_dir / "radar.sqlite3")
    embedder = SentenceTransformerEmbedder(settings, db) if settings.embedding_enabled else None
    index = {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "instructions_sha256": hashlib.sha256(SCREENING_INSTRUCTIONS.encode()).hexdigest(),
        "comparisons": [],
    }
    with SecClient(settings, offline=offline) as client:
        for identity, cohort, comparison in selected:
            comparison = fetch_pair(client, comparison)
            old = parse_filing(comparison.previous, settings.min_paragraph_chars)
            new = parse_filing(comparison.current, settings.min_paragraph_chars)
            if not old or not new:
                raise ValueError(f"No narrative paragraphs extracted for {identity}")
            alignment = align_paragraphs(old, new, settings, embedder)
            packet = build_packet(comparison, old, new, alignment)
            packet_bytes = json.dumps(packet, ensure_ascii=False, indent=2).encode()
            atomic_write(output / f"{identity}.packet.json", packet_bytes)
            atomic_write(
                output / f"{identity}.alignment.json",
                alignment.model_dump_json(indent=2).encode(),
            )
            index["comparisons"].append(
                {
                    "id": identity,
                    "cohort": cohort,
                    "comparison": comparison.model_dump(mode="json"),
                    "packet": f"{identity}.packet.json",
                    "alignment": f"{identity}.alignment.json",
                    "packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
                    "raw_sha256": {
                        side: hashlib.sha256(filing.local_path.read_bytes()).hexdigest()
                        for side, filing in (
                            ("previous", comparison.previous),
                            ("current", comparison.current),
                        )
                    },
                }
            )
    atomic_write(output / "screening-instructions.txt", SCREENING_INSTRUCTIONS.encode())
    atomic_write(
        output / "screening-response.schema.json",
        json.dumps(SCREENING_RESPONSE_SCHEMA, ensure_ascii=False, indent=2).encode(),
    )
    index_path = output / "index.json"
    atomic_write(index_path, json.dumps(index, ensure_ascii=False, indent=2).encode())
    return index_path
