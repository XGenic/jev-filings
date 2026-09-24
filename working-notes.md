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
- Deliver human spot-checks as readable, self-contained HTML. JSON may remain as
  machine-readable audit data, but must not be the only human review format.

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

## 2026-09-21 (UTC run timestamps)

### Completed the user-selected four-company live run

- Tested NPK, PKE, BFLY, and CPSH as a small-company disclosure-discovery cohort,
  using latest/prior non-amended same-form selection without changing strategies.
- Final run: `20260921T033232-0ee464b545`, with one standalone report containing
  20 ranked deltas per company and no remaining company or semantic errors.

| Ticker | Form | Previous period | Current period | Matched changes | Added | Deleted | Jev evaluated | Alignment review flags |
|---|---|---|---|---:|---:|---:|---:|---:|
| NPK | 10-Q | 2026-04-05 | 2026-07-05 | 35 | 20 | 6 | 61 | 15 |
| PKE | 10-Q | 2025-11-30 | 2026-05-31 | 65 | 3 | 33 | 101 | 25 |
| BFLY | 10-Q | 2026-03-31 | 2026-06-30 | 50 | 32 | 10 | 92 | 23 |
| CPSH | 10-Q | 2026-03-28 | 2026-06-27 | 54 | 13 | 15 | 82 | 27 |

- Extracted 621 previous and 625 current paragraphs; skipped 353 exact pairs.
  Live Jev (`jev-1.13.0`) supplied all 336 non-skipped evaluations.
- Seven initial responses (two PKE, five CPSH) failed probability-sum validation.
  A cache-based recomputation reused 329 valid evaluations and successfully retried
  the seven missing ones; validation was not weakened.
- Replayed all 336 final evaluations with semantic-provider calls forbidden:
  zero calls, identical signals, and all 441 semantic-cache records unchanged.
- Confirmed credential-free `radar report 20260921T033232-0ee464b545`.
- Browser-checked all 80 cards, independent company controls, relation/section/domain/
  alignment filters, economic sorting, empty results, and expanded alignment evidence.
  Desktop and 390-pixel mobile layouts had no horizontal overflow.

### Fixed a real ingestion blocker exposed by the cohort

- NPK, PKE, and CPSH initially failed because the selector required every historical
  metadata row to name an HTML file, including unused legacy `.txt` and blank filenames.
- Separated safe metadata-path validation from selected-document HTML validation.
  Unsafe paths still fail, and a selected non-HTML document fails explicitly rather
  than silently substituting an older HTML filing.
- Added regressions for legacy metadata coexistence and no silent selected-form fallback.
  The legacy regression failed before the fix; all four live comparisons now complete.
- `uv run --all-extras pytest -q`: **151 passed**.
  Ruff lint and formatting checks passed; all 37 Python files are formatted.

### Assistant-reviewed disclosure candidates and limits

- NPK: a new $38.116 million Tech Ord construction commitment; $2.245 million in
  tariff refunds recognized in the quarter and another $7.553 million received after
  quarter-end that the filing says will be recognized in Q3.
- PKE: a new Tulsa composites manufacturing/development site, approximately 18 acres,
  with a 25-year initial sublease and construction expected in fiscal 2027.
- BFLY: Embedded revenue-growth language now names Midjourney; software/services
  revenue grew 149.8% year over year in Q2 versus 68.2% in Q1. The current exhibit
  index dates the agreement to November 2025: newly named in this comparison does
  not mean newly signed or previously unknown to the market.
- CPSH: language changes from no significant tariff-attributable cost increases to
  actual margin pressure and constrained pass-through to foreign customers; the
  company describes the overall impact as relatively small. Inventory and demand
  explanations also change, but do not establish newly won orders.
- Checked candidate facts against complete cached source filings and searched prior
  text for purported additions. Retained evidence and caveats in
  `data/processed/four-company-disclosure-review.json`.
- The review is **assistant review, not independent human labels or a ranking benchmark**.
  Top-20 alignment flags: NPK 8, PKE 15, BFLY 7, CPSH 4.
- Concrete noise remains: quarter/YTD mismatches, NPK MD&A mislabelled as Item 1,
  BFLY page-break fragments and an existing lawsuit labelled added, and a CPSH
  tax paragraph paired to unrelated stock compensation under a stale subsection.
  Monetary units can live outside the extracted paragraph and require source context.
- PKE's prior 10-Q is from November 2025; the same-form strategy intentionally skips
  the intervening annual filing. Changes are not necessarily new since that 10-K.
