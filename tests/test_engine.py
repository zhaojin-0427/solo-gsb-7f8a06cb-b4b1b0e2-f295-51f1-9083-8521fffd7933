"""Unit tests for the evaluation engine."""

import pytest

from app.engine import (
    evaluate_cells,
    repair_cycles,
    CYCLE,
    SYNTAX,
    InvalidCellKey,
    MAX_REPAIR_FORMULAS,
    TooManyFormulas,
)


def ev(cells):
    return evaluate_cells(cells)


class TestBasicEvaluation:
    def test_integer_literals(self):
        assert ev({"A1": 1, "B1": 2}) == {"A1": 1, "B1": 2}

    def test_formula_with_literal(self):
        assert ev({"A1": "=1+2"}) == {"A1": 3}

    def test_precedence_and_parens(self):
        assert ev({"A1": "=2+3*4", "A2": "=(2+3)*4"}) == {
            "A1": 14,
            "A2": 20,
        }

    def test_unary_operators(self):
        assert ev(
            {"A1": -5, "A2": "=-A1", "A3": "=--A1", "A4": "=+A1*2"}
        ) == {"A1": -5, "A2": 5, "A3": -5, "A4": -10}

    def test_references(self):
        assert ev({"A1": 10, "B1": "=A1+5", "C1": "=B1*2"}) == {
            "A1": 10,
            "B1": 15,
            "C1": 30,
        }

    def test_unprovided_cell_is_zero(self):
        assert ev({"A1": "=Z99+1", "B1": "=Z99"}) == {
            "A1": 1,
            "B1": 0,
        }

    def test_subtraction_chain(self):
        assert ev({"A1": "=10-2-3"}) == {"A1": 5}

    def test_big_integers(self):
        big = 10**30
        out = ev({"A1": big, "B1": f"=A1+{big}"})
        assert out["A1"] == big
        assert out["B1"] == 2 * big

    def test_whitespace_in_formula(self):
        assert ev({"A1": "= 1 + 2 * ( -3 ) "}) == {"A1": -5}

    def test_lowercase_key_canonicalised(self):
        assert ev({"a1": "=1+1"}) == {"A1": 2}


class TestSum:
    def test_rectangular_sum(self):
        cells = {
            "A1": 1,
            "A2": 2,
            "A3": 3,
            "B1": 4,
            "B2": 5,
            "B3": 6,
            "C1": "=SUM(A1:B3)",
        }
        assert ev(cells)["C1"] == 21

    def test_sum_missing_cells_treated_as_zero(self):
        # Only A1 provided; A2, A3 and the B column are all zero.
        assert ev({"A1": 5, "D1": "=SUM(A1:B3)"}) == {
            "A1": 5,
            "D1": 5,
        }

    def test_sum_single_cell(self):
        assert ev({"A1": 9, "B1": "=SUM(A1:A1)"})["B1"] == 9

    def test_sum_reversed_corners(self):
        cells = {"B2": 7, "D4": "=SUM(B2:A1)"}
        assert ev(cells)["D4"] == 7

    def test_sum_with_other_arithmetic(self):
        cells = {
            "A1": 1,
            "A2": 2,
            "B1": "=SUM(A1:A2)*3+1",
        }
        assert ev(cells)["B1"] == 10

    def test_sum_overlapping_formulas(self):
        cells = {
            "A1": "=1",
            "A2": "=A1+1",
            "A3": "=SUM(A1:A2)",
        }
        assert ev(cells)["A3"] == 3

    def test_sum_wide_range(self):
        cells = {"A1": 1, "B1": 2, "C1": 3, "D1": "=SUM(A1:C1)"}
        assert ev(cells)["D1"] == 6


