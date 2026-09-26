"""Unit and end-to-end tests for the integer goal-seek feature."""

import pytest
from fastapi.testclient import TestClient

from app.engine import InvalidCellKey, evaluate_cells
from app.goalseek import (
    STATUS_ANY,
    STATUS_NONE,
    STATUS_UNIQUE,
    GoalSeekError,
    goal_seek,
)
from app.main import app

client = TestClient(app)


def solve(cells, variable, target, desired):
    return goal_seek(cells, variable, target, desired)


def verify_solution(cells, variable, target, desired, value):
    """Plug ``value`` into the table and check the target really equals it."""
    adjusted = dict(cells)
    adjusted[variable] = value
    assert evaluate_cells(adjusted)[target] == desired


class TestUniqueSolutions:
    def test_simple_linear_chain(self):
        cells = {"A1": 0, "B1": "=2*A1+3"}
        assert solve(cells, "A1", "B1", 11) == (STATUS_UNIQUE, 4)

    def test_solution_actually_drives_target(self):
        cells = {
            "A1": 0,
            "A2": 10,
            "B1": "=A1-1",
            "C1": "=SUM(A1:A3)+B1",
        }
        status, value = solve(cells, "A1", "C1", 19)
        assert status == STATUS_UNIQUE
        verify_solution(cells, "A1", "C1", 19, value)

    def test_negative_coefficient(self):
        # target = 3 - x; desired -4 -> x = 7
        assert solve({"A1": 0, "B1": "=3-A1"}, "A1", "B1", -4) == (
            STATUS_UNIQUE,
            7,
        )

    def test_negative_solution(self):
        assert solve({"A1": 0, "B1": "=-A1"}, "A1", "B1", 7) == (
            STATUS_UNIQUE,
            -7,
        )

    def test_unary_double_negation(self):
        assert solve({"A1": 2, "B1": "=--A1"}, "A1", "B1", 8) == (
            STATUS_UNIQUE,
            8,
        )

    def test_unary_plus(self):
        assert solve({"A1": 2, "B1": "=+A1"}, "A1", "B1", -3) == (
            STATUS_UNIQUE,
            -3,
        )

    def test_subtraction_only(self):
        assert solve({"A1": 2, "B1": "=A1-5"}, "A1", "B1", 0) == (
            STATUS_UNIQUE,
            5,
        )

    def test_parenthesised_form(self):
        # target = (x + 2) * 3 = 3x + 6; desired 21 -> x = 5
        assert solve({"A1": 0, "B1": "=(A1+2)*3"}, "A1", "B1", 21) == (
            STATUS_UNIQUE,
            5,
        )

    def test_sum_range(self):
        cells = {"A1": 0, "A2": 10, "A3": 20, "B1": "=SUM(A1:A3)"}
        status, value = solve(cells, "A1", "B1", 35)
        assert (status, value) == (STATUS_UNIQUE, 5)
        verify_solution(cells, "A1", "B1", 35, value)

    def test_sum_reversed_corners(self):
        assert solve({"A2": 1, "A1": 0, "B1": "=SUM(A2:A1)"}, "A1", "B1", 7) == (
            STATUS_UNIQUE,
            6,
        )

    def test_sum_with_missing_members(self):
        # Only A1 (=x) provided inside A1:B2; every other member is zero.
        assert solve({"A1": 9, "C1": "=SUM(A1:B2)"}, "A1", "C1", 3) == (
            STATUS_UNIQUE,
            3,
        )

    def test_variable_inside_sum_with_other_arithmetic(self):
        # B1 = x+1; C1 = x + B1 - 3 = 2x - 2; desired 8 -> x = 5
        cells = {"A1": 0, "B1": "=A1+1", "C1": "=A1+B1-3"}
        status, value = solve(cells, "A1", "C1", 8)
        assert (status, value) == (STATUS_UNIQUE, 5)
        verify_solution(cells, "A1", "C1", 8, value)

    def test_diamond_dependency(self):
        # B1 = x+1 used twice: B1*2+B1 = 3x+3; desired 12 -> x = 3
        cells = {"A1": 0, "B1": "=A1+1", "C1": "=B1*2+B1"}
        status, value = solve(cells, "A1", "C1", 12)
        assert (status, value) == (STATUS_UNIQUE, 3)
        verify_solution(cells, "A1", "C1", 12, value)

    def test_multiplication_constant_left(self):
        assert solve({"A1": 0, "B1": "=3*A1+1"}, "A1", "B1", 10) == (
            STATUS_UNIQUE,
            3,
        )

    def test_multiplication_constant_right(self):
        assert solve({"A1": 0, "B1": "=A1*3+1"}, "A1", "B1", 10) == (
            STATUS_UNIQUE,
            3,
        )

    def test_missing_reference_is_zero(self):
        assert solve({"A1": 0, "B1": "=A1+Z9"}, "A1", "B1", 5) == (
            STATUS_UNIQUE,
            5,
        )

    def test_variable_equals_target(self):
        assert solve({"A1": 100}, "A1", "A1", 33) == (STATUS_UNIQUE, 33)

    def test_supplied_variable_value_ignored(self):
        # The variable is x itself, no matter which integer was submitted.
        assert solve({"A1": 100, "B1": "=A1+1"}, "A1", "B1", 2) == (
            STATUS_UNIQUE,
            1,
        )

    def test_long_dependency_chain(self):
        n = 3000
        cells = {"A1": 0}
        for i in range(2, n + 1):
            cells[f"A{i}"] = f"=A{i-1}+1"
        status, value = solve(cells, "A1", f"A{n}", 2 * n)
        assert (status, value) == (STATUS_UNIQUE, n + 1)

    def test_big_integers(self):
        big = 10**40
        assert solve({"A1": 0, "B1": "=A1+1"}, "A1", "B1", big) == (
            STATUS_UNIQUE,
            big - 1,
        )

    def test_addresses_canonicalised(self):
        assert solve({"a1": 0, "b1": "=A1+1"}, "a1", "b1", 5) == (
            STATUS_UNIQUE,
            4,
        )