- This completes the real multi-company execution/report acceptance exercise, not proof
  of market neglect, investment value, calibrated probabilities, or Jev ranking uplift.

### Completed a provisional blinded assistant ranking evaluation

- User approved using the assistant as a first-pass quality judge. This is an
  **assistant-labeled benchmark, not independent human ground truth**.
- Froze `four-company-assistant-eval-v1` against run `20260921T033232-0ee464b545`.
  Four fresh-context default-model judging calls received complete previous/current
  primary-filing text and shuffled candidate passages, but no scores, ranks, Jev
  answers, alignment warnings, or previous assistant-review conclusions.
- Labelled 251 of 336 eligible candidates: pooled top-20 results across lexical,
  embedding, and Jev-assisted rankings, matched-only and all-candidate views,
  five lower-ranked controls per company, and all unmatched candidates.
- A clean follow-up requires a sound comparison, a concrete development worth
  follow-up, and source-supported evidence. Context-only financial updates do not
  count. All supplied evidence quotes passed normalized source-substring checks;
  this checks provenance, not correctness of interpretation.
- Primary comparison is **matched-only ranking** on the same aligned candidate pool.
  Lexical and embedding baselines sort by text/cosine drift; the Jev-assisted method
  uses the persisted heuristic including its existing section and other contributions.
  This is not a separate lexical-only pipeline or a causal Jev-only ablation.

| Ranking | Clean follow-ups in top 10/company | Clean follow-ups in top 20/company |
|---|---:|---:|
| Lexical drift | 5/40 (12.5%) | 12/80 (15.0%) |
| Embedding drift | 9/40 (22.5%) | 14/80 (17.5%) |
| Jev-assisted heuristic | 13/40 (32.5%) | 18/80 (22.5%) |

- Jev versus embedding top-10 counts by issuer: NPK 2 vs 1, PKE 3 vs 1,
  BFLY 6 vs 4, CPSH 2 vs 3. The aggregate gain is four cards, not universal improvement.
- In the full report's top 20/company, 21/80 cards were clean follow-ups and 18/80
  were judged broken comparisons/false additions or deletions. The 21 positives map
  to 13 judge-assigned issuer/topic groups, so repeated developments consume slots.
- All-candidate lexical/embedding baselines place unmatched disclosures in a tied
  first-priority block; no missing cosine is imputed. Their tie-sensitive results
  are secondary and should not be used as the headline Jev improvement.
- A preselected 20-case fresh-context repeat agreed on clean-follow-up status 20/20,
  but contained only one positive. A supplemental balanced repeat retained all eight
  positives and all eight nonpositives (16/16); together these cover 35 distinct cases.
  Priority agreement in the first repeat was 18/20, and some error/restatement labels
  differed. Original labels stayed fixed; repeats did not change the ranking metrics.
- The primary judge returned no uncertain labels. That is not evidence of certainty;
  same-model consistency is not independent validation or probability calibration.
- Source audit confirmed a CPSH inconsistency: Note 15 reports $8.977 million net
  offering proceeds, while MD&A and cash-flow/equity statements report $8.997 million.
  Preserve that discrepancy rather than presenting one figure as independently settled.
- Saved rubric, sources, blinded packets, raw/validated labels, ranking definitions,
  tie bounds, metrics, and repeats under `data/processed/four-company-assistant-eval-v1/`.
  `judgments.csv` has 251 auditable rows; `human-spotcheck.json` has ten selected cases.
- Independently recomputed all primary metrics from the saved artifacts successfully.
  No production ranking weights, parser/alignment code, or semantic judgments were changed.
- Interpretation: provisional evidence favors retaining the current Jev-assisted
  ranker, but the modest gain, duplicated topics, and false additions/matches warrant
  better alignment/context and deduplication before scaling. Human spot-checks,
  broader issuers, market novelty, cost-benefit and investment performance remain untested.

### Clarified the discovery objective and human review format

- The user emphasized early evidence of expansion or a developing deal/partnership:
  preparatory commitments and indirect, qualified language that may precede an explicit
  announcement, rather than generic economic significance.
- Prioritize company-specific changes such as dedicated capacity or facilities,
  customer-funded development, qualification/pilot activity, supplier/customer
  commitments, exclusivity or licensing terms, and conditional commercial arrangements.
  These are candidate signals, not evidence that an undisclosed deal necessarily exists.
- Separate exact observations from the inferred development, plausible routine
  explanations, counterevidence, and what would confirm or disprove the hypothesis.
  Do not name an undisclosed partner or claim an imminent/signed deal without support.
