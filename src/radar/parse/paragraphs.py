"""Deterministic narrative paragraphs with traceable filing-local identifiers."""

from hashlib import sha256

from radar.models import Filing, FilingParagraph

from .html import clean_html, text_blocks
from .sections import SectionTracker, normalize_text


def _extract(html: str | bytes, accession: str, min_chars: int) -> list[FilingParagraph]:
    if min_chars < 1:
        raise ValueError("min_chars must be at least 1")
    sections = SectionTracker()
    result: list[FilingParagraph] = []
    for block in text_blocks(clean_html(html)):
        if sections.consume_heading(block):
            continue
        normalized = normalize_text(block.text)
        if len(normalized) < min_chars:
            continue
        ordinal = len(result)
        result.append(
            FilingParagraph(
                filing_accession=accession,
                paragraph_id=f"{accession}:{ordinal}",
                section=sections.section,
                item=sections.item,
                ordinal=ordinal,
                text=block.text,
                normalized_text=normalized,
                text_hash=sha256(normalized.encode("utf-8")).hexdigest(),
            )
        )
    return result


def extract_paragraphs(html: str, accession: str, min_chars: int = 40) -> list[FilingParagraph]:
    """Extract fixture/document HTML without touching or rewriting its source."""
    return _extract(html, accession, min_chars)


def parse_filing(filing: Filing, min_chars: int = 40) -> list[FilingParagraph]:
    """Parse cached bytes, letting HTML encoding metadata guide Unicode decoding."""
    return _extract(filing.local_path.read_bytes(), filing.accession, min_chars)