class TestSyntaxErrors:
    def test_bad_formula_cell(self):
        assert ev({"A1": "=1+"}) == {"A1": SYNTAX}

    def test_unknown_function(self):
        assert ev({"A1": "=AVG(A1:A2)"}) == {"A1": SYNTAX}

    def test_bad_reference_in_formula(self):
        assert ev({"A1": "=A0"})["A1"] == SYNTAX
        assert ev({"A1": "=AAAA1"})["A1"] == SYNTAX

    def test_syntax_propagates_to_referrers(self):
        cells = {
            "A1": "=1+",
            "B1": "=A1+1",
            "C1": "=B1*2",
        }
        assert ev(cells) == {"A1": SYNTAX, "B1": SYNTAX, "C1": SYNTAX}

    def test_syntax_in_sum_propagates_per_cell_not_range(self):
        # The malformed cell references a good cell; the good cell is fine.
        cells = {"A1": 5, "A2": "=SUM(", "B1": "=A2"}
        out = ev(cells)
        assert out == {"A1": 5, "A2": SYNTAX, "B1": SYNTAX}

    def test_reference_inside_bad_sum(self):
        # A SUM covering a syntax-broken cell: SUM's value includes that
        # cell, so the SUM cell itself is SYNTAX too.
        cells = {
            "A1": "=1+",
            "B1": "=SUM(A1:A1)",
            "C1": 8,
        }
        out = ev(cells)
        assert out == {"A1": SYNTAX, "B1": SYNTAX, "C1": 8}

    def test_unrelated_cell_unaffected(self):
        cells = {
            "A1": "=1+",
            "B1": "=A1",
            "C1": "=2+2",
            "D1": "=C1*2",
        }
        assert ev(cells) == {
            "A1": SYNTAX,
            "B1": SYNTAX,
            "C1": 4,
            "D1": 8,
        }

    def test_empty_formula(self):
        assert ev({"A1": "="}) == {"A1": SYNTAX}


class TestCycles:
    def test_self_cycle(self):
        assert ev({"A1": "=A1"}) == {"A1": CYCLE}

    def test_two_node_cycle(self):
        cells = {"A1": "=B1", "B1": "=A1"}
        assert ev(cells) == {"A1": CYCLE, "B1": CYCLE}

    def test_three_node_cycle(self):
        cells = {"A1": "=B1+1", "B1": "=C1", "C1": "=A1-1"}
        assert ev(cells) == {"A1": CYCLE, "B1": CYCLE, "C1": CYCLE}

    def test_dependent_of_cycle_gets_cycle(self):
        cells = {
            "A1": "=B1",
            "B1": "=A1",
            "C1": "=A1+1",
            "D1": "=C1*2",
        }
        assert ev(cells) == {
            "A1": CYCLE,
            "B1": CYCLE,
            "C1": CYCLE,
            "D1": CYCLE,
        }

    def test_unrelated_cell_unaffected_by_cycle(self):
        cells = {
            "A1": "=A1",
            "B1": "=1+1",
            "C1": "=B1+10",
        }
        assert ev(cells) == {"A1": CYCLE, "B1": 2, "C1": 12}

    def test_cycle_through_sum(self):
        cells = {
            "A1": "=SUM(B1:B1)",
            "B1": "=A1",
        }
        assert ev(cells) == {"A1": CYCLE, "B1": CYCLE}

    def test_sum_covering_cycle_cell(self):
        cells = {
            "A1": "=A1",
            "B1": "=SUM(A1:A2)",
            "C1": "=B1+1",
            "D1": 42,
        }
        assert ev(cells) == {
            "A1": CYCLE,
            "B1": CYCLE,
            "C1": CYCLE,
            "D1": 42,
        }

    def test_diamond_dag_is_not_a_cycle(self):
        # A1 -> B1, A1 -> C1, B1 -> D1, C1 -> D1 (no cycle).
        cells = {
            "A1": 1,
            "B1": "=A1+1",
            "C1": "=A1+2",
            "D1": "=B1+C1",
        }
        assert ev(cells)["D1"] == 5

    def test_cycle_node_also_points_outward(self):
        # A1 <-> B1 cycle; A1 also reads the healthy cell C1.
        cells = {
            "A1": "=B1+C1",
            "B1": "=A1",
            "C1": 5,
        }
        assert ev(cells) == {"A1": CYCLE, "B1": CYCLE, "C1": 5}


class TestSyntaxAndCycleInteraction:
    def test_syntax_takes_precedence_when_reaching_both(self):
        # C1 is a syntax error; A1<->B1 is a cycle; D1 depends on both.
        cells = {
            "A1": "=B1",
            "B1": "=A1",
            "C1": "=1+",
            "D1": "=B1+C1",
        }
        assert ev(cells) == {
            "A1": CYCLE,
            "B1": CYCLE,
            "C1": SYNTAX,
            "D1": SYNTAX,
        }