- Broad tariff commentary and routine financial rollforwards are normally background
  for this objective. Do not suppress every tariff mention: a concrete company-specific
  expansion or commercial commitment can still be relevant.
- The existing blinded benchmark used a broader substantive-development rubric.
  Keep its labels and metrics frozen; it does not measure success at the narrower
  early-commercial-development objective. Any later rerating needs a separately
  versioned rubric and cohort, with macro-only/background cases retained as controls.
- Future human spot-check deliverables must be HTML, with source passages, filing
  dates/links and expandable assessments. Keep JSON as optional audit data.
- Converted all ten existing cases to
  `data/reports/four-company-assistant-spotcheck.html`, reusing the report styling,
  escaped word diff, SEC URL validation, and autoescaped template rendering.
  Source passages and original assessments are preserved; assessments start collapsed.
- Verified ten cases, twenty SEC source links, company filtering, assessment reveal,
  lexical-diff expansion, and desktop/390-pixel mobile layouts without horizontal
  overflow. The report has no external asset dependencies.
- Confirmed the frozen source JSON still matches its recorded SHA-256 and all ten
  source passages/assessment reasons appear unchanged in the HTML. The page explicitly
  warns that the original cases have not been rerated under the refined objective.

### Recorded the first human HTML spot-check feedback

- Preserved the user's feedback separately in
  `data/processed/four-company-human-calibration-v1.json`; do not overwrite the
  frozen assistant labels or interpret this selected review as a precision estimate.
- Cases 1 (NPK Tech Ord) and 3 (PKE Tulsa facility) are the user's clearest examples
  of "things are building up." Public-announcement novelty has not been checked for
  these two cases; strong preparatory evidence does not itself prove public novelty.
- Case 10 (BFLY product mix) is potentially useful for understanding future revenues.
  Include commercial-trajectory evidence alongside preparatory expansion/deal signals.
  Its page-break/false-deletion problem remains separate: missing wording does not
  establish that favorable product mix reversed.
- Cases 5 (Midjourney) and 7 (Rose litigation) have conditional interest, depending
  on whether the information was already public. The user's "remaining cases" comment
  maps cases 2, 4, 6, 8, and 9 to background/low priority for this objective, not a
  blanket rejection of those subjects or a literal revenue/tariff description of each.
- Subsequent source check: the Midjourney agreement is named in a November 2025 8-K
  (agreement dated November 17; signature November 18), and Butterfly's June 18, 2026
  press release discusses the collaboration. Both precede the July 30 current 10-Q.
  This is confirmation/revenue context, not discovery of an unannounced partner.
- Subsequent source check: the prior BFLY 10-Q already describes Rose litigation and
  the $0.3 million accrual. The current filing explicitly reports no change to that
  estimate in the quarter. The aligner's added label is not evidence of novelty.
- Keep three separate judgments: commercial research interest, validity of the
  old/new comparison, and public-information novelty as of the filing date. Public
  availability does not establish market awareness or pricing; an unsuccessful
  announcement search does not establish that a development was unannounced.
- Added a separate, initially collapsed human-feedback/source-check panel to all ten
  HTML cases. Browser-verified independent human/assistant reveal controls, BFLY
  filtering and all-company reset, and desktop/390-pixel mobile presentation without
  horizontal overflow. All original excerpts and assistant reasons are preserved;
  the frozen source JSON SHA-256 is unchanged.

### Completed the source-first preparatory/commercial screening experiment

- Human review: `data/reports/research-screen-v2.html`, exactly 20 cases across
  ASTE/GRC/HURC (12 fresh cases) and earlier BFLY/PKE pairs (8 historical cases).
  Old/new passages, SEC dates/links, surrounding context, word diffs, two separately
  collapsed assistant assessments, baseline ranks and announcement evidence are retained.
  Only historical cases have later-outcome panels; those start collapsed.
- Corrected the two identified BFLY comparison defects at their sources:
  `loss contingency` is not a reporting-period income/loss measure, so the existing
  lawsuit now matches its earlier disclosure; conservative page-furniture removal
  joins the split product-revenue passage before extraction. Real-source smoke checks
  confirmed a coherent product-mix paragraph, no orphan continuation, unchanged raw
  HTML, and a matched litigation comparison. These are not universal alignment fixes.
- Production reports/CLI now describe added/deleted relations as unmatched current/
  previous passages, not evidence of new or absent disclosure. Jev questions and
  ranking weights are unchanged. Corrected parsing/alignment affects new or recomputed
  analyses; frozen reports and labels are not rewritten and paragraph ordinals can shift.
