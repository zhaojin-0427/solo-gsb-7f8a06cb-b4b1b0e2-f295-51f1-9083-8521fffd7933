"""End-to-end tests for the FastAPI endpoints."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_endpoints():
    assert client.get("/").json()["status"] == "ok"
    assert client.get("/healthz").json() == {"status": "ok"}


def test_evaluate_simple():
    resp = client.post(
        "/evaluate",
        json={
            "cells": {
                "A1": 2,
                "A2": 3,
                "A3": "=A1+A2*2",
                "A4": "=SUM(A1:A3)",
            }
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"results": {"A1": 2, "A2": 3, "A3": 8, "A4": 13}}


def test_empty_cells():
    resp = client.post("/evaluate", json={"cells": {}})
    assert resp.status_code == 200
    assert resp.json() == {"results": {}}


def test_missing_cells_body_defaults_to_empty():
    resp = client.post("/evaluate", json={})
    assert resp.status_code == 200
    assert resp.json() == {"results": {}}


def test_cycle_and_syntax_codes():
    resp = client.post(
        "/evaluate",
        json={
            "cells": {
                "A1": "=B1",
                "B1": "=A1",
                "C1": "=1+",
                "D1": "=C1",
                "E1": "=40+2",
            }
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "results": {
            "A1": "CYCLE",
            "B1": "CYCLE",
            "C1": "SYNTAX",
            "D1": "SYNTAX",
            "E1": 42,
        }
    }


def test_unprovided_reference_is_zero():
    resp = client.post(
        "/evaluate",
        json={"cells": {"A1": "=Z99+5", "B1": "=SUM(X1:X5)"}},
    )
    assert resp.status_code == 200
    assert resp.json() == {"results": {"A1": 5, "B1": 0}}


def test_result_keys_sorted():
    resp = client.post(
        "/evaluate",
        json={"cells": {"B1": 1, "A2": 2, "A1": 3}},
    )
    assert list(resp.json()["results"].keys()) == ["A1", "A2", "B1"]


def test_invalid_cell_key_rejected():
    resp = client.post("/evaluate", json={"cells": {"nope": 1}})
    assert resp.status_code == 422


def test_non_formula_string_rejected():
    resp = client.post("/evaluate", json={"cells": {"A1": "hello"}})
    assert resp.status_code == 422


def test_float_value_rejected():
    resp = client.post("/evaluate", json={"cells": {"A1": 1.5}})
    assert resp.status_code == 422


def test_boolean_value_rejected():
    resp = client.post("/evaluate", json={"cells": {"A1": True}})
    assert resp.status_code == 422


def test_no_code_execution():
    # Input that would be catastrophic under eval(): it is just an invalid
    # formula and must yield SYNTAX without any side effect.
    resp = client.post(
        "/evaluate",
        json={"cells": {"A1": "__import__('os').system('echo pwned')"}},
    )
    # Not starting with '=' -> request validation error.
    assert resp.status_code == 422

    resp = client.post(
        "/evaluate",
        json={"cells": {"A1": "=__import__('os')"}},
    )
    assert resp.status_code == 200
    assert resp.json()["results"]["A1"] == "SYNTAX"


def test_openapi_available():
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert "/evaluate" in paths
    assert "/cycle-repair" in paths


class TestCycleRepair:
    def test_no_cycle(self):
        resp = client.post(
            "/cycle-repair",
            json={"cells": {"A1": 1, "B1": "=A1+1"}},
        )
        assert resp.status_code == 200
        assert resp.json() == {"replace_with_zero": [], "minimum_count": 0}

    def test_empty_cells(self):
        resp = client.post("/cycle-repair", json={"cells": {}})
        assert resp.status_code == 200
        assert resp.json() == {"replace_with_zero": [], "minimum_count": 0}

    def test_missing_body_defaults_to_empty(self):
        resp = client.post("/cycle-repair", json={})
        assert resp.status_code == 200
        assert resp.json() == {"replace_with_zero": [], "minimum_count": 0}

    def test_two_node_cycle(self):
        resp = client.post(
            "/cycle-repair",
            json={"cells": {"A1": "=B1", "B1": "=A1"}},
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "replace_with_zero": ["A1"],
            "minimum_count": 1,
        }

    def test_disjoint_cycles(self):
        resp = client.post(
            "/cycle-repair",
            json={
                "cells": {
                    "A1": "=B1",
                    "B1": "=A1",
                    "C1": "=D1",
                    "D1": "=C1",
                }
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "replace_with_zero": ["A1", "C1"],
            "minimum_count": 2,
        }

    def test_syntax_errors_and_missing_cells_have_no_edges(self):
        resp = client.post(
            "/cycle-repair",
            json={
                "cells": {
                    "A1": "=B1+Z9",
                    "B1": "=1+",
                    "C1": 8,
                }
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"replace_with_zero": [], "minimum_count": 0}

    def test_cycle_through_sum_range(self):
        resp = client.post(
            "/cycle-repair",
            json={"cells": {"A1": "=SUM(B1:B1)", "B1": "=A1"}},
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "replace_with_zero": ["A1"],
            "minimum_count": 1,
        }

    def test_addresses_sorted_ascending(self):
        # Two disjoint cycles; keys arrive scrambled/lowercase (formula text
        # references themselves stay uppercase, as the grammar requires).
        resp = client.post(
            "/cycle-repair",
            json={
                "cells": {
                    "d1": "=C1",
                    "C1": "=D1",
                    "a1": "=B1",
                    "B1": "=A1",
                }
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["minimum_count"] == 2
        assert body["replace_with_zero"] == ["A1", "C1"]

    def test_too_many_valid_formulas_returns_422(self):
        resp = client.post(
            "/cycle-repair",
            json={"cells": {f"A{i}": "=1" for i in range(1, 18)}},
        )
        assert resp.status_code == 422

    def test_limit_ignores_integers_and_bad_formulas(self):
        cells = {f"A{i}": "=1" for i in range(1, 17)}
        cells["A17"] = 5
        cells["A18"] = "=syntax"
        resp = client.post("/cycle-repair", json={"cells": cells})
        assert resp.status_code == 200

    def test_invalid_cell_key_returns_422(self):
        resp = client.post(
            "/cycle-repair", json={"cells": {"nope": 1}}
        )
        assert resp.status_code == 422

    def test_validation_rules_shared_with_evaluate(self):
        for bad in (
            {"A1": 1.5},
            {"A1": True},
            {"A1": "bare string"},
        ):
            resp = client.post("/cycle-repair", json={"cells": bad})
            assert resp.status_code == 422


class TestGoalSeek:
    def _post(self, payload):
        return client.post("/goal-seek", json=payload)

    def test_unique_solution(self):
        resp = self._post(
            {
                "cells": {"A1": 1, "B1": "=A1*2", "C1": "=SUM(A1:B1)+1"},
                "variable": "A1",
                "target": "C1",
                "desired": 10,
            }
        )
        assert resp.status_code == 200
        # C1 = 3*A1 + 1 = 10 -> A1 = 3.
        assert resp.json() == {"status": "unique", "value": 3}

    def test_none_status(self):
        resp = self._post(
            {
                "cells": {"A1": 1, "B1": "=A1*2"},
                "variable": "A1",
                "target": "B1",
                "desired": 3,
            }
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "none", "value": None}

    def test_any_status(self):
        resp = self._post(
            {
                "cells": {"A1": 5, "B1": 10},
                "variable": "A1",
                "target": "B1",
                "desired": 10,
            }
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "any", "value": None}

    def test_lowercase_addresses_canonicalised(self):
        resp = self._post(
            {
                "cells": {"a1": 1, "b1": "=A1+1"},
                "variable": "a1",
                "target": "b1",
                "desired": 6,
            }
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "unique", "value": 5}

    def test_off_chain_errors_do_not_block_solving(self):
        resp = self._post(
            {
                "cells": {
                    "A1": 1,
                    "B1": "=A1+1",
                    "C1": "=1+",
                    "D1": "=D1",
                },
                "variable": "A1",
                "target": "B1",
                "desired": 5,
            }
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "unique", "value": 4}

    def test_syntax_on_chain_returns_422(self):
        resp = self._post(
            {
                "cells": {"A1": 1, "B1": "=1+", "C1": "=A1+B1"},
                "variable": "A1",
                "target": "C1",
                "desired": 4,
            }
        )
        assert resp.status_code == 422

    def test_cycle_on_chain_returns_422(self):
        resp = self._post(
            {
                "cells": {"A1": 1, "B1": "=C1", "C1": "=B1", "D1": "=A1+B1"},
                "variable": "A1",
                "target": "D1",
                "desired": 4,
            }
        )
        assert resp.status_code == 422

    def test_nonlinear_target_returns_422(self):
        resp = self._post(
            {
                "cells": {"A1": 1, "B1": "=A1*A1"},
                "variable": "A1",
                "target": "B1",
                "desired": 4,
            }
        )
        assert resp.status_code == 422

    def test_variable_must_be_integer_cell(self):
        for cells in ({"A1": "=1", "B1": 2}, {"A1": "=1+", "B1": 2}):
            resp = self._post(
                {
                    "cells": cells,
                    "variable": "A1",
                    "target": "B1",
                    "desired": 2,
                }
            )
            assert resp.status_code == 422

    def test_missing_variable_or_target_returns_422(self):
        resp = self._post(
            {
                "cells": {"B1": 1},
                "variable": "A1",
                "target": "B1",
                "desired": 1,
            }
        )
        assert resp.status_code == 422
        resp = self._post(
            {
                "cells": {"A1": 1},
                "variable": "A1",
                "target": "B1",
                "desired": 1,
            }
        )
        assert resp.status_code == 422

    def test_invalid_addresses_return_422(self):
        base = {"cells": {"A1": 1, "B1": 2}, "desired": 2}
        resp = self._post({**base, "variable": "nope", "target": "B1"})
        assert resp.status_code == 422
        resp = self._post({**base, "variable": "A1", "target": "A0"})
        assert resp.status_code == 422
        resp = self._post(
            {
                "cells": {"nope": 1, "A1": 1},
                "variable": "A1",
                "target": "A1",
                "desired": 1,
            }
        )
        assert resp.status_code == 422

    def test_desired_must_be_an_integer(self):
        base = {
            "cells": {"A1": 1, "B1": "=A1"},
            "variable": "A1",
            "target": "B1",
        }
        for bad in (True, 1.5, "3", None):
            resp = self._post({**base, "desired": bad})
            assert resp.status_code == 422

    def test_missing_fields_return_422(self):
        cells = {"A1": 1, "B1": "=A1"}
        resp = self._post(
            {"cells": cells, "variable": "A1", "target": "B1"}
        )
        assert resp.status_code == 422
        resp = self._post(
            {"cells": cells, "variable": "A1", "desired": 1}
        )
        assert resp.status_code == 422
        resp = self._post({"cells": cells, "target": "B1", "desired": 1})
        assert resp.status_code == 422

    def test_cells_validation_shared_with_evaluate(self):
        for bad in (
            {"A1": 1.5},
            {"A1": True},
            {"A1": "bare string"},
        ):
            resp = self._post(
                {
                    "cells": bad,
                    "variable": "A1",
                    "target": "A1",
                    "desired": 1,
                }
            )
            assert resp.status_code == 422

    def test_openapi_lists_goal_seek(self):
        paths = client.get("/openapi.json").json()["paths"]
        assert "/goal-seek" in paths
