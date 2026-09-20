"""Maximum-utility one-to-one assignments, with explicit zero-utility unmatched choices."""

from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linear_sum_assignment


@dataclass(frozen=True, slots=True)
class Candidate:
    old: int
    new: int
    score: float
    cosine: float | None = None
    components: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class Decision:
    old: int | None
    new: int | None
    candidate: Candidate | None
    margin: float
    alternatives: list[tuple[int | None, int | None, Candidate | None]]


def _components(candidates: list[Candidate]) -> list[list[Candidate]]:
    parent = {}

    def root(node):
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for edge in candidates:
        parent[root(("old", edge.old))] = root(("new", edge.new))
    groups = defaultdict(list)
    for edge in candidates:
        groups[root(("old", edge.old))].append(edge)
    return list(groups.values())


def assign_candidates(candidates: list[Candidate], unmatched_score: float) -> list[Decision]:
    """Margin is loss of total component utility, not local top-two distance or probability.

    A match earns score - unmatched_score. Both unmatched sides earn zero. Incompatible
    or below-threshold edges are absent, so the solver cannot force an invalid match.
    """
    eligible = [edge for edge in candidates if edge.score > unmatched_score]
    decisions = []
    for edges in _components(eligible):
        olds = sorted({edge.old for edge in edges})
        news = sorted({edge.new for edge in edges})
        old_columns = {value: index for index, value in enumerate(olds)}
        new_rows = {value: index for index, value in enumerate(news)}
        by_pair = {(edge.old, edge.new): edge for edge in edges}
        forbidden = -1e6
        utility = np.full((len(news), len(olds) + len(news)), forbidden, dtype=np.float64)
        utility[:, len(olds) :] = 0.0
        for edge in edges:
            utility[new_rows[edge.new], old_columns[edge.old]] = edge.score - unmatched_score

        def solve(matrix):
            rows, columns = linear_sum_assignment(matrix, maximize=True)
            assignment = {
                news[r]: olds[c] if c < len(olds) else None
                for r, c in zip(rows, columns, strict=True)
            }
            total = float(utility[rows, columns].sum())
            return assignment, total

        selected, best = solve(utility)
        used_old = {value for value in selected.values() if value is not None}

        def counterfactual(before: int | None, after: int | None):
            changed = utility.copy()
            if before is not None and after is not None:
                changed[new_rows[after], old_columns[before]] = forbidden
            elif after is not None:
                # Force this otherwise-unmatched new paragraph to have a real partner.
                changed[new_rows[after], len(olds) :] = forbidden
            else:
                # Force this otherwise-unmatched old column to be used. The bonus is
                # removed when computing the actual objective from the original matrix.
                column = old_columns[before]
                mask = changed[:, column] > forbidden
                changed[mask, column] += len(news) + 1
            alternative, value = solve(changed)
            moves = [
                (old, new, by_pair.get((old, new)))
                for new, old in alternative.items()
                if old != selected[new]
            ]
            now_used = {old for old in alternative.values() if old is not None}
            moves.extend((old, None, None) for old in sorted(used_old - now_used))
            return max(0.0, best - value), moves

        for new, old in selected.items():
            margin, moves = counterfactual(old, new)
            decisions.append(Decision(old, new, by_pair.get((old, new)), margin, moves))
        for old in olds:
            if old not in used_old:
                margin, moves = counterfactual(old, None)
                decisions.append(Decision(old, None, None, margin, moves))
    return decisions
