"""Unit and end-to-end tests for the relative formula copy feature."""

import pytest
from fastapi.testclient import TestClient

from app.fill import fill_formula, FillFormulaError
from app.main import app

client = TestClient(app)


class TestFillUnit:
    def test_shift_basic_references(self):
        cells = {"A1": "=B1+1"}
        out = fill_formula(cells, "A1", ["A2", "A3"])
        assert out == {"A1": "=B1+1", "A2": "=B2+1", "A3": "=B3+1"}

    def test_shift_columns_and_rows(self):
        cells = {"C3": "=A1"}
        out = fill_formula(cells, "C3", ["D4"])
        assert out == {"C3": "=A1", "D4": "=B2"}

    def test_sum_range_both_endpoints_shifted(self):
        cells = {"C1": "=SUM(A1:B2)+1"}
        out = fill_formula(cells, "C1", ["C2", "D1"])
        assert out["C2"] == "=SUM(A2:B3)+1"
        assert out["D1"] == "=SUM(B1:C2)+1"

    def test_reversed_range_endpoints_keep_positions(self):
        cells = {"D4": "=SUM(B2:A1)"}
        out = fill_formula(cells, "D4", ["E5"])
        assert out["E5"] == "=SUM(C3:B2)"

    def test_numbers_operators_whitespace_kept_verbatim(self):
        cells = {"A1": "= 1 + SUM( A1 : B2 ) * -3 "}
        out = fill_formula(cells, "A1", ["B3"])
        assert out["B3"] == "= 1 + SUM( B3 : C4 ) * -3 "

    def test_formula_without_references_is_copied_unchanged(self):
        cells = {"A1": "=40+2"}
        out = fill_formula(cells, "A1", ["D9"])
        assert out["D9"] == "=40+2"

    def test_every_destination_generated_from_original(self):
        # C2 is empty/missing before the call; the second destination must not
        # depend on the first generated formula.
        cells = {"A1": "=A1"}
        out = fill_formula(cells, "A1", ["B1", "C2"])
        assert out["B1"] == "=B1"
        assert out["C2"] == "=C2"

    def test_destination_overwrites_existing_cell(self):
        cells = {"A1": "=B1", "B1": 99, "A2": "=5"}
        out = fill_formula(cells, "A1", ["A2"])
        assert out == {"A1": "=B1", "A2": "=B2", "B1": 99}

    def test_destination_equal_to_source_overwrites_source(self):
        # Copying a formula onto itself canonicalises its row numbers.
        cells = {"A1": "=A001+A007"}
        out = fill_formula(cells, "A1", ["A1"])
        assert out == {"A1": "=A1+A7"}

    def test_empty_destinations_returns_full_table(self):
        cells = {"A1": 1, "B1": "=A1"}
        out = fill_formula(cells, "B1", [])
        assert out == {"A1": 1, "B1": "=A1"}

    def test_addresses_canonicalised(self):
        cells = {"a1": 1, "b1": "=A1"}
        out = fill_formula(cells, "b1", ["c1"])
        assert out == {"A1": 1, "B1": "=A1", "C1": "=B1"}

    def test_output_keys_sorted(self):
        cells = {"B10": "=A1", "C1": 1}
        out = fill_formula(cells, "B10", ["C10", "D20"])
        assert list(out) == ["B10", "C1", "C10", "D20"]

    @pytest.mark.parametrize("destination", ["A0", "Z0"])
    def test_invalid_destination_address_raises(self, destination):
        with pytest.raises(FillFormulaError):
            fill_formula({"A1": "=A1"}, "A1", [destination])

    def test_row_underflow_raises(self):
        # Valid destination (A1), but delta -1 pushes the ref A1 -> A0.
        with pytest.raises(FillFormulaError):
            fill_formula({"A2": "=A1"}, "A2", ["A1"])

    def test_column_underflow_raises(self):
        # Source B1 references column A; a one-column-left destination shifts
        # that reference below column A (A -> the invalid column 0).
        with pytest.raises(FillFormulaError):
            fill_formula({"B1": "=A1"}, "B1", ["A1"])

    def test_row_overflow_raises(self):
        # Delta +1: the destination cell itself is still valid, but the shifted
        # reference leaves the last row (1048576 -> 1048577).
        with pytest.raises(FillFormulaError):
            fill_formula(
                {"A1048575": "=A1048576"},
                "A1048575",
                ["A1048576"],
            )

    def test_column_overflow_raises(self):
        # The reference sits in the rightmost column; a one-column-right
        # destination (still a valid address) pushes it beyond ZZZ.
        with pytest.raises(FillFormulaError):
            fill_formula({"ZZY1": "=ZZZ1"}, "ZZY1", ["ZZZ1"])

    def test_sum_endpoint_out_of_range_raises(self):
        # B1:A1 copied one column left => first endpoint A1, second invalid.
        with pytest.raises(FillFormulaError):
            fill_formula({"B1": "=SUM(B1:A1)"}, "B1", ["A1"])

    def test_generated_too_long_raises(self):
        # Body at the 1000-char limit: shifting expands a column token
        # ("A999999" -> "B999999" keeps length, so instead build a body of
        # length 1000 whose shift gains a digit/letter). Use a reference whose
        # row gains a digit after the shift: A9 -> B10 grows by one character.
        body = "A9" + "+0" * ((1000 - 2) // 2)
        # len(body) is 1000; shifting A9 -> B10 makes it 1001.
        assert len(body) == 1000
        with pytest.raises(FillFormulaError):
            fill_formula(
                {"A9": "=" + body}, "A9", ["B10"]
            )

    def test_generated_at_limit_allowed(self):
        # Body of exactly 999 chars; A9 -> B10 gains one char => 1000 (limit).
        body = "A9" + "+0" * 498 + " "  # trailing whitespace is valid
        assert len(body) == 999
        out = fill_formula({"A9": "=" + body}, "A9", ["B10"])
        assert len(out["B10"]) == 1 + 1000  # leading '=' plus shifted body

    def test_failure_aborts_entire_request(self):
        original = {"A1": "=A1"}
        with pytest.raises(FillFormulaError):
            fill_formula(original, "A1", ["B1", "A0"])
        # Input untouched; nothing partially generated/returned.
        assert original == {"A1": "=A1"}

    @pytest.mark.parametrize(
        "source",
        [None, "nope", "A0", "AAAA1"],
    )
    def test_invalid_source_raises(self, source):
        with pytest.raises(FillFormulaError):
            fill_formula({"A1": "=B1"}, source, ["A2"])

    def test_source_missing_raises(self):
        with pytest.raises(FillFormulaError):
            fill_formula({"A1": "=B1"}, "Z9", ["A2"])

    def test_source_integer_raises(self):
        with pytest.raises(FillFormulaError):
            fill_formula({"A1": 5}, "A1", ["A2"])

    def test_source_broken_formula_raises(self):
        with pytest.raises(FillFormulaError):
            fill_formula({"A1": "=1+"}, "A1", ["A2"])

    def test_duplicate_destination_raises(self):
        with pytest.raises(FillFormulaError):
            fill_formula({"A1": "=B1"}, "A1", ["B2", "b2"])

    def test_invalid_destination_raises(self):
        with pytest.raises(FillFormulaError):
            fill_formula({"A1": "=B1"}, "A1", ["nope"])

    def test_invalid_cells_still_rejected(self):
        from app.engine import InvalidCellKey

        with pytest.raises(InvalidCellKey):
            fill_formula({"nope": 1}, "A1", ["A2"])


class TestFillAPI:
    def test_basic_fill(self):
        resp = client.post(
            "/fill-formula",
            json={
                "cells": {"A1": 1, "A2": 2, "B1": "=A1*2"},
                "source": "B1",
                "destinations": ["B2", "b3"],
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "cells": {
                "A1": 1,
                "A2": 2,
                "B1": "=A1*2",
                "B2": "=A2*2",
                "B3": "=A3*2",
            }
        }

    def test_result_is_submittable_to_evaluate(self):
        fill_resp = client.post(
            "/fill-formula",
            json={
                "cells": {"A1": 10, "A2": 20, "A3": 30, "B1": "=A1*2"},
                "source": "B1",
                "destinations": ["B2", "B3"],
            },
        )
        payload = fill_resp.json()
        ev_resp = client.post("/evaluate", json={"cells": payload["cells"]})
        assert ev_resp.status_code == 200
        assert ev_resp.json() == {
            "results": {"A1": 10, "A2": 20, "A3": 30, "B1": 20, "B2": 40, "B3": 60}
        }

    def test_sum_fill(self):
        resp = client.post(
            "/fill-formula",
            json={
                "cells": {"A1": 1, "C1": "=SUM(A1:B1)"},
                "source": "C1",
                "destinations": ["C2", "C3"],
            },
        )
        assert resp.status_code == 200
        cells = resp.json()["cells"]
        assert cells["C2"] == "=SUM(A2:B2)"
        assert cells["C3"] == "=SUM(A3:B3)"

    def test_duplicate_destination_422(self):
        resp = client.post(
            "/fill-formula",
            json={
                "cells": {"A1": "=B1"},
                "source": "A1",
                "destinations": ["C1", "c1"],
            },
        )
        assert resp.status_code == 422

    def test_invalid_source_422(self):
        resp = client.post(
            "/fill-formula",
            json={"cells": {"A1": 1}, "source": "A1", "destinations": ["A2"]},
        )
        assert resp.status_code == 422

    def test_reference_out_of_range_422(self):
        resp = client.post(
            "/fill-formula",
            json={
                "cells": {"A1": "=A1"},
                "source": "A1",
                "destinations": ["A0"],
            },
        )
        assert resp.status_code == 422

    def test_generated_formula_too_long_422(self):
        body = "A9" + "+0" * ((1000 - 2) // 2)
        resp = client.post(
            "/fill-formula",
            json={
                "cells": {"A9": "=" + body},
                "source": "A9",
                "destinations": ["B10"],
            },
        )
        assert resp.status_code == 422

    def test_shared_cell_validation_422(self):
        for bad in ({"A1": 1.5}, {"A1": True}, {"A1": "bare"}, {"nope": 1}):
            resp = client.post(
                "/fill-formula",
                json={"cells": bad, "source": "A1", "destinations": ["B1"]},
            )
            assert resp.status_code == 422

    def test_missing_source_field_422(self):
        resp = client.post(
            "/fill-formula",
            json={"cells": {"A1": "=1"}, "destinations": ["B1"]},
        )
        assert resp.status_code == 422

    def test_overwrite_destination(self):
        resp = client.post(
            "/fill-formula",
            json={
                "cells": {"A1": "=1+1", "A2": "=999"},
                "source": "A1",
                "destinations": ["A2"],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["cells"]["A2"] == "=1+1"

    def test_openapi_documents_endpoint(self):
        paths = client.get("/openapi.json").json()["paths"]
        assert "/fill-formula" in paths
