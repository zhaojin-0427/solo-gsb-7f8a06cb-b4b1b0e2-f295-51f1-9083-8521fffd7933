"""Integer goal seeking for ``POST /goal-seek``.

Only one cell, *variable*, is allowed to change; it is treated as the symbol
``x``.  Every cell reachable from *target* through its reference chain is then
evaluated as an affine form ``a*x + b`` with integer coefficients, using the
same rules the ordinary evaluator uses:

* addition and subtraction combine the forms component-wise;
* unary ``+`` / ``-`` propagate to both coefficients;
* multiplication is only linear when at least one side is constant; when both
  sides depend on ``x`` the result is quadratic and the request is rejected;
* ``SUM`` adds the forms of its members cell by cell;
* unprovided cells are the constant zero.

The chain is validated exactly as in ``/evaluate``: a ``SYNTAX`` or ``CYCLE``
cell reachable from the target rejects the request, while errors that the
target cannot reach are irrelevant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

from .engine import (
    CYCLE,
    SYNTAX,
    InvalidCellKey,
    _parse_cells,
    canonical_key,
    evaluate_cells,
)
from .parser import Binary, Node, Num, Ref, SumRange, Unary

# Solver outcomes.
STATUS_UNIQUE = "unique"
STATUS_NONE = "none"
STATUS_ANY = "any"


class GoalSeekError(Exception):
    """Request-level failure for ``/goal-seek``; always maps to HTTP 422."""


@dataclass(frozen=True)
class _ApplyUnaryForm:
    op: str


@dataclass(frozen=True)
class _ApplyBinaryForm:
    op: str


@dataclass(frozen=True)
class _ReadCell:
    """Push the affine form of ``key`` onto the work stack."""

    key: str


@dataclass(frozen=True)
class _ApplySum:
    """Pop ``count`` member forms and replace them with their sum."""

    count: int


@dataclass(frozen=True)
class _Memoize:
    """Bind the form on top of the result stack to ``key``."""

    key: str


def _evaluate_form(
    node: Node,
    parsed,
    variable: str,
    memo: Dict[str, Tuple[int, int]],
) -> Tuple[int, int]:
    """Evaluate ``node`` as ``(a, b)`` meaning ``a*variable + b``.

    Cell forms are cached in ``memo``; the dependency graph reachable from the
    target is guaranteed acyclic and syntax-free by the caller, so every
    referenced formula terminates.  The evaluation itself is iterative
    (explicit work stack) to stay independent of Python's recursion limit.
    """
    asts = parsed.asts
    literals = parsed.literals

    work: List[object] = [node]
    results: List[Tuple[int, int]] = []

    while work:
        cur = work.pop()

        if isinstance(cur, Num):
            results.append((0, cur.value))

        elif isinstance(cur, _ReadCell):
            key = cur.key
            if key in memo:
                # Diamond DAG: the same cell feeds more than one branch.
                results.append(memo[key])
                continue
            if key == variable:
                # The variable cell is an input integer: it is x itself,
                # regardless of any integer value supplied in the table.
                form = (1, 0)
            elif key not in asts:
                # Unprovided cells evaluate to zero.
                form = (0, 0)
            elif asts[key] is None:
                form = (0, literals[key])
            else:
                # Expand the formula: the AST sits above a pending _Memoize
                # and is fully reduced before any sibling branch can read the
                # same key (the graph is acyclic at this point).
                work.append(_Memoize(key))
                work.append(asts[key])
                continue
            memo[key] = form
            results.append(form)

        elif isinstance(cur, _Memoize):
            memo[cur.key] = results[-1]

        elif isinstance(cur, Ref):
            work.append(_ReadCell(cur.key))

        elif isinstance(cur, SumRange):
            # Members are pushed last-first so they reduce in range order;
            # a one-cell range simply returns that single form.
            work.append(_ApplySum(len(cur.members)))
            for key in reversed(cur.members):
                work.append(_ReadCell(key))

        elif isinstance(cur, _ApplySum):
            total_a, total_b = 0, 0
            for _ in range(cur.count):
                a, b = results.pop()
                total_a += a
                total_b += b
            results.append((total_a, total_b))

        elif isinstance(cur, Unary):
            work.append(_ApplyUnaryForm(cur.op))
            work.append(cur.operand)

        elif isinstance(cur, _ApplyUnaryForm):
            a, b = results.pop()
            results.append((-a, -b) if cur.op == "-" else (a, b))

        elif isinstance(cur, Binary):
            work.append(_ApplyBinaryForm(cur.op))
            work.append(cur.right)
            work.append(cur.left)

        elif isinstance(cur, _ApplyBinaryForm):
            ra, rb = results.pop()
            la, lb = results.pop()
            if cur.op == "+":
                results.append((la + ra, lb + rb))
            elif cur.op == "-":
                results.append((la - ra, lb - rb))
            else:
                if la != 0 and ra != 0:
                    raise GoalSeekError(
                        "target depends on the variable through both operands "
                        "of a multiplication; only linear spreadsheets can be "
                        "goal-seeked"
                    )
                results.append((la * rb + ra * lb, lb * rb))

        else:  # pragma: no cover - every work item is handled above
            raise AssertionError(f"unexpected work item {cur!r}")

    return results[0]


def goal_seek(
    cells: Mapping[str, object],
    variable: str,
    target: str,
    desired: int,
) -> Tuple[str, Optional[int]]:
    """Find the integer value of ``variable`` making ``target`` equal ``desired``.

    Returns ``(status, value)`` where status is one of :data:`STATUS_UNIQUE`,
    :data:`STATUS_NONE`, :data:`STATUS_ANY`; ``value`` is the solution for the
    unique case and ``None`` otherwise.

    Raises :class:`GoalSeekError` for every request-level problem (missing or
    invalid addresses, a formula used as the variable, a non-linear target
    chain) and :class:`~app.engine.InvalidCellKey` when the cell table itself
    is malformed.
    """
    # 1) Same key/value validation as the other endpoints.
    parsed = _parse_cells(cells)

    try:
        variable_key = canonical_key(str(variable))
    except InvalidCellKey:
        raise GoalSeekError(
            f"invalid variable address {variable!r}; expected A1-style address"
        )
    try:
        target_key = canonical_key(str(target))
    except InvalidCellKey:
        raise GoalSeekError(
            f"invalid target address {target!r}; expected A1-style address"
        )

    # 2) Both cells must be provided, and the adjustable cell is an integer.
    if variable_key not in parsed.asts:
        raise GoalSeekError(
            f"variable cell {variable_key!r} is missing from the request"
        )
    if target_key not in parsed.asts:
        raise GoalSeekError(
            f"target cell {target_key!r} is missing from the request"
        )
    # An integer literal is the one parsed shape with ``asts[key] is None``
    # and no syntax error; a formula string (even an unparseable one) is not
    # adjustable.
    if (
        parsed.asts[variable_key] is not None
        or variable_key in parsed.syntax_nodes
    ):
        raise GoalSeekError(
            f"variable cell {variable_key!r} must be an integer literal, "
            "not a formula"
        )

    # 3) Classify the target exactly as /evaluate would.  Errors outside the
    # target's reference chain do not appear in its result and are ignored.
    evaluated = evaluate_cells(cells)
    target_result = evaluated[target_key]
    if target_result == SYNTAX:
        raise GoalSeekError(
            f"target cell {target_key!r} (transitively) contains a syntax error"
        )
    if target_result == CYCLE:
        raise GoalSeekError(
            f"target cell {target_key!r} (transitively) belongs to a cycle"
        )

    # 4) Symbolic evaluation of the target chain: target == a*x + b.
    if target_key == variable_key:
        a, b = 1, 0
    else:
        target_ast = parsed.asts[target_key]
        if target_ast is None:
            a, b = 0, parsed.literals[target_key]
        else:
            memo: Dict[str, Tuple[int, int]] = {}
            a, b = _evaluate_form(target_ast, parsed, variable_key, memo)

    # 5) Solve a*x + b = desired exactly over the integers.
    if a == 0:
        if b == desired:
            return STATUS_ANY, None
        return STATUS_NONE, None
    difference = desired - b
    if difference % a != 0:
        return STATUS_NONE, None
    return STATUS_UNIQUE, difference // a
