"""Unit tests for the goal-seek solver."""

import pytest

from app.engine import InvalidCellKey
from app.goal import GoalSeekError, goal_seek


def gs(cells, variable="A1", target="B1", desired=0):
    return goal_seek(cells, variable, target, desired)


class TestLinearForm:
    def test_simple_chain(self):
        # B1 = A1 + 1 -> 5 means A1 = 4.
        assert gs({"A1": 1, "B1": "=A1+1"}, desired=5) == ("unique", 4)

    def test_multi_hop_chain(self):
        # C1 = 2*A1 + 3.
        cells = {"A1": 1, "B1": "=A1*2", "C1": "=B1+3"}
        assert gs(cells, target="C1", desired=9) == ("unique", 3)

    def test_unary_minus_flips_coefficients(self):
        # B1 = -A1 + 3 -> 1 means A1 = 2.
        assert gs({"A1": 1, "B1": "=-A1+3"}, desired=1) == ("unique", 2)

    def test_unary_plus_keeps_coefficients(self):
        assert gs({"A1": 1, "B1": "=+A1"}, desired=7) == ("unique", 7)

    def test_subtraction(self):
        # B1 = 10 - A1 -> 4 means A1 = 6.
        assert gs({"A1": 1, "B1": "=10-A1"}, desired=4) == ("unique", 6)

    def test_multiplication_by_constant(self):
        assert gs({"A1": 1, "B1": "=A1*3"}, desired=9) == ("unique", 3)

    def test_constant_expression_times_variable(self):
        # B1 = (2+3) * (A1+1) = 5*A1 + 5.
        cells = {"A1": 1, "B1": "=(2+3)*(A1+1)"}
        assert gs(cells, desired=15) == ("unique", 2)

    def test_negative_solution(self):
        assert gs({"A1": 1, "B1": "=A1+10"}, desired=3) == ("unique", -7)

    def test_variable_mentioned_twice_adds_up(self):
        # B1 = A1 + A1 = 2*A1.
        assert gs({"A1": 1, "B1": "=A1+A1"}, desired=8) == ("unique", 4)

    def test_sum_range_counts_each_member(self):
        # B1 = A1 + A2 where A2 = 2 -> a=1, b=2.
        cells = {"A1": 1, "A2": 2, "B1": "=SUM(A1:A2)"}
        assert gs(cells, desired=10) == ("unique", 8)

    def test_sum_range_with_variable_inside_rectangle(self):
        # C1 = SUM(A1:A2) = A1 + 10 -> 21 means A1 = 11.
        cells = {"A1": 1, "A2": 10, "C1": "=SUM(A1:A2)"}
        assert gs(cells, target="C1", desired=21) == ("unique", 11)

    def test_missing_cells_are_zero(self):
        # B1 = A1 + Z9 + 5 with Z9 missing -> a=1, b=5.
        cells = {"A1": 1, "B1": "=A1+Z9+5"}
        assert gs(cells, desired=8) == ("unique", 3)

    def test_missing_cells_inside_sum_are_zero(self):
        # D1 = SUM(A1:C1) = A1 + 0 + 0 (B1, C1 missing).
        cells = {"A1": 1, "D1": "=SUM(A1:C1)"}
        assert gs(cells, target="D1", desired=6) == ("unique", 6)

    def test_variable_equal_target(self):
        # Solving "variable = desired" directly.
        assert goal_seek({"A1": 3}, "A1", "A1", 7) == ("unique", 7)

    def test_lowercase_addresses_canonicalised(self):
        assert goal_seek({"a1": 1, "b1": "=A1+1"}, "a1", "b1", 6) == (
            "unique",
            5,
        )

    def test_big_integers(self):
        big = 10**30
        cells = {"A1": 1, "B1": "=A1*3"}
        assert gs(cells, desired=3 * big) == ("unique", big)

    def test_deep_chain(self):
        n = 3000
        cells = {"A1": 0}
        for i in range(2, n + 1):
            cells[f"A{i}"] = f"=A{i-1}+1"
        # An = A1 + (n-1); solve An = 10 * n -> A1 = 10*n - (n-1).
        assert goal_seek(cells, "A1", f"A{n}", 10 * n) == (
            "unique",
            9 * n + 1,
        )


