from hashlib import sha256

import numpy as np

from radar.config import Settings
from radar.match.align import align_paragraphs
from radar.match.lexical import lexical_similarity, word_diff
from radar.models import FilingParagraph


def paragraph(text, ordinal=0, accession="old", item="Item 7"):
    return FilingParagraph(
        filing_accession=accession,
        paragraph_id=f"{accession}:{ordinal}",
        item=item,
        ordinal=ordinal,
        text=text,
        normalized_text=text,
        text_hash=sha256(text.encode()).hexdigest(),
    )


class Vectors:
    name = "synthetic-test-vectors"

    def __init__(self, vectors):
        self.vectors = vectors

    def embed(self, paragraphs):
        return np.asarray([self.vectors[p.text] for p in paragraphs], dtype=float)


def test_tiny_financial_edit_is_not_cosmetic_even_above_threshold():
    context = "Our ongoing operations require substantial capital investment. " * 15
    old = paragraph(context + "Our cash runway is 12 months.")
    new = paragraph(context + "Our cash runway is 2 months.", accession="new")
    assert lexical_similarity(old.text, new.text) > 0.985
    result = align_paragraphs([old], [new], Settings(), None)
    assert result.pairs[0].relation == "matched"
    assert result.pairs[0].skip_reason is None


def test_semantic_rewrite_matches_and_unrelated_paragraphs_remain_unpaired():
    old = [
        paragraph("Revenue depends on a small number of large customers."),
        paragraph("The former plant remediation reserve is fully funded.", 1),
    ]
    new = [
        paragraph("A few major clients account for most of our sales.", accession="new"),
        paragraph("The bank refused to renew our revolving credit line.", 1, "new"),
    ]
    embedder = Vectors(
        {
            old[0].text: [1, 0, 0],
            new[0].text: [0.98, 0.1, 0],
            old[1].text: [0, 1, 0],
            new[1].text: [0, 0, 1],
        }
    )
    result = align_paragraphs(old, new, Settings(), embedder)
    assert [
        (p.relation, p.old.paragraph_id if p.old else None, p.new.paragraph_id if p.new else None)
        for p in result.pairs
    ] == [("matched", "old:0", "new:0"), ("added", None, "new:1"), ("deleted", "old:1", None)]
    assert result.pairs[0].cosine_similarity > 0.98


def test_duplicate_exact_blocks_consumed_once_and_provider_not_called():
    class NoCalls:
        name = "must-not-be-used"

        def embed(self, paragraphs):
            raise AssertionError("Exact duplicates should not require embeddings")

    old = [paragraph("Repeated disclosure about our business operations.", i) for i in range(2)]
    new = [paragraph(old[0].text, i, "new") for i in range(3)]
    result = align_paragraphs(old, new, Settings(), NoCalls())
    assert [p.relation for p in result.pairs] == ["matched", "matched", "added"]
    assert len({p.old.paragraph_id for p in result.pairs if p.old}) == 2
    assert not result.warnings


def test_split_and_merge_are_flagged_without_reusing_paragraphs():
    original = paragraph("Credit is limited and our customer contracts can be cancelled.")
    split = [
        paragraph("Our credit availability is constrained.", 0, "new"),
        paragraph("Customers have the right to cancel their contracts.", 1, "new"),
    ]
    vectors = {original.text: [1, 0], split[0].text: [1, 0.1], split[1].text: [1, -0.1]}
    result = align_paragraphs([original], split, Settings(), Vectors(vectors))
    assert sorted(p.relation for p in result.pairs) == ["added", "matched"]
    assert all(p.warnings for p in result.pairs)
    merged = align_paragraphs(split, [original], Settings(), Vectors(vectors))
    assert sorted(p.relation for p in merged.pairs) == ["deleted", "matched"]
    assert all(p.warnings for p in merged.pairs)


def test_embedding_failure_reports_lexical_fallback_and_no_fake_cosine():
    class Broken:
        name = "broken-provider"

        def embed(self, paragraphs):
            raise OSError("model cannot be loaded")

    old = paragraph("We can fund operations with existing cash reserves.")
    new = paragraph("We cannot fund operations with existing cash reserves.", accession="new")
    result = align_paragraphs([old], [new], Settings(), Broken())
    assert result.pairs[0].relation == "matched"
    assert result.pairs[0].cosine_similarity is None
    assert result.warnings
    assert "lexical-only" in result.embedding_model


def test_date_update_is_not_assumed_semantically_identical():
    old = paragraph("Our credit agreement expires on December 31, 2026.")
    new = paragraph("Our credit agreement expires on December 31, 2027.", accession="new")
    result = align_paragraphs([old], [new], Settings(), None)
    assert result.pairs[0].skip_reason is None
    assert result.pairs[0].relation == "matched"


def test_lexical_diff_escapes_filing_tags_and_preserves_deletion():
    diff = word_diff("<script>alert(1)</script> old", "<img src=x onerror=alert(1)> new")
    assert "<script>" not in diff
    assert "<img" not in diff
    assert "&lt;" in diff
    assert "<del>" in diff and "<ins>" in diff
    assert lexical_similarity("same disclosure", "same disclosure") == 1
    assert lexical_similarity("", "totally unrelated") == 0


