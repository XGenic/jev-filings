"""Render persisted judgments without provider calls or trusted filing markup."""

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup

from radar.jev.questions import BUSINESS_QUESTIONS, QUESTION_SCHEMA_VERSION
from radar.match.lexical import word_diff
from radar.models import AnalysisRun, RankedDelta
from radar.report.selection import REPORT_SELECTION_POLICY, EvidenceGroup, build_report_selection

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
    if value == "additions":
        return "Unmatched current passages"
    if value == "deletions":
        return "Unmatched previous passages"
    return value.replace("_", " ").capitalize()


def _relation_label(value: str) -> str:
    return {
        "matched": "Matched",
        "added": "Unmatched current passage",
        "deleted": "Unmatched previous passage",
    }[value]


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
    values = delta.signals.model_dump(exclude={"assessment"}) if delta.signals is not None else {}
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
        if name not in {"question_schema_version", "assessment"}
        and not name.endswith(("_direction", "_probabilities"))
    ]
    assessment = delta.signals.assessment if delta.signals else None
    dimensions = []
    if assessment is not None:
        schema = delta.signals.question_schema_version
        unavailable = f"Criterion unavailable for recorded schema {schema}."
        for name, question in BUSINESS_QUESTIONS.items():
            judgment = getattr(assessment, name)
            criteria = question["criteria"] if schema == QUESTION_SCHEMA_VERSION else {}
            dimensions.append(
                {
                    "key": name,
                    "name": _label(name),
                    "choice": judgment.choice,
                    "criterion": criteria.get(
                        judgment.choice,
                        unavailable,
                    ),
                    "distribution": [
                        {
                            "choice": choice,
                            "probability": probability,
                            "criterion": criteria.get(
                                choice,
                                unavailable,
                            ),
                        }
                        for choice, probability in judgment.probabilities.items()
                    ],
                }
            )
    categories = {
        name: getattr(assessment, name).choice if assessment else "unavailable"
        for name in ("business_subject", "change_nature", "business_direction")
    }
    categories.update(
        impact_band=(delta.impact_band or "unavailable") if assessment else "unavailable",
        comparison_reliability=(delta.comparison_reliability or "unavailable")
        if assessment
        else "unavailable",
    )
    return {
        "delta": delta,
        "rank": rank,
        "relation_label": _relation_label(pair.relation),
        "sections": sections,
        "sections_json": json.dumps(sections, ensure_ascii=False),
        "domains": domains,
        "domains_json": json.dumps([domain["key"] for domain in domains]),
        "directions": directions,
        "scalar_signals": scalar_signals,
        "categories": categories,
        "dimensions": dimensions,
        "priority": (delta.priority_band or "unavailable") if assessment else "unavailable",
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


def _group_view(group: EvidenceGroup) -> dict:
    members = [_delta_view(delta, rank) for rank, delta in group.members]
    lead_rank, _ = group.leader
    lead = next(member for member in members if member["rank"] == lead_rank)
    filters = [
        {
            **member["categories"],
            "section": member["sections"],
            "domain": [domain["key"] for domain in member["domains"]],
            "relation": member["delta"].pair.relation,
            "alignment": member["alignment_status"],
        }
        for member in members
    ]
    return {
        **lead,
        "rank": group.source_rank,
        "lead": lead,
        "members": members,
        "others": [member for member in members if member is not lead],
        "group_priority": group.priority,
        "lane": group.lane,
        "group_reliability": (
            "supported"
            if group.priority in {"high", "medium", "low"}
            else "unavailable"
            if group.priority == "unavailable"
            else "needs_review"
        ),
        "member_filters_json": json.dumps(filters, ensure_ascii=False),
    }


def _lane_view(key: str, title: str, groups: list[EvidenceGroup], selected: list[dict]) -> dict:
    members = [member for group in selected for member in group["members"]]
    return {
        "key": key,
        "title": title,
        "deltas": selected,
        "total_groups": len(groups),
        "candidate_passages": sum(len(group.members) for group in groups),
        "included_passages": len(members),
        "category_filters": [
            {
                "key": name,
                "name": _label(name),
                "choices": sorted({member["categories"][name] for member in members}),
            }
            for name in (
                "business_subject",
                "change_nature",
                "impact_band",
                "business_direction",
                "comparison_reliability",
            )
        ],
        "sections": sorted({section for member in members for section in member["sections"]}),
        "domains": sorted(
            {(domain["key"], domain["name"]) for member in members for domain in member["domains"]}
        ),
    }


def render_report(run: AnalysisRun, output: Path, top_n: int = 20) -> Path:
    """Atomically render persisted evidence with one bounded card budget per company."""
    if top_n < 1:
        raise ValueError("top_n must be positive")
    output = Path(output)
    companies = []
    historical = not any(
        delta.comparison_evidence is not None or (delta.signals and delta.signals.assessment)
        for company in run.companies
        for delta in company.deltas
    )
    for index, company in enumerate(run.companies):
        eligible = [delta for delta in company.deltas if delta.pair.skip_reason is None]
        selection = build_report_selection(company.deltas, top_n, historical)
        groups, selected = selection.groups, selection.selected
        deltas = [_group_view(group) for group in selected]
        if historical:
            lanes = [_lane_view("historical", "Historical persisted ranking", groups, deltas)]
        else:
            lanes = [
                _lane_view(
                    key,
                    title,
                    [group for group in groups if group.lane == key],
                    [row for row in deltas if row["lane"] == key],
                )
                for key, title in (
                    ("supported", "Supported business changes"),
                    ("review", "Potentially important disclosures requiring comparison review"),
                )
            ]
        priority_counts = dict.fromkeys(("high", "medium", "low", "review", "unavailable"), 0)
        for delta in eligible:
            priority_counts[delta.priority_band or "unavailable"] += 1
        companies.append(
            {
                "id": f"company-{index}",
                "analysis": company,
                "deltas": deltas,
                "historical": historical,
                "lanes": lanes,
                "groups": len(groups),
                "included_passages": sum(len(group.members) for group in selected),
                "omitted_groups": len(groups) - len(selected),
                "reservation": max(1, top_n // 4),
                "eligible": len(eligible),
                "skipped": len(company.deltas) - len(eligible),
                "priority_counts": priority_counts,
                "review_selected": sum(
                    group.priority in {"review", "unavailable"} for group in selected
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
        selection_policy=REPORT_SELECTION_POLICY,
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
