"""Goal seek for ``POST /goal-seek``: adjust one integer cell to hit a target.

The *target* cell's value is rewritten as ``a * variable + b`` by walking the
target's reference chain.  Every cell on the chain gets a coefficient pair
``(a, b)`` meaning "this cell equals ``a * variable + b``":

* the *variable* cell is ``(1, 0)``; other integer literals are ``(0, v)``;
* missing cells are ``(0, 0)`` and ``SUM`` ranges add up the member pairs,
  exactly like the evaluator treats them;
* ``+`` / ``-`` and unary ``+`` / ``-`` add, subtract or negate the pairs;
* ``*`` scales one pair by the other side's constant — if *both* sides depend
  on the variable the product is quadratic and the request is rejected.

Only the chain matters: a ``SYNTAX`` or ``CYCLE`` cell reachable from the
target aborts the solve with :class:`GoalSeekError` (HTTP 422), while broken
cells elsewhere in the table are ignored.

With ``target == a * variable + b`` and an integer ``desired`` the solve is
exact:

* ``a != 0`` and ``a`` divides ``desired - b`` -> ``("unique", solution)``;
* ``a != 0`` without exact divisibility, or ``a == 0`` and ``b != desired``
  -> ``("none", None)``;
* ``a == 0`` and ``b == desired`` (any value works) -> ``("any", None)``.
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Tuple

from .engine import (
    InvalidCellKey,
    _ApplyBinary,
    _ApplyUnary,
    _dependency_graph,
    _find_cycle_nodes,
    _parse_cells,
    canonical_key,
)
from .parser import Binary, Node, Num, Ref, SumRange, Unary


class GoalSeekError(Exception):
    """Request-level failure for ``/goal-seek``; always maps to HTTP 422."""


class _NonLinear(Exception):
    """A multiplication made the target non-linear in the variable."""


# Coefficient pair (a, b) standing for "cell == a * variable + b".
_Pair = Tuple[int, int]


def _coef_ast(node: Node, coefs: Mapping[str, _Pair]) -> _Pair:
    """Coefficient pair of one formula, given pairs for every cell it reads.

    Iterative post-order walk (same shape as the evaluator) so deeply nested
    formulas cannot hit the recursion limit.  References to cells outside
    ``coefs`` are missing cells and contribute ``(0, 0)``.
    """
    stack: List[object] = [node]
    pairs: List[_Pair] = []

    while stack:
        cur = stack.pop()
        if isinstance(cur, Num):
            pairs.append((0, cur.value))
        elif isinstance(cur, Ref):
            pairs.append(coefs.get(cur.key, (0, 0)))
        elif isinstance(cur, SumRange):
            a = b = 0
            for key in cur.members:
                ma, mb = coefs.get(key, (0, 0))
                a += ma
                b += mb
            pairs.append((a, b))
        elif isinstance(cur, Unary):
            stack.append(_ApplyUnary(cur.op))
            stack.append(cur.operand)
        elif isinstance(cur, Binary):
            stack.append(_ApplyBinary(cur.op))
            # Pushed first, popped last, so the left operand is handled first.
            stack.append(cur.right)
            stack.append(cur.left)
        elif isinstance(cur, _ApplyUnary):
            a, b = pairs.pop()
            pairs.append((-a, -b) if cur.op == "-" else (a, b))
        else:  # _ApplyBinary
            ra, rb = pairs.pop()
            la, lb = pairs.pop()
            if cur.op == "+":
                pairs.append((la + ra, lb + rb))
            elif cur.op == "-":
                pairs.append((la - ra, lb - rb))
            else:  # '*'
                if la != 0 and ra != 0:
                    raise _NonLinear
                if la == 0:
                    pairs.append((lb * ra, lb * rb))
                else:  # ra == 0: scale the left pair by the right constant
                    pairs.append((la * rb, lb * rb))

    return pairs[0]


def goal_seek(
    cells: Mapping[str, object],
    variable: str,
    target: str,
    desired: int,
) -> Tuple[str, Optional[int]]:
    """Solve ``target == desired`` by adjusting only the *variable* cell.

    Returns ``(status, value)``: ``"unique"`` with the integer solution,
    ``"none"`` or ``"any"`` with ``None``.  Raises :class:`GoalSeekError` —
    mapped to HTTP 422 — for invalid addresses, missing cells, a non-integer
    variable, a ``SYNTAX``/``CYCLE`` cell on the target's reference chain, or
    a target that is not linear in the variable.
    """
    # 1) Same key/value validation as /evaluate.
    parsed = _parse_cells(cells)

    try:
        var_key = canonical_key(str(variable))
    except InvalidCellKey:
        raise GoalSeekError(
            f"invalid variable address {variable!r}; expected A1-style address"
        )
    try:
        tgt_key = canonical_key(str(target))
    except InvalidCellKey:
        raise GoalSeekError(
            f"invalid target address {target!r}; expected A1-style address"
        )

    if var_key not in parsed.provided:
        raise GoalSeekError(f"variable cell {var_key!r} is not provided")
    if var_key not in parsed.literals:
        raise GoalSeekError(
            f"variable cell {var_key!r} must hold an integer, not a formula"
        )
    if tgt_key not in parsed.provided:
        raise GoalSeekError(f"target cell {tgt_key!r} is not provided")

    # 2) The chain: provided cells the target (transitively) refers to.
    deps = _dependency_graph(parsed)
    reachable: set[str] = set()
    stack = [tgt_key]
    while stack:
        node = stack.pop()
        if node in reachable:
            continue
        reachable.add(node)
        stack.extend(deps[node])

    # 3) Errors on the chain abort the solve; off-chain errors are ignored.
    broken = sorted(reachable & parsed.syntax_nodes)
    if broken:
        raise GoalSeekError(
            "target chain contains SYNTAX cell(s): " + ", ".join(broken)
        )
    cyclic = sorted(_find_cycle_nodes(reachable, deps))
    if cyclic:
        raise GoalSeekError(
            "target chain contains CYCLE cell(s): " + ", ".join(cyclic)
        )

    # 4) Coefficient pairs in topological order (Kahn) over the chain, which
    # is a DAG now, so every node is assigned exactly once.
    reverse: Dict[str, List[str]] = {key: [] for key in reachable}
    for key in reachable:
        for dep in deps[key]:
            reverse[dep].append(key)

    indegree = {key: len(deps[key]) for key in reachable}
    ready = [key for key, deg in indegree.items() if deg == 0]
    coefs: Dict[str, _Pair] = {}

    while ready:
        key = ready.pop()
        if key == var_key:
            coefs[key] = (1, 0)
        elif key in parsed.literals:
            coefs[key] = (0, parsed.literals[key])
        else:
            ast = parsed.asts[key]
            assert ast is not None  # syntax nodes were rejected above
            try:
                coefs[key] = _coef_ast(ast, coefs)
            except _NonLinear:
                raise GoalSeekError(
                    f"target {tgt_key!r} is not linear in variable "
                    f"{var_key!r}: multiplication of two "
                    "variable-dependent terms"
                )
        for pred in reverse[key]:
            indegree[pred] -= 1
            if indegree[pred] == 0:
                ready.append(pred)

    # 5) Solve a * variable + b == desired over the integers.
    a, b = coefs[tgt_key]
    if a != 0:
        diff = desired - b
        if diff % a == 0:
            return "unique", diff // a
        return "none", None
    if b == desired:
        return "any", None
    return "none", None
