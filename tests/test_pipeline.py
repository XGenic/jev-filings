import json

from radar.config import RankWeights, Settings
from radar.db import Database
from radar.pipeline import analyze_tickers, rerank_run, write_report
from radar.sec.cache import DiskCache
from radar.sec.filings import ARCHIVES_BASE, SUBMISSIONS_BASE, TICKERS_URL


def test_partial_failure_keeps_results_and_can_rerank_without_providers(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    cache = DiskCache(tmp_path)
    cache.write(
        TICKERS_URL,
        json.dumps(
            {"0": {"cik_str": 1, "ticker": "SYN", "title": "Synthetic test company"}}
        ).encode(),
    )
    accessions = ["0000000001-26-000002", "0000000001-26-000001"]
    cache.write(
        f"{SUBMISSIONS_BASE}CIK0000000001.json",
        json.dumps(
            {
                "filings": {
                    "recent": {
                        "form": ["10-Q", "10-Q"],
                        "accessionNumber": accessions,
                        "filingDate": ["2026-08-01", "2026-05-01"],
                        "reportDate": ["2026-06-30", "2026-03-31"],
                        "primaryDocument": ["current.htm", "prior.htm"],
                    },
                    "files": [],
                }
            }
        ).encode(),
    )
    for accession, document, negation in zip(
        accessions, ["current.htm", "prior.htm"], ["not ", ""]
    ):
        html = (
            "<h1>Part I</h1><h2>Item 2. Management Discussion</h2>"
            f"<p>We are {negation}able to finance operations from our existing cash reserves.</p>"
        )
        cache.write(
            f"{ARCHIVES_BASE}1/{accession.replace('-', '')}/{document}",
            html.encode(),
            permanent=True,
        )
    settings = Settings(data_dir=tmp_path, embedding_enabled=False, jev_enabled=True)
    run = analyze_tickers(["MISSING", "SYN"], settings, "10-Q", recompute=True)
    assert list(run.errors) == ["MISSING"]
    assert len(run.companies) == 1
    changed = run.companies[0].deltas[0]
    assert changed.pair.relation == "matched" and changed.pair.skip_reason is None
    assert changed.signals is None and changed.semantic_error
    restored = Database(tmp_path / "radar.sqlite3").load_run(run.run_id)
    assert restored == run
    settings.weights = RankWeights(embedding_drift=0.9, section_boost=0)
    reranked = rerank_run(restored, settings)
    assert reranked.companies[0].deltas[0].score != changed.score
    assert restored.companies[0].deltas[0].score == changed.score
    output = write_report(reranked, settings)
    assert output.is_file()
    assert "MISSING" in output.read_text()
    assert "existing cash reserves" in output.read_text()
