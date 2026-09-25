# SEC Filing Delta Radar

A local Python tool for comparing SEC 10-K and 10-Q filings: align passages,
extract numerical evidence, optionally evaluate changes with TypeSafe Jev, and
produce source-linked HTML reports.

**Status: development stopped.** This is a completed technical prototype, preserved
as a research and engineering artifact. The pipeline works; its incremental value
as an investment-research tool was not established. There is no active roadmap.

## Retrospective

### The idea

Ordinary filing diffs surface too much noise. The hypothesis was that paragraph
alignment, local embeddings, and narrow semantic judgments could identify changes
worth a researcher's attention more effectively than text drift alone. The research
objective later narrowed to early signs of expansion or developing commercial
relationships, then pivoted to general business categorization and impact.

The core workflow was delivered. Subsequent work improved alignment, added source
context and inline-XBRL evidence, and separated supported changes from questionable
comparisons. Those were engineering improvements, not proof of a differentiated
research product.

### What worked

- **Alignment and assignment:** subsection and reporting-scope constraints, joint
  one-to-one matching, explicit unmatched passages, and inspectable alternatives.
  Real-source regressions preserve fixes for concrete comparison failures.
- **Numerical evidence:** exact decimal values, units, periods, dimensions, table
  provenance, and conservative calculations between compatible target facts.
- **Caching and replay:** persisted filings, embeddings, requests, judgments, and
  run provenance. Reports can be regenerated without calling the SEC or a model.
- **Auditability:** source-linked passages, word diffs, evidence panels, strict
  response validation, and preserved experimental records.

### What did not earn further investment

- **Early discovery was not demonstrated.** Source checks repeatedly found already
  announced developments or existing disclosures misidentified as additions. A
  filing-to-filing difference does not establish public novelty or market neglect.
- **Jev's incremental value remained unproven.** Many leading cards were familiar
  financial movements: backlog, cash, financing, sales mix, and customer concentration.
  Important numbers can be useful without requiring a semantic model. A simple
  tagged-fact change ranking was not tested head-to-head, so this project cannot
  claim that Jev beats that cheaper alternative—or that it adds nothing.
- **The pivot changed what counted as success.** Routine financial updates that had
  been background under the discovery objective became successes under business
  impact. Better performance on that new objective did not validate the old thesis.
- **More rubric rules did not establish reliable reasoning.** Results remained
  issuer-dependent, dimensions could disagree, and malformed probability outputs
  still required retries. The displayed explanations are selected rubric criteria,
  not model-generated accounts of why a particular card matters.
- **Audit completeness hurt usability.** Long passages, repeated qualifications,
  and expandable evidence made the reports better audit records than fast research
  reading. A larger, more elaborate report was not necessarily a better product.

### What the evaluations actually showed

These are historical, **assistant-judged development results**, not independent
human accuracy measurements. No new benchmark was run to justify stopping.

The final five-company run covered ASPI, CLMT, QURE, AOSL, and AIP: **1,147 eligible
passages**, 9,626 extracted numeric facts, and 125 candidate-level computed changes.
It required **1,413 provider calls including invalid responses and retries**, with
426 seconds of recorded wall time. No dollar cost was inferred from those counts.

Two fresh issuers, AEHR and INOD, were selected before reading their sources. The
original and enriched configurations evaluated all 352 eligible comparisons. At a
fixed 20-card budget per issuer:

| Assistant-judged outcome | Original | Enriched |
| --- | ---: | ---: |
| Worthwhile cards | 15/40 | 21/40 |
| Sound, worthwhile medium/high-impact cards | 11/40 | 15/40 |
| Distinct worthwhile issuer/topic groups | 11 | 16 |
| Cards containing a broken comparison | 0 | 6 |

AEHR improved from 7 to 14 worthwhile cards; INOD declined from 8 to 7. All six
broken comparisons were in the review lane, not the supported-change lane. There
was one assistant judge per issuer, with no repeat judgments or human adjudication.
The comparison changed the rubric, evidence, and selection policy together; it does
not isolate their contributions or estimate population recall.

In the controlled development ablation, enriched evidence improved magnitude
agreement, but gains were not consistent across dimensions; displayed-impact
agreement fell in some configurations. Better assistant agreement is not proof of
better research decisions.

### Why stop

The reusable engineering was delivered. Further refinement did not establish enough
incremental research value to justify continuing. The conclusion is **an unvalidated
product thesis**, not a claim that semantic models can never help with filings.

If this is ever revisited, the first comparison should be against a plain tagged-fact
change ranking, judged by humans on useful, source-valid findings and their public
availability at the filing date. That is a condition for reopening the question,
not unfinished work promised by this repository.

## Using the preserved tool

