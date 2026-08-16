"""Tests for `fabulexa_forge._sql`: the one predicate-rendering authority.

Covers `render_predicate_condition` — the sole place that renders `=` or `IN`
over a config predicate value — plus its composition of `render_typed_literal`
and `quote_identifier`. `render_typed_literal` / `is_recognized_sql_type` /
`quote_identifier` themselves are exercised indirectly through the authority;
this module owns their contract as exposed through it. Also covers
`cast_predicate_element` — the plan-time constant evaluation of the same CAST
`render_typed_literal` compiles (source-row-selection sprint § The
constant-column gate) — including the '5'/'05' typed-value-identity case its
docstring names, which the event-source disjointness gate (a later phase)
relies on. Also covers `render_date_parse_expr` — the one VARCHAR->DATE parse
renderer every mode (dimensional, source, base) shares (temporal-elections
sprint Phase 4).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import duckdb
import pytest

from fabulexa_forge._sql import (
    cast_predicate_element,
    quote_identifier,
    render_date_parse_expr,
    render_predicate_condition,
)
from fabulexa_forge.errors import ExportError

# ---------------------------------------------------------------------------
# Scalar rendering — byte-identical to a manual render_typed_literal composition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column,value,sql_type,expected",
    [
        ("prop__status", "active", "VARCHAR", "\"prop__status\" = 'active'"),
        ("prop__count", "7", "BIGINT", "\"prop__count\" = CAST('7' AS BIGINT)"),
        (
            "prop__ready",
            "true",
            "BOOLEAN",
            "\"prop__ready\" = CAST('true' AS BOOLEAN)",
        ),
        (
            "prop__amount",
            "9.99",
            "DECIMAL(10,2)",
            "\"prop__amount\" = CAST('9.99' AS DECIMAL(10,2))",
        ),
    ],
)
def test_scalar_renders_equals_typed_literal(
    column: str, value: str, sql_type: str, expected: str
) -> None:
    """A scalar value renders `"col" = <typed literal>` for each recommended
    column type, byte-identical to a manual `render_typed_literal` composition."""
    assert render_predicate_condition(column, value, sql_type, None) == expected


def test_scalar_varchar_renders_raw_single_quoted_literal() -> None:
    """VARCHAR renders the raw single-quoted literal, no CAST — the history
    `value` surface's untyped comparison."""
    rendered = render_predicate_condition("value", "completed", "VARCHAR", None)
    assert rendered == "\"value\" = 'completed'"


# ---------------------------------------------------------------------------
# List rendering — IN, preserving element order
# ---------------------------------------------------------------------------


def test_one_element_list_renders_in_not_equals() -> None:
    """A one-element list still renders `IN`, never collapses to `=`."""
    rendered = render_predicate_condition(
        "prop__decision_type", ["referred"], "VARCHAR", None
    )
    assert rendered == "\"prop__decision_type\" IN ('referred')"


def test_multi_element_list_preserves_config_order() -> None:
    """A multi-element list renders `IN` with elements in config order, not
    sorted or deduplicated by the renderer."""
    rendered = render_predicate_condition(
        "prop__decision_type",
        ["referred", "admitted", "discharged"],
        "VARCHAR",
        None,
    )
    assert rendered == (
        "\"prop__decision_type\" IN ('referred', 'admitted', 'discharged')"
    )


def test_list_elements_typed_individually() -> None:
    """Each list element is typed by `render_typed_literal` against sql_type —
    a list is exactly the scalar rule applied element-wise."""
    rendered = render_predicate_condition(
        "prop__priority", ["1", "2", "3"], "BIGINT", None
    )
    assert rendered == (
        "\"prop__priority\" IN (CAST('1' AS BIGINT),"
        " CAST('2' AS BIGINT), CAST('3' AS BIGINT))"
    )


# ---------------------------------------------------------------------------
# Alias qualification
# ---------------------------------------------------------------------------


def test_alias_none_leaves_column_unqualified() -> None:
    """alias=None produces an unqualified condition."""
    rendered = render_predicate_condition("elem__role", "lead", "VARCHAR", None)
    assert rendered == "\"elem__role\" = 'lead'"


def test_alias_qualifies_scalar_condition() -> None:
    """A non-None alias qualifies the column as `<alias>."<column>"`, spliced
    verbatim — the point-in-time membership FK's correlated-subquery form."""
    rendered = render_predicate_condition("elem__role", "lead", "VARCHAR", "h")
    assert rendered == "h.\"elem__role\" = 'lead'"


def test_alias_qualifies_list_condition() -> None:
    """An alias qualifies the column in the IN form too."""
    rendered = render_predicate_condition(
        "elem__role", ["lead", "backup"], "VARCHAR", "h"
    )
    assert rendered == "h.\"elem__role\" IN ('lead', 'backup')"


# ---------------------------------------------------------------------------
# str discrimination — never treated as a Sequence of characters
# ---------------------------------------------------------------------------


