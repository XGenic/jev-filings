"""Render persisted judgments without provider calls or trusted filing markup."""

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup

from radar.match.lexical import word_diff
from radar.models import AnalysisRun, RankedDelta

_DOMAINS = (
    "liquidity_or_financing",
    "supply_or_capacity",
    "customer_concentration",
    "regulatory_or_government",
    "outlook_or_guidance",
    "uncertainty",
)

_ALIGNMENT_REASONS = {
    "near_tied_assignment": "A competing assignment has a similar total matching score.",
    "scope_unclear": (
        "The reporting scope is unclear or mixed; financial comparability needs review."
    ),
    "cross_subsection": "The paragraphs come from different normalized subsections.",
    "possible_split_merge": (
        "The assignment suggests a possible split or merge; inspect the competing paragraphs."
    ),
}

_ALIGNMENT_STATUSES = {
    "exact": "Exact",
    "cosmetic": "Cosmetic",
    "aligned": "Aligned",
    "review": "Needs review",
    "unmatched": "Unmatched",
}


def _label(value: str) -> str:
    return value.replace("_", " ").capitalize()


def _sec_url(value: str) -> str | None:
    # urlsplit alone tolerates control characters and userinfo; reject both explicitly.
    if any(character.isspace() or ord(character) < 32 for character in value):
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if (
            parts.scheme == "https"
            and (host == "sec.gov" or host.endswith(".sec.gov"))
            and parts.username is None
            and parts.password is None
            and parts.port in (None, 443)
            and "\\" not in value
        ):
            return value
    except ValueError:
        pass
    return None


def _delta_view(delta: RankedDelta, rank: int) -> dict:
    pair = delta.pair
    paragraphs = [paragraph for paragraph in (pair.old, pair.new) if paragraph is not None]
    sections = list(dict.fromkeys(paragraph.section or "Unclassified" for paragraph in paragraphs))
    alignment = pair.alignment
    alignment_status = (
        "unavailable"
        if alignment is None
        else "aligned"
        if alignment.status in {"exact", "cosmetic"}
        else alignment.status
    )
    values = delta.signals.model_dump() if delta.signals is not None else {}
    directions = []
    domains = []
    for domain in _DOMAINS:
        direction = values.get(f"{domain}_direction")
        probabilities = values.get(f"{domain}_probabilities")
        directions.append(
            {"name": _label(domain), "direction": direction, "probabilities": probabilities}
        )
        if direction in {"introduced_or_increased", "reduced_or_resolved", "present"}:
            domains.append({"key": domain, "name": _label(domain), "direction": direction})
    scalar_signals = [
        {"name": _label(name), "value": value}
        for name, value in values.items()
        if name != "question_schema_version" and not name.endswith(("_direction", "_probabilities"))
    ]
    return {
        "delta": delta,
        "rank": rank,
        "sections": sections,
        "sections_json": json.dumps(sections, ensure_ascii=False),
        "domains": domains,
        "domains_json": json.dumps([domain["key"] for domain in domains]),
        "directions": directions,
        "scalar_signals": scalar_signals,
        "alignment_status": alignment_status,
        "alignment_label": (
            _ALIGNMENT_STATUSES[alignment.status] if alignment else "Unavailable (historical)"
        ),
        "alignment_reasons": (
            [_ALIGNMENT_REASONS[reason] for reason in alignment.review_reasons] if alignment else []
        ),
        "economic": values.get("plausibly_economically_consequential"),
        # This is the only trusted markup: persisted lexical_diff_html is never used.
        "diff": Markup(
            word_diff(pair.old.text if pair.old else "", pair.new.text if pair.new else "")
        ),
    }


def render_report(run: AnalysisRun, output: Path, top_n: int = 20) -> Path:
    """Atomically write a self-contained report; preserve pipeline ranking and counts."""
    if top_n < 1:
        raise ValueError("top_n must be positive")
    output = Path(output)
    companies = []
    for index, company in enumerate(run.companies):
        eligible = [delta for delta in company.deltas if delta.pair.skip_reason is None]
        deltas = [_delta_view(delta, rank) for rank, delta in enumerate(eligible[:top_n], 1)]
        companies.append(
            {
                "id": f"company-{index}",
                "analysis": company,
                "deltas": deltas,
                "eligible": len(eligible),
                "skipped": len(company.deltas) - len(eligible),
                "sections": sorted({section for delta in deltas for section in delta["sections"]}),
                "domains": sorted(
                    {
                        (domain["key"], domain["name"])
                        for delta in deltas
                        for domain in delta["domains"]
                    }
                ),
            }
        )
    environment = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        autoescape=select_autoescape(default_for_string=True, default=True),
        undefined=StrictUndefined,
    )
    environment.filters.update(label=_label, sec_url=_sec_url)
    html = environment.get_template("report.html.j2").render(
        run=run,
        companies=companies,
        top_n=top_n,
        provider=run.settings.get("jev_provider") or "Unavailable / not recorded",
        provider_model=run.settings.get("jev_model") or "Not recorded",
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(html)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return output
