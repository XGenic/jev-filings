import pytest

from radar.match.assignment import Candidate, assign_candidates


def chosen(decisions):
    return {(d.old, d.new) for d in decisions if d.candidate is not None}


def test_joint_solution_beats_greedy_choice():
    decisions = assign_candidates(
        [
            Candidate(0, 0, 0.95),
            Candidate(0, 1, 0.94),
            Candidate(1, 0, 0.93),
            Candidate(1, 1, 0.61),
        ],
        0.60,
    )
    assert chosen(decisions) == {(0, 1), (1, 0)}


def test_competition_resolved_by_other_match_has_large_assignment_margin():
    decisions = assign_candidates(
        [
            Candidate(0, 0, 0.90),
            Candidate(1, 0, 0.89),
            Candidate(1, 1, 0.99),
        ],
        0.60,
    )
    assert chosen(decisions) == {(0, 0), (1, 1)}
    first = next(d for d in decisions if d.new == 0)
    assert first.margin == pytest.approx(0.30)
    assert first.margin > 0.04  # The misleading local candidate margin was only .01.
    assert any(new == 0 and old is None for old, new, _ in first.alternatives)


def test_real_near_tied_swap_keeps_both_competing_pairs():
    decisions = assign_candidates(
        [
            Candidate(0, 0, 0.90),
            Candidate(1, 0, 0.89),
            Candidate(0, 1, 0.88),
            Candidate(1, 1, 0.89),
        ],
        0.60,
    )
    assert chosen(decisions) == {(0, 0), (1, 1)}
    assert all(d.margin == pytest.approx(0.02) for d in decisions)
    assert all(
        {(old, new) for old, new, _ in d.alternatives} == {(1, 0), (0, 1)} for d in decisions
    )


def test_below_threshold_candidates_are_not_forced_into_matches():
    assert assign_candidates([Candidate(0, 0, 0.59)], 0.60) == []
    decisions = assign_candidates([Candidate(0, 0, 0.90), Candidate(0, 1, 0.89)], 0.60)
    assert chosen(decisions) == {(0, 0)}
    unmatched = next(d for d in decisions if d.new == 1)
    assert unmatched.old is None
    assert unmatched.margin == pytest.approx(0.01)
    assert (0, 1) in {(old, new) for old, new, _ in unmatched.alternatives}


def test_unmatched_old_has_feasible_merge_counterfactual():
    decisions = assign_candidates([Candidate(0, 0, 0.90), Candidate(1, 0, 0.89)], 0.60)
    unmatched = next(d for d in decisions if d.old == 1)
    assert unmatched.new is None
    assert unmatched.margin == pytest.approx(0.01)
    assert (1, 0) in {(old, new) for old, new, _ in unmatched.alternatives}
