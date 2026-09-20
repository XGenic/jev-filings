# SEC Filing Delta Radar — Codex Handoff

**Status:** first implementation pass complete; live RELL pipeline verified; broader-corpus and human-labeled evaluation outstanding (see §33).
**Primary goal:** build a local tool that detects *meaningful disclosure changes* between comparable SEC 10-K/10-Q filings, uses TypeSafe Jev as a high-throughput semantic judgment layer, and produces a compact HTML report for human review.

---

## 1. Product thesis

Normal text diffs are poor at SEC filings. A paragraph can be extensively rewritten while saying essentially the same thing, while a single added sentence can introduce a material financing, liquidity, supply-chain, customer-concentration, regulatory, or outlook change.

The tool should therefore use three layers:

1. **Deterministic parsing + lexical comparison** — identify filings, sections, paragraphs, exact/near-exact edits, additions, deletions.
2. **Embeddings** — cheaply align semantically comparable paragraphs and estimate coarse meaning similarity.
3. **TypeSafe Jev** — evaluate many narrow semantic predicates over each plausible old/new paragraph pair in parallel.

A frontier generative LLM is *not* required for v1. Later, it can be used only on the highest-ranked deltas to explain or synthesize them.

The architecture should preserve all intermediate signals rather than collapsing everything into a single opaque “AI score.”

---

## 2. Definition of done for v1

Given 3–5 user-supplied U.S. public-company tickers, the application can:

- Resolve ticker → CIK.
- Find the latest 10-K or 10-Q and a sensible prior comparable filing.
- Download and cache the primary filing HTML from SEC EDGAR.
- Extract useful narrative sections and paragraph-like blocks.
- Align old and new paragraphs using exact matching + embeddings.
- Identify:
  - modified/aligned paragraphs,
  - genuinely new paragraphs,
  - disappeared paragraphs.
- Compute for each aligned pair:
  - lexical diff,
  - cosine similarity,
  - Jev semantic signals,
  - a configurable ranking score.
- Generate **one self-contained HTML report** containing roughly the top 10–20 deltas per ticker.
- Persist raw data and model outputs so reruns do not redownload filings or re-call Jev unnecessarily.

The report should make it easy for a human to answer: **“What changed, where, and why might I care?”** It should not make investment recommendations.

---

## 3. Non-goals for v1

Do **not** build these yet:

- Full-market real-time scanning.
- Trading signals or expected-return predictions.
- Autonomous order execution.
- News ingestion.
- Earnings-call ingestion.
- Sophisticated XBRL financial-statement analysis.
- A React/Next.js dashboard.
- User accounts/cloud sync.
- LLM-generated summaries of every filing.
- Perfect SEC document-layout reconstruction.
- Perfect paragraph alignment across every pathological filing.

Build a good research instrument first.

---

## 4. Recommended stack

Use Python 3.12+.

Suggested dependencies:

```text
httpx
beautifulsoup4
lxml
pydantic
numpy
pandas
scikit-learn
sentence-transformers
rapidfuzz
jinja2
typer
rich
python-dotenv
sqlalchemy
aiosqlite        # optional; normal sqlite3 is also fine
pytest
pytest-asyncio
```

TypeSafe:

```text
typesafe-sdk
```

Prefer `uv` for environment/dependency management if convenient.

For embeddings, start with a local SentenceTransformers model so the baseline has no per-call embedding cost. Hide embedding behind an interface so OpenAI/other providers can be swapped in later.

---

## 5. Environment variables

```bash
SEC_USER_AGENT="Your Name your-email@example.com"
TYPESAFE_API_KEY="..."
```

Optional later:

```bash
OPENAI_API_KEY="..."
```

Never commit secrets. Include `.env.example`.

---

## 6. SEC access constraints

Use SEC public filing data directly; authentication is not required for public filings.

Important operational rules:

- Always send a declared `User-Agent` identifying the application/contact.
- SEC currently limits automated access to **10 requests/second maximum** across a user/IP. For this project, implement a conservative application limiter of **<= 5 requests/second** plus retries/backoff.
- Cache aggressively.
- Do not crawl SEC pages blindly.
- Prefer SEC submissions metadata to discover filings, then fetch only the filing documents needed.

Useful public sources/endpoints:

- Company submissions metadata: `https://data.sec.gov/submissions/CIK##########.json`
- Company ticker mapping is available from SEC developer resources / ticker files.
- Filing archives live under `https://www.sec.gov/Archives/edgar/data/...`

The submissions API is updated very quickly after dissemination and is suitable for later expansion into a monitoring service.

---