class TestOutputOrdering:
    def test_results_sorted_lexicographically(self):
        cells = {
            "B2": 1,
            "A10": 1,
            "A2": 1,
            "A1": 1,
            "AA1": 1,
        }
        assert list(ev(cells).keys()) == [
            "A1",
            "A10",
            "A2",
            "AA1",
            "B2",
        ]


class TestInvalidRequests:
    def test_bad_key_raises(self):
        from app.engine import InvalidCellKey

        with pytest.raises(InvalidCellKey):
            ev({"not-a-cell": 1})

    def test_duplicate_canonical_key_raises(self):
        # JSON objects cannot contain duplicate keys, but programmatic calls
        # may map 'a1' and 'A1' to the same canonical address.
        from app.engine import InvalidCellKey

        with pytest.raises(InvalidCellKey):
            ev({"a1": 1, "A1": 2})

    def test_unsupported_value_type_raises(self):
        from app.engine import InvalidCellKey

        with pytest.raises(InvalidCellKey):
            ev({"A1": 1.5})
        with pytest.raises(InvalidCellKey):
            ev({"A1": True})
        with pytest.raises(InvalidCellKey):
            ev({"A1": "bare string"})


class TestDeepChains:
    def test_long_dependency_chain(self):
        # Far longer than the default Python recursion limit would allow a
        # naive recursive evaluator to handle.
        n = 3000
        cells = {"A1": 1}
        for i in range(2, n + 1):
            cells[f"A{i}"] = f"=A{i-1}+1"
        out = ev(cells)
        assert out[f"A{n}"] == n

    def test_deeply_nested_parentheses(self):
        depth = 400
        formula = "(" * depth + "1" + ")" * depth
        assert ev({"A1": f"={formula}"}) == {"A1": 1}