def test_single_char_str_value_takes_scalar_branch() -> None:
    """Discrimination is on `isinstance(value, str)`, never `Sequence` — a
    single-character string renders `=`, not `IN` over its (one) character."""
    rendered = render_predicate_condition("prop__code", "a", "VARCHAR", None)
    assert rendered == "\"prop__code\" = 'a'"


def test_multi_char_str_value_never_iterated() -> None:
    """A multi-character string is one scalar literal, never split into an
    IN list of its characters."""
    rendered = render_predicate_condition("prop__code", "abc", "VARCHAR", None)
    assert rendered == "\"prop__code\" = 'abc'"
    assert "IN" not in rendered


# ---------------------------------------------------------------------------
# Refusal — unrecognized SQL type
# ---------------------------------------------------------------------------


def test_unrecognized_type_raises_export_error() -> None:
    """An unrecognized SQL type raises ExportError — no silent VARCHAR fallback."""
    with pytest.raises(ExportError, match="unrecognized SQL type"):
        render_predicate_condition("prop__blob_col", "x", "BLOB", None)


def test_unrecognized_type_raises_for_list_value_too() -> None:
    """The list branch raises the same ExportError for an unrecognized type,
    on the first offending element."""
    with pytest.raises(ExportError, match="unrecognized SQL type"):
        render_predicate_condition("prop__blob_col", ["x", "y"], "BLOB", None)


# ---------------------------------------------------------------------------
# Column quoting
# ---------------------------------------------------------------------------


def test_column_quoted_via_quote_identifier() -> None:
    """The column name is quoted exactly as `quote_identifier` would render it,
    including doubling an embedded quote."""
    rendered = render_predicate_condition('weird"col', "v", "VARCHAR", None)
    assert rendered.startswith(quote_identifier('weird"col'))


# ---------------------------------------------------------------------------
# cast_predicate_element — the plan-time constant evaluation of the CAST
# render_typed_literal compiles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "element,sql_type,expected",
    [
        ("hello", "VARCHAR", "hello"),
        ("7", "BIGINT", 7),
        ("7", "SMALLINT", 7),
        ("3.5", "DOUBLE", 3.5),
        ("true", "BOOLEAN", True),
        ("false", "BOOLEAN", False),
        ("9.99", "DECIMAL(10,2)", Decimal("9.99")),
    ],
)
def test_cast_predicate_element_typed_value(
    element: str, sql_type: str, expected: object
) -> None:
    """Each recognized type family casts its element to the matching Python
    type — the same value `render_typed_literal`'s CAST would produce."""
    assert cast_predicate_element(element, sql_type) == expected


@pytest.mark.parametrize(
    "true_literal",
    ["true", "TRUE", "t", "T", "yes", "Yes", "y", "1"],
)
def test_cast_predicate_element_boolean_true_literals(true_literal: str) -> None:
    """Every DuckDB-recognized truthy literal (case-insensitive) casts True."""
    assert cast_predicate_element(true_literal, "BOOLEAN") is True


@pytest.mark.parametrize(
    "false_literal",
    ["false", "FALSE", "f", "F", "no", "No", "n", "0"],
)
def test_cast_predicate_element_boolean_false_literals(false_literal: str) -> None:
    """Every DuckDB-recognized falsy literal (case-insensitive) casts False."""
    assert cast_predicate_element(false_literal, "BOOLEAN") is False


def test_cast_predicate_element_bigint_two_spellings_equate() -> None:
    """'5' and '05' under BIGINT are one typed value — `==` and `hash` both
    realize typed-value identity, never string identity (the disjointness
    gate's comparison set relies on this)."""
    five = cast_predicate_element("5", "BIGINT")
    zero_five = cast_predicate_element("05", "BIGINT")
    assert five == zero_five
    assert hash(five) == hash(zero_five)
    assert {five, zero_five} == {five}


def test_cast_predicate_element_bigint_distinct_values_not_equal() -> None:
    """Two genuinely distinct BIGINT values are not equal — the equating
    above is about spelling, not about collapsing every value to one."""
    assert cast_predicate_element("5", "BIGINT") != cast_predicate_element(
        "6", "BIGINT"
    )


@pytest.mark.parametrize(
    "element,sql_type",
    [
        ("abc", "BIGINT"),
        ("1.5", "BIGINT"),
        ("", "BIGINT"),
        ("abc", "DOUBLE"),
        ("maybe", "BOOLEAN"),
        ("abc", "DECIMAL(10,2)"),
    ],
)
def test_cast_predicate_element_uncastable_raises_value_error(
    element: str, sql_type: str
) -> None:
    """An element the declared type cannot cast raises ValueError naming the
    element — the caller wraps this into `SourceWhereValueUncastable`."""
    with pytest.raises(ValueError) as excinfo:
        cast_predicate_element(element, sql_type)
    assert f"{element!r} does not cast to {sql_type}" == str(excinfo.value)


