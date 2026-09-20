"""Clean a working HTML tree and yield non-overlapping visible text blocks."""

import re
from collections.abc import Iterator
from dataclasses import dataclass

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from soupsieve.util import SelectorSyntaxError

_SPACE = re.compile(r"[ \t\r\n\f\v]+")
_HIDDEN_STYLE = re.compile(
    r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\s*(?:!important\s*)?(?:;|$)",
    re.I,
)
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "caption",
        "center",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)


@dataclass(frozen=True)
class TextBlock:
    text: str
    heading: bool = False
    linked: bool = False


def _text(tag: Tag) -> str:
    return _SPACE.sub(" ", tag.get_text(" ", strip=True)).strip()


def _numeric_table(table: Tag) -> bool:
    # A table used for page layout must not swallow its narrative cells.
    cells = [cell for cell in table.find_all(["td", "th"]) if cell.find_parent("table") is table]
    values = [value for cell in cells if (value := _text(cell))]
    if len(values) < 4:
        return False
    numeric = sum(
        bool(re.search(r"\d", value)) and sum(character.isalpha() for character in value) <= 4
        for value in values
    )
    return numeric / len(values) >= 0.5


def _toc_table(table: Tag) -> bool:
    links = [a for a in table.find_all("a", href=True) if str(a["href"]).startswith("#")]
    if len(links) < 2:
        return False
    text = _text(table)
    linked_chars = sum(len(_text(link)) for link in links)
    return linked_chars >= len(text) * 0.45


def clean_html(html: str | bytes) -> BeautifulSoup:
    """Remove identifiable non-visible content from an in-memory copy only."""
    soup = BeautifulSoup(html, "lxml")
    # Honor simple stylesheet visibility rules as well as inline attributes.
    hidden_selectors: list[str] = []
    for style in soup.find_all("style"):
        for selectors, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", style.get_text()):
            if _HIDDEN_STYLE.search(declarations):
                hidden_selectors.extend(selector.strip() for selector in selectors.split(","))
    for selector in hidden_selectors:
        try:
            matches = soup.select(selector)
        except SelectorSyntaxError:
            # SEC styles may contain browser-specific syntax soupsieve cannot parse.
            continue
        for tag in matches:
            if tag.name is not None and tag.attrs is not None:
                tag.decompose()
    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()
    for tag in list(soup.find_all(True)):
        if tag.name is None or tag.attrs is None:
            continue
        name = tag.name.lower()
        local_name = name.split(":")[-1]
        metadata = (
            name.startswith("ix:") and local_name in {"hidden", "header", "references", "resources"}
        ) or name.startswith(("xbrli:", "link:"))
        attributes = tag.attrs
        role = str(attributes.get("role", "")).lower()
        identity = " ".join([str(attributes.get("id", "")), *attributes.get("class", [])])
        hidden = (
            "hidden" in attributes
            or str(attributes.get("aria-hidden", "")).lower() == "true"
            or bool(_HIDDEN_STYLE.search(str(attributes.get("style", ""))))
        )
        chrome = (
            name in {"head", "script", "style", "noscript", "template", "nav"}
            or role in {"navigation", "banner", "contentinfo"}
            or bool(
                re.search(
                    r"(?:^|[\s_-])(?:toc|tableofcontents|edgar-header)(?:$|[\s_-])", identity, re.I
                )
            )
        )
        if metadata or hidden or chrome:
            tag.decompose()
    for table in list(soup.find_all("table")):
        if (
            table.name is not None
            and table.attrs is not None
            and (_toc_table(table) or _numeric_table(table))
        ):
            table.decompose()
    return soup


def _heading_style(tag: Tag) -> bool:
    if tag.name in {"h1", "h2", "h3", "h4", "h5", "h6", "th"}:
        return True
    if tag.name in {"b", "strong"}:
        return True
    style = str(tag.get("style", ""))
    if re.search(r"font-weight\s*:\s*(?:bold|[6-9]00)\b", style, re.I):
        return True
    children = [child for child in tag.children if isinstance(child, Tag) or str(child).strip()]
    return bool(children) and all(
        isinstance(child, Tag) and _heading_style(child) for child in children
    )


def text_blocks(soup: BeautifulSoup) -> Iterator[TextBlock]:
    """Traverse every text node once; never emit both a container and its child."""

    def walk(tag: Tag) -> Iterator[TextBlock]:
        buffer: list[str] = []
        linked = False

        def flush() -> TextBlock | None:
            nonlocal linked
            value = _SPACE.sub(" ", "".join(buffer)).strip()
            block = TextBlock(value, _heading_style(tag), linked) if value else None
            buffer.clear()
            linked = False
            return block

        def visit(node: Tag | NavigableString) -> Iterator[TextBlock]:
            nonlocal linked
            if isinstance(node, NavigableString):
                buffer.append(str(node))
            elif node.name == "br":
                previous = node.previous_sibling
                while isinstance(previous, NavigableString) and not previous.strip():
                    previous = previous.previous_sibling
                if isinstance(previous, Tag) and previous.name == "br":
                    block = flush()
                    if block:
                        yield block
                else:
                    buffer.append(" ")
            elif node.name in _BLOCK_TAGS:
                block = flush()
                if block:
                    yield block
                yield from walk(node)
            else:
                if node.name == "a" and str(node.get("href", "")).startswith("#"):
                    linked = True
                for child in node.children:
                    yield from visit(child)

        for child in tag.children:
            yield from visit(child)
        block = flush()
        if block:
            yield block

    yield from walk(soup.body or soup)