Requires **Python 3.12+** and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/XGenic/jev-filings.git
cd jev-filings
uv sync --frozen
uv run --frozen radar --help
cp .env.example .env
```

Set `SEC_USER_AGENT` in `.env` to your identifying name and contact email before
accessing the SEC. Keep credentials local. See [the example configuration](radar.example.toml)
for rate limits, model settings, and report size; pass it with `--config` when needed.

### Without model calls

```bash
uv run --frozen radar analyze RELL --form 10-Q --no-jev --lexical-only
```

This downloads SEC filings and uses the lexical alignment fallback. It needs no
TypeSafe key or embedding model, but it is **not** the proposed XBRL-only ranking
baseline, and business-impact judgments will be unavailable.

### With embeddings and Jev

```bash
uv sync --frozen --all-extras
# Set TYPESAFE_API_KEY in .env before running paid semantic analysis.
uv run --frozen --all-extras radar analyze RELL --form 10-Q
```

Jev and embeddings are enabled by default. Uncached evaluations can incur provider
charges; the embedding model may download on first use. Pass `--no-jev` to disable
TypeSafe calls. Installing optional packages alone does not download model weights.

### Cached inputs and saved reports

```bash
# Fetch and inspect the selected filings without semantic evaluation.
uv run --frozen radar fetch RELL --form 10-Q

# Use existing SEC caches, with both model paths disabled.
uv run --frozen radar analyze RELL --form 10-Q --recompute --no-jev --lexical-only

# Replace RUN_ID with an ID printed by a completed analysis.
uv run --frozen radar report RUN_ID
```

`--recompute` prohibits SEC downloads, **not** uncached Jev requests or an initial
embedding-model download. `radar report` uses persisted judgments without SEC,
embedding, or provider calls; it does not upgrade historical assessments.

Selection uses the latest and previous non-amended filings of the same form.
Successive 10-Qs can straddle a 10-K, so these comparisons are not necessarily
changes since the immediately preceding disclosure. Unmatched text is not proof
of a new or removed fact. Scores and label probabilities are uncalibrated, not
investment advice.

The separate `research-prepare`, `research-validate`, and `research-report` commands
preserve the experimental workflow; they are not an autonomous judge or a promoted
alternative ranking system. Their protocols are in the implementation history.

## Repository contents

| Path | Purpose |
| --- | --- |
| [`src/radar/sec/`](src/radar/sec/) | Filing selection, SEC access, rate limiting, disk cache |
| [`src/radar/parse/`](src/radar/parse/) | Paragraphs, sections, inline-XBRL facts, table provenance |
| [`src/radar/match/`](src/radar/match/) | Lexical/embedding alignment, assignment, reporting scopes |
| [`src/radar/evidence.py`](src/radar/evidence.py), [`evidence_packet.py`](src/radar/evidence_packet.py) | Context retrieval, compatible fact changes, bounded provider packets |
| [`src/radar/jev/`](src/radar/jev/) | Versioned questions, validation, provider cache, ranking |
| [`src/radar/report/`](src/radar/report/) | Card selection and standalone HTML rendering |
| [`tests/`](tests/) | Synthetic and small real-source regression fixtures |
| [`docs/working-notes.md`](docs/working-notes.md) | Dated development, verification, and closure record |
| [`docs/implementation-history.md`](docs/implementation-history.md) | Preserved design, protocols, and implementation details |

**A clone contains code and regression fixtures, not the historical research data.**
Raw filings, databases, model caches, generated reports, and evaluation artifacts
under `data/` remain local and ignored. Historical paths and run IDs in the docs
are inventories, not downloadable deliverables. The summarized experimental numbers
cannot be independently reproduced from this repository alone. New live runs select
then-current filings and may return different provider outputs.

Parsing and alignment remain heuristic, company-relative context can be incomplete,
and topic grouping is conservative. There is no independent human-labeled ranking
corpus or complete historical HTML-pair ingestion fixture. The documented
BeautifulSoup XML-as-HTML warning was not suppressed. These are preserved limitations,
not an active backlog.

## Development checks

```bash
uv sync --frozen --extra jev
uv run --frozen --extra jev pytest -q
uv run --frozen --extra jev ruff check src tests
uv run --frozen --extra jev ruff format --check src tests
uv build
```

The tests use fixtures and mocked transports: no SEC access, paid provider calls,
credentials, or embedding weights are needed. The `jev` extra exercises SDK response
compatibility; embedding/model packages are optional for these checks. CI runs the
same checks on Python 3.12. Passing tests establish software behavior, not research
quality. The last development checkpoint recorded 234 passing tests and successful
provider-free replay of 1,851 judgments; see the working log for scope and caveats.

## License

[MIT](LICENSE). Real-source regression excerpts retain their filing provenance;
company disclosures are not authored by this project.