def test_cast_predicate_element_unrecognized_type_raises_export_error() -> None:
    """An unrecognized SQL type raises `ExportError` — never a silent VARCHAR
    fallback, matching `render_typed_literal`'s posture."""
    with pytest.raises(ExportError, match="unrecognized SQL type"):
        cast_predicate_element("x", "BLOB")


def test_cast_predicate_element_returns_hashable_value() -> None:
    """Every returned typed value is hashable — the disjointness gate builds
    a set from `typed_values` across `where` entries."""
    hash(cast_predicate_element("hello", "VARCHAR"))
    hash(cast_predicate_element("7", "BIGINT"))
    hash(cast_predicate_element("true", "BOOLEAN"))
    hash(cast_predicate_element("9.99", "DECIMAL(10,2)"))


# ---------------------------------------------------------------------------
# render_date_parse_expr — the one VARCHAR->DATE parse renderer
# ---------------------------------------------------------------------------


def _run_date_parse(
    value: str | None,
    date_format: str,
    *,
    table_label: str = "visits",
    from_table: str = "visits",
    column_name: str = "dob",
) -> object:
    """Execute render_date_parse_expr's SQL fragment against a one-row table.

    `from_table` is the physical table the SELECT reads from; `table_label`
    is the (independently-controlled) name spliced into the guard's error
    message — separate parameters so a test can prove attribution uses
    `table_label`, not the physical FROM table.
    """
    conn = duckdb.connect(":memory:")
    conn.execute(f'CREATE TABLE "{from_table}" ("{column_name}" VARCHAR)')
    conn.execute(f'INSERT INTO "{from_table}" VALUES (?)', [value])
    expr = render_date_parse_expr(
        f'"{from_table}"."{column_name}"', date_format, "parsed", table_label
    )
    try:
        row = conn.execute(f'SELECT {expr} FROM "{from_table}"').fetchone()
    finally:
        conn.close()
    assert row is not None
    return row[0]


def test_render_date_parse_expr_match_yields_date() -> None:
    """A value matching the format parses to the correct DATE."""
    assert _run_date_parse("2024-01-15", "%Y-%m-%d") == date(2024, 1, 15)


def test_render_date_parse_expr_null_source_yields_null() -> None:
    """A NULL source value yields NULL, not a parse failure."""
    assert _run_date_parse(None, "%Y-%m-%d") is None


def test_render_date_parse_expr_mismatch_names_table_column_value() -> None:
    """A non-matching non-NULL value fails loudly, naming table_label, the
    source column, and the offending value — never a silent NULL."""
    conn = duckdb.connect(":memory:")
    conn.execute('CREATE TABLE "_grain" ("dob" VARCHAR)')
    conn.execute('INSERT INTO "_grain" VALUES (?)', ["not-a-date"])
    expr = render_date_parse_expr('"_grain"."dob"', "%Y-%m-%d", "birth_date", "visits")
    try:
        with pytest.raises(duckdb.Error) as excinfo:
            conn.execute(f'SELECT {expr} FROM "_grain"').fetchall()
    finally:
        conn.close()
    message = str(excinfo.value)
    assert "visits" in message
    assert "dob" in message
    assert "not-a-date" in message
    assert "%Y-%m-%d" in message


def test_render_date_parse_expr_table_label_independent_of_from_table() -> None:
    """The guard names table_label, not the physical FROM table it reads."""
    conn = duckdb.connect(":memory:")
    conn.execute('CREATE TABLE "_grain" ("dob" VARCHAR)')
    conn.execute('INSERT INTO "_grain" VALUES (?)', ["nope"])
    expr = render_date_parse_expr(
        '"_grain"."dob"', "%Y-%m-%d", "birth_date", "output_table"
    )
    try:
        with pytest.raises(duckdb.Error) as excinfo:
            conn.execute(f'SELECT {expr} FROM "_grain"').fetchall()
    finally:
        conn.close()
    assert "output_table" in str(excinfo.value)


def test_render_date_parse_expr_quotes_splice_safely() -> None:
    """A table_label containing a single quote splices safely (no SQL
    breakout) and its unescaped text survives into the raised error."""
    value = _run_date_parse(
        "2024-01-15", "%Y-%m-%d", table_label="vi'sits", column_name="dob"
    )
    assert value == date(2024, 1, 15)

    conn = duckdb.connect(":memory:")
    conn.execute('CREATE TABLE "_grain" ("dob" VARCHAR)')
    conn.execute('INSERT INTO "_grain" VALUES (?)', ["bad-value"])
    expr = render_date_parse_expr('"_grain"."dob"', "%Y-%m-%d", "parsed", "vi'sits")
    try:
        with pytest.raises(duckdb.Error) as excinfo:
            conn.execute(f'SELECT {expr} FROM "_grain"').fetchall()
    finally:
        conn.close()
    assert "vi'sits" in str(excinfo.value)
