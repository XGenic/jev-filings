"""Visible source evidence; numeric interpretation is limited to explicit XBRL metadata."""

import re
from collections import defaultdict, deque
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from itertools import chain

from bs4 import BeautifulSoup, Tag

from radar.models import Filing, FilingEvidence, FilingParagraph, SourceFact, SourceTable

from .html import _clean_visible, _text, text_blocks
from .sections import SectionTracker, normalize_text

_TRANSFORM_REGISTRIES = frozenset(
    f"http://www.xbrl.org/inlineXBRL/transformation/{version}"
    for version in ("2010-04-20", "2011-07-31", "2015-02-26", "2020-02-12", "2022-02-16")
)
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")
_GROUPED_DECIMAL = re.compile(r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?")
_SPACE_GROUPED_DECIMAL = re.compile(r"[0-9]{1,3}(?: [0-9]{3})+(?:\.[0-9]+)?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _namespace(tag: Tag, prefix: str) -> str | None:
    for node in chain((tag,), tag.parents):
        if isinstance(node, Tag) and (namespace := node.get(f"xmlns:{prefix}")):
            return str(namespace)
    return None


def _one(tag: Tag, name: str) -> Tag:
    matches = tag.find_all(name)
    if len(matches) != 1:
        raise ValueError(f"expected one {name}, found {len(matches)}")
    return matches[0]


def _date(tag: Tag) -> date:
    text = tag.get_text(strip=True)
    if not _DATE.fullmatch(text):
        raise ValueError(f"unsupported period date {text!r}")
    return date.fromisoformat(text)


def _context(tag: Tag) -> dict:
    entity = _one(tag, "xbrli:identifier").get_text(strip=True)
    if not entity:
        raise ValueError("empty entity identifier")
    period = _one(tag, "xbrli:period")
    instant = period.find_all("xbrli:instant")
    if instant:
        if len(instant) != 1 or period.find(["xbrli:startdate", "xbrli:enddate", "xbrli:forever"]):
            raise ValueError("ambiguous instant period")
        start, end = None, _date(instant[0])
    else:
        start, end = _date(_one(period, "xbrli:startdate")), _date(_one(period, "xbrli:enddate"))
        if period.find("xbrli:forever") or start > end:
            raise ValueError("unsupported duration period")
    dimensions: dict[str, str] = {}
    for scope in tag.find_all(["xbrli:segment", "xbrli:scenario"]):
        for child in scope.find_all(recursive=False):
            if child.name != "xbrldi:explicitmember":
                raise ValueError("unsupported non-explicit context dimension")
            axis, member = str(child.get("dimension", "")), child.get_text(strip=True)
            if not axis or not member or axis in dimensions:
                raise ValueError("missing or duplicate explicit dimension")
            dimensions[axis] = member
    return {"entity": entity, "period_start": start, "period_end": end, "dimensions": dimensions}


def _measure(tag: Tag) -> str:
    value = tag.get_text(strip=True)
    prefix, separator, local = value.partition(":")
    namespace = _namespace(tag, prefix)
    if not separator or not local:
        raise ValueError("unit measure is not a qualified name")
    if namespace == "http://www.xbrl.org/2003/iso4217":
        return local
    if namespace == "http://www.xbrl.org/2003/instance" and local in {"shares", "pure"}:
        return local
    # Preserve an explicitly qualified custom measure, without inventing conversion.
    if namespace:
        return f"{{{namespace}}}{local}"
    raise ValueError(f"unresolved unit namespace {prefix!r}")


def _unit(tag: Tag) -> str:
    divides = tag.find_all("xbrli:divide")
    if divides:
        divide = _one(tag, "xbrli:divide")
        if tag.find("xbrli:measure", recursive=False):
            raise ValueError("ambiguous divided unit")
        numerator = _one(divide, "xbrli:unitnumerator").find_all("xbrli:measure")
        denominator = _one(divide, "xbrli:unitdenominator").find_all("xbrli:measure")
        if not numerator or not denominator:
            raise ValueError("empty unit numerator or denominator")
        numerator_text = "*".join(sorted(_measure(node) for node in numerator))
        denominator_text = "*".join(sorted(_measure(node) for node in denominator))
        if len(denominator) > 1:
            denominator_text = f"({denominator_text})"
        return f"{numerator_text}/{denominator_text}"
    measures = tag.find_all("xbrli:measure")
    if not measures:
        raise ValueError("unit has no measures")
    return "*".join(sorted(_measure(node) for node in measures))


def _metadata(soup: BeautifulSoup, name: str, parse) -> dict:
    result = {}
    for tag in soup.find_all(name):
        key = str(tag.get("id", ""))
        if not key:
            continue
        if key in result:
            result[key] = ValueError(f"duplicate {name} identifier {key!r}")
            continue
        try:
            result[key] = parse(tag)
        except (ValueError, InvalidOperation) as error:
            result[key] = error
    return result


def _numeric_value(tag: Tag) -> tuple[Decimal, int, str | None, str | None]:
    if str(tag.get("xsi:nil", "false")).casefold() in {"true", "1"}:
        raise ValueError("nil fact has no numeric value")
    if (
        tag.get("continuedat")
        or tag.find(["ix:nonfraction", "ix:exclude"])
        or tag.find_parent("ix:nonfraction")
    ):
        raise ValueError("unsupported continued, nested or excluded numeric content")
    text = tag.get_text().strip().replace("\u00a0", " ")
    format_name = str(tag["format"]) if tag.has_attr("format") else None
    if format_name:
        prefix, separator, transform = format_name.partition(":")
        if not separator or _namespace(tag, prefix) not in _TRANSFORM_REGISTRIES:
            raise ValueError(f"unsupported transformation {format_name!r}")
        if transform in {"num-dot-decimal", "numdotdecimal"}:
            if _GROUPED_DECIMAL.fullmatch(text):
                text = text.replace(",", "")
            elif _SPACE_GROUPED_DECIMAL.fullmatch(text):
                text = text.replace(" ", "")
            else:
                raise ValueError(f"ambiguous dot-decimal content {text!r}")
        elif transform == "fixed-zero":
            text = "0"
        elif transform in {"zerodash", "numdash"} and text in {"-", "—", "–", "−"}:
            text = "0"
        else:
            raise ValueError(f"unsupported transformation {format_name!r}")
    elif not _DECIMAL.fullmatch(text):
        raise ValueError(f"untransformed content is not a decimal {text!r}")
    scale_text = str(tag.get("scale", "0"))
    if not re.fullmatch(r"[+-]?[0-9]+", scale_text):
        raise ValueError(f"unsupported scale {scale_text!r}")
    scale = int(scale_text)
    sign = str(tag["sign"]) if tag.has_attr("sign") else None
    if sign not in {None, "-"}:
        raise ValueError(f"unsupported sign {sign!r}")
    number = Decimal(text)
    if not number.is_finite() or (sign and number.is_signed()):
        raise ValueError("ambiguous numeric sign or nonfinite value")
    parts = number.as_tuple()
    # Construct the shifted tuple: Decimal multiplication/scaleb can round under
    # the caller's ambient decimal precision, even though source digits are exact.
    return (
        Decimal((int(bool(parts.sign) ^ (sign == "-")), parts.digits, parts.exponent + scale)),
        scale,
        sign,
        format_name,
    )


def _anchor(tag: Tag) -> str | None:
    for node in chain((tag,), tag.parents):
        if isinstance(node, Tag) and (anchor := node.get("id")):
            return str(anchor)
    anchor = tag.find("a", id=True) or tag.find("a", attrs={"name": True})
    return str(anchor.get("id") or anchor["name"]) if anchor else None


def _span(cell: Tag, attribute: str, warnings: list[str], table_id: str) -> int:
    value = str(cell.get(attribute, "1"))
    if re.fullmatch(r"[0-9]+", value) and (int(value) > 0 or attribute == "rowspan"):
        return int(value)
    warnings.append(f"{table_id}: invalid {attribute} {value!r}; browser-default span 1 retained")
    return 1


def _table(
    tag: Tag,
    table_id: str,
    filing: Filing,
    caption: str,
    sections: SectionTracker,
    warnings: list[str],
    source_rows: dict[int, int],
) -> SourceTable:
    rows: list[list[str]] = []
    spans: list[list[tuple[int, int]]] = []
    headers: list[list[bool]] = []
    row_indices: list[int] = []
    for row in tag.find_all("tr"):
        if row.find_parent("table") is not tag:
            continue
        cells = [cell for cell in row.find_all(["td", "th"]) if cell.find_parent("tr") is row]
        rows.append([_text(cell) for cell in cells])
        row_indices.append(source_rows[id(row)])
        spans.append(
            [
                (
                    _span(cell, "rowspan", warnings, table_id),
                    _span(cell, "colspan", warnings, table_id),
                )
                for cell in cells
            ]
        )
        headers.append(
            [
                cell.name == "th"
                or cell.find_parent("thead") is not None
                or str(cell.get("role", "")) in {"columnheader", "rowheader"}
                for cell in cells
            ]
        )
    explicit_caption = tag.find("caption", recursive=False)
    if explicit_caption:
        caption = "\n".join(value for value in (caption, _text(explicit_caption)) if value)
    return SourceTable(
        table_id=table_id,
        filing_accession=filing.accession,
        caption=caption,
        rows=rows,
        row_indices=row_indices,
        cell_spans=spans,
        header_cells=headers,
        source_anchor=_anchor(tag),
        section=sections.section,
        item=sections.item,
    )


def _concept_label(concept: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", concept.rsplit(":", 1)[-1])


def parse_evidence(filing: Filing, paragraphs: list[FilingParagraph]) -> FilingEvidence:
    """Extract visible facts and tables from immutable cached bytes, without providers.

    Table rows are source cells, not a guessed rectangular expansion. Contexts and
    units may reside in hidden resources; hidden numeric tags never become facts.
    Unsupported facts remain visible in their original tables and produce warnings.
    """
    raw = filing.local_path.read_bytes()
    soup = BeautifulSoup(raw, "lxml")
    contexts = _metadata(soup, "xbrli:context", _context)
    units = _metadata(soup, "xbrli:unit", _unit)
    fact_ids = {
        id(tag): f"{filing.accession}:fact:{index}"
        for index, tag in enumerate(soup.find_all("ix:nonfraction"))
    }
    table_ids = {
        id(tag): f"{filing.accession}:table:{index}"
        for index, tag in enumerate(soup.find_all("table"))
    }
    source_rows = {
        id(row): index
        for table in soup.find_all("table")
        for index, row in enumerate(
            row for row in table.find_all("tr") if row.find_parent("table") is table
        )
    }
    _clean_visible(soup)
    evidence = FilingEvidence(filing_accession=filing.accession, raw_sha256=sha256(raw).hexdigest())
    supplied: dict[str, deque[FilingParagraph]] = defaultdict(deque)
    for paragraph in paragraphs:
        if paragraph.filing_accession == filing.accession:
            supplied[paragraph.normalized_text].append(paragraph)
    links: dict[int, list[str]] = {}
    quotes: dict[int, str] = {}
    fact_tables: dict[int, str] = {}
    labels: dict[int, str] = {}
    sections = SectionTracker()
    recent: deque[str] = deque(maxlen=3)
    for block in text_blocks(soup, include_tables=True):
        if block.table is not None:
            table_id = table_ids[id(block.table)]
            source_table = _table(
                block.table,
                table_id,
                filing,
                "\n".join(recent),
                sections,
                evidence.warnings,
                source_rows,
            )
            evidence.tables.append(source_table)
            for row in block.table.find_all("tr"):
                if row.find_parent("table") is not block.table:
                    continue
                cells = [
                    cell for cell in row.find_all(["td", "th"]) if cell.find_parent("tr") is row
                ]
                values = [_text(cell) for cell in cells]
                quote = " | ".join(values)
                label = next(
                    (
                        value
                        for value in values
                        if re.search(r"[A-Za-z]", value) and len(value) <= 240
                    ),
                    "",
                )
                for fact in row.find_all("ix:nonfraction"):
                    fact_tables[id(fact)] = table_id
                    quotes[id(fact)] = quote
                    if label:
                        labels[id(fact)] = label
            recent.clear()
            continue
        heading = sections.consume_heading(block)
        if heading or re.search(
            r"\b(?:table|schedule|thousands|millions|per share|unaudited)\b", block.text, re.I
        ):
            if len(block.text) <= 600:
                recent.append(block.text)
        elif block.text.strip():
            recent.clear()
        normalized = normalize_text(block.text)
        matches = supplied.get(normalized)
        paragraph = matches.popleft() if matches and not heading else None
        for node in block.fact_nodes:
            quotes[id(node)] = block.text
            if paragraph:
                links[id(node)] = [paragraph.paragraph_id]
    for tag in soup.find_all("ix:nonfraction"):
        fact_id = fact_ids[id(tag)]
        try:
            context = contexts.get(str(tag.get("contextref", "")))
            unit = units.get(str(tag.get("unitref", "")))
            if context is None or isinstance(context, Exception):
                raise ValueError(f"unavailable context: {context or tag.get('contextref')}")
            if unit is None or isinstance(unit, Exception):
                raise ValueError(f"unavailable unit: {unit or tag.get('unitref')}")
            concept = str(tag.get("name", ""))
            if not concept or ":" not in concept:
                raise ValueError("missing qualified concept name")
            value, scale, sign, format_name = _numeric_value(tag)
            evidence.facts.append(
                SourceFact(
                    fact_id=fact_id,
                    filing_accession=filing.accession,
                    concept=concept,
                    label=labels.get(id(tag), _concept_label(concept)),
                    value=value,
                    unit=unit,
                    **context,
                    scale=scale,
                    sign=sign,
                    format=format_name,
                    quote=quotes.get(id(tag), _text(tag)),
                    source_anchor=_anchor(tag),
                    paragraph_ids=links.get(id(tag), []),
                    table_id=fact_tables.get(id(tag)),
                )
            )
        except (ValueError, InvalidOperation, OverflowError) as error:
            evidence.warnings.append(f"{fact_id} ({tag.get('id', 'no anchor')}): {error}")
    return evidence