## 7. Comparable-filing selection

Implement this in a deterministic function and log the choice.

### 10-K

Compare the latest 10-K against the immediately preceding 10-K for the same issuer.

### 10-Q

For v1, compare the latest 10-Q against the previous 10-Q filing for the issuer. Record both `periodOfReport` values prominently in the report.

Design the selector so a later strategy can instead choose the same fiscal quarter one year earlier. Do **not** silently switch strategies.

Exclude amendments (`10-K/A`, `10-Q/A`) unless explicitly requested.

Return a typed object roughly like:

```python
ComparableFilingPair(
    cik: str,
    ticker: str,
    form: Literal["10-K", "10-Q"],
    current_accession: str,
    current_period: date,
    current_filed: date,
    previous_accession: str,
    previous_period: date,
    previous_filed: date,
)
```

---

## 8. Suggested repository layout

```text
filing-delta-radar/
├── pyproject.toml
├── README.md
├── .env.example
├── src/
│   └── radar/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       ├── models.py
│       ├── db.py
│       ├── sec/
│       │   ├── client.py
│       │   ├── filings.py
│       │   └── cache.py
│       ├── parse/
│       │   ├── html.py
│       │   ├── sections.py
│       │   └── paragraphs.py
│       ├── match/
│       │   ├── lexical.py
│       │   ├── embeddings.py
│       │   └── align.py
│       ├── jev/
│       │   ├── client.py
│       │   ├── questions.py
│       │   ├── schemas.py
│       │   └── score.py
│       ├── report/
│       │   ├── render.py
│       │   └── templates/
│       │       └── report.html.j2
│       └── pipeline.py
├── tests/
│   ├── fixtures/
│   ├── test_filings.py
│   ├── test_parsing.py
│   ├── test_alignment.py
│   ├── test_scoring.py
│   └── test_report.py
└── data/
    ├── raw/
    ├── processed/
    ├── cache/
    └── reports/
```

Keep provider-specific logic behind small interfaces.

---

## 9. Core data model

Use explicit Pydantic/dataclass models rather than passing anonymous dicts everywhere.

### Filing

```python
class Filing(BaseModel):
    ticker: str
    cik: str
    company_name: str
    form: str
    accession: str
    filed_date: date
    period_of_report: date | None
    primary_document: str
    source_url: str
    local_path: Path
```

### Paragraph

```python
class FilingParagraph(BaseModel):
    filing_accession: str
    paragraph_id: str
    section: str | None
    item: str | None
    ordinal: int
    text: str
    normalized_text: str
    text_hash: str
```

### AlignedPair

```python
class AlignedPair(BaseModel):
    old: FilingParagraph | None
    new: FilingParagraph | None
    relation: Literal["matched", "added", "deleted"]
    cosine_similarity: float | None
    lexical_similarity: float | None
    lexical_diff_html: str | None
```

### JevSemanticSignals

Persist raw Jev outputs, not just the eventual combined score.

```python
class JevSemanticSignals(BaseModel):
    question_schema_version: str
    same_underlying_meaning: float | None
    introduces_new_substantive_information: float | None
    plausibly_economically_consequential: float | None
    mostly_boilerplate_or_rephrasing: float | None

    liquidity_or_financing_direction: str | None
    liquidity_or_financing_probabilities: dict[str, float] | None

    supply_or_capacity_direction: str | None
    supply_or_capacity_probabilities: dict[str, float] | None

    customer_concentration_direction: str | None
    customer_concentration_probabilities: dict[str, float] | None

    regulatory_or_government_direction: str | None
    regulatory_or_government_probabilities: dict[str, float] | None

    outlook_or_guidance_direction: str | None
    outlook_or_guidance_probabilities: dict[str, float] | None

    uncertainty_direction: str | None
    uncertainty_probabilities: dict[str, float] | None
```

Store complete raw responses as JSON as well so question design can evolve without losing provenance.

---

## 10. Parsing strategy

SEC filing HTML is messy. Do not optimize for visual fidelity; optimize for stable narrative blocks.

Pipeline:

1. Fetch the primary HTML filing document.
2. Parse with BeautifulSoup/lxml.
3. Remove:
   - scripts/styles,
   - hidden elements when identifiable,
   - obvious navigation/EDGAR chrome,
   - repeated page headers/footers where practical.
4. Preserve headings.
5. Extract item/section boundaries.
6. Convert narrative text into paragraph-like blocks.
7. Normalize whitespace and Unicode.
8. Keep original text alongside normalized text.

Minimum paragraph filter:

- Ignore empty strings.
- Ignore very short fragments unless they look like headings.
- Start with ~40 visible characters as a narrative paragraph threshold; make configurable.

Do not discard tables from the cached raw filing. For v1, table-heavy blocks can be omitted from semantic comparison if parsing quality is poor. Later, financial tables can be handled separately using XBRL.

---

## 11. Sections worth prioritizing

Do not hard-code the entire pipeline to these, but initially give them higher reporting priority:

### 10-K

- Item 1 — Business
- Item 1A — Risk Factors
- Item 3 — Legal Proceedings
- Item 7 — Management’s Discussion and Analysis
- Liquidity / Capital Resources subsections inside MD&A

### 10-Q

- Part I, Item 2 — Management’s Discussion and Analysis
- Part II, Item 1 — Legal Proceedings
- Part II, Item 1A — Risk Factors

The system should still retain other narrative paragraphs; section priority should be a ranking feature, not a destructive filter.

---

## 12. Paragraph alignment

This step should happen **before Jev**.

### Stage A — context and exact/near-exact elimination

Derive normalized subsections and reporting scopes without changing original SEC text,
section labels, paragraph hashes, or semantic request payloads. Recognize quarterly,
year-to-date (including six-to-nine-month comparisons), annual, point-in-time, mixed,
and unknown subjects. Incidental transaction dates and forward-looking durations are
not themselves reporting subjects. This is heuristic extraction, not financial validation.

Clear incompatible scopes cannot match. Unknown/mixed scopes remain eligible rather
than silently discarding disclosures. Scope-compatible exact hash matches skip Jev;
conservative cosmetic filtering also preserves changes to numbers, dates, and qualifiers.

### Stage B — compatible candidate tiers

Embed remaining paragraphs through the existing persistent cache. First consider all
compatible candidates in the corresponding normalized subsection and item. After assigning
that tier, search remaining same-item candidates (`alignment_top_k = 3`); filing-wide
fallback is permitted only when no old paragraph remains in that item.

The matching score combines embedding similarity (or explicit lexical-only fallback),
lexical similarity, relative position, and nearby established paragraph anchors.
The `cosine_floor` applies to the primary similarity and the combined matching score.
Broader-subsection matches remain visible as review cases.

### Stage C — joint assignment and review evidence

Use maximum-weight one-to-one assignment within each connected candidate component and
tier, with explicit unmatched options. A match contributes its score minus the configured
floor; leaving paragraphs unmatched contributes zero. Assigned higher-priority tiers
remain fixed while broader tiers are considered.

For each selected pair, solve again with that edge forbidden. The loss in total objective
is the assignment margin; unmatched paragraphs use a forced-match counterfactual.
Margins below `alignment_ambiguity_margin` (default `0.04`) require review. This margin is
conditional on the candidate tier/component, **not** a probability or global confidence.
Mixed or one-sided scope evidence and cross-subsection fallback have distinct review reasons.
Split/merge warnings require adjacent passages to improve combined-text correspondence,
not merely a close local embedding neighbor.

Persist contexts, weighted score components, margins, and actual alternative pairings.
The report exposes these separately from economic ranking and provides an alignment-status
filter. Historical runs lacking this metadata display it as unavailable; no confidence is
inferred from their cosine or ranking scores.

---

## 13. Why Jev is used here

Embeddings answer: **“Are these passages semantically similar?”**

Jev should answer narrower questions like:

- Is the underlying business meaning unchanged despite wording edits?
- Is genuinely new substantive information introduced?
- Does the new passage increase/decrease a specific risk category?
- Is this mostly boilerplate/rephrasing?

TypeSafe’s intended pattern is exactly this: keep deterministic control flow in code, ask many narrow independent questions over the same state, and combine probabilities in software. Questions in one request are evaluated independently and in parallel.

Do not ask Jev to “analyze the paragraph and tell us what matters.” That is too broad and defeats the architecture.

---

## 14. Jev request state

For a matched pair, keep state tight:

```python
state = {
    "company": {
        "ticker": ticker,
        "name": company_name,
        "form": form,
        "section": section,
    },
    "old": {
        "period": old_period.isoformat(),
        "text": old_text,
    },
    "new": {
        "period": new_period.isoformat(),
        "text": new_text,
    },
}
```

Do **not** send the full filing. Jev works best when each judgment receives only the relevant context.

For added/deleted paragraphs, use a separate question set with only the existing paragraph plus filing metadata.

---

## 15. Initial Jev question battery

Treat this as **question schema v1**. Put it in one module, version it, and make changes explicit.

### A. General semantic delta

Use `Noul` for clean yes/no probabilities.

