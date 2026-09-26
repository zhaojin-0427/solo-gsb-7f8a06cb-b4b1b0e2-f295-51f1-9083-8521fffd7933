"""Dependency resolution and evaluation for a batch of cells.

Each input cell is parsed independently.  Errors propagate transitively along
the reference graph but never reach unrelated cells:

* a cell whose formula is malformed gets ``SYNTAX``;
* any cell that (transitively) refers to a malformed cell gets ``SYNTAX``;
* a cell that is part of a reference cycle, or that (transitively) refers to
  one, gets ``CYCLE``;
* when a cell can reach both kinds of problem, ``SYNTAX`` wins.

Unprovided cells are not part of the graph and evaluate to zero.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

from .parser import (
    Node,
    Num,
    Ref,
    Unary,
    Binary,
    SumRange,
    parse_address,
    parse_formula,
    iter_references,
)

CYCLE = "CYCLE"
SYNTAX = "SYNTAX"


class InvalidCellKey(Exception):
    """Raised when an input object key is not a valid A1 address."""


# /cycle-repair only attempts an exact solution for at most this many valid
# formulas (minimum feedback vertex set is NP-hard; 2**16 is cheap to exhaust).
MAX_REPAIR_FORMULAS = 16


class TooManyFormulas(Exception):
    """Raised when /cycle-repair sees more than ``MAX_REPAIR_FORMULAS``."""


@dataclass(frozen=True)
class ParsedCells:
    """Per-cell parse result for one request.

    * ``asts[key]`` is the parsed AST, or ``None`` for an integer literal or
      a syntax-broken formula;
    * ``literals`` holds integer literal values;
    * ``syntax_nodes`` holds cells whose formula failed to parse.
    """

    asts: Dict[str, Optional[Node]]
    literals: Dict[str, int]
    syntax_nodes: frozenset[str]

    @property
    def provided(self) -> frozenset[str]:
        return frozenset(self.asts)


def _parse_cells(cells: Mapping[str, object]) -> ParsedCells:
    """Canonicalise keys and parse every cell independently."""
    asts: Dict[str, Optional[Node]] = {}
    literals: Dict[str, int] = {}
    syntax_nodes: set[str] = set()

    for raw_key, raw_value in cells.items():
        key = canonical_key(str(raw_key))
        if key in asts:
            raise InvalidCellKey(raw_key)

        # bool is a subclass of int: reject it explicitly.
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, str)):
            raise InvalidCellKey(raw_key)

        if isinstance(raw_value, int):
            asts[key] = None
            literals[key] = raw_value
            continue

        if not raw_value.startswith("="):
            raise InvalidCellKey(raw_key)

        try:
            asts[key] = parse_formula(raw_value[1:])
        except Exception:
            asts[key] = None
            syntax_nodes.add(key)

    return ParsedCells(asts, literals, frozenset(syntax_nodes))


def canonical_key(raw: str) -> str:
    """Validate/normalise an input object key (e.g. ``a1`` -> ``A1``)."""
    parsed = parse_address(raw.upper())
    if parsed is None:
        raise InvalidCellKey(raw)
    return parsed[2]


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


def _find_cycle_nodes(
    nodes: set[str], deps: Mapping[str, List[str]]
) -> set[str]:
    """Iterative three-colour DFS; return every node that lies on a cycle."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {key: WHITE for key in nodes}
    in_cycle: set[str] = set()

    for root in tuple(nodes):
        if colour[root] != WHITE:
            continue
        colour[root] = GREY
        # Each frame is (node, index of the next edge to inspect).
        stack: List[Tuple[str, int]] = [(root, 0)]

        while stack:
            node, idx = stack[-1]
            neighbours = deps.get(node, ())
            if idx < len(neighbours):
                nxt = neighbours[idx]
                stack[-1] = (node, idx + 1)
                state = colour.get(nxt)
                if state is None:
                    # Unprovided cell: its value is zero, no graph edge.
                    continue
                if state == WHITE:
                    colour[nxt] = GREY
                    stack.append((nxt, 0))
                elif state == GREY:
                    # Back edge: nxt and every frame above it form a cycle.
                    for i, (frame, _) in enumerate(stack):
                        if frame == nxt:
                            for f, _ in stack[i:]:
                                in_cycle.add(f)
                            break
            else:
                colour[node] = BLACK
                stack.pop()

    return in_cycle


def _reverse_reachable(
    seeds: set[str], reverse: Mapping[str, List[str]]
) -> set[str]:
    """All nodes that can reach ``seeds`` following *reverse* edges."""
    reached = set(seeds)
    stack = list(seeds)
    while stack:
        node = stack.pop()
        for pred in reverse.get(node, ()):
            if pred not in reached:
                reached.add(pred)
                stack.append(pred)
    return reached


# ---------------------------------------------------------------------------
# AST evaluation (iterative post-order, so nesting depth cannot blow the
# Python recursion limit)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ApplyUnary:
    op: str


@dataclass(frozen=True)
class _ApplyBinary:
    op: str


def _eval_ast(node: Node, values: Mapping[str, int]) -> int:
    stack: List[object] = [node]
    nums: List[int] = []

    while stack:
        cur = stack.pop()
        if isinstance(cur, Num):
            nums.append(cur.value)
        elif isinstance(cur, Ref):
            nums.append(values.get(cur.key, 0))
        elif isinstance(cur, SumRange):
            nums.append(sum(values.get(key, 0) for key in cur.members))
        elif isinstance(cur, Unary):
            stack.append(_ApplyUnary(cur.op))
            stack.append(cur.operand)
        elif isinstance(cur, Binary):
            stack.append(_ApplyBinary(cur.op))
            # Pushed first, popped last, so the left operand is handled first.
            stack.append(cur.right)
            stack.append(cur.left)
        elif isinstance(cur, _ApplyUnary):
            value = nums.pop()
            nums.append(-value if cur.op == "-" else value)
        else:  # _ApplyBinary
            right = nums.pop()
            left = nums.pop()
            if cur.op == "+":
                nums.append(left + right)
            elif cur.op == "-":
                nums.append(left - right)
            else:
                nums.append(left * right)

    return nums[0]


