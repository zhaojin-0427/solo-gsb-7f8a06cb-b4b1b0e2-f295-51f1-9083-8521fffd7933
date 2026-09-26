"""Recursive-descent parser for the small spreadsheet formula language.

Grammar (whitespace is allowed between tokens)::

    expr   := term (('+' | '-') term)*
    term   := factor ('*' factor)*
    factor := ('+' | '-') factor | primary
    primary := INTEGER | CELL_REF | '(' expr ')' | 'SUM(' CELL_REF ':' CELL_REF ')'

The parser *only* parses: nothing derived from the input is ever evaluated as
Python code, files are never opened and no external service is contacted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple, Union
import re
import sys

# The grammar is parsed with recursive descent; formulas are capped at
# MAX_FORMULA_CHARS characters, which bounds nesting depth well below this.
sys.setrecursionlimit(max(sys.getrecursionlimit(), 10_000))

# A1-style address: one to three ASCII letters followed by one or more digits.
ADDR_RE = re.compile(r"^([A-Z]{1,3})([0-9]+)$")

MAX_ROW = 1_048_576
# Defence in depth against pathological inputs.
MAX_LITERAL_DIGITS = 100
MAX_FORMULA_CHARS = 1000
# A SUM range may cover at most this many cells.
MAX_SUM_CELLS = 100_000


class ParseError(Exception):
    """Raised when a formula is not grammatically valid."""


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Num:
    value: int


@dataclass(frozen=True)
class Ref:
    key: str


@dataclass(frozen=True)
class Unary:
    op: str  # '+' or '-'
    operand: "Node"


@dataclass(frozen=True)
class Binary:
    op: str  # '+', '-' or '*'
    left: "Node"
    right: "Node"


@dataclass(frozen=True)
class SumRange:
    """SUM(top_left : bottom_right); ``members`` lists every covered key."""

    top: str
    bottom: str
    members: Tuple[str, ...]


Node = Union[Num, Ref, Unary, Binary, SumRange]


# ---------------------------------------------------------------------------
# Address helpers
# ---------------------------------------------------------------------------


def column_to_index(letters: str) -> int:
    """``A`` -> 1, ``Z`` -> 26, ``AA`` -> 27 ..."""
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx


def index_to_column(index: int) -> str:
    """Inverse of :func:`column_to_index`."""
    out = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def parse_address(text: str) -> Optional[Tuple[str, int, str]]:
    """Parse ``text`` (already upper-cased) into ``(letters, row, key)``.

    Returns ``None`` when the text is not a syntactically valid A1 reference
    (wrong shape or row out of range).
    """
    m = ADDR_RE.match(text)
    if not m:
        return None
    letters, digits = m.group(1), m.group(2)
    if len(digits) > MAX_LITERAL_DIGITS:
        return None
    row = int(digits)
    if row < 1 or row > MAX_ROW:
        return None
    # Canonical key keeps the row as-is (no leading zeros can occur: the
    # regex allows them, so normalise them away here).
    key = f"{letters}{row}"
    return letters, row, key


def expand_range(top: str, bottom: str) -> Tuple[str, ...]:
    """Return all addresses inside the rectangle ``top:bottom``.

    Callers pass addresses produced by :func:`parse_address`, so both are
    guaranteed valid; raises :class:`ParseError` if the rectangle is too large.
    """
    tl = parse_address(top)
    br = parse_address(bottom)
    assert tl is not None and br is not None

    c1, r1 = column_to_index(tl[0]), tl[1]
    c2, r2 = column_to_index(br[0]), br[1]
    col_lo, col_hi = sorted((c1, c2))
    row_lo, row_hi = sorted((r1, r2))

    area = (col_hi - col_lo + 1) * (row_hi - row_lo + 1)
    if area > MAX_SUM_CELLS:
        raise ParseError("SUM range too large")

    members: List[str] = []
    for ci in range(col_lo, col_hi + 1):
        letters = index_to_column(ci)
        for r in range(row_lo, row_hi + 1):
            members.append(f"{letters}{r}")
    return tuple(members)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class Parser:
    def __init__(self, text: str) -> None:
        if len(text) > MAX_FORMULA_CHARS:
            raise ParseError("formula too long")
        self.text = text
        self.pos = 0
        self.length = len(text)

    def _skip_ws(self) -> None:
        while self.pos < self.length and self.text[self.pos].isspace():
            self.pos += 1

    def _peek(self) -> str:
        return self.text[self.pos] if self.pos < self.length else ""

    # entry point ----------------------------------------------------------

    def parse(self) -> Node:
        node = self._expr()
        self._skip_ws()
        if self.pos != self.length:
            raise ParseError("unexpected trailing characters")
        return node

    # grammar --------------------------------------------------------------

    def _expr(self) -> Node:
        node = self._term()
        while True:
            self._skip_ws()
            ch = self._peek()
            if ch in ("+", "-"):
                self.pos += 1
                node = Binary(ch, node, self._term())
            else:
                return node

    def _term(self) -> Node:
        node = self._factor()
        while True:
            self._skip_ws()
            if self._peek() == "*":
                self.pos += 1
                node = Binary("*", node, self._factor())
            else:
                return node

    def _factor(self) -> Node:
        self._skip_ws()
        ch = self._peek()
        if ch in ("+", "-"):
            self.pos += 1
            return Unary(ch, self._factor())
        return self._primary()

    def _primary(self) -> Node:
        self._skip_ws()
        ch = self._peek()
        if ch == "(":
            self.pos += 1
            node = self._expr()
            self._skip_ws()
            if self._peek() != ")":
                raise ParseError("missing closing parenthesis")
            self.pos += 1
            return node
        if ch.isdigit():
            return self._number()
        if "A" <= ch <= "Z":
            return self._reference_or_call()
        raise ParseError("expected integer, cell reference, SUM(...) or '('")

    def _number(self) -> Num:
        start = self.pos
        while self.pos < self.length and self.text[self.pos].isdigit():
            self.pos += 1
        digits = self.text[start:self.pos]
        if len(digits) > MAX_LITERAL_DIGITS:
            raise ParseError("integer literal too long")
        return Num(int(digits))

    def _reference_or_call(self) -> Node:
        letters = self._read_letters()
        ch = self._peek()
        if ch == "(":
            self.pos += 1
            if letters != "SUM":
                raise ParseError("unknown function")
            return self._sum_arguments()
        if ch.isspace():
            # Whitespace is fine before '(' of a call, but never inside a
            # cell reference token such as "AA1".
            self._skip_ws()
            if self._peek() == "(":
                self.pos += 1
                if letters != "SUM":
                    raise ParseError("unknown function")
                return self._sum_arguments()
            raise ParseError("malformed cell reference")
        if ch.isdigit():
            digits = self._read_digits()
            parsed = parse_address(letters + digits)
            if parsed is None:
                raise ParseError("cell reference out of range")
            return Ref(parsed[2])
        raise ParseError("malformed cell reference or function call")

    def _sum_arguments(self) -> Node:
        self._skip_ws()
        top = self._read_address_token()
        self._skip_ws()
        if self._peek() != ":":
            raise ParseError("SUM expects a range like A1:B3")
        self.pos += 1
        self._skip_ws()
        bottom = self._read_address_token()
        self._skip_ws()
        if self._peek() != ")":
            raise ParseError("missing ')' after SUM range")
        self.pos += 1
        members = expand_range(top, bottom)
        return SumRange(top, bottom, members)

    # token readers --------------------------------------------------------

    def _read_letters(self) -> str:
        start = self.pos
        while self.pos < self.length and "A" <= self.text[self.pos] <= "Z":
            self.pos += 1
        letters = self.text[start:self.pos]
        if not letters:
            raise ParseError("expected letters")
        return letters

    def _read_digits(self) -> str:
        start = self.pos
        while self.pos < self.length and self.text[self.pos].isdigit():
            self.pos += 1
        return self.text[start:self.pos]

    def _read_address_token(self) -> str:
        letters = self._read_letters()
        digits = self._read_digits()
        if not digits:
            raise ParseError("malformed cell reference")
        parsed = parse_address(letters + digits)
        if parsed is None:
            raise ParseError("cell reference out of range")
        return parsed[2]


def parse_formula(text: str) -> Node:
    """Parse a formula body (without the leading ``=``) into an AST."""
    if not text:
        raise ParseError("empty formula")
    return Parser(text).parse()


def iter_references(node: Node) -> List[str]:
    """Collect every cell reference appearing in an AST (de-duplicated)."""
    seen: set[str] = set()
    out: List[str] = []

    def visit(n: Node) -> None:
        if isinstance(n, Ref):
            if n.key not in seen:
                seen.add(n.key)
                out.append(n.key)
        elif isinstance(n, SumRange):
            for key in n.members:
                if key not in seen:
                    seen.add(key)
                    out.append(key)
        elif isinstance(n, Unary):
            visit(n.operand)
        elif isinstance(n, Binary):
            visit(n.left)
            visit(n.right)

    visit(node)
    return out