```python
"same_underlying_meaning": Noul(
    instructions=(
        "Do `old.text` and `new.text` communicate essentially the same underlying "
        "business fact, risk, obligation, or outlook despite wording changes?"
    )
)
```

```python
"new_substantive_information": Noul(
    instructions=(
        "Does `new.text` introduce a substantive business fact, risk, obligation, "
        "constraint, trend, or outlook that is not present in `old.text`?"
    )
)
```

```python
"mostly_boilerplate_or_rephrasing": Noul(
    instructions=(
        "Is the difference between `old.text` and `new.text` mainly boilerplate, "
        "stylistic rewriting, formatting, or immaterial clarification?"
    )
)
```

```python
"plausibly_economically_consequential": Noul(
    instructions=(
        "Could the substantive difference between `old.text` and `new.text`, if accurate, "
        "plausibly affect the company's operations, cash needs, financing, demand, margins, "
        "legal/regulatory exposure, or business outlook? Judge the difference only, not "
        "whether the company is a good investment."
    )
)
```

### B. Domain-direction questions

Use `Choice` with the same four-option shape wherever possible:

```python
{
    "introduced_or_increased": "The new passage introduces this issue or makes it meaningfully more adverse/prominent.",
    "reduced_or_resolved": "The new passage reduces, resolves, or makes this issue meaningfully less adverse.",
    "unchanged_or_not_present": "No meaningful directional change, or this issue is not present in either passage.",
    "unclear": "The direction cannot be determined reliably from these passages."
}
```

Ask separately about:

1. `liquidity_or_financing_direction`
   - cash runway, liquidity pressure, covenant/refinancing need, capital raises, debt access, going-concern-like pressure.

2. `supply_or_capacity_direction`
   - shortages, suppliers, manufacturing capacity, production constraints, lead times.

3. `customer_concentration_direction`
   - dependence on major customers/contracts/channels.

4. `regulatory_or_government_direction`
   - regulation, approvals, government contracts/programs, sanctions/export controls, reimbursement/policy exposure.

5. `outlook_or_guidance_direction`
   - management expectations, demand outlook, growth, timing, forecast language.

6. `uncertainty_direction`
   - stronger/weaker conditional language, uncertainty, inability to estimate, qualification of prior claims.

All should be asked in the **same Jev request** because they share the same state and are independent. Ignore irrelevant answers in code.

### C. Added paragraph question set

For genuinely new paragraphs, ask:

- `substantive_disclosure` — is this substantive rather than boilerplate?
- `economic_relevance` — could it plausibly affect operations/capital/demand/margins/regulatory exposure/outlook?
- the same domain classification/direction questions, but criteria become `present` / `not_present` / `unclear` rather than old→new direction.

### D. Deleted paragraph question set

For deleted paragraphs, ask:

- Was the removed text substantive?
- Does removal appear consistent with a risk/constraint being resolved, versus simple restructuring/relocation?

Be conservative: deletion alone does not prove resolution.

---

## 16. TypeSafe implementation notes

Python SDK package: `typesafe-sdk`.

The SDK reads `TYPESAFE_API_KEY` from the environment.

Typical shape:

```python
from typesafe_sdk import Choice, Noul, TypeSafeClient

with TypeSafeClient() as client:
    response = client.system_one(
        state=state,
        questions=questions,
    )
```

Jev `Choice` and `Score` outputs include probabilities/confidence; `Noul` returns a probability from 0–1 that the answer is yes.

Important design constraints:

- Questions in one request see the same state.
- Questions are independent.
- Ask many independent questions together.
- If one question truly depends on another answer, use a second request — but avoid serial calls unless required.
- Keep numerical calculations, dates, ranking, thresholds, and business logic in Python.

Wrap Jev behind a local interface so the rest of the pipeline can run with a mocked provider.

---

## 17. Jev caching

Avoid repeated calls on identical inputs.

Cache key should include at least:

```text
model/provider
question_schema_version
old_paragraph_hash
new_paragraph_hash
form
section/item
```

Persist:

- request state,
- question definitions/schema version,
- raw API response,
- normalized output fields,
- request timestamp,
- latency if available.

This lets us later re-score reports without rerunning the API.

---

## 18. Ranking

Do **not** pretend there is a scientifically validated relevance formula yet.

Create a transparent configurable heuristic for v1. Example starting point for matched paragraphs:

```python
semantic_drift = 1.0 - jev.same_underlying_meaning
embedding_drift = 1.0 - cosine_similarity

rank_score = (
    0.25 * embedding_drift
    + 0.25 * semantic_drift
    + 0.20 * jev.new_substantive_information
    + 0.20 * jev.plausibly_economically_consequential
    + 0.10 * (1.0 - jev.mostly_boilerplate_or_rephrasing)
)
```