- Added `radar research-prepare`, `research-validate`, and `research-report`.
  Preparation uses explicit filing pairs and exports the full ordered source corpus,
  candidate IDs, instructions and response schema without production scores or outcomes.
  The contextual judge runs separately; validation requires complete candidate coverage,
  valid side/paragraph IDs and source-supported quotes. Sound matched follow-ups require
  evidence from both sides. Uncertain/broken comparisons are not clean usable leads.
- Froze the design, five filing pairs and labels under
  `data/processed/research-screen-v2/`. Fresh issuers were selected before content review.
  PKE's initially assumed May 28 historical cutoff was corrected before screening:
  that 8-K was earnings, while the verified later Tulsa disclosure is July 20.
  The original protocol and explicit amendment are both preserved.
- Screened all 467 eligible changed passages: ASTE 108, GRC 89, HURC 104,
  historical BFLY 85 and historical PKE 81. All quotes/IDs validated. One unsupported
  GRC quote required a fresh source-only reassessment; the rejected response remains.
- Fresh: 22 primary follow-up paragraphs, all commercial trajectory; **zero preparatory
  follow-ups**. The screen also flagged 92/301 comparisons as broken and 11 as uncertain.
  Historical: 18 follow-ups, with no demonstrated specific connection to the later
  Midjourney or Tulsa event. Comparison flags and relevance labels are assistant
  judgments, not adjudicated error counts or human gold labels; repeated topics remain.
- The fixed 20-case selection uses three priority-band/comparison-validity/stable-ID
  cases per issuer plus one production-baseline contrast. A second source-only pass,
  with the same requested `default` model alias in fresh contexts, retained 14/20 as
  conditional follow-ups versus the primary pass's 16/20. Three categorical disagreements
  remain visible. The helper did not expose the exact resolved backend model.
- Prior-public checks: 8 cases were already announced, 10 concern known developments
  with additional filing detail, 1 search was inconclusive and 1 background case was
  not separately checked. Hurco's private-label frames and Gorman-Rupp's data-center
  commentary were already in earlier earnings releases. Searches were bounded;
  Hurco social-post dating uses current creation metadata, not an archived January copy.
- Publication instants take precedence when supplied; require timezone-aware release
  and filing timestamps. Date-only evidence must predate the filing day. A regression
  reproduced and fixed a stale date label overriding a post-cutoff instant, and covers
  cross-timezone/UTC-midnight ordering.
- Persisted production Jev, lexical and embedding baselines separately. Drift ranks
  exclude unmatched pairs rather than imputing missing similarities; a matched-only
  production ranking supports like-for-like comparison. All 467 Jev evaluations
  succeeded after strict probability-validation retries; rejected outputs are retained.
- Verification: Ruff passed; full pytest suite **167 passed**. Browser checks exercised
  every company filter, source/diff/assessment/baseline/outcome controls and desktop/
  390-pixel layouts. Fixed an observed mobile overflow from long audit identifiers.
  Real-data production filters showed 29 unmatched-current and 19 unmatched-previous
  GRC passages with corresponding labels/counts. The temporary surface-smoke HTML
  is not a new persisted production run.
- Verified all 31 original benchmark hashes, all 22 frozen primary-response hashes,
  five packet hashes and ten raw-filing hashes; the evaluated prompt/schema are unchanged.
  This pilot does **not** justify promoting the experimental screen to production
  or claiming early-deal prediction, population precision, recall or investment returns.

## 2026-09-24 (UTC run timestamps)

### Pivoted production analysis to general categorization and business impact

- The user approved moving away from unannounced-deal discovery as the central objective.
  The product now asks what changed, what kind of business change it is, and how much it
  could matter. Public novelty is separate; favorable and adverse developments matter.
- Added the versioned `filing-delta-2` rubric: business subject, change nature, business
  direction, relative magnitude, duration, timing, evidence strength, comparison validity
  and high/medium/low/unclear impact. Active risk-specific questions were replaced;
  historical fields remain readable without invented new assessments.
- Added adjacent previous/current source context to evaluation and persistence. Context
  participates in cache identity and is validated against filing sides. It is not a
  complete-filing search, and missing company-scale evidence stays explicit.
- Added `business-impact-1` ranking: supported high/medium/low assessments precede review
  and unavailable candidates; impact and then existing weighted scores break ties.
  Definite impact/comparability decisions require a 0.60 selected-label probability.
  This is an uncalibrated decision rule, not financial-outcome confidence.