class TestNoneAndAny:
    def test_not_divisible_returns_none(self):
        # 2x + 3 = 10 -> 7/2 is not integral
        assert solve({"A1": 0, "B1": "=A1*2+3"}, "A1", "B1", 10) == (
            STATUS_NONE,
            None,
        )

    def test_negative_divisor_not_divisible(self):
        # -x = 1 -> -1 divides 1, so this is unique; pick -2x = 1 -> none
        assert solve({"A1": 0, "B1": "=-2*A1"}, "A1", "B1", 1) == (
            STATUS_NONE,
            None,
        )

    def test_constant_target_matches_returns_any(self):
        assert solve({"A1": 5, "B1": "=42"}, "A1", "B1", 42) == (
            STATUS_ANY,
            None,
        )

    def test_constant_target_differs_returns_none(self):
        assert solve({"A1": 5, "B1": "=42"}, "A1", "B1", 41) == (
            STATUS_NONE,
            None,
        )

    def test_literal_target_matches_returns_any(self):
        assert solve({"A1": 0, "B1": 5}, "A1", "B1", 5) == (STATUS_ANY, None)

    def test_literal_target_differs_returns_none(self):
        assert solve({"A1": 0, "B1": 5}, "A1", "B1", 6) == (STATUS_NONE, None)

    def test_variable_cancelled_by_zero_multiply_is_any(self):
        # (x + 2) * 0 = 0 regardless of x
        assert solve({"A1": 2, "B1": "=(A1+2)*0"}, "A1", "B1", 0) == (
            STATUS_ANY,
            None,
        )

    def test_variable_cancelled_but_constant_differs_is_none(self):
        assert solve({"A1": 2, "B1": "=A1*0+1"}, "A1", "B1", 2) == (
            STATUS_NONE,
            None,
        )

    def test_zero_solution_is_unique(self):
        assert solve({"A1": 9, "B1": "=A1+4"}, "A1", "B1", 4) == (
            STATUS_UNIQUE,
            0,
        )


