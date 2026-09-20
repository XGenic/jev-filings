"""SQLite provenance and caches. Each operation owns its connection for thread safety."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from radar.models import AnalysisRun, Filing, FilingParagraph, JevEvaluation


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def content_key(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS companies (
                    cik TEXT PRIMARY KEY, ticker TEXT NOT NULL, name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS filings (
                    accession TEXT PRIMARY KEY, cik TEXT NOT NULL, metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paragraphs (
                    paragraph_id TEXT PRIMARY KEY, accession TEXT NOT NULL,
                    text_hash TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS paragraphs_accession ON paragraphs(accession);
                CREATE TABLE IF NOT EXISTS embeddings (
                    cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jev_evaluations (
                    cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS comparison_runs (
                    run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paragraph_pairs (
                    run_id TEXT NOT NULL, ticker TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY (run_id, ticker, ordinal)
                );
                CREATE TABLE IF NOT EXISTS reports (
                    run_id TEXT NOT NULL, path TEXT NOT NULL, rendered_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, path)
                );
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def save_filing(self, filing: Filing, paragraphs: list[FilingParagraph]) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO companies VALUES (?, ?, ?)",
                (filing.cik, filing.ticker, filing.company_name),
            )
            conn.execute(
                "INSERT OR REPLACE INTO filings VALUES (?, ?, ?)",
                (filing.accession, filing.cik, filing.model_dump_json()),
            )
            conn.execute("DELETE FROM paragraphs WHERE accession = ?", (filing.accession,))
            conn.executemany(
                "INSERT INTO paragraphs VALUES (?, ?, ?, ?)",
                [
                    (p.paragraph_id, filing.accession, p.text_hash, p.model_dump_json())
                    for p in paragraphs
                ],
            )

    def get_embedding(self, key: str) -> list[float] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM embeddings WHERE cache_key = ?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save_embedding(self, key: str, vector: list[float]) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO embeddings VALUES (?, ?)", (key, canonical_json(vector))
            )

    def get_evaluation(self, key: str) -> JevEvaluation | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM jev_evaluations WHERE cache_key = ?", (key,)
            ).fetchone()
        return JevEvaluation.model_validate_json(row[0]) if row else None

    def save_evaluation(self, evaluation: JevEvaluation) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jev_evaluations VALUES (?, ?)",
                (evaluation.cache_key, evaluation.model_dump_json()),
            )

    def save_run(self, run: AnalysisRun) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO comparison_runs VALUES (?, ?, ?)",
                (run.run_id, run.generated_at.isoformat(), run.model_dump_json()),
            )
            conn.execute("DELETE FROM paragraph_pairs WHERE run_id = ?", (run.run_id,))
            conn.executemany(
                "INSERT INTO paragraph_pairs VALUES (?, ?, ?, ?)",
                [
                    (run.run_id, company.comparison.current.ticker, index, delta.model_dump_json())
                    for company in run.companies
                    for index, delta in enumerate(company.deltas)
                ],
            )

    def load_run(self, run_id: str) -> AnalysisRun:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM comparison_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"Unknown run: {run_id}")
        return AnalysisRun.model_validate_json(row[0])

    def save_report(self, run_id: str, path: Path) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO reports VALUES (?, ?, ?)",
                (run_id, str(path), datetime.now(UTC).isoformat()),
            )