- Unmatched or questionable comparisons do not become supported changes merely because
  their disclosed subject is important. Impact, direction and comparison reliability
  remain separate. Explanations reproduce selected rubric criteria, not generated reasoning.
- Reports expose the new categories, impact and reliability filters, impact sorting,
  expandable distributions and surrounding source passages. Historical dimensions are
  explicitly unavailable. Full-set priority/review counts remain visible when `top_n`
  excludes candidates; filtering and sorting operate on included cards only.
- Existing production commands now use the new rubric on new analyses. Offline `radar report`
  does not upgrade historical judgments. Frozen benchmarks and research workflows were
  not rewritten; the new categorization/impact rubric has no independent quality benchmark.

### Fixed two concrete NPK comparison defects

- Empty destination anchors no longer mark real Item headings as navigation, correcting
  NPK MD&A attribution to Part I / Item 2.
- Reporting-scope extraction now recognizes plural `quarters`, preventing a quarterly
  income-tax disclosure from accepting the six-month counterpart. The real-source run
  matched the quarterly passages and preserved the current raw filing byte-for-byte.
- Retained boundary regressions for both defects; these are not universal parser fixes.

### Exercised the new production path

- Final live NPK run: `20260924T024532-36a42abd00`.
  Same filing pair as the earlier cohort, using cached SEC filings and live Jev:
  34 matched changes, 21 unmatched current and seven unmatched previous passages.
- All **62 assessments** validated. Twelve initial responses failed strict probability/
  selected-choice validation; one still failed on the first cache-based retry. The next
  retry completed it. Validation was not relaxed and invalid responses were not cached
  as successful judgments.
- Saved `data/reports/business-impact-npk.html` with all 62 eligible cards, using
  `top_n=500` for this review. Impact counts: five medium, 34 low and 23 unclear.
  Priority counts: one medium, 15 low and 46 review. These are model judgments, not
  human-adjudicated quality metrics.
- Tech Ord's construction commitment is operations/capacity, medium impact and review
  priority because its passage is unmatched. It appears at rank 20; the classification
  does not establish public novelty or independently verify the commitment.
- Replayed all 62 evaluations with semantic calls forbidden: **zero provider calls**.
  Confirmed unchanged ranking on rerender and preserved context. Credential-free
  historical report rendering also succeeded; the old four-company ranking was unchanged
  at unchanged weights.
- Verified unchanged payload hashes for all seven pre-existing runs and 908 pre-existing
  semantic-cache records. Frozen research artifacts were not edited.
- Verification: **187 pytest tests passed**; Ruff lint passed; source distribution and
  wheel built. Browser exercised all 30 new-filter option selections, empty states,
  stable impact sorting, independent historical-company controls and expanded dimensions/
  source context. Desktop and 390px mobile had no horizontal overflow or browser errors.
- Remaining limits: source extraction/alignment are heuristic, company-relative context
  can be insufficient, topics can repeat, and the new rubric needs human evaluation.
  The existing BeautifulSoup XML-as-HTML warning remains unsuppressed.

### Ran five-company reports and blinded source-only assistant judging

- Final production run: `20260924T030625-b5de787e2b`, covering ASPI, CLMT, QURE,
  AOSL and AIP. All **1,361 eligible assessments** validated: 369 ASPI, 263 CLMT,
  188 QURE, 330 AOSL and 211 AIP. Production code, rubric and ranking were not
  changed for this audit.
- ASPI, CLMT, QURE and AIP compare March 31 with June 30, 2026 10-Qs. AOSL
  compares June 30, 2025 with June 30, 2026 10-Ks; this is not a uniform quarterly cohort.
- Initial strict-validation failures numbered 205. Four cache-based retries reduced
  remaining failures to 50, 11, one and zero; validation was not relaxed.
- Replayed all 1,361 assessments with provider calls forbidden: **zero calls**.
  All 2,331 semantic-cache payload hashes were unchanged across that replay.
  Credential-free `radar report` rendering of the final run also succeeded.
- Combined report: `data/reports/business-impact-five-company.html`. Individual
  `business-impact-{aspi,clmt,qure,aosl,aip}.html` reports each contain the top 20.
- Judge review: `data/reports/five-company-impact-judge-review.html`. Frozen inputs,
  raw judgments, metrics, verification and delivery hashes are under
  `data/processed/five-company-impact-review-v1/`.