Then apply small optional boosts for domain-direction changes and priority sections.

Requirements:

- Keep every component visible in output.
- Put weights in config, not scattered through code.
- Make it easy to rerank historical results without new Jev calls.
- Never label the combined value as a probability.

Later, replace heuristic weights with a learned ranking model using manually labeled examples.

---

## 19. Lexical diff

The report should show exactly what changed.

Generate word-level or token-level HTML diffs:

- additions visually highlighted,
- deletions visually highlighted,
- unchanged text readable.

Use Python `difflib` or another small dependency. Sanitize all filing text before inserting into HTML.

Do not rely on the lexical diff to rank meaning changes; it is an interpretability aid.

---

## 20. HTML report requirements

One self-contained report per run.

### Header

- Report generation timestamp.
- Tickers analyzed.
- Jev question-schema version.
- Embedding model.
- Filing comparison strategy.

### Company summary card

For each ticker:

- company name,
- ticker + CIK,
- form,
- current filing date + period of report + accession,
- prior filing date + period of report + accession,
- counts:
  - total paragraphs,
  - unchanged skipped,
  - matched changes,
  - additions,
  - deletions,
  - Jev-evaluated pairs.

### Top deltas

For each ranked result show:

- rank,
- section/item,
- relation: matched / added / deleted,
- heuristic rank score,
- cosine similarity,
- lexical similarity,
- old text,
- new text,
- word-level diff,
- Jev raw signals / probability distributions,
- badges for directional domain changes,
- SEC source links/accessions.

Include controls or simple JS for:

- sort by rank/cosine/materiality,
- filter by section,
- filter by relation,
- filter by Jev domain flag.

Keep styling clean and compact; this is an analytical instrument, not a marketing page.

---

## 21. CLI

Use Typer.

Target commands:

```bash
# Analyze latest filing pair for one ticker
radar analyze RELL --form 10-Q

# Analyze several
radar analyze RELL OUST KTOS --form 10-Q

# Force use of cached filings but recompute local alignment/ranking
radar analyze RELL --form 10-Q --recompute

# Re-render report from persisted results
radar report <run-id>
```

Useful later:

```bash
radar fetch RELL --form 10-Q
radar evaluate --dataset data/gold/pairs.jsonl
radar scan --since 2026-09-19
```

Do not implement `scan` in v1 unless everything else is solid.

---

## 22. Persistence

Use SQLite for v1.

Tables/entities roughly:

- companies
- filings
- paragraphs
- comparison_runs
- paragraph_pairs
- embeddings (or file cache if simpler)
- jev_evaluations
- reports

Raw SEC HTML should live on disk/object-cache rather than inside SQLite blobs.

Ensure all analysis artifacts can be traced back to accession numbers and paragraph hashes.

---

## 23. Evaluation strategy

This matters. The purpose is to determine whether Jev adds value beyond embeddings/diffs.

Create a small gold/evaluation set while using the tool.

For at least ~50–100 paragraph pairs across several companies, manually label:

```text
same_meaning: yes/no/ambiguous
substantive_change: yes/no
worth_human_review: yes/no
primary_domain: liquidity | supply | customer | regulation | outlook | other | none
```

Compare these systems:

### Baseline A
Lexical change only.

### Baseline B
Embedding drift only.

### System C
Embedding + Jev.

Measure:

- Precision@10 for “worth human review.”
- Precision@20.
- Recall of manually identified important changes where measurable.
- False-positive categories.
- Jev confidence/probability calibration against labels.
- Latency.
- Jev token/cost usage.

The key question is not “does Jev sound smart?” It is:

> Does Jev move genuinely important semantic changes upward in the ranking and suppress meaningless rewrites relative to embeddings alone?

---

## 24. Tests

### Unit tests

- ticker/CIK normalization,
- accession-number formatting,
- comparable-filing selection,
- paragraph normalization/hash stability,
- lexical similarity,
- alignment on synthetic examples,
- rank-score calculations,
- cache keys,
- HTML escaping.

### SEC integration test

Use one stable historical filing pair as a fixture or recorded test. Do not hit SEC on every test run.

### Jev tests

Mock the TypeSafe client for ordinary test runs.

Optionally mark live integration tests:

```bash
pytest -m live_jev
```

Live tests should be opt-in and require `TYPESAFE_API_KEY`.

### Regression fixtures

Keep a few known paragraph cases:

1. Large rewrite, same meaning.
2. Tiny edit, important new sentence.
3. Clearly new liquidity risk.
4. Risk language removed.
5. Paragraph split into two.
6. Two paragraphs merged into one.
7. Pure boilerplate date/year update.

---

## 25. Error handling

The pipeline should degrade gracefully.

Examples:

- SEC request fails → retry/backoff, keep cached data.
- Filing has malformed HTML → log, preserve raw document, continue where possible.
- No prior comparable filing → report clearly; do not guess.
- Jev unavailable → still generate baseline embedding/lexical report with semantic fields marked unavailable.
- Embedding provider unavailable → lexical-only fallback is acceptable for debugging, but report the limitation.
- Ambiguous paragraph alignment → flag rather than invent confidence.

Use structured logging.

---

## 26. Security / untrusted input

SEC filing text is untrusted external text.

- Treat it purely as data.
- Never execute scripts from filings.
- Strip/sanitize HTML before report rendering.
- Do not let filing text alter system prompts/question definitions.
- Keep question definitions static and separate from document content.
- Escape all output inserted into report HTML.

This becomes more important later if generative LLMs are added.

---

## 27. Milestones

### Milestone 0 — skeleton

- `uv init` / project setup.
- Config + `.env.example`.
- Typer CLI.
- SQLite schema.
- Basic tests.

**Acceptance:** `radar --help` works and tests run.

### Milestone 1 — SEC ingestion

- ticker→CIK.
- submissions fetch/cache.
- comparable filing pair selection.
- primary filing HTML download/cache.

**Acceptance:** for a sample ticker, CLI prints two correct accession numbers and local cached paths.

### Milestone 2 — parse + section + paragraph extraction

- HTML cleaning.
- section/item detection.
- stable paragraph IDs/hashes.

**Acceptance:** output JSON shows sensible narrative blocks with section labels for both filings.

### Milestone 3 — baseline delta engine

- exact-match elimination.
- embeddings.
- paragraph candidate matching.
- additions/deletions.
- lexical diff.

**Acceptance:** terminal/debug output clearly surfaces meaningful changed passages without Jev.

### Milestone 4 — Jev semantic layer

- TypeSafe wrapper.
- question schema v1.
- batched independent questions per pair.
- caching.
- raw signals persisted.

**Acceptance:** one comparison run produces Jev outputs for candidate pairs and rerunning does not repeat cached calls.

### Milestone 5 — report

- self-contained HTML.
- top ranked deltas.
- raw metrics and semantic signals.
- filters/sorting.

**Acceptance:** open one file and inspect all 3–5 companies coherently.

### Milestone 6 — evaluate whether Jev is worth keeping

- label small gold set.
- compare lexical vs embeddings vs embeddings+Jev.

**Acceptance:** quantitative answer to whether Jev improves precision of top-ranked changes.

---

## 28. What Codex should do first

Proceed without asking for product-design clarification unless a missing credential blocks execution.

Recommended first implementation pass:

1. Create repository skeleton and `pyproject.toml`.
2. Implement SEC client with declared User-Agent, cache, <=5 req/s limiter, retry/backoff.
3. Implement ticker→CIK and filing-pair discovery.
4. Fetch one known ticker’s latest/prior 10-Q and cache both.
5. Parse both into sectioned paragraphs and dump diagnostic JSON.
6. Add tests around the filing selector and parser.
7. Only after that, implement embeddings/alignment.
8. Add Jev after paragraph alignment works independently.
9. Build HTML report last.

Keep commits logically separated by milestone where possible.

---

## 29. Implementation philosophy

The most important architectural rule:

> **Code owns the workflow. Embeddings propose relationships. Jev makes narrow semantic judgments. Nothing gets to invent the control flow.**

Do not turn Jev into a pseudo-agent.

Do not send giant filings to Jev.

Do not ask one broad question when five atomic questions expose more useful independent signals.

Do not throw away raw probabilities in favor of a single label.

Do not optimize for full-market scale before proving that the semantic layer increases ranking quality on a tiny corpus.

---

## 30. Post-v1 path if the experiment works

Only after v1 shows real signal:

### Scale-out ingestion

```text
SEC latest-filings/RSS/submissions updates
    ↓
filing queue
    ↓
fetch + parse + section
    ↓
prior-comparable lookup
    ↓
paragraph alignment
    ↓
cheap deterministic filters
    ↓
Jev semantic fan-out
    ↓
ranker
    ↓
top anomalies only
    ↓
optional frontier LLM synthesis
    ↓
alert / dashboard / research queue
```

### Learned ranking

