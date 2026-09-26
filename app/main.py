"""FastAPI application exposing the spreadsheet formula evaluator."""

from __future__ import annotations

from typing import Dict, List, Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .engine import (
    InvalidCellKey,
    MAX_REPAIR_FORMULAS,
    TooManyFormulas,
    evaluate_cells,
    repair_cycles,
)
from .fill import FillFormulaError, fill_formula
from .goalseek import GoalSeekError, goal_seek

app = FastAPI(
    title="Spreadsheet Formula Evaluator",
    version="1.0.0",
    description=(
        "Local spreadsheet formula evaluation API. "
        "No code from input is executed."
    ),
)

# Cell values: an integer literal or a formula string starting with '='.
CellValue = Union[int, str]


class EvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cells: Dict[str, CellValue] = Field(
        default_factory=dict,
        description="Mapping of A1-style cell addresses to integers or "
        "'='-prefixed formulas.",
    )

    @field_validator("cells", mode="before")
    @classmethod
    def _reject_booleans(cls, v):
        # bool is a subclass of int: reject it before Pydantic coerces it.
        if isinstance(v, dict):
            for key, value in v.items():
                if isinstance(value, bool):
                    raise ValueError(
                        f"cell {key!r}: booleans are not supported"
                    )
        return v

    @field_validator("cells")
    @classmethod
    def _validate_values(cls, v: Dict[str, CellValue]) -> Dict[str, CellValue]:
        for key, value in v.items():
            if not isinstance(value, (int, str)):
                raise ValueError(
                    f"cell {key!r}: value must be an integer or a formula "
                    "string starting with '='"
                )
            if isinstance(value, str) and not value.startswith("="):
                raise ValueError(
                    f"cell {key!r}: string values must start with '='"
                )
        return v


class EvaluateResponse(BaseModel):
    results: Dict[str, Union[int, str]]


class CycleRepairResponse(BaseModel):
    replace_with_zero: List[str]
    minimum_count: int


class FillFormulaRequest(EvaluateRequest):
    # Inherits the cells field and all of its validators.
    source: str = Field(
        description="Address of the cell holding the formula to copy."
    )
    destinations: List[str] = Field(
        description="Addresses receiving a copy; each formula reference is "
        "shifted by the destination's displacement from the source."
    )


class FillFormulaResponse(BaseModel):
    cells: Dict[str, CellValue]


class GoalSeekRequest(EvaluateRequest):
    # Inherits the cells field and all of its validators.
    variable: str = Field(
        description="Address of the provided integer cell allowed to change."
    )
    target: str = Field(
        description="Address of the provided cell whose value is driven."
    )
    desired: int = Field(description="Integer value the target cell should equal.")

    @field_validator("desired", mode="before")
    @classmethod
    def _reject_desired_non_int(cls, v):
        # bool is a subclass of int: reject it (and floats) before coercion.
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("desired must be an integer")
        return v


class GoalSeekResponse(BaseModel):
    status: str = Field(
        description="One of 'unique' (value holds the integer solution), "
        "'none' (no integer solution) or 'any' (every integer works)."
    )
    value: Union[int, None] = None


@app.get("/")
def root() -> Dict[str, str]:
    return {"service": "spreadsheet-evaluator", "status": "ok"}


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/evaluate", response_model=EvaluateResponse)
def evaluate(request: EvaluateRequest) -> EvaluateResponse:
    try:
        results = evaluate_cells(request.cells)
    except InvalidCellKey as exc:
        # Bad request shape (invalid address key); formula-level syntax
        # problems are reported per cell as SYNTAX instead.
        raise HTTPException(
            status_code=422,
            detail=f"invalid cell key {str(exc)!r}; expected A1-style address",
        )
    return EvaluateResponse(results=results)


@app.post("/cycle-repair", response_model=CycleRepairResponse)
def cycle_repair(request: EvaluateRequest) -> CycleRepairResponse:
    try:
        addresses, count = repair_cycles(request.cells)
    except InvalidCellKey as exc:
        raise HTTPException(
            status_code=422,
            detail=f"invalid cell key {str(exc)!r}; expected A1-style address",
        )
    except TooManyFormulas as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"too many syntactically valid formulas: {exc.args[0]} "
                f"provided, at most {MAX_REPAIR_FORMULAS} are supported"
            ),
        )
    return CycleRepairResponse(
        replace_with_zero=addresses, minimum_count=count
    )


@app.post("/fill-formula", response_model=FillFormulaResponse)
def fill(request: FillFormulaRequest) -> FillFormulaResponse:
    try:
        cells = fill_formula(
            request.cells, request.source, request.destinations
        )
    except InvalidCellKey as exc:
        raise HTTPException(
            status_code=422,
            detail=f"invalid cell key {str(exc)!r}; expected A1-style address",
        )
    except FillFormulaError as exc:
        raise HTTPException(status_code=422, detail=exc.args[0])
    return FillFormulaResponse(cells=cells)


@app.post("/goal-seek", response_model=GoalSeekResponse)
def goal_seek_endpoint(request: GoalSeekRequest) -> GoalSeekResponse:
    try:
        status, value = goal_seek(
            request.cells, request.variable, request.target, request.desired
        )
    except InvalidCellKey as exc:
        raise HTTPException(
            status_code=422,
            detail=f"invalid cell key {str(exc)!r}; expected A1-style address",
        )
    except GoalSeekError as exc:
        raise HTTPException(status_code=422, detail=exc.args[0])
    return GoalSeekResponse(status=status, value=value)
