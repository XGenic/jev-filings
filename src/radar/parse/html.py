"""Clean a working HTML tree and yield non-overlapping visible text blocks."""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from itertools import takewhile

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
    fact_nodes: tuple[Tag, ...] = field(default=(), repr=False, compare=False)
    table: Tag | None = field(default=None, repr=False, compare=False)


def _text(tag: Tag) -> str:
    return _SPACE.sub(" ", tag.get_text(" ", strip=True)).strip()


def _numeric_table(table: Tag) -> bool:
    # A table used for page layout must not swallow its narrative cells.
    cells = [cell for cell in table.find_all(["td", "th"]) if cell.find_parent("table") is table]
    data_cells = [cell for cell in cells if cell.name == "td"]
    cells = data_cells or cells
    values = [value for cell in cells if (value := _text(cell))]
    if not values:
        return False
    numeric = sum(
        (bool(re.search(r"\d", value)) and sum(character.isalpha() for character in value) <= 4)
        or any(
            value.strip("$€£¥()% \u00a0") == _text(fact) for fact in cell.find_all("ix:nonfraction")
        )
        for cell in cells
        if (value := _text(cell))
    )
    # Currency/parenthesis spacer cells must not dilute a financial schedule.
    substantive = [value for value in values if not re.fullmatch(r"[$€£¥()%—–−-]+", value)]
    tagged = sum(cell.find("ix:nonfraction") is not None for cell in cells)
    return bool(substantive) and (
        numeric / len(substantive) >= 0.5 or (tagged >= 2 and numeric / len(substantive) >= 0.3)
    )


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
    return _clean_visible(soup)


def _clean_visible(soup: BeautifulSoup) -> BeautifulSoup:
    """Retain visible tables/facts; callers separately select narrative or evidence."""
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
    _join_page_continuations(soup)
    return soup


def narrative_html(html: str | bytes) -> BeautifulSoup:
    """Exclude numeric schedules from the narrative alignment stream."""
    soup = clean_html(html)
    for table in list(soup.find_all("table")):
        if table.name is not None and (_toc_table(table) or _numeric_table(table)):
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


def _edge_paragraph(container: Tag, *, last: bool) -> Tag | None:
    """Find the literal page-content edge, never skip intervening visible content."""
    node = container
    while True:
        if node.name in {"p", "div"} and node.find(_BLOCK_TAGS) is None:
            return node
        if node.name not in {"div", "ix:nonnumeric", "ix:continuation"}:
            return None
        children = [child for child in node.children if isinstance(child, Tag) or child.strip()]
        if not children:
            return None
        edge = children[-1 if last else 0]
        if not isinstance(edge, Tag):
            return None
        node = edge


def _same_typography(previous: Tag, current: Tag) -> bool:
    def typography(tag: Tag) -> dict[str, str]:
        return {
            key.strip().casefold(): value.strip().casefold()
            for key, value in re.findall(r"([^:;]+):([^;]+)", str(tag.get("style", "")))
            if key.strip().casefold() in {"font-family", "font-size", "text-align"}
        }

    before, after = typography(previous), typography(current)
    return all(before[key] == after[key] for key in before.keys() & after.keys())


def _join_page_continuations(soup: BeautifulSoup) -> None:
    """Reunite unfinished prose across explicit numbered footer/page/header furniture."""
    for rule in soup.find_all("hr"):
        if not re.search(
            r"(?:^|;)\s*page-break-after\s*:\s*always\s*(?:;|$)",
            str(rule.get("style", "")),
            re.I,
        ):
            continue
        footer = rule.find_previous_sibling()
        header = rule.find_next_sibling()
        if footer is None or header is None:
            continue
        if footer.name != "div" or not re.fullmatch(r"\d+", _text(footer)):
            continue
        if header.name != "div":
            continue
        header_text = _text(header)
        links = header.find_all("a", href=True)
        toc_header = (
            header_text.casefold() == "table of contents"
            and len(links) == 1
            and str(links[0]["href"]).startswith("#")
        )
        blank_page_header = not header_text and bool(
            re.search(r"(?:^|;)\s*(?:padding-top|min-height)\s*:", str(header.get("style", "")))
        )
        if not (toc_header or blank_page_header):
            continue
        previous_container = footer.find_previous_sibling()
        current_container = header.find_next_sibling()
        if previous_container is None or current_container is None:
            continue
        previous = _edge_paragraph(previous_container, last=True)
        current = _edge_paragraph(current_container, last=False)
        if previous is None or current is None:
            continue
        if (
            current.name != previous.name
            or not _same_typography(previous, current)
            or previous.find_parent("table") is not None
            or current.find_parent("table") is not None
            or previous.find(_BLOCK_TAGS) is not None
            or current.find(_BLOCK_TAGS) is not None
            or _heading_style(previous)
            or _heading_style(current)
        ):
            continue
        # Do not skip meaningful sibling text, even in malformed HTML.
        run = (previous_container, footer, rule, header, current_container)
        if any(
            isinstance(node, NavigableString) and node.strip()
            for left, right in zip(run, run[1:])
            for node in takewhile(lambda sibling: sibling is not right, left.next_siblings)
        ):
            continue
        before, after = _text(previous), _text(current)
        if (
            not before
            or not after
            or not after[0].islower()
            or re.search(r"""[.!?;:]["'’”)\]]*$""", before)
        ):
            continue
        # Move original inline nodes in the working tree: Unicode, links and XBRL
        # content survive unchanged; raw cached bytes are never rewritten.
        previous.append(" ")
        for child in list(current.contents):
            previous.append(child.extract())
        current.decompose()


def text_blocks(soup: BeautifulSoup, *, include_tables: bool = False) -> Iterator[TextBlock]:
    """Traverse every text node once; never emit both a container and its child."""

    def walk(tag: Tag) -> Iterator[TextBlock]:
        buffer: list[str] = []
        linked = False
        fact_nodes: list[Tag] = []

        def flush() -> TextBlock | None:
            nonlocal linked
            value = _SPACE.sub(" ", "".join(buffer)).strip()
            block = (
                TextBlock(value, _heading_style(tag), linked, tuple(fact_nodes)) if value else None
            )
            buffer.clear()
            linked = False
            fact_nodes.clear()
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
            elif (
                node.name == "table"
                and include_tables
                and (_numeric_table(node) or _toc_table(node))
            ):
                block = flush()
                if block:
                    yield block
                if not _toc_table(node):
                    yield TextBlock("", table=node)
            elif node.name in _BLOCK_TAGS:
                block = flush()
                if block:
                    yield block
                yield from walk(node)
            else:
                if node.name == "ix:nonfraction":
                    fact_nodes.append(node)
                if node.name == "a" and str(node.get("href", "")).startswith("#") and _text(node):
                    linked = True
                for child in node.children:
                    yield from visit(child)

        for child in tag.children:
            yield from visit(child)
        block = flush()
        if block:
            yield block

    yield from walk(soup.body or soup)
