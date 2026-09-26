"""Relative formula copying for ``POST /fill-formula``.

The formula at the *source* cell is copied to every *destination* cell, with
each cell reference translated by the (column, row) displacement between the
destination and the source.  Ordinary references and the two end points of
``SUM`` ranges are shifted; numbers, operators, parentheses and whitespace are
copied byte-for-byte.  Nothing is evaluated.

The original formula text is the single source of truth: every destination is
generated directly from it, never from another generated copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping

from .engine import _parse_cells, canonical_key, InvalidCellKey
from .parser import (
    MAX_FORMULA_CHARS,
    MAX_ROW,
    column_to_index,
    index_to_column,
    parse_address,
)

# Largest column expressible by the A1 grammar (three letters): "ZZZ".
MAX_COLUMN = column_to_index("ZZZ")


class FillFormulaError(Exception):
    """Request-level failure for ``/fill-formula``; always maps to HTTP 422."""


@dataclass(frozen=True)
class _RefToken:
    """A cell reference token located inside a formula body.

    ``text[start:end]`` is exactly the reference as written; ``key`` is its
    canonical address.
    """

    start: int
    end: int  # exclusive
    key: str


def _scan_ref_tokens(body: str) -> List[_RefToken]:
    """Locate every shifted reference token in an already validated body.

    This mirrors the lexical rules of :mod:`app.parser`: the ``SUM`` name and
    everything that is not a reference (digits, operators, parentheses, the
    ``:`` separator, whitespace) is skipped.  Only ordinary references and the
    two end points of a ``SUM`` range are returned, in source order.

    The caller guarantees ``body`` has already been accepted by
    :func:`app.parser.parse_formula`, so the grammar assumptions made here
    (one ``SUM`` call with exactly two ``A1:B2`` endpoints) cannot fail on
    real input.
    """
    tokens: List[_RefToken] = []
    length = len(body)
    pos = 0

    def skip_ws(p: int) -> int:
        while p < length and body[p].isspace():
            p += 1
        return p

    def read_letters(p: int) -> int:
        while p < length and "A" <= body[p] <= "Z":
            p += 1
        return p

    def read_digits(p: int) -> int:
        while p < length and body[p].isdigit():
            p += 1
        return p

    def emit(p: int) -> _RefToken:
        end = read_digits(read_letters(p))
        return _RefToken(p, end, body[p:end])

    while pos < length:
        ch = body[pos]
        if not ("A" <= ch <= "Z"):
            pos += 1
            continue

        start = pos
        after_letters = read_letters(pos)
        call_paren = skip_ws(after_letters)
        if call_paren < length and body[call_paren] == "(":
            # Function call; the grammar accepts only SUM(ref:ref). Whitespace
            # is allowed after "SUM" and around the endpoints/colon.
            pos = skip_ws(call_paren + 1)
            top = emit(pos)
            tokens.append(top)
            pos = skip_ws(top.end) + 1  # ':'
            pos = skip_ws(pos)
            bottom = emit(pos)
            tokens.append(bottom)
            pos = skip_ws(bottom.end) + 1  # ')'
            continue

        # An ordinary reference never has whitespace inside it (the parser
        # rejects e.g. "A 1"), so digits must follow the letters directly.
        if after_letters < length and body[after_letters].isdigit():
            end = read_digits(after_letters)
            tokens.append(_RefToken(start, end, body[start:end]))
            pos = end
        else:
            # Bare letters cannot occur in a validated formula.
            pos = after_letters

    return tokens


def _shift_key(key: str, delta_col: int, delta_row: int) -> str:
    """Translate one canonical address; reject results outside A1 range."""
    parsed = parse_address(key)
    assert parsed is not None  # tokens come from a validated formula
    letters, row, _ = parsed
    new_col = column_to_index(letters) + delta_col
    new_row = row + delta_row
    if not 1 <= new_col <= MAX_COLUMN or not 1 <= new_row <= MAX_ROW:
        raise FillFormulaError(
            f"shifted reference {key!r} falls outside the supported A1 "
            "address range"
        )
    return f"{index_to_column(new_col)}{new_row}"


def _render(
    body: str, tokens: List[_RefToken], delta_col: int, delta_row: int
) -> str:
    """Rebuild ``body`` with each reference token translated, rest verbatim."""
    parts: List[str] = []
    cursor = 0
    for token in tokens:
        parts.append(body[cursor:token.start])
        parts.append(_shift_key(token.key, delta_col, delta_row))
        cursor = token.end
    parts.append(body[cursor:])
    return "".join(parts)


def fill_formula(
    cells: Mapping[str, object],
    source: str,
    destinations: List[str],
) -> Dict[str, object]:
    """Copy the formula at ``source`` to each destination with shifted refs.

    Returns the complete updated cell table (canonical uppercase keys, sorted)
    ready for ``POST /evaluate``.  Raises :class:`FillFormulaError` (mapped to
    HTTP 422) on any invalid request; all new formulas are generated before
    the table is touched, so no partial result is ever produced.
    """
    # 1) Same key/value validation as /evaluate and /cycle-repair.
    parsed = _parse_cells(cells)
    # Canonicalised view of the submitted table (keys are unique here:
    # _parse_cells rejects two raw keys that normalise to the same address).
    table = {
        canonical_key(str(raw_key)): value for raw_key, value in cells.items()
    }

    try:
        source_key = canonical_key(str(source))
    except InvalidCellKey:
        raise FillFormulaError(
            f"invalid source address {source!r}; expected A1-style address"
        )

    # 2) Canonicalise destinations; duplicates (also after normalisation) 422.
    dest_keys: List[str] = []
    seen: set[str] = set()
    for raw in destinations:
        try:
            key = canonical_key(str(raw))
        except InvalidCellKey:
            raise FillFormulaError(
                f"invalid destination address {raw!r}; "
                "expected A1-style address"
            )
        if key in seen:
            raise FillFormulaError(f"duplicate destination address {key!r}")
        seen.add(key)
        dest_keys.append(key)

    # 3) The source must hold a syntactically valid formula.
    source_ast = parsed.asts.get(source_key)
    if source_ast is None or source_key in parsed.syntax_nodes:
        raise FillFormulaError(
            f"source {source_key!r} must be a syntactically valid formula"
        )

    # Recover the exact original text (whitespace included).
    original = table[source_key]
    assert isinstance(original, str) and original.startswith("=")
    body = original[1:]
    tokens = _scan_ref_tokens(body)

    src = parse_address(source_key)
    assert src is not None
    src_col = column_to_index(src[0])
    src_row = src[1]

    # 4) Generate every formula up front: any out-of-range shift or over-long
    # result aborts the whole request before the table is modified.
    generated: Dict[str, str] = {}
    for dest in dest_keys:
        dst = parse_address(dest)
        assert dst is not None
        delta_col = column_to_index(dst[0]) - src_col
        delta_row = dst[1] - src_row
        new_body = _render(body, tokens, delta_col, delta_row)
        if len(new_body) > MAX_FORMULA_CHARS:
            raise FillFormulaError(
                f"generated formula for {dest!r} is {len(new_body)} "
                f"characters long; limit is {MAX_FORMULA_CHARS}"
            )
        generated[dest] = "=" + new_body

    # 5) Start from the submitted table and let destinations overwrite
    # whatever was there.
    table.update(generated)
    return {key: table[key] for key in sorted(table)}
