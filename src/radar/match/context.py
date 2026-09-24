"""Derived alignment context; preserved SEC paragraphs are never rewritten."""

import re
import unicodedata
from collections import defaultdict

from radar.models import FilingParagraph, ParagraphContext

_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_DATE = re.compile(
    rf"\b(?:{_MONTH}\.?\s+(?:19|20)\d{{2}}"
    rf"|{_MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+(?:19|20)\d{{2}})?"
    rf"|\d{{1,2}}\s+{_MONTH}\.?\s+(?:19|20)\d{{2}}"
    r"|(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}"
    r"|\d{1,2}[/.-]\d{1,2}[/.-](?:\d{4}|\d{2}))\b"
)
_COUNT = r"(?:three|six|nine|twelve|3|6|9|12)"
_MONTH_PERIOD = re.compile(
    rf"\b(?:first\s+)?(?P<counts>{_COUNT}(?:\s+months?)?"
    rf"(?:\s*(?:,|and|&)\s*{_COUNT})*)\s+months?"
    r"(?:\s+periods?)?(?:\s+ended)?\b"
)
_QUARTER = re.compile(
    r"\b(?:(?:first|second|third|fourth|1st|2nd|3rd|4th)\s+(?:fiscal\s+)?"
    r"|fiscal\s+(?:(?:first|second|third|fourth)\s+)?)?quarter(?:s|ly)?"
    r"(?:\s+ended)?\b|\bq[1-4]\b"
)
_YTD = re.compile(r"\byear\s+to\s+date\b|\bytd\b")
_ANNUAL = re.compile(
    r"\b(?:fiscal\s+(?:years?\s+)?(?:19|20)\d{2}|fiscal\s+years?"
    r"|full\s+years?|years?\s+ended|annual)\b"
)
_AS_OF = re.compile(rf"\b(?:as\s+of|at)\s+(?:{_DATE.pattern}|(?:fiscal\s+)?year\s+end)")
# A loss contingency identifies an uncertain obligation, not a period's income/loss.
_REPORT_SUBJECT = re.compile(
    r"\b(?:revenues?|sales|income|loss(?:es)?(?![\s-]+contingenc(?:y|ies)\b)"
    r"|margins?|earnings|expenses?|costs?|cash\s+flows?|net\s+cash|results|profits?"
    r"|ebitda|tax(?:es)?|financial\s+performance|sources\s+and\s+uses\s+of\s+cash)\b"
)
_BALANCE_SUBJECT = re.compile(
    r"\b(?:cash|cash\s+equivalents|balance(?:s)?|assets?|liabilit(?:y|ies)"
    r"|inventor(?:y|ies)|reserves?|receivables?|payables?|debt|borrowings?"
    r"|working\s+capital|equity|backlog)\b"
)
_BALANCE_FACT = re.compile(
    r"\b(?:was|were|is|are|had|held|total(?:ed|led)?|amounted|stood|"
    r"increased|decreased|include[ds]?|of\s+\$)\b|\$\s*\d"
)
_FORWARD = re.compile(
    r"\b(?:expect(?:s|ed)?|anticipate[ds]?|forecast\w*|project(?:s|ed)?"
    r"|will|would|could|may|might|intend\w*|plan(?:s|ned)?|next|upcoming)\b"
)
_NONREPORTING_DURATION = re.compile(
    r"\b(?:runway|matur(?:e[sd]?|ity|ities)|expir(?:e[sd]?|ation)|"
    r"contractual|sufficient|fund\s+(?:our\s+)?operations)\b"
)
_NEGATED_PERIOD = re.compile(r"\b(?:not|rather\s+than|instead\s+of)\s+(?:for\s+)?(?:the\s+)?$")
_CAUSAL_TRANSACTION = re.compile(
    r"\b(?:due\s+to|because\s+of|reflects?|resulted?\s+from|attributable\s+to)\b"
    r".*\b(?:sale|disposal|acquisition|merger|transaction)\b"
    r"(?:(?!\b(?:and|but|for)\b).)*\b(?:in|during|on)\s+(?:the\s+)?$"
)
_SENTENCES = re.compile(r"(?<=[.!?;])\s+(?!\d)|\s+(?:but|whereas)\s+")
_STRUCTURE = re.compile(r"^(?:part\s+[ivx]+\b|items?\s+\d{1,2}[a-c]?\b)")
_FISCAL_YEAR = re.compile(
    r"\b(?:fiscal\s+(?:years?\s+)?|fy\s*)(?:19|20)\d{2}"
    r"(?:\s*(?:,|and|&)\s*(?:(?:fiscal\s+)?(?:years?\s+)?)?(?:19|20)\d{2})*\b"
)