- Five parallel fresh-context subagents each assessed 20 leading cards and five
  deterministically sampled lower-ranked controls: **125 cases**. Packets withheld
  production labels, scores, ranks, warnings and sample membership. Judges received
  broader filing context, including flattened tables, than the production evaluator.
- All 125 outputs passed schema, coverage and source checks without corrections.
  Independent recomputation verified **31 frozen hashes and 474 evidence quotations**
  and reproduced every saved metric. Quote provenance does not prove interpretation.
- Judges marked **49/100 top-20 passages** worth reviewing, in 38 judge-assigned
  issuer-scoped topic groups: ASPI 6/20, CLMT 13/20, QURE 9/20, AOSL 13/20,
  AIP 8/20. Of these, 42 were judged sound medium/high-impact changes.
- Top-20 agreement: subject 68/100, direction 48/100, relative magnitude 24/100,
  displayed impact 62/100. Judges assigned high impact to 15 leading cases; production
  assigned none. These are descriptive assistant disagreements, not accuracy measures.
- Judges rated 96 leading comparisons sound, three broken (ASPI) and one uncertain
  (QURE). Controls had four review-worthy cases and eight broken comparisons.
  Notable lower-ranked controls were ASPI's Noble subscription funding at rank 206
  and AOSL's completed JV stake sale at rank 238; this sample does not estimate recall.
- Execution caveat: shared Eval state briefly exposed the ASPI judge to AIP source
  text after a generic-variable collision. It reported no production-label/score
  exposure; all judges subsequently used issuer-prefixed state. The incident and
  each judge's disclosure are retained. Conversational contexts were separate,
  but execution was not perfectly isolated.
- Browser verification covered the combined report and all five individual reports,
  production subject filtering and impact sorting, all 17 judge-filter selections,
  intersecting/empty states, all 125 nine-category tables, expanded source evidence
  and valid local report/case links. Desktop and 390px mobile had no page overflow
  or browser errors. No project tests were rerun for this one-off, code-unchanged audit.
- No ranking or labels were tuned after judging. One judge per issuer, no repeat
  judgments and no independent human adjudication: impact calibration, generalization,
  public novelty, causal ranking uplift and investment performance remain unestablished.

### Implemented evidence-first comparisons and bounded source context

- Added visible inline-XBRL facts with exact Decimal values, source scale/sign,
  units, entities, dimensions, periods, anchors and paragraph/table references.
  Preserved original table cells, row indices, spans and header flags without
  inventing rectangular column relationships. Unsupported numeric transformations
  and continuations remain explicit extraction diagnostics.
- Added indexed, scope-gated adjacent/relevant context and unmatched counterpart
  suggestions. An unmatched suggestion never becomes a verified numeric change.
  Arithmetic requires facts attached to both exact target paragraphs, compatible
  periods, identical concepts/entities/units/dimensions and no conflicting values.
  Quarter/YTD totals and within-filing YoY comparators are not naively subtracted.
- Real-source smoke checked 897 target spans and 24 attributed calculations in
  the frozen 125-case sample, plus unchanged hashes for all ten raw filings.
  Repaired ASPI's restricted-cash and technology-ownership page continuations.
  An unrelated cash forecast no longer inherits a numerical cash-balance change.
- Count limits alone failed on two live requests. Deduplicated facts/quotes in the
  provider representation and bounded supplemental packets to **24,000 UTF-8 bytes**.
  Whole optional records are omitted with a persisted notice; original targets
  remain intact. Maximum in the frozen sample: 23,992 bytes. All 125 sample packets
  required some optional-context omission. Missing context is not evidence of absence.
- Revised rubric `filing-delta-3` clarifies recurring operating/exposure changes,
  direction, company-relative magnitude and financing versus capital deployment.
  The 0.60 reporting floor and strict probability validation are unchanged.
- Report policy `evidence-review-1` separates supported changes from comparison
  review, with one total leading-card budget. Supported high/medium groups come
  first; assessed review may use `min(remaining_budget, max(1, top_n // 4))` slots,
  then supported low and remaining review/unavailable groups. Filters and sorting
  are lane-local. Conservative same-event/period grouping retains every member's
  original passages and independent judgment; broad topic overlap is insufficient.

### Completed production and controlled development ablation

- Final five-company run: **`20260924T042703-f0fdba83b6`**.
  All **1,147 eligible passages** have strict-valid assessments: ASPI 310, CLMT 223,
  QURE 180, AOSL 251, AIP 183. Extracted 9,626 numeric source facts and retained
  125 candidate-level computed changes across the complete run.