class TestStatuses:
    def test_not_divisible_is_none(self):
        assert gs({"A1": 1, "B1": "=A1*2"}, desired=3) == ("none", None)

    def test_not_divisible_negative_coefficient(self):
        # -2*A1 = 3 has no integer solution.
        assert gs({"A1": 1, "B1": "=A1*-2"}, desired=3) == ("none", None)

    def test_divisible_negative_coefficient(self):
        # -2*A1 = 6 -> A1 = -3.
        assert gs({"A1": 1, "B1": "=A1*-2"}, desired=6) == ("unique", -3)

    def test_target_independent_of_variable_matching_is_any(self):
        cells = {"A1": 5, "B1": 10}
        assert gs(cells, desired=10) == ("any", None)

    def test_target_independent_of_variable_mismatch_is_none(self):
        cells = {"A1": 5, "B1": 10}
        assert gs(cells, desired=11) == ("none", None)

    def test_cancelled_coefficient_is_any_when_already_desired(self):
        # B1 = A1 - A1 = 0 whatever A1 is.
        cells = {"A1": 1, "B1": "=A1-A1"}
        assert gs(cells, desired=0) == ("any", None)
        assert gs(cells, desired=1) == ("none", None)

    def test_zero_times_variable_stays_constant(self):
        # B1 = (A1-A1) * C1 is constantly 0 even though C1 depends on A1.
        cells = {"A1": 1, "C1": "=A1+1", "B1": "=(A1-A1)*C1"}
        assert gs(cells, desired=0) == ("any", None)


class TestNonLinear:
    def test_variable_times_variable_rejected(self):
        with pytest.raises(GoalSeekError):
            gs({"A1": 1, "B1": "=A1*A1"}, desired=4)

    def test_two_dependent_cells_multiplied_rejected(self):
        cells = {"A1": 1, "B1": "=A1+1", "C1": "=A1*B1"}
        with pytest.raises(GoalSeekError):
            gs(cells, target="C1", desired=4)

    def test_nonlinear_off_chain_is_ignored(self):
        # C1 is non-linear in A1 but the target B1 does not use it.
        cells = {"A1": 1, "B1": "=A1+1", "C1": "=A1*A1"}
        assert gs(cells, desired=4) == ("unique", 3)


class TestChainErrors:
    def test_syntax_on_chain_rejected(self):
        cells = {"A1": 1, "B1": "=1+", "C1": "=A1+B1"}
        with pytest.raises(GoalSeekError):
            gs(cells, target="C1", desired=4)

    def test_syntax_off_chain_ignored(self):
        cells = {"A1": 1, "B1": "=1+", "C1": "=A1+1"}
        assert gs(cells, target="C1", desired=5) == ("unique", 4)

    def test_cycle_on_chain_rejected(self):
        cells = {"A1": 1, "B1": "=C1", "C1": "=B1", "D1": "=A1+B1"}
        with pytest.raises(GoalSeekError):
            gs(cells, target="D1", desired=4)

    def test_cycle_off_chain_ignored(self):
        cells = {"A1": 1, "B1": "=B1", "C1": "=A1+2"}
        assert gs(cells, target="C1", desired=7) == ("unique", 5)

    def test_cycle_inside_sum_range_rejected(self):
        cells = {"V1": 1, "A1": "=A1", "B1": "=SUM(A1:A2)"}
        with pytest.raises(GoalSeekError):
            gs(cells, variable="V1", target="B1", desired=4)

    def test_syntax_inside_sum_range_rejected(self):
        cells = {"V1": 1, "A1": "=1+", "B1": "=SUM(A1:A2)"}
        with pytest.raises(GoalSeekError):
            gs(cells, variable="V1", target="B1", desired=4)


class TestInvalidRequests:
    def test_variable_must_be_provided(self):
        with pytest.raises(GoalSeekError):
            gs({"B1": 1}, desired=1)

    def test_variable_must_not_be_a_formula(self):
        with pytest.raises(GoalSeekError):
            gs({"A1": "=1", "B1": 2}, desired=2)

    def test_variable_must_not_be_a_broken_formula(self):
        with pytest.raises(GoalSeekError):
            gs({"A1": "=1+", "B1": 2}, desired=2)

    def test_target_must_be_provided(self):
        with pytest.raises(GoalSeekError):
            gs({"A1": 1}, target="B1", desired=1)

    def test_invalid_variable_address(self):
        with pytest.raises(GoalSeekError):
            gs({"A1": 1, "B1": 2}, variable="nope", desired=2)

    def test_invalid_target_address(self):
        with pytest.raises(GoalSeekError):
            gs({"A1": 1, "B1": 2}, target="A0", desired=2)

    def test_bad_cell_key_raises(self):
        with pytest.raises(InvalidCellKey):
            gs({"nope": 1, "A1": 1}, desired=1)

    def test_cells_validation_shared_with_evaluate(self):
        for bad in ({"A1": 1.5}, {"A1": True}, {"A1": "bare"}):
            with pytest.raises(InvalidCellKey):
                gs(bad, desired=1)