class TestRequestErrors:
    @pytest.mark.parametrize(
        "cells,variable,target",
        [
            ({"A1": "=1", "B1": "=A1"}, "A1", "B1"),
            ({"A1": "=1+", "B1": "=1"}, "A1", "B1"),
        ],
    )
    def test_variable_formula_raises(self, cells, variable, target):
        with pytest.raises(GoalSeekError):
            goal_seek(cells, variable, target, 10)

    def test_variable_missing_raises(self):
        with pytest.raises(GoalSeekError):
            goal_seek({"B1": "=A1"}, "A1", "B1", 10)

    def test_target_missing_raises(self):
        with pytest.raises(GoalSeekError):
            goal_seek({"A1": 1}, "A1", "B1", 10)

    @pytest.mark.parametrize("address", ["nope", "A0", "AAAA1", ""])
    def test_invalid_variable_address_raises(self, address):
        with pytest.raises(GoalSeekError):
            goal_seek({"A1": 1, "B1": 1}, address, "B1", 10)

    @pytest.mark.parametrize("address", ["nope", "A0", "AAAA1", ""])
    def test_invalid_target_address_raises(self, address):
        with pytest.raises(GoalSeekError):
            goal_seek({"A1": 1, "B1": 1}, "A1", address, 10)

    def test_invalid_cell_key_still_raises(self):
        with pytest.raises(InvalidCellKey):
            goal_seek({"nope": 1}, "A1", "B1", 10)

    def test_target_syntax_error_raises(self):
        with pytest.raises(GoalSeekError):
            goal_seek({"A1": 1, "B1": "=1+"}, "A1", "B1", 10)

    def test_transitive_syntax_error_raises(self):
        cells = {"A1": 1, "B1": "=1+", "C1": "=B1+1", "D1": "=C1*2"}
        with pytest.raises(GoalSeekError):
            goal_seek(cells, "A1", "D1", 10)

    def test_syntax_inside_sum_member_raises(self):
        cells = {"A1": 1, "B1": "=1+", "C1": "=SUM(A1:B1)"}
        with pytest.raises(GoalSeekError):
            goal_seek(cells, "A1", "C1", 10)

    def test_target_self_cycle_raises(self):
        with pytest.raises(GoalSeekError):
            goal_seek({"A1": 0, "B1": "=B1"}, "A1", "B1", 10)

    def test_transitive_cycle_raises(self):
        cells = {"A1": 0, "B1": "=C1", "C1": "=B1", "D1": "=C1+1"}
        with pytest.raises(GoalSeekError):
            goal_seek(cells, "A1", "D1", 10)

    def test_cycle_through_sum_raises(self):
        cells = {"A1": 0, "C1": "=SUM(B1:B1)", "B1": "=C1+A1"}
        with pytest.raises(GoalSeekError):
            goal_seek(cells, "A1", "C1", 10)

    def test_both_multiply_operands_depend_on_variable_raises(self):
        with pytest.raises(GoalSeekError):
            goal_seek({"A1": 0, "B1": "=A1*A1"}, "A1", "B1", 10)

    def test_nested_both_side_multiplication_raises(self):
        with pytest.raises(GoalSeekError):
            goal_seek({"A1": 0, "B1": "=(A1+1)*(A1+2)"}, "A1", "B1", 10)

    def test_diamond_into_multiplication_raises(self):
        # B1 = x+1; B1*B1 is quadratic.
        cells = {"A1": 0, "B1": "=A1+1", "C1": "=B1*B1"}
        with pytest.raises(GoalSeekError):
            goal_seek(cells, "A1", "C1", 10)


class TestErrorsOffChainIgnored:
    def test_syntax_error_off_chain(self):
        cells = {"A1": 0, "B1": "=A1+1", "C1": "=1+"}
        assert solve(cells, "A1", "B1", 5) == (STATUS_UNIQUE, 4)

    def test_cycle_off_chain(self):
        cells = {"A1": 0, "B1": "=A1+1", "D1": "=E1", "E1": "=D1"}
        assert solve(cells, "A1", "B1", 5) == (STATUS_UNIQUE, 4)

    def test_both_kinds_off_chain(self):
        cells = {
            "A1": 0,
            "B1": "=A1*2",
            "C1": "=1+",
            "D1": "=D1",
        }
        assert solve(cells, "A1", "B1", 8) == (STATUS_UNIQUE, 4)


