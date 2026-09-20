# Working notes — SEC Filing Delta Radar

A running log of completed work, decisions, verification, and remaining limitations.
For the full specification and implementation details, see
[SEC_Filing_Delta_Radar_Codex_Handoff.md](SEC_Filing_Delta_Radar_Codex_Handoff.md).

## Maintaining this log

- Append dated entries after meaningful changes; preserve earlier results as history.
- Record what changed, why, what was actually verified, and any remaining risks.
- Distinguish synthetic tests, live runs, assistant review, and independent human labels.
- Record commits and pushes only after they happen. Never include credentials or secret values.
- This is a manually maintained log, not an automatic activity recorder.

## 2026-09-20

### Implemented the local analysis pipeline

- Built the installable Python package `filing-delta-radar`, exposing `radar fetch`,
  `radar analyze`, and `radar report`.
- Added SEC filing discovery, latest/prior same-form selection, declared-contact access,
  process-shared rate limiting, retries, cached downloads, and cache-only recomputation.
  Amendments are excluded from normal comparison selection.
- Added narrative extraction with item/subsection labels, original source text,
  normalized hashes, and diagnostic paragraph JSON.
- Added exact/cosmetic filtering, cached local SentenceTransformers embeddings,
  paragraph matching, and explicit additions/deletions.
- Integrated versioned TypeSafe/Jev questions, separate matched/added/deleted judgments,
  response validation, bounded concurrent evaluation, provenance, and SQLite caching.
- Added configurable heuristic ranking and standalone HTML reports with escaped text,
  word-level differences, semantic distributions, sorting, and filtering.
- Kept semantic probabilities and heuristic ranking scores explicitly distinct from
  verified financial facts or investment recommendations.

### Exercised the initial implementation

- Ran a three-company synthetic cache through the actual CLI, producing parsed
  paragraphs, persisted runs, and HTML reports. This was not live multi-company research.
- Exercised a real local MiniLM model and persistent embedding reuse.
- A mocked semantic-provider run made 18 initial requests and zero additional requests
  on an identical rerun. These were fixture responses, not live Jev judgments.
- Browser-checked section/relation/domain filters, sorting, empty states, independent
  company controls, and desktop/mobile layouts.
- Confirmed persisted reports can be rerendered without provider credentials.

### Completed the first live RELL comparison

- Used the configured identifying SEC contact and TypeSafe credentials locally;
  credential values are not recorded here.
- Compared these RELL 10-Q filings:

  | | Accession | Filed | Report period | Extracted paragraphs |
  |---|---|---|---|---:|
  | Previous | `0001193125-26-007399` | 2026-01-08 | 2025-11-29 | 197 |
  | Current | `0001193125-26-149207` | 2026-04-09 | 2026-02-28 | 208 |

- Baseline run: `20260920T041813-1c7e0a8e94`.
- Produced 86 matched changes, 14 additions, and 3 deletions; 108 exact pairs skipped
  semantic evaluation. Live Jev (`jev-1.13.0`) supplied 103 validated evaluations.
- Two initial responses failed probability-sum validation. Targeted retries returned
  valid responses; validation was not weakened and rejected responses were not ranked.
- Replayed all 103 evaluations with semantic network calls forbidden: zero calls,
  unchanged cached payloads, and no semantic errors.
- Browser inspection exposed reversed previous/current section labels in some report
  headings. Fixed the ordering and retained a regression test.

### Reworked alignment to reduce misleading ambiguity

Problem: the baseline flagged 42 pairs, including 18 of the top 20 results. Local
embedding neighbors could confuse quarterly and year-to-date disclosures.

Changes:

- Derived normalized subsection keys and reporting scopes without rewriting source
  text or changing Jev request payloads.
- Distinguished quarterly, year-to-date, annual, point-in-time, mixed, and unknown
  reporting subjects. Avoided treating incidental historical transaction dates or
  forward-looking durations as the subject's reporting period.
- Preferred corresponding subsections before broader same-item and filing fallbacks.
- Replaced local pair selection with joint one-to-one assignment within each candidate
  tier/component, including explicit unmatched options.
- Measured ambiguity after assignment using counterfactual objective loss. These
  margins are conditional matching evidence, not probabilities or global confidence.
- Required adjacent-text evidence for split/merge warnings rather than warning merely
  because two local embedding neighbors were close.
- Persisted scopes, matching components, margins, review reasons, and alternative
  pairings. Added an alignment-status filter and expandable evidence to the report,
  separately from economic ranking. Historical runs without metadata show it as unavailable.