- The recorded production invocation made 1,413 provider calls including invalid
  responses/retries; wall time was 426.32 seconds. Provider-reported usage is in
  `production-invocation.json`; no dollar-cost estimate was inferred.
- Preserved the failed oversized first experiment as `evidence-tuning-v1`.
  Revised operational inputs/results are under `evidence-tuning-v2`; exact targets,
  alignments and both rubric snapshots remained unchanged. All three ablations
  completed all 125 frozen cases.
- Original top-100 agreement with the frozen assistant labels:

  | Configuration | Subject | Direction | Magnitude | Raw impact | Displayed impact |
  |---|---:|---:|---:|---:|---:|
  | Original rubric / adjacent context | 68 | 48 | 24 | 62 | 62 |
  | Revised rubric only | 72 | 47 | 14 | 62 | 44 |
  | Enriched evidence only | 68 | 52 | 52 | 62 | 52 |
  | Revised rubric + evidence | 73 | 47 | 45 | 62 | 37 |

- Evidence helps magnitude interpretation, but the rubric does not improve every
  dimension and lowers displayed-impact agreement. These are development diagnostics,
  not human accuracy. Parser repairs and the new full-pool selection are not isolated
  by this fixed-target ablation. The threshold was not relaxed to inflate agreement.

### Evaluated two preregistered fresh issuers

- Selected **AEHR and INOD before reading their sources**. Froze code, exact questions,
  raw sources, complete corpora, targets and alignment. Both configurations completed
  all **352 eligible comparisons** (AEHR 212, INOD 140): original rubric/adjacent
  context versus revised rubric/enriched evidence. Observed provider calls were
  437 and 424 respectively, including strict-validation retries.
- Separate-kernel Eval child agents judged the blinded union of all selected members:
  **63 candidates and 228 verified source quotations**, with every required target
  endpoint cited. Labels were frozen before joining hidden selections and outcomes.
  This avoids the shared-kernel incident recorded in the earlier five-company audit.

  | Fixed 20-card budget per issuer | Original | Enriched |
  |---|---:|---:|
  | Judge-worthwhile cards | 15/40 | 21/40 |
  | Judge-sound, worthwhile medium/high cards | 11/40 | 15/40 |
  | Worthwhile issuer/topic groups | 11 | 16 |
  | Cards containing a judge-broken comparison | 0 | 6 |

- AEHR worthwhile cards improved 7→14; INOD declined 8→7. All six broken comparisons
  were already `needs_review` and in the review lane; none entered the supported-change
  lane. Seven of ten review-lane cards were judged worthwhile as disclosed matters,
  not necessarily as valid changes. No multi-member groups occurred in this selected
  fresh sample, so it does not demonstrate automatic deduplication gains.
- One assistant judge per issuer, no repeats or human adjudication. Selected-set
  coverage is not population recall. This combined configuration comparison does not
  identify separate causal contributions from the rubric, evidence and selection policy.

### Verified persistence, CLI and report interactions

- **234 tests passed**; Ruff lint and formatting checks passed; wheel and sdist built.
- A persisted-evidence replay exposed quote-reference IDs depending on dictionary
  side order. Canonicalized previous/current traversal and retained a regression.
  The post-freeze operational fix and old/new source hashes are explicitly recorded;
  it did not change any frozen request, source, rubric, threshold or judgment.
- Replayed **1,851 judgments** with the provider forbidden: 1,147 persisted five-company
  records and 352 records for each fresh variant. Zero calls; identical original cache
  keys and signals. All **2,331 pre-existing evaluation payloads and 15 run payloads**
  retained their hashes. The original audit still verifies 31 frozen inputs, 125 labels
  and 474 quotations.
- `radar report 20260924T042703-f0fdba83b6` with SEC and TypeSafe credential environment
  values explicitly empty produced byte-identical HTML.
- Browser checked 100 combined leading cards, all five 20-card individual reports,
  the 40-card fresh report, both source-linked audit reviews, lane isolation,
  filtering/empty states, sorting and expanded numeric evidence. Group-member expansion
  used an explicitly synthetic duplicate of one recorded assessment, not a claimed
  production discovery.
- Fixed mobile overflow from long audit candidate IDs by wrapping them. Desktop and
  390px mobile checks, including expanded source/assessment panels, had no page overflow
  or recorded browser errors. Temporary UI fixtures are excluded from deliverables.

### Final delivery checkpoint

