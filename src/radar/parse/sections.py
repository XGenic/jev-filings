"""Track SEC Part/Item boundaries independently of HTML layout."""

import re
import unicodedata
from dataclasses import dataclass

from .html import TextBlock

_PART = re.compile(r"^part\s+([IVX]+)\b\s*[.\-–—:]?\s*(.*)$", re.I)
_ITEM = re.compile(r"^items?\s+(\d{1,2}[A-C]?)\b\s*[.\-–—:]?\s*(.*)$", re.I)
_TOC = re.compile(r"^(?:table\s+of\s+contents|index|page)\s*\d*$", re.I)
_PAGE = re.compile(r"^(?:page\s+)?(?:\d+|[ivx]+)$", re.I)
_DOT_LEADER = re.compile(r"\.{3,}\s*\d+\s*$")
_REFERENCE = re.compile(
    r"^(?:of|in|is|was|contains|provides|discusses|includes|describes|above|below|has|for|to|under)\b",
    re.I,
)


def normalize_text(text: str) -> str:
    """Canonical comparison text without changing the preserved source text."""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate({ord(char): None for char in "\u00ad\u200b\ufeff"})
    return " ".join(text.split())


@dataclass
class SectionTracker:
    part: str | None = None
    item: str | None = None
    item_title: str | None = None
    subheading: str | None = None

    @property
    def section(self) -> str | None:
        components = [value for value in (self.part, self.item_title, self.subheading) if value]
        return " / ".join(components) or None

    def consume_heading(self, block: TextBlock) -> bool:
        """Consume headings/navigation; return False only for narrative candidates."""
        text = normalize_text(block.text)
        if _TOC.fullmatch(text) or _PAGE.fullmatch(text) or _DOT_LEADER.search(text):
            return True
        part = _PART.match(text) if len(text) <= 180 else None
        item = _ITEM.match(text) if len(text) <= 240 else None
        if part and _REFERENCE.match(part[2]):
            part = None
        if item and _REFERENCE.match(item[2]):
            item = None
        if (part or item) and block.linked:
            # TOC anchors do not establish section boundaries.
            return True
        if part:
            self.part = f"Part {part[1].upper()}"
            self.item = None
            self.item_title = None
            self.subheading = part[2].strip() or None
            # Some issuers put both structural headings in one text block.
            if part[2] and _ITEM.match(part[2]):
                self.consume_heading(TextBlock(part[2], heading=True))
            return True
        if item:
            label = f"Item {item[1].upper()}"
            self.item = f"{self.part} / {label}" if self.part else label
            self.item_title = f"{label}. {item[2]}" if item[2] else label
            self.subheading = None
            return True
        # SEC subheadings are commonly bold or uppercase paragraphs, not <h*>.
        # Sentence-like emphasized prose remains a semantic paragraph.
        letters = [char for char in text if char.isalpha()]
        uppercase = bool(letters) and all(char.isupper() for char in letters)
        if (block.heading or uppercase) and len(text) <= 160 and len(text.split()) <= 18:
            if not re.search(r"[.!?;]$", text) and not block.linked:
                self.subheading = text
                return True
        return False