Replace hand-set weights with CatBoost/LightGBM trained on human labels such as:

- opened/read,
- marked interesting,
- followed up,
- later associated with a meaningful catalyst,
- dismissed as boilerplate.

Use raw Jev probabilities + embedding/lexical/section features as model inputs.

### Broaden documents

Eventually add:

- 8-K,
- earnings releases,
- earnings-call transcripts,
- S-1/S-3 registration statements,
- prospectus supplements,
- 13D/13G,
- Form 4 / insider activity as structured signals,
- government-contract / regulatory feeds.

Keep each source adapter independent.

---

## 31. Source notes / current external constraints

These were checked when this brief was written (September 2026):

- SEC EDGAR public data APIs provide submissions history and XBRL data without API keys:  
  https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- SEC developer guidance currently caps automated access at 10 requests/second and requires a declared User-Agent for scripted access:  
  https://www.sec.gov/about/developer-resources  
  https://www.sec.gov/about/webmaster-frequently-asked-questions
- TypeSafe Jev docs describe `Choice`, `Score`, and `Noul` primitives; multiple questions over the same state are evaluated independently/in parallel:  
  https://docs.typesafe.ai/primitives
- TypeSafe recommends deterministic control flow in code, narrow atomic questions, small relevant state, and composing probabilities in code/classical ML:  
  https://docs.typesafe.ai/concepts/how-to-build-with-system-one
- TypeSafe Python SDK package is `typesafe-sdk`, using `TYPESAFE_API_KEY`:  
  https://docs.typesafe.ai/sdk/python
- TypeSafe speculative fan-out guidance:  
  https://docs.typesafe.ai/patterns/fan-out

Because Jev is an early-access product, Codex should verify SDK names/method signatures against the installed/current SDK before hard-coding assumptions beyond the wrapper layer.

---

## 32. Short version

Build a local SEC semantic-diff pipeline that first works **without** Jev. Then insert Jev only after paragraph pairing, ask many narrow questions about each old/new pair in one call, persist every probability, and test whether those signals improve the top-10/top-20 changes a human would actually want to inspect. If that works on 3–5 companies, the same architecture can later scale into a broad SEC filing anomaly radar.

---

## 33. Implementation notes — 2026-09-20

### Available now

- Installable Python package with `radar fetch`, `radar analyze`, and `radar report`.
- Declared-contact SEC client, process-shared rate limiter (4 requests/second by default, at most 5), bounded retries, atomic disk cache, stale-cache fallback, and cache-only mode.
- Deterministic latest/prior same-form selection, including older submissions pages where necessary; amendments are excluded.
- Sectioned narrative extraction, preserved source text, normalized hashes, and diagnostic JSON.
- Scope-compatible exact/cosmetic filtering, cached local embeddings, subsection-first joint assignment with unmatched options, and inspectable post-assignment review evidence.
- Versioned TypeSafe questions, separate matched/added/deleted semantics, bounded concurrent pair evaluation, full response provenance, and SQLite caching.
- Configurable weighted ranking and standalone HTML with escaped excerpts, word diffs, signal distributions, sorting, and filters.

### Running locally

```bash
uv sync --all-extras
```

Create `.env` using `.env.example`, supplying a real identifying name/contact in `SEC_USER_AGENT`.
Set `TYPESAFE_API_KEY` to enable uncached semantic judgments; without it, baseline reports still work
and explicitly mark semantic signals unavailable. Never commit `.env`.

```bash
# Fetch, cache, and inspect two filing accessions and paragraph JSON paths
uv run --all-extras radar fetch RELL --form 10-Q

# One report for all tickers
uv run --all-extras radar analyze RELL OUST KTOS --form 10-Q

# Reparse/re-align SEC cache only; existing Jev evaluations are reused
uv run --all-extras radar analyze RELL --form 10-Q --recompute

# Rerank and render persisted signals, without any SEC/model/provider calls
uv run --all-extras radar report RUN_ID --config radar.example.toml

# Explicit debugging baseline
uv run radar analyze RELL --form 10-Q --recompute --no-jev --lexical-only

uv run --all-extras pytest -q
```

`radar.example.toml` contains supported operational thresholds and ranking weights.
The default data root is `data/`; override it with `RADAR_DATA_DIR` or `data_dir` in TOML.
Raw HTML lives in `data/raw/`, diagnostic paragraph JSON in `data/processed/`, all comparison
signals/provenance in `data/radar.sqlite3`, and reports in `data/reports/`.
When `--form` is omitted, the latest non-amended 10-K or 10-Q determines the comparison form.

