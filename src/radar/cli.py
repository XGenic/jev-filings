"""Command-line interface. Provider imports are lazy so --help needs no credentials."""

import json
import logging
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from radar.config import Settings

app = typer.Typer(no_args_is_help=True, help="Compare SEC disclosures; not investment advice.")


class FilingForm(StrEnum):
    annual = "10-K"
    quarterly = "10-Q"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {"level": record.levelname, "logger": record.name, "message": record.getMessage()},
            ensure_ascii=False,
        )


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def load_settings(config: Path | None) -> Settings:
    configure_logging()
    try:
        return Settings.load(config)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command()
def analyze(
    tickers: Annotated[list[str], typer.Argument(help="One or more U.S. public-company tickers.")],
    form: Annotated[FilingForm | None, typer.Option(help="Default: latest 10-K or 10-Q.")] = None,
    recompute: Annotated[
        bool, typer.Option(help="Reparse/re-align cached SEC data; no SEC network.")
    ] = False,
    no_jev: Annotated[bool, typer.Option(help="Baseline only; do not call TypeSafe.")] = False,
    lexical_only: Annotated[
        bool, typer.Option(help="Disable embeddings (debugging fallback).")
    ] = False,
    config: Annotated[Path | None, typer.Option(help="Settings TOML file.")] = None,
    output: Annotated[Path | None, typer.Option(help="Output HTML path.")] = None,
):
    """Analyze filing pairs, persist all signals, and write one self-contained report."""
    from radar.pipeline import analyze_tickers, write_report

    settings = load_settings(config)
    if no_jev:
        settings.jev_enabled = False
    if lexical_only:
        settings.embedding_enabled = False
    try:
        run = analyze_tickers(tickers, settings, form.value if form else None, recompute)
        path = write_report(run, settings, output)
    except (OSError, ValueError, RuntimeError) as exc:
        typer.echo(f"Analysis failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Run: {run.run_id}\nReport: {path.resolve()}")
    for company in run.companies:
        filing = company.comparison.current
        typer.echo(
            f"{filing.ticker}: {company.comparison.previous.accession} → {filing.accession}; "
            f"{company.counts['matched_changes']} matched changes, "
            f"{company.counts['additions']} unmatched current, "
            f"{company.counts['deletions']} unmatched previous passages"
        )
    for ticker, error in run.errors.items():
        typer.echo(f"{ticker}: {error}", err=True)
    if run.errors:
        raise typer.Exit(1)


@app.command()
def fetch(
    ticker: str,
    form: FilingForm | None = None,
    config: Path | None = None,
    cached: Annotated[bool, typer.Option(help="Use cached SEC data only.")] = False,
):
    """Cache both filings and dump sectioned paragraph JSON for inspection."""
    from radar.pipeline import fetch_and_parse
    from radar.sec.client import SecClient

    settings = load_settings(config)
    try:
        with SecClient(settings, offline=cached) as client:
            pair, old, new = fetch_and_parse(client, ticker, form.value if form else None, settings)
    except (OSError, ValueError, RuntimeError) as exc:
        typer.echo(f"Fetch failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    for filing, paragraphs in ((pair.previous, old), (pair.current, new)):
        typer.echo(
            f"{filing.ticker} {filing.form} {filing.accession} "
            f"period={filing.period_of_report} filed={filing.filed_date}\n"
            f"  HTML: {filing.local_path}\n"
            f"  Paragraphs: {len(paragraphs)}; "
            f"JSON: {settings.data_dir / 'processed' / (filing.accession + '.json')}"
        )


@app.command()
def report(
    run_id: str,
    config: Path | None = None,
    output: Path | None = None,
):
    """Rerank and render persisted results with current weights, without any provider calls."""
    from radar.db import Database
    from radar.pipeline import rerank_run, write_report

    settings = load_settings(config)
    try:
        db = Database(settings.data_dir / "radar.sqlite3")
        run = rerank_run(db.load_run(run_id), settings)
        path = write_report(run, settings, output)
    except (OSError, ValueError, KeyError) as exc:
        typer.echo(f"Report failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Report: {path.resolve()}")


@app.command("research-prepare")
def research_prepare(
    manifest: Annotated[Path, typer.Argument(help="JSON list of explicit filing comparisons.")],
    output: Annotated[Path, typer.Option(help="Directory for blinded packets and alignments.")],
    config: Path | None = None,
    online: Annotated[
        bool, typer.Option(help="Permit SEC downloads; default is cached-only.")
    ] = False,
):
    """Prepare a separate research experiment; no semantic provider calls or score changes."""
    from radar.pipeline import prepare_research

    settings = load_settings(config)
    try:
        path = prepare_research(manifest, output, settings, offline=not online)
    except (OSError, ValueError, RuntimeError) as exc:
        typer.echo(f"Research preparation failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Research index: {path.resolve()}")


@app.command("research-report")
def research_report(
    study: Annotated[Path, typer.Argument(help="Source-checked research study JSON.")],
    output: Annotated[Path, typer.Option(help="Self-contained HTML output.")],
):
    """Render experimental assessments separately from production rankings, without providers."""
    from radar.research import render_research_report
    from radar.sec.cache import atomic_write

    try:
        html = render_research_report(json.loads(study.read_text()))
        atomic_write(output, html.encode())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        typer.echo(f"Research report failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Research report: {output.resolve()}")


@app.command("research-validate")
def research_validate(
    packet: Annotated[Path, typer.Argument(help="Blinded packet supplied to the external judge.")],
    response: Annotated[Path, typer.Argument(help="Raw structured judge response JSON.")],
    output: Annotated[Path, typer.Option(help="Validated assessments JSON output.")],
):
    """Reject incomplete assessments and unsupported quotations before review."""
    from radar.research import validate_screening
    from radar.sec.cache import atomic_write

    try:
        result = validate_screening(
            json.loads(packet.read_text()), json.loads(response.read_text())
        )
        atomic_write(output, json.dumps(result, ensure_ascii=False, indent=2).encode())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        typer.echo(f"Research validation failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Validated {len(result['assessments'])} assessments: {output.resolve()}")