class TestGoalSeekAPI:
    def test_unique(self):
        resp = client.post(
            "/goal-seek",
            json={
                "cells": {"A1": 0, "B1": "=2*A1+3"},
                "variable": "A1",
                "target": "B1",
                "desired": 11,
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "unique", "value": 4}

    def test_none_not_divisible(self):
        resp = client.post(
            "/goal-seek",
            json={
                "cells": {"A1": 0, "B1": "=2*A1+3"},
                "variable": "A1",
                "target": "B1",
                "desired": 10,
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "none", "value": None}

    def test_any(self):
        resp = client.post(
            "/goal-seek",
            json={
                "cells": {"A1": 5, "B1": "=42"},
                "variable": "A1",
                "target": "B1",
                "desired": 42,
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "any", "value": None}

    def test_sum_example(self):
        resp = client.post(
            "/goal-seek",
            json={
                "cells": {
                    "A1": 0,
                    "A2": 10,
                    "B1": "=A1-1",
                    "C1": "=SUM(A1:A3)+B1",
                },
                "variable": "A1",
                "target": "C1",
                "desired": 19,
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "unique", "value": 5}

    def test_variable_formula_422(self):
        resp = client.post(
            "/goal-seek",
            json={
                "cells": {"A1": "=1", "B1": "=A1"},
                "variable": "A1",
                "target": "B1",
                "desired": 10,
            },
        )
        assert resp.status_code == 422

    def test_missing_cell_422(self):
        resp = client.post(
            "/goal-seek",
            json={
                "cells": {"B1": "=A1"},
                "variable": "A1",
                "target": "B1",
                "desired": 10,
            },
        )
        assert resp.status_code == 422

    def test_invalid_address_422(self):
        resp = client.post(
            "/goal-seek",
            json={
                "cells": {"A1": 1, "B1": 1},
                "variable": "nope",
                "target": "B1",
                "desired": 10,
            },
        )
        assert resp.status_code == 422

    def test_target_syntax_422(self):
        resp = client.post(
            "/goal-seek",
            json={
                "cells": {"A1": 1, "B1": "=1+"},
                "variable": "A1",
                "target": "B1",
                "desired": 10,
            },
        )
        assert resp.status_code == 422

    def test_target_cycle_422(self):
        resp = client.post(
            "/goal-seek",
            json={
                "cells": {"A1": 0, "B1": "=B1"},
                "variable": "A1",
                "target": "B1",
                "desired": 10,
            },
        )
        assert resp.status_code == 422

    def test_non_linear_422(self):
        resp = client.post(
            "/goal-seek",
            json={
                "cells": {"A1": 0, "B1": "=A1*A1"},
                "variable": "A1",
                "target": "B1",
                "desired": 10,
            },
        )
        assert resp.status_code == 422

    def test_off_chain_errors_do_not_block(self):
        resp = client.post(
            "/goal-seek",
            json={
                "cells": {
                    "A1": 0,
                    "B1": "=A1+1",
                    "C1": "=1+",
                    "D1": "=D1",
                },
                "variable": "A1",
                "target": "B1",
                "desired": 5,
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "unique", "value": 4}

    def test_shared_cell_validation_422(self):
        base = {"variable": "A1", "target": "B1", "desired": 1}
        for bad in (
            {"A1": 1.5},
            {"A1": True},
            {"A1": "bare"},
            {"nope": 1},
        ):
            resp = client.post("/goal-seek", json={**base, "cells": bad})
            assert resp.status_code == 422

    def test_desired_must_be_integer(self):
        payload = {
            "cells": {"A1": 0, "B1": "=A1"},
            "variable": "A1",
            "target": "B1",
            "desired": 1.5,
        }
        assert client.post("/goal-seek", json=payload).status_code == 422
        payload["desired"] = True
        assert client.post("/goal-seek", json=payload).status_code == 422

    @pytest.mark.parametrize("missing", ["variable", "target", "desired"])
    def test_missing_fields_422(self, missing):
        payload = {
            "cells": {"A1": 0, "B1": "=A1"},
            "variable": "A1",
            "target": "B1",
            "desired": 1,
        }
        del payload[missing]
        assert client.post("/goal-seek", json=payload).status_code == 422

    def test_openapi_documents_endpoint(self):
        paths = client.get("/openapi.json").json()["paths"]
        assert "/goal-seek" in paths