The `embeddings` and `jev` extras are optional. `--all-extras` selects both; the Linux/Windows
embedding installation uses CPU PyTorch wheels. First model use downloads MiniLM weights.
`--recompute` prohibits **SEC** network requests, not a first embedding-model download or an
uncached Jev request. Use `--no-jev` and a cached model (or `--lexical-only`) when fully offline.
The rate limiter is shared within one process: do not run concurrent SEC-ingesting CLI processes
and assume they share a global IP limit.

### Verification and remaining acceptance work

- 149 offline tests pass, covering filing selection, parsing, alignment, scoring, caches,
  mocked SDK transport, partial failures, and HTML escaping. Ruff checks pass; wheel build
  includes the report template.
- A three-company **synthetic** cache was exercised through the actual CLI, producing
  paragraph JSON, persisted runs, and reports. A real local MiniLM run and persistent embedding
  reuse succeeded. `radar report` rerendered without credentials.
- A mocked semantic-provider run made 18 requests initially and zero additional requests on
  an identical full rerun. These are fixture responses, not live Jev judgments.
- Browser checks exercised section/relation/domain filters, empty states, cosine/economic
  sorting, and independent company controls. Desktop and mobile layouts were inspected.
- `data/reports/synthetic-smoke.html` is a local, ignored demonstration report using synthetic
  filings and real local embeddings, with Jev marked unavailable. It is not SEC research output.
- Live SEC access now succeeds with the configured identifying `SEC_USER_AGENT`. RELL's
  10-Q pair is cached: current `0001193125-26-149207` (filed 2026-04-09, period 2026-02-28)
  versus prior `0001193125-26-007399` (filed 2026-01-08, period 2025-11-29).
  Extraction produced 208 current and 197 prior paragraphs.
- Live Jev (`jev-1.13.0`) produced 103 validated evaluations: 86 matched changes,
  14 additions, and 3 deletions; 108 exact pairs skipped semantic calls.
  Two initial responses failed probability-sum validation; targeted retries returned valid
  responses. Validation was not weakened, and rejected responses were not used in ranking.
- A complete replay with semantic network calls forbidden reused all 103 evaluations,
  made zero provider calls, preserved cached payloads, and produced no semantic errors.
  The successful report is `data/reports/20260920T041813-1c7e0a8e94.html`.
- Live browser inspection confirmed 20 displayed deltas and working domain filtering.
  It also exposed alphabetically reversed section transitions; report headings now retain
  previous-to-current order, protected by a regression that failed before the fix.
- The revised alignment run is `20260920T050007-23c12bd0de`; its report is
  `data/reports/20260920T050007-23c12bd0de.html`. Compared with the baseline above,
  flagged pairs fell from **42 to 14**, and flagged top-20 results from **18 to 2**.
  Counts remain 108 exact skips, 86 matched changes, 14 additions, and 3 deletions.
  The remaining flags are seven cross-subsection fallbacks and seven mixed/one-sided
  scope cases; these are retained rather than hidden to improve the count.
- The revised run reused 101 semantic evaluations and made exactly two successful new
  Jev requests. A subsequent replay of all 103 non-skipped evaluations with provider calls
  forbidden made zero calls and left all 105 stored cache records byte-for-byte unchanged.
  Alignment-only metadata does not invalidate semantic inputs or question versions.
- Assistant review checked 34 structural counterpart expectations, including a new
  quarterly tax note remaining unmatched while the prior six-month note matches the
  nine-month note. The revised result satisfies 34/34 versus 32/34 for the baseline.
  These are selected **assistant-reviewed cases, not independent human gold labels or
  a general accuracy estimate**. Detailed evidence is retained locally in
  `data/processed/rell-alignment-review.json`; actual SEC tax-note excerpts with source URLs
  are versioned in `tests/fixtures/rell_income_tax_alignment.json`.
- A brute-force comparison across 160 deterministic sparse candidate graphs agreed with
  the joint solver's optimal objective and matched/unmatched counterfactual margins.
  Browser checks exercised alignment filters, evidence/alternative expansion, historical
  unavailable states, independent company controls, and desktop/mobile layouts without
  horizontal overflow.
- A real three-to-five-company run and a complete versioned historical SEC HTML-pair fixture
  remain outstanding; live verification covers RELL only. The excerpt regression above is
  not a substitute for full historical ingestion coverage.
- No human gold labels or ranking-quality results were invented. Milestone 6 still requires
  a genuinely human-labeled corpus and comparison of lexical, embedding, and Jev rankings.
  The heuristic score is not calibrated, and this implementation does not claim Jev improves it.