def test_quarter_cannot_be_stolen_by_more_similar_year_to_date_candidate():
    old = [
        paragraph("PMT net sales during the second quarter increased due to customer demand.", 0),
        paragraph("PMT net sales during the first six months increased due to customer demand.", 1),
    ]
    new = [
        paragraph(
            "PMT net sales during the third quarter increased due to customer demand.", 0, "new"
        )
    ]
    for p in old + new:
        p.section = "Part I / Item 2. MD&A / Power and Microwave Technologies"
    result = align_paragraphs(
        old,
        new,
        Settings(),
        Vectors(
            {
                old[0].text: [0.9, 0.2],
                old[1].text: [1, 0],
                new[0].text: [1, 0],
            }
        ),
    )
    match = next(p for p in result.pairs if p.relation == "matched")
    assert match.old.paragraph_id == "old:0"
    assert match.alignment.old_context.reporting_scope == "quarter"
    assert all(
        not (a.old and a.old.paragraph_id == "old:1" and a.new)
        for a in match.alignment.alternatives
    )


def test_corresponding_subsection_precedes_broader_embedding_neighbor():
    old = [
        paragraph("A few customers represent a substantial portion of our revenue.", 0),
        paragraph("Several suppliers represent a substantial portion of our purchases.", 1),
    ]
    new = [paragraph("A few customers now represent most of our annual revenue.", 0, "new")]
    old[0].section = new[0].section = "Part I / Item 2. MD&A / Customers"
    old[1].section = "Part I / Item 2. MD&A / Suppliers"
    result = align_paragraphs(
        old,
        new,
        Settings(),
        Vectors(
            {
                old[0].text: [0.9, 0.2],
                old[1].text: [1, 0],
                new[0].text: [1, 0],
            }
        ),
    )
    match = next(p for p in result.pairs if p.relation == "matched")
    assert match.old.paragraph_id == "old:0"
    assert match.alignment.candidate_tier == "subsection"


def test_broader_fallback_is_visible_when_local_candidate_is_unrelated():
    old = [
        paragraph("Legal proceedings concerning patents continue in federal court.", 0),
        paragraph("A few customers represent a substantial portion of our revenue.", 1),
    ]
    new = [paragraph("A few customers now represent most of our revenue.", 0, "new")]
    old[0].section = new[0].section = "Part I / Item 2. MD&A / Business"
    old[1].section = "Part I / Item 2. MD&A / Customers"
    result = align_paragraphs(
        old,
        new,
        Settings(),
        Vectors(
            {
                old[0].text: [0, 1],
                old[1].text: [1, 0],
                new[0].text: [1, 0],
            }
        ),
    )
    match = next(p for p in result.pairs if p.relation == "matched")
    assert match.old.paragraph_id == "old:1"
    assert match.alignment.candidate_tier == "item"
    assert "cross_subsection" in match.alignment.review_reasons


def test_adjacent_split_requires_combined_text_evidence():
    first = "Available borrowing under our revolving credit agreement remains limited."
    second = "Our major customers have the right to cancel their contracts at any time."
    old = [paragraph(first + " " + second)]
    new = [paragraph(first, 0, "new"), paragraph(second, 1, "new")]
    result = align_paragraphs(
        old,
        new,
        Settings(),
        Vectors(
            {
                old[0].text: [1, 0],
                new[0].text: [1, 0.1],
                new[1].text: [1, -0.1],
            }
        ),
    )
    assert sorted(p.relation for p in result.pairs) == ["added", "matched"]
    assert all("possible_split_merge" in p.alignment.review_reasons for p in result.pairs)
    assert all(p.alignment.alternatives for p in result.pairs)


def test_recorded_rell_tax_note_keeps_year_to_date_counterpart():
    import json
    from pathlib import Path

    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "rell_income_tax_alignment.json").read_text()
    )
    old = [FilingParagraph.model_validate(p) for p in fixture["old"]]
    new = [FilingParagraph.model_validate(p) for p in fixture["new"]]
    # Even a stronger embedding neighbor cannot override a clear scope mismatch.
    provider = Vectors({old[0].text: [1, 0], new[0].text: [1, 0], new[1].text: [0.8, 0.6]})
    result = align_paragraphs(old, new, Settings(), provider)
    matched = next(p for p in result.pairs if p.relation == "matched")
    added = next(p for p in result.pairs if p.relation == "added")
    assert matched.new.paragraph_id == fixture["expected"]["matched_new_id"]
    assert added.new.paragraph_id == fixture["expected"]["added_new_id"]
    assert matched.alignment.old_context.reporting_scope == "year_to_date"
    assert matched.alignment.new_context.reporting_scope == "year_to_date"


def test_recorded_litigation_stays_matched_when_accrual_period_rolls_forward():
    import json
    from pathlib import Path

    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "bfly_litigation_alignment.json").read_text()
    )
    old = [FilingParagraph.model_validate(p) for p in fixture["old"]]
    new = [FilingParagraph.model_validate(p) for p in fixture["new"]]
    addition = paragraph(
        "We entered into an exclusive five-year license for semiconductor imaging technology.",
        ordinal=new[0].ordinal + 1,
        accession=new[0].filing_accession,
        item=new[0].item,
    )
    addition.section = new[0].section
    result = align_paragraphs(old, [*new, addition], Settings())
    matched = [p for p in result.pairs if p.relation == "matched"]
    assert len(matched) == 1
    assert matched[0].old.paragraph_id == old[0].paragraph_id
    assert matched[0].new.paragraph_id == new[0].paragraph_id
    assert matched[0].skip_reason is None
    assert [p.new.paragraph_id for p in result.pairs if p.relation == "added"] == [
        addition.paragraph_id
    ]
    assert not any(p.relation == "deleted" for p in result.pairs)