# ---------------------------------------------------------------------------
# Batch evaluation
# ---------------------------------------------------------------------------


def evaluate_cells(cells: Mapping[str, object]) -> Dict[str, object]:
    """Evaluate every cell and return results keyed by canonical address."""
    # 1) Canonicalise keys and parse every cell -----------------------------
    parsed = _parse_cells(cells)
    asts = parsed.asts
    literals = parsed.literals
    syntax_nodes = parsed.syntax_nodes
    provided = set(parsed.provided)

    # 2) Build the dependency graph (edges to provided cells only) ----------
    deps: Dict[str, List[str]] = {}
    for key, ast in asts.items():
        if key in syntax_nodes or ast is None:
            deps[key] = []
        else:
            deps[key] = [
                ref for ref in iter_references(ast) if ref in provided
            ]

    reverse: Dict[str, List[str]] = {key: [] for key in provided}
    for key, targets in deps.items():
        for target in targets:
            reverse[target].append(key)

    # 3) Classify errors ----------------------------------------------------
    # Syntax takes precedence over CYCLE.
    syntax_reach = _reverse_reachable(set(syntax_nodes), reverse)
    cycle_pool = provided - syntax_reach
    cycle_nodes = _find_cycle_nodes(cycle_pool, deps)
    cycle_reach = _reverse_reachable(cycle_nodes, reverse) - syntax_reach

    # 4) Evaluate healthy nodes in topological order (Kahn's algorithm) -----
    healthy = provided - syntax_reach - cycle_reach
    indegree = {
        key: sum(1 for dep in deps[key] if dep in healthy) for key in healthy
    }
    ready = [key for key, deg in indegree.items() if deg == 0]
    values: Dict[str, int] = {}

    while ready:
        key = ready.pop()
        ast = asts[key]
        values[key] = literals[key] if ast is None else _eval_ast(ast, values)
        for pred in reverse.get(key, ()):
            if pred in indegree:
                indegree[pred] -= 1
                if indegree[pred] == 0:
                    ready.append(pred)

    # 5) Assemble output sorted by address lexicographically ----------------
    results: Dict[str, object] = {}
    for key in sorted(provided):
        if key in syntax_reach:
            results[key] = SYNTAX
        elif key in cycle_reach:
            results[key] = CYCLE
        else:
            results[key] = values[key]
    return results


# ---------------------------------------------------------------------------
# Cycle repair: minimum feedback vertex set over the valid-formula graph
# ---------------------------------------------------------------------------


def _valid_formula_graph(
    parsed: ParsedCells,
) -> Tuple[List[str], Dict[str, "set[str]"]]:
    """Build the directed graph induced by syntactically valid formulas.

    An edge goes from a valid formula cell to each *provided* cell it refers
    to; ``SUM`` ranges count cell by cell.  Missing cells, integer literals
    and syntax-broken formulas have no outgoing edges and therefore can never
    lie on a cycle, so only valid-formula nodes are kept.
    """
    formula_keys = sorted(
        key
        for key, ast in parsed.asts.items()
        if key not in parsed.syntax_nodes and ast is not None
    )
    formula_set = set(formula_keys)
    deps: Dict[str, set[str]] = {}
    for key in formula_keys:
        deps[key] = {
            ref
            for ref in iter_references(parsed.asts[key])
            if ref in formula_set
        }
    return formula_keys, deps


def _is_acyclic(nodes: set[str], deps: Mapping[str, "set[str]"]) -> bool:
    """Kahn's algorithm: True iff the induced directed graph has no cycle."""
    indegree = {key: 0 for key in nodes}
    for key in nodes:
        for target in deps[key]:
            if target in indegree:
                indegree[target] += 1
    ready = [key for key, deg in indegree.items() if deg == 0]
    seen = 0
    while ready:
        key = ready.pop()
        seen += 1
        for target in deps[key]:
            if target in indegree:
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
    return seen == len(nodes)


def repair_cycles(cells: Mapping[str, object]) -> Tuple[List[str], int]:
    """Find the fewest formulas to replace with literal ``0`` to break cycles.

    Replacing a formula deletes only that cell's outgoing edges; because the
    replacement is an integer (no outgoing edges), this is exactly vertex
    deletion for the purpose of directed-cycle breaking.  Among all solutions
    of minimum size, the lexicographically smallest sorted address list wins.

    Returns ``(addresses, count)``; with no cycle present the list is empty.
    Raises :class:`TooManyFormulas` above ``MAX_REPAIR_FORMULAS`` valid
    formulas.
    """
    parsed = _parse_cells(cells)
    formula_keys, deps = _valid_formula_graph(parsed)

    if len(formula_keys) > MAX_REPAIR_FORMULAS:
        raise TooManyFormulas(len(formula_keys))

    nodes = set(formula_keys)
    if _is_acyclic(nodes, deps):
        return [], 0

    # Exhaust combinations by increasing size.  itertools.combinations yields
    # index tuples in lexicographic order, so over a list of ascending
    # canonical addresses the first feasible hit is the required tie-breaker.
    for size in range(1, len(formula_keys) + 1):
        for combo in itertools.combinations(range(len(formula_keys)), size):
            removed = {formula_keys[i] for i in combo}
            if _is_acyclic(nodes - removed, deps):
                return [formula_keys[i] for i in combo], size

    raise AssertionError("unreachable: removing every node breaks all cycles")
