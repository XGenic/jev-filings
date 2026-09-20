"""Visible narrative extraction for cached SEC filings and HTML fixtures."""

from .paragraphs import extract_paragraphs, parse_filing

__all__ = ["extract_paragraphs", "parse_filing"]
