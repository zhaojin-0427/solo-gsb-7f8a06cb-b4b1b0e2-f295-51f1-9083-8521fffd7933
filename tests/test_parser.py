"""Unit tests for the formula parser."""

import pytest

from app.parser import (
    ParseError,
    Num,
    Ref,
    Unary,
    Binary,
    SumRange,
    parse_formula,
    expand_range,
    iter_references,
    parse_address,
)


def parse(text):
    return parse_formula(text)


class TestLiteralsAndArithmetic:
    def test_integer(self):
        assert parse("42") == Num(42)

    def test_addition(self):
        assert parse("1+2") == Binary("+", Num(1), Num(2))

    def test_precedence(self):
        ast = parse("1+2*3")
        assert ast == Binary("+", Num(1), Binary("*", Num(2), Num(3)))

    def test_left_associativity(self):
        ast = parse("10-2-3")
        assert ast == Binary(
            "-", Binary("-", Num(10), Num(2)), Num(3)
        )

    def test_parentheses_change_precedence(self):
        ast = parse("(1+2)*3")
        assert ast == Binary("*", Binary("+", Num(1), Num(2)), Num(3))

    def test_unary_minus(self):
        assert parse("-5") == Unary("-", Num(5))

    def test_double_unary(self):
        assert parse("--5") == Unary("-", Unary("-", Num(5)))

    def test_unary_plus(self):
        assert parse("+5") == Unary("+", Num(5))

    def test_unary_before_paren(self):
        assert parse("-(1+2)") == Unary(
            "-", Binary("+", Num(1), Num(2))
        )

    def test_whitespace_everywhere(self):
        ast = parse("  1  + ( -2 ) * A1 ")
        assert ast == Binary(
            "+",
            Num(1),
            Binary("*", Unary("-", Num(2)), Ref("A1")),
        )

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "1+",
            "*3",
            "1 2",
            "1++",
            "(1+2",
            "1+2)",
            "()",
            "1//2",
            "1/2",
            "2^3",
            "=1",
            "abc",
            "A",
            "1A",
            "A1+",
            "A 1",
            "1..2",
        ],
    )
    def test_syntax_errors(self, text):
        with pytest.raises(ParseError):
            parse(text)


class TestReferences:
    def test_simple_reference(self):
        assert parse("A1") == Ref("A1")

    def test_multi_letter_column(self):
        assert parse("AA12") == Ref("AA12")
        assert parse("XFD1") == Ref("XFD1")

    def test_four_letter_column_invalid(self):
        with pytest.raises(ParseError):
            parse("AAAA1")

    def test_row_zero_invalid(self):
        with pytest.raises(ParseError):
            parse("A0")

    def test_row_too_large_invalid(self):
        with pytest.raises(ParseError):
            parse("A1048577")

    def test_leading_zero_row_normalised(self):
        assert parse("A007") == Ref("A7")


class TestSum:
    def test_basic_range(self):
        ast = parse("SUM(A1:B3)")
        assert isinstance(ast, SumRange)
        assert ast.top == "A1"
        assert ast.bottom == "B3"
        assert ast.members[0] == "A1"
        assert ast.members[-1] == "B3"
        assert len(ast.members) == 6

    def test_range_in_expression(self):
        ast = parse("1 + SUM(A1:A2) * 2")
        assert isinstance(ast, Binary)
        assert isinstance(ast.right, Binary)
        assert isinstance(ast.right.left, SumRange)

    def test_unary_sum(self):
        assert parse("-SUM(A1:B2)") == Unary(
            "-", parse("SUM(A1:B2)")
        )

    @pytest.mark.parametrize(
        "text",
        [
            "SUM()",
            "SUM(A1)",
            "SUM(A1:)",
            "SUM(:A1)",
            "SUM(A1 B2)",
            "SUM(A1:B2",
            "SUM(A1:B2))",
            "AVG(A1:B2)",
            "SUMX(A1:B2)",
            "SUM(1:2)",
            "SUM(A:B)",
            "SUM(A1:B3, A1:B3)",
        ],
    )
    def test_sum_syntax_errors(self, text):
        with pytest.raises(ParseError):
            parse(text)

    def test_reversed_range_is_normalised(self):
        members = expand_range("B2", "A1")
        assert members[0] == "A1"
        assert members[-1] == "B2"
        assert len(members) == 4

    def test_single_cell_range(self):
        assert expand_range("A1", "A1") == ("A1",)

    def test_iter_references_flattens_range(self):
        ast = parse("A1 + SUM(B1:B3)")
        refs = iter_references(ast)
        assert refs == ["A1", "B1", "B2", "B3"]


class TestAddressHelpers:
    def test_parse_address(self):
        assert parse_address("A1") == ("A", 1, "A1")
        assert parse_address("ZZ99") == ("ZZ", 99, "ZZ99")
        assert parse_address("a1") is None  # caller must upper-case first

    def test_round_trip_column_index(self):
        from app.parser import column_to_index, index_to_column

        for name in ("A", "Z", "AA", "AZ", "ZZ", "AAA", "XFD"):
            assert index_to_column(column_to_index(name)) == name