def _canonical(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.translate({ord(char): None for char in "\u00ad\u200b\ufeff"})
    # Decimal points and date separators remain intact until dates/periods are parsed.
    text = re.sub(r"[‐‑‒–—−]|(?<!\d)-|-(?!\d)", " ", text)
    return " ".join(text.split())


def _subsections(section: str | None) -> list[str]:
    # The parser's path delimiter is spaced: an unspaced slash can belong to a date.
    parts = re.split(r"\s+/\s+", section or "")
    return [part for part in parts if part and not _STRUCTURE.match(_canonical(part))]


def _subsection_key(parts: list[str]) -> str | None:
    keys = []
    for part in parts:
        text = _canonical(part)
        text = _DATE.sub(" ", text)
        text = _FISCAL_YEAR.sub(" ", text)
        text = _MONTH_PERIOD.sub(" ", text)
        text = _QUARTER.sub(" ", text)
        text = _YTD.sub(" ", text)
        text = _ANNUAL.sub(" ", text)
        # Years following a reporting/date heading are not identifiers such as ASC 842.
        if _MONTH_PERIOD.search(_canonical(part)) or _ANNUAL.search(_canonical(part)):
            text = re.sub(r"(?<!\w)(?:19|20)\d{2}(?!\w)", " ", text)
        text = re.sub(r"[^\w\s]", " ", text)
        text = " ".join(text.split())
        text = re.sub(r"^(?:(?:for|the|of|and|ended|during|as|at)\s+)+", "", text)
        text = re.sub(r"(?:\s+(?:for|the|of|and|ended|during|as|at))+$", "", text)
        if text and text not in {"for", "the", "of", "and", "ended", "during", "as", "at"}:
            keys.append(text)
    return " / ".join(keys) or None


def _periods(text: str, *, heading: bool) -> list[tuple[str, int, int]]:
    found = []
    for match in _MONTH_PERIOD.finditer(text):
        # A duration alone describes runway, contracts, etc., not reporting results.
        prefix = text[max(0, match.start() - 24) : match.start()]
        following = re.sub(r"^\s+(?:year\s+)?", "", text[match.end() :])
        if not (
            heading
            or "ended" in match[0]
            or match[0].startswith("first ")
            or re.search(r"\b(?:for|during)\s+(?:the\s+)?$", prefix)
            or _REPORT_SUBJECT.match(following)
        ):
            continue
        for count in re.findall(_COUNT, match["counts"]):
            scope = (
                "quarter"
                if count in {"three", "3"}
                else ("annual" if count in {"twelve", "12"} else "year_to_date")
            )
            found.append((scope, match.start(), match.end()))
    for pattern, scope in ((_QUARTER, "quarter"), (_YTD, "year_to_date")):
        found.extend((scope, match.start(), match.end()) for match in pattern.finditer(text))
    for match in _ANNUAL.finditer(text):
        # Fiscal-year labels qualify quarter/YTD periods; they are not another subject.
        prefix = text[max(0, match.start() - 60) : match.start()]
        if re.search(r"(?:quarter|months?|year to date)\s+(?:ended\s+)?(?:of\s+)?$", prefix):
            continue
        if not heading and match[0].startswith("fiscal"):
            # A fiscal-year label in a comparison/explanation is not by itself a
            # full-year measurement. Require a reporting-period binding.
            following = text[match.end() :].lstrip()
            if not (
                re.search(r"\b(?:for|during|in|throughout)\s+(?:the\s+)?$", prefix)
                or _REPORT_SUBJECT.match(following)
            ):
                continue
        if found and re.search(r"(?:and|of|compared to)\s*$", prefix):
            continue
        found.append(("annual", match.start(), match.end()))
    found.extend(("point_in_time", match.start(), match.end()) for match in _AS_OF.finditer(text))
    return sorted(set(found), key=lambda value: (value[1], value[0]))


def _scope_evidence(text: str, *, heading: bool) -> list[tuple[str, str]]:
    evidence = []
    for sentence in _SENTENCES.split(_canonical(text)):
        # Separate a trailing projection without throwing away an observed first clause.
        clauses = re.split(r",?\s+and\s+(?=(?:we\s+)?(?:expect|anticipate|will)\b)", sentence)
        for clause in clauses:
            if not heading and _FORWARD.search(_DATE.sub("date", clause)):
                continue
            for scope, start, end in _periods(clause, heading=heading):
                if _NEGATED_PERIOD.search(clause[max(0, start - 30) : start]):
                    continue
                if not heading and _CAUSAL_TRANSACTION.search(clause[:start]):
                    # Date of the explanatory transaction, not scope of the metric.
                    continue
                nonreporting_text = clause if scope == "point_in_time" else clause[:start]
                if _NONREPORTING_DURATION.search(nonreporting_text):
                    continue
                if heading and scope == "point_in_time" and not _BALANCE_SUBJECT.search(clause):
                    continue
                if (
                    heading
                    and scope == "annual"
                    and not (
                        _REPORT_SUBJECT.search(clause)
                        or re.search(r"\b(?:fiscal|full|ended|statements)\b", clause)
                    )
                ):
                    continue
                if not heading:
                    if scope == "point_in_time":
                        if not (_BALANCE_SUBJECT.search(clause) and _BALANCE_FACT.search(clause)):
                            continue
                    elif not _REPORT_SUBJECT.search(clause):
                        continue
                source = "heading" if heading else "body"
                entry = (scope, f"{source}: {scope}: {clause.strip()}")
                if entry not in evidence:
                    evidence.append(entry)
    return evidence


def paragraph_contexts(paragraphs: list[FilingParagraph]) -> list[ParagraphContext]:
    """Return independent, derived contexts in input order, without mutating sources.

    Explicit body reporting subjects take precedence over inherited heading periods.
    Multiple explicit body subjects remain mixed rather than selecting the first date.
    Positions are within each (item, normalized subsection), including unknown scopes.
    """
    contexts = []
    groups: dict[tuple[str | None, str | None], list[int]] = defaultdict(list)
    for index, paragraph in enumerate(paragraphs):
        parts = _subsections(paragraph.section)
        heading_evidence = _scope_evidence(" / ".join(parts), heading=True)
        body_evidence = _scope_evidence(paragraph.text, heading=False)
        selected = body_evidence or heading_evidence
        scopes = {scope for scope, _ in selected}
        scope = next(iter(scopes)) if len(scopes) == 1 else "mixed" if scopes else "unknown"
        context = ParagraphContext(
            subsection_key=_subsection_key(parts),
            reporting_scope=scope,
            scope_evidence=[evidence for _, evidence in heading_evidence + body_evidence],
        )
        contexts.append(context)
        groups[(paragraph.item, context.subsection_key)].append(index)
    for indices in groups.values():
        denominator = len(indices) - 1
        for position, index in enumerate(indices):
            contexts[index].subsection_position = position / denominator if denominator else 0.5
    return contexts


def scopes_compatible(old: ParagraphContext, new: ParagraphContext) -> bool:
    """Only reject two explicit, different reporting scopes; uncertainty stays eligible."""
    return (
        old.reporting_scope in {"unknown", "mixed"}
        or new.reporting_scope in {"unknown", "mixed"}
        or old.reporting_scope == new.reporting_scope
    )