- Sealed and verified **89 delivery artifacts**, including nine HTML reports, in
  `data/processed/evidence-tuning-v2/delivery-manifest.json`. This records the completed
  implementation/review snapshot; the pre-publication working log is preserved separately
  under `data/processed/evidence-tuning-v2/publication/working-notes.before`.
- Removed the temporary grouped-evidence and offline-rendering smoke HTML files,
  closed browser tabs and stopped the local report preview. Reproducible audit scripts,
  original inputs, provider results and verification records remain available locally.
- Source, regression tests, example configuration and documentation are repository
  artifacts. Credentials, raw filings, databases, generated reports, audit data, virtual
  environments and build outputs remain excluded from Git. A fresh clone therefore
  does not include the locally evaluated reports or filing cache.

## Local artifacts and operational notes

These `data/` artifacts are ignored by Git and are not included in a fresh clone:

- General business-impact NPK report: `data/reports/business-impact-npk.html`.
  Persisted run: `20260924T024532-36a42abd00`; all 62 eligible passages included.
- Five-company production report: `data/reports/business-impact-five-company.html`.
- Five-company judge review: `data/reports/five-company-impact-judge-review.html`.
- Five-company audit inputs/results: `data/processed/five-company-impact-review-v1/`.
- Tuned five-company report: `data/reports/evidence-tuned-five-company.html`.
- Controlled ablation review: `data/reports/evidence-tuning-ablation-review.html`.
- Fresh source-judge review: `data/reports/evidence-tuning-fresh-review.html`.
- Fresh production report: `data/reports/evidence-tuned-fresh-issuers.html`.
- Tuning inputs, execution accounting, frozen labels and verification:
  `data/processed/evidence-tuning-v2/`.
- Latest four-company report: `data/reports/20260921T033232-0ee464b545.html`.
- Four-company source review: `data/processed/four-company-disclosure-review.json`.
- Blinded evaluation: `data/processed/four-company-assistant-eval-v1/results.json`.
- Auditable labels: `data/processed/four-company-assistant-eval-v1/judgments.csv`.
- Human spot-checks (HTML): `data/reports/four-company-assistant-spotcheck.html`.
- Spot-check audit data (JSON): `data/processed/four-company-assistant-eval-v1/human-spotcheck.json`.
- Separate human calibration and source checks: `data/processed/four-company-human-calibration-v1.json`.
- New 20-case preparatory/commercial review: `data/reports/research-screen-v2.html`.
- Full experimental inputs, labels, rankings, source checks and results:
  `data/processed/research-screen-v2/`.
- Revised RELL report: `data/reports/20260920T050007-23c12bd0de.html`.
- Baseline report: `data/reports/20260920T041813-1c7e0a8e94.html`.
- Reviewed alignment audit: `data/processed/rell-alignment-review.json`.
- Persisted runs, embeddings, and semantic provenance: `data/radar.sqlite3`.
- Raw SEC documents: `data/raw/`; extracted paragraph JSON: `data/processed/`.

`--recompute` uses cached SEC filings but does not prohibit a first embedding-model
 download or uncached Jev requests. A fresh clone needs its own configuration and filing
cache. `radar report RUN_ID` uses persisted judgments without SEC/model/provider calls.

## Remaining limitations and unfinished acceptance work

- Live/research comparisons now cover RELL, NPK, PKE, BFLY, CPSH, ASTE, GRC, HURC,
  ASPI, CLMT, QURE, AOSL, AIP, AEHR and INOD. Wider coverage and human ranking-quality
  evaluation remain open.
- A complete versioned historical SEC HTML-pair fixture remains outstanding. The real
  tax-note excerpt regression does not cover full historical ingestion.
- No independent human-labeled ranking corpus exists yet. The provisional blinded
  assistant benchmark above favors the Jev-assisted heuristic on these four pairs;
  human confirmation, broader generalization, and a clean causal/cost-benefit test remain open.
- Reporting-scope extraction and alignment remain heuristic. The remaining review flags
  should be inspected, not removed merely to reduce their count.
- The live recomputation emitted BeautifulSoup's `XMLParsedAsHTMLWarning` while parsing
  the cached SEC document. The run completed; the warning was not suppressed.
- Economic ranking scores are not calibrated probabilities or investment advice.
- The general categorization/impact rubric now has source-only assistant review, not
  a human-adjudicated quality benchmark. Impact and comparison-reliability judgments
  remain uncalibrated; bounded retrieved context and structured facts cannot always
  establish company-relative magnitude. Fresh improvements were issuer-dependent.