class TestCycleRepair:
    def test_no_cycle_returns_empty(self):
        assert repair_cycles({}) == ([], 0)
        assert repair_cycles({"A1": 1, "B1": "=A1+1"}) == ([], 0)

    def test_acyclic_chain_through_sum(self):
        cells = {
            "A1": 1,
            "A2": 2,
            "B1": "=SUM(A1:A2)",
            "C1": "=B1+1",
        }
        assert repair_cycles(cells) == ([], 0)

    def test_self_cycle(self):
        assert repair_cycles({"A1": "=A1"}) == (["A1"], 1)

    def test_two_node_cycle_picks_smallest_address(self):
        assert repair_cycles({"A1": "=B1", "B1": "=A1"}) == (["A1"], 1)

    def test_three_node_cycle_picks_smallest_address(self):
        cells = {"A1": "=B1+1", "B1": "=C1", "C1": "=A1-1"}
        assert repair_cycles(cells) == (["A1"], 1)

    def test_disjoint_cycles_need_one_per_cycle(self):
        cells = {
            "A1": "=B1",
            "B1": "=A1",
            "C1": "=D1",
            "D1": "=C1",
        }
        assert repair_cycles(cells) == (["A1", "C1"], 2)

    def test_shared_node_breaks_two_cycles(self):
        # Two cycles B1<->A1 and B1<->C1: removing the shared B1 alone wins.
        cells = {
            "A1": "=B1",
            "B1": "=A1+C1",
            "C1": "=B1",
        }
        assert repair_cycles(cells) == (["B1"], 1)

    def test_replacing_formula_only_drops_outgoing_edges(self):
        # A1->B1->C1->B1: B1,C1 form the cycle; A1 points into it but is not
        # on it. Removing A1 must NOT break the cycle, so the answer is B1.
        cells = {"A1": "=B1", "B1": "=C1", "C1": "=B1"}
        assert repair_cycles(cells) == (["B1"], 1)

    def test_integer_cells_have_no_outgoing_edges(self):
        # A1 reads literal B1, but literals have no outgoing edge.
        assert repair_cycles({"A1": "=B1", "B1": 42}) == ([], 0)

    def test_missing_cells_have_no_outgoing_edges(self):
        assert repair_cycles({"A1": "=Z99+1"}) == ([], 0)

    def test_syntax_error_formulas_have_no_outgoing_edges(self):
        # The broken formula is a sink; nothing can cycle through it.
        cells = {"A1": "=B1", "B1": "=C1", "C1": "=1+"}
        assert repair_cycles(cells) == ([], 0)

    def test_self_cycle_through_sum_range(self):
        assert repair_cycles({"A1": "=SUM(A1:B1)", "B1": 5}) == (["A1"], 1)

    def test_cycle_through_sum_range_member(self):
        cells = {"A1": "=SUM(B1:B1)", "B1": "=A1"}
        assert repair_cycles(cells) == (["A1"], 1)

    def test_sum_range_counts_each_cell_for_edges(self):
        # A1 sums over B1:B2; B1 and B2 both point back at A1: two disjoint
        # cycles share A1, so replacing A1 alone fixes both.
        cells = {
            "A1": "=SUM(B1:B2)",
            "B1": "=A1",
            "B2": "=A1",
        }
        assert repair_cycles(cells) == (["A1"], 1)

    def test_lexicographic_tie_break_uses_sorted_address_list(self):
        # Two independent self-loops: the answer is the sorted pair itself.
        cells = {"B1": "=B1", "A1": "=A1", "C1": 3}
        assert repair_cycles(cells) == (["A1", "B1"], 2)

    def test_lexicographic_tie_break_uses_string_order(self):
        # String ordering makes "A10" < "A2", same as the result ordering.
        cells = {"A2": "=A10", "A10": "=A2"}
        assert repair_cycles(cells) == (["A10"], 1)

    def test_lexicographic_tie_break_compares_whole_list(self):
        # One cycle is {A9, B9}; the other is {A1, Z1}.  Two replacements are
        # needed; the smallest sorted pair is (A1, A9): first elements tie at
        # "A1" vs "A9" depending on which pairing... enumerate: pairings are
        # {A1|Z1} x {A9|B9} -> [A1,A9], [A1,B9], [A9,Z1], [B9,Z1]; smallest
        # is [A1, A9].
        cells = {
            "A1": "=Z1",
            "Z1": "=A1",
            "A9": "=B9",
            "B9": "=A9",
        }
        assert repair_cycles(cells) == (["A1", "A9"], 2)

    def test_minimum_count_prefers_fewer_replacements(self):
        # A simple 3-node directed cycle: any single vertex breaks it, and
        # no zero-vertex solution exists.
        cells = {
            "A1": "=B1",
            "B1": "=C1",
            "C1": "=A1",
        }
        assert repair_cycles(cells) == (["A1"], 1)

    def test_self_loops_demand_each_holder(self):
        cells = {f"A{i}": f"=A{i}+1" for i in range(1, 5)}
        addresses, count = repair_cycles(cells)
        assert count == 4
        assert addresses == [f"A{i}" for i in range(1, 5)]

    def test_lowercase_keys_canonicalised_in_output(self):
        assert repair_cycles({"a1": "=A1"}) == (["A1"], 1)

    def test_exactly_sixteen_valid_formulas_allowed(self):
        cells = {f"A{i}": f"=A{i}" for i in range(1, MAX_REPAIR_FORMULAS + 1)}
        addresses, count = repair_cycles(cells)
        assert count == MAX_REPAIR_FORMULAS

    def test_over_sixteen_valid_formulas_rejected(self):
        cells = {f"A{i}": "=1" for i in range(1, MAX_REPAIR_FORMULAS + 2)}
        with pytest.raises(TooManyFormulas):
            repair_cycles(cells)

    def test_non_formula_cells_do_not_count_toward_limit(self):
        # More than 16 provided cells, but only 16 valid formulas (and they are
        # acyclic); the rest are integers and a syntax-broken formula.
        cells = {f"A{i}": "=1" for i in range(1, MAX_REPAIR_FORMULAS + 1)}
        cells["A17"] = 0
        cells["A18"] = "=1+"
        assert repair_cycles(cells) == ([], 0)

    def test_invalid_key_still_rejected(self):
        with pytest.raises(InvalidCellKey):
            repair_cycles({"nope": 1})
