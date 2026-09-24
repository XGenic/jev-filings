"""Visible narrative and source-linked numeric evidence from cached SEC filings."""

from .evidence import parse_evidence
from .paragraphs import extract_paragraphs, parse_filing

__all__ = ["extract_paragraphs", "parse_evidence", "parse_filing"]
