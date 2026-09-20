"""Context-constrained assignment; matching reliability stays separate from economic ranking."""

import logging
from collections import defaultdict

import numpy as np

from radar.config import Settings
from radar.match.assignment import Candidate, Decision, assign_candidates
from radar.match.context import paragraph_contexts, scopes_compatible
from radar.match.embeddings import EmbeddingProvider
from radar.match.lexical import candidate_similarity, cosmetic_only, lexical_similarity, word_diff
from radar.models import (
    AlignedPair,
    AlignmentAlternative,
    AlignmentEvidence,
    AlignmentResult,
    FilingParagraph,
)

logger = logging.getLogger(__name__)
_REASON_TEXT = {
    "near_tied_assignment": "Near-tied feasible assignment; inspect the competing pairings.",
    "scope_unclear": "Reporting scope is mixed or unclear for this comparison.",
    "cross_subsection": "Broader-context fallback; corresponding subsection was not sufficient.",
    "possible_split_merge": (
        "Adjacent passages together resemble one counterpart; possible split or merge."
    ),
}


def align_paragraphs(
    old: list[FilingParagraph],
    new: list[FilingParagraph],
    settings: Settings,
    embedder: EmbeddingProvider | None = None,
) -> AlignmentResult:
    old_contexts, new_contexts = paragraph_contexts(old), paragraph_contexts(new)
    pairs: list[AlignedPair] = []
    old_used: set[int] = set()
    new_used: set[int] = set()
    anchors: list[tuple[int, int]] = []
    warnings: list[str] = []
    pending_old: dict[int, tuple[Decision, str]] = {}
    pending_new: dict[int, tuple[Decision, str]] = {}

    def record(i, j, *, skip=None, decision=None, tier="none"):
        before = old[i] if i is not None else None
        after = new[j] if j is not None else None
        before_context = old_contexts[i] if i is not None else None
        after_context = new_contexts[j] if j is not None else None
        edge = decision.candidate if decision else None
        relation = (
            "matched"
            if before is not None and after is not None
            else ("added" if after is not None else "deleted")
        )
        evidence = AlignmentEvidence(
            status=skip or ("aligned" if relation == "matched" else "unmatched"),
            old_context=before_context,
            new_context=after_context,
            candidate_tier=tier,
            score=edge.score if edge else None,
            components=edge.components if edge else {},
            assignment_margin=decision.margin if decision else None,
        )
        if decision:
            evidence.alternatives = [
                AlignmentAlternative(
                    old=old[a] if a is not None else None,
                    new=new[b] if b is not None else None,
                    score=candidate.score if candidate else None,
                    cosine_similarity=candidate.cosine if candidate else None,
                )
                for a, b, candidate in decision.alternatives
            ]
            if decision.margin < settings.alignment_ambiguity_margin and decision.alternatives:
                evidence.review_reasons.append("near_tied_assignment")
        if not skip:
            scopes = [
                context.reporting_scope
                for context in (before_context, after_context)
                if context is not None
            ]
            if "mixed" in scopes or (len(set(scopes)) > 1 and "unknown" in scopes):
                evidence.review_reasons.append("scope_unclear")
            if (
                before_context
                and after_context
                and tier in {"item", "filing"}
                and (
                    tier == "filing"
                    or before_context.subsection_key != after_context.subsection_key
                )
            ):
                evidence.review_reasons.append("cross_subsection")
        if evidence.review_reasons:
            evidence.status = "review"
        pairs.append(
            AlignedPair(
                old=before,
                new=after,
                relation=relation,
                cosine_similarity=edge.cosine if edge else None,
                lexical_similarity=(
                    lexical_similarity(before.normalized_text, after.normalized_text)
                    if before is not None and after is not None
                    else None
                ),
                lexical_diff_html=word_diff(
                    before.text if before else "", after.text if after else ""
                ),
                skip_reason=skip,
                alignment=evidence,
            )
        )
        if i is not None:
            old_used.add(i)
        if j is not None:
            new_used.add(j)
        if i is not None and j is not None:
            anchors.append((i, j))

    by_hash = defaultdict(list)
    for i, paragraph in enumerate(old):
        by_hash[paragraph.text_hash].append(i)
    for j, paragraph in enumerate(new):
        candidates = [
            i
            for i in by_hash[paragraph.text_hash]
            if i not in old_used and scopes_compatible(old_contexts[i], new_contexts[j])
        ]
        if candidates:
            i = min(
                candidates,
                key=lambda i: (
                    old[i].item != paragraph.item,
                    old_contexts[i].subsection_key != new_contexts[j].subsection_key,
                    abs(old_contexts[i].subsection_position - new_contexts[j].subsection_position),
                    i,
                ),
            )
            record(i, j, skip="exact")
    for j, paragraph in enumerate(new):
        if j in new_used:
            continue
        for i, before in enumerate(old):
            if (
                i in old_used
                or before.item != paragraph.item
                or not scopes_compatible(old_contexts[i], new_contexts[j])
            ):
                continue
            if lexical_similarity(
                before.normalized_text, paragraph.normalized_text
            ) > settings.cosmetic_threshold and cosmetic_only(
                before.normalized_text, paragraph.normalized_text
            ):
                record(i, j, skip="cosmetic")
                break

    old_remaining = [i for i in range(len(old)) if i not in old_used]
    new_remaining = [j for j in range(len(new)) if j not in new_used]
    vectors = None
    model = embedder.name if embedder else "unavailable (lexical-only)"
    if embedder and old_remaining and new_remaining:
        try:
            vectors = embedder.embed(
                [old[i] for i in old_remaining] + [new[j] for j in new_remaining]
            )
            if (
                vectors.ndim != 2
                or len(vectors) != len(old_remaining) + len(new_remaining)
                or not np.isfinite(vectors).all()
            ):
                raise ValueError("Invalid embedding matrix")
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            if np.any(norms == 0):
                raise ValueError("Zero-length embedding vector")
            vectors = vectors / norms
        except Exception as exc:
            warnings.append(
                f"Embeddings unavailable; lexical-only alignment: {type(exc).__name__}: {exc}"
            )
            model = "unavailable (lexical-only)"
            vectors = None
    elif not embedder:
        warnings.append("Embeddings disabled or unavailable; lexical-only alignment.")
    old_offsets = {i: offset for offset, i in enumerate(old_remaining)}
    new_offsets = {j: offset for offset, j in enumerate(new_remaining)}
    floor = settings.cosine_floor if vectors is not None else settings.lexical_match_floor
    # Compute each semantic row once; reuse it across subsection and fallback phases.
    cosines = (
        np.clip(vectors[: len(old_remaining)] @ vectors[len(old_remaining) :].T, -1, 1)
        if vectors is not None
        else None
    )

    for tier in ("subsection", "item", "filing"):
        old_active = [i for i in old_remaining if i not in old_used]
        new_active = [j for j in new_remaining if j not in new_used]
        edges = []
        for j in new_active:
            context = new_contexts[j]
            if tier == "subsection":
                pool = [
                    i
                    for i in old_active
                    if context.subsection_key is not None
                    and old[i].item == new[j].item
                    and old_contexts[i].subsection_key == context.subsection_key
                ]
            elif tier == "item":
                pool = [i for i in old_active if old[i].item == new[j].item]
            else:
                # Do not cross SEC items merely because same-item candidates scored poorly.
                pool = [] if any(old[i].item == new[j].item for i in old_active) else old_active
            relevant_anchors = [
                (a, b)
                for a, b in anchors
                if old[a].item == new[j].item
                and new[b].item == new[j].item
                and (
                    tier != "subsection"
                    or old_contexts[a].subsection_key
                    == context.subsection_key
                    == new_contexts[b].subsection_key
                )
            ]
            previous = max(
                ((a, b) for a, b in relevant_anchors if b < j), key=lambda ab: ab[1], default=None
            )
            following = min(
                ((a, b) for a, b in relevant_anchors if b > j), key=lambda ab: ab[1], default=None
            )
            row = []
            for i in pool:
                if not scopes_compatible(old_contexts[i], context):
                    continue
                lexical = candidate_similarity(old[i].normalized_text, new[j].normalized_text)
                cosine = (
                    float(cosines[old_offsets[i], new_offsets[j]]) if cosines is not None else None
                )
                metric = cosine if cosine is not None else lexical
                if metric < floor:
                    continue
                position = 1 - abs(
                    old_contexts[i].subsection_position - context.subsection_position
                )
                if tier != "subsection":
                    position = 1 - abs(i / max(len(old), 1) - j / max(len(new), 1))
                bounds = ([i > previous[0]] if previous else []) + (
                    [i < following[0]] if following else []
                )
                support = sum(bounds) / len(bounds) if bounds else 0.5
                components = {
                    "semantic_similarity" if cosine is not None else "lexical_fallback": (
                        0.85 * metric
                    ),
                    "lexical_similarity": 0.10 * lexical,
                    "relative_position": 0.03 * position,
                    "neighbor_anchors": 0.02 * support,
                }
                score = sum(components.values())
                if score > floor:
                    row.append(Candidate(i, j, score, cosine, components))
            row.sort(key=lambda edge: (-edge.score, edge.old))
            # Joint matching sees every compatible local candidate. Broader retrieval is bounded.
            edges.extend(row if tier == "subsection" else row[: settings.alignment_top_k])
        for decision in assign_candidates(edges, floor):
            if decision.old is not None and decision.new is not None:
                record(decision.old, decision.new, decision=decision, tier=tier)
            elif decision.old is not None:
                pending_old.setdefault(decision.old, (decision, tier))
            else:
                pending_new.setdefault(decision.new, (decision, tier))
    for j in new_remaining:
        if j not in new_used:
            decision, tier = pending_new.get(j, (None, "none"))
            record(None, j, decision=decision, tier=tier)
    for i in old_remaining:
        if i not in old_used:
            decision, tier = pending_old.get(i, (None, "none"))
            record(i, None, decision=decision, tier=tier)

    # Only call something a possible split/merge when adjacent chunks together provide
    # substantially better lexical coverage than either chosen chunk alone.
    matched = [pair for pair in pairs if pair.relation == "matched" and not pair.skip_reason]
    for unpaired in [pair for pair in pairs if pair.relation != "matched"]:
        added = unpaired.new is not None
        fragment = unpaired.new if added else unpaired.old
        context = unpaired.alignment.new_context if added else unpaired.alignment.old_context
        for paired in matched:
            neighbor = paired.new if added else paired.old
            counterpart = paired.old if added else paired.new
            neighbor_context = (
                paired.alignment.new_context if added else paired.alignment.old_context
            )
            if (
                fragment.item != neighbor.item
                or abs(fragment.ordinal - neighbor.ordinal) != 1
                or context.subsection_key != neighbor_context.subsection_key
                or not scopes_compatible(context, neighbor_context)
            ):
                continue
            chunks = sorted((fragment, neighbor), key=lambda p: p.ordinal)
            combined = lexical_similarity(
                counterpart.normalized_text, " ".join(p.normalized_text for p in chunks)
            )
            single = max(
                lexical_similarity(counterpart.normalized_text, p.normalized_text) for p in chunks
            )
            if combined >= 0.75 and combined - single >= 0.10:
                for affected in (unpaired, paired):
                    if "possible_split_merge" not in affected.alignment.review_reasons:
                        affected.alignment.review_reasons.append("possible_split_merge")
                    affected.alignment.status = "review"
                unpaired.alignment.alternatives.append(
                    AlignmentAlternative(
                        old=paired.old,
                        new=paired.new,
                        score=paired.alignment.score,
                        cosine_similarity=paired.cosine_similarity,
                    )
                )
                paired.alignment.alternatives.append(
                    AlignmentAlternative(old=unpaired.old, new=unpaired.new)
                )
    for pair in pairs:
        pair.warnings = [_REASON_TEXT[reason] for reason in pair.alignment.review_reasons]
        if pair.warnings:
            identity = (pair.new or pair.old).paragraph_id
            warnings.append(f"{identity}: {' '.join(pair.warnings)}")
    for message in warnings:
        logger.warning(message)
    pairs.sort(key=lambda pair: (pair.new is None, (pair.new or pair.old).ordinal))
    return AlignmentResult(pairs=pairs, embedding_model=model, warnings=warnings)