- Added `alignment_ambiguity_margin` to settings and the example configuration
  (default `0.04`), and declared SciPy as a direct dependency for assignment.

Results from revised run `20260920T050007-23c12bd0de`:

| Measure | Baseline | Revised |
|---|---:|---:|
| Flagged pairs | 42 | 14 |
| Flagged results in the final top 20 | 18 | 2 |
| Selected assistant-reviewed counterpart expectations satisfied | 32/34 | 34/34 |

- Corrected a concrete tax-note mismatch: the prior six-month note now matches the
  current nine-month note; the new quarterly note remains unmatched in that subsection.
- The 34 reviewed expectations are selected assistant-reviewed cases, **not independent
  human gold labels or a general accuracy benchmark**. Fewer flags alone do not prove accuracy.
- Retained seven broader-subsection flags and seven mixed/one-sided scope flags.
  Counts remained 108 exact skips, 86 matched changes, 14 additions, and 3 deletions.
- Reused 101 existing Jev evaluations and made exactly two successful new requests.
  A subsequent replay reused all 103 evaluations with zero provider calls and left
  all 105 stored semantic-cache records unchanged.
- Preserved an actual SEC tax-note regression fixture, including source URLs, in
  `tests/fixtures/rell_income_tax_alignment.json`.

### Verified the revised implementation

- `uv run --all-extras pytest -q`: **149 passed**.
- `uv run ruff check src tests`: passed.
- `uv run ruff format --check src tests`: all 37 Python files already formatted.
- `uv build`: source distribution and wheel built successfully; the wheel contains
  the new alignment modules and report template.
- Compared the assignment solver against brute-force enumeration on 160 deterministic
  sparse graphs; optimal objectives and counterfactual margins agreed.
- Browser-checked alignment filtering, evidence and alternative expansion, historical
  unavailable states, independent company controls, and empty filter results.
- Inspected desktop and 390-pixel mobile layouts, including expanded evidence;
  no horizontal overflow was observed.
- Confirmed `radar report 20260920T050007-23c12bd0de` works with SEC and TypeSafe
  credential environment variables empty.
- Removed temporary validation scripts and preview reports; retained the final report
  and comparison audit locally. Updated the handoff and example configuration.

### Initialized Git and published privately

- Initialized Git on `main` and created initial commit `1cab9d2`
  (`Initial commit: SEC filing delta radar`).
- Created [XGenic/jev-filings](https://github.com/XGenic/jev-filings) as a **private**
  GitHub repository, pushed `main`, and configured tracking of `origin/main`.
- Verified private visibility, matching local/remote commit IDs, and a clean working
  tree at the end of initial publication.
- Scanned all 48 initially committed files against local credential values and common
  credential patterns; no findings.
- Kept `.env`, filing data, SQLite databases, reports, caches, the virtual environment,
  and build artifacts out of Git. Added a safe `.env.example`.
- Added this working log after initial publication; it was not part of `1cab9d2`.

## Local artifacts and operational notes

These `data/` artifacts are ignored by Git and are not included in a fresh clone:

- Latest report: `data/reports/20260920T050007-23c12bd0de.html`.
- Baseline report: `data/reports/20260920T041813-1c7e0a8e94.html`.
- Reviewed alignment audit: `data/processed/rell-alignment-review.json`.
- Persisted runs, embeddings, and semantic provenance: `data/radar.sqlite3`.
- Raw SEC documents: `data/raw/`; extracted paragraph JSON: `data/processed/`.

`--recompute` uses cached SEC filings but does not prohibit a first embedding-model
 download or uncached Jev requests. A fresh clone needs its own configuration and filing
cache. `radar report RUN_ID` uses persisted judgments without SEC/model/provider calls.

## Remaining limitations and unfinished acceptance work

- Live verification currently covers RELL only. A real three-to-five-company run remains
  outstanding; the earlier three-company exercise used synthetic filings.
- A complete versioned historical SEC HTML-pair fixture remains outstanding. The real
  tax-note excerpt regression does not cover full historical ingestion.
- No independent human-labeled ranking corpus exists yet. Comparing lexical, embedding,
  and Jev ranking quality remains open; no improvement in ranking quality is claimed.
- Reporting-scope extraction and alignment remain heuristic. The remaining review flags
  should be inspected, not removed merely to reduce their count.
- The live recomputation emitted BeautifulSoup's `XMLParsedAsHTMLWarning` while parsing
  the cached SEC document. The run completed; the warning was not suppressed.
- Economic ranking scores are not calibrated probabilities or investment advice.
