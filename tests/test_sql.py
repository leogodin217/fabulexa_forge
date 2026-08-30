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
relies on. Also covers `date_parse_denoted_type` — the single DATE/TIME/
TIMESTAMP denotation authority — and `render_date_parse_expr`, the one
VARCHAR->{DATE,TIME,TIMESTAMP} parse renderer every mode (dimensional,
source, base) shares (temporal-elections sprint Phase 4;
scd2-derived-temporal-parse sprint Phase 1 widens the family beyond DATE).
Also covers `render_decimal_expr` / `render_json_precision_expr` /
`forge_json_precision` / `register_render_functions` — the two value-
rendering-election authorities (value-rendering-elections sprint Phase 1).
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

import duckdb
import pytest

from fabulexa_forge._sql import (
    cast_predicate_element,
    date_parse_denoted_type,
    forge_json_precision,
    quote_identifier,
    register_render_functions,
    render_date_parse_expr,
    render_decimal_expr,
    render_json_precision_expr,
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
# render_date_parse_expr — the one VARCHAR->{DATE,TIME,TIMESTAMP} parse
# renderer, denoted by the format (date_parse_denoted_type)
# ---------------------------------------------------------------------------


def _execute_date_parse(
    value: str | None,
    date_format: str,
    *,
    table_label: str = "visits",
    from_table: str = "visits",
    column_name: str = "dob",
) -> tuple[object, str]:
    """Execute render_date_parse_expr's SQL fragment against a one-row table,
    returning the parsed value and the fragment's DuckDB type name.

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
        row = conn.execute(
            f"SELECT parsed, typeof(parsed)"
            f' FROM (SELECT {expr} FROM "{from_table}") "_typed"'
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row[0], row[1]


def _run_date_parse(
    value: str | None,
    date_format: str,
    *,
    table_label: str = "visits",
    from_table: str = "visits",
    column_name: str = "dob",
) -> object:
    """Execute render_date_parse_expr's SQL fragment, returning only the
    parsed value (see `_execute_date_parse` for the value+type pair)."""
    return _execute_date_parse(
        value,
        date_format,
        table_label=table_label,
        from_table=from_table,
        column_name=column_name,
    )[0]


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


# ---------------------------------------------------------------------------
# date_parse_denoted_type — the single denotation authority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fmt,expected",
    [
        ("%Y-%m-%d", "DATE"),
        ("%d %B %Y", "DATE"),
        ("%Y/%b/%d", "DATE"),
        ("%Y-%m-%d %H:%M:%S", "TIMESTAMP"),
        ("%Y-%m-%d %I:%M %p", "TIMESTAMP"),
        ("%H:%M", "TIME"),
        ("%I:%M %p", "TIME"),
    ],
)
def test_date_parse_denoted_type(fmt: str, expected: str) -> None:
    """A date-only format denotes DATE, a date+time format denotes
    TIMESTAMP, a time-only format denotes TIME — across directive variants
    (%I+%p, %b/%B)."""
    assert date_parse_denoted_type(fmt) == expected


# ---------------------------------------------------------------------------
# The renderer emits the denoted type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_value,date_format,expected_type,expected_value",
    [
        (
            "2026-08-17 14:30:05",
            "%Y-%m-%d %H:%M:%S",
            "TIMESTAMP",
            datetime(2026, 8, 17, 14, 30, 5),
        ),
        ("14:30:05", "%H:%M:%S", "TIME", time(14, 30, 5)),
        ("2024-01-15", "%Y-%m-%d", "DATE", date(2024, 1, 15)),
    ],
    ids=["timestamp", "time", "date"],
)
def test_render_date_parse_expr_denotation_type(
    raw_value: str, date_format: str, expected_type: str, expected_value: object
) -> None:
    """A datetime format's fragment is typed TIMESTAMP, a time-only format's
    is typed TIME, and a date-only format's is typed DATE (existing behavior,
    unchanged) — each parsing to the correct value."""
    value, sql_type = _execute_date_parse(raw_value, date_format)
    assert sql_type == expected_type
    assert value == expected_value


# ---------------------------------------------------------------------------
# NULL source yields NULL of the denoted type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("date_format", ["%H:%M:%S", "%Y-%m-%d %H:%M:%S"])
def test_render_date_parse_expr_null_source_yields_null_of_denoted_type(
    date_format: str,
) -> None:
    """A NULL source value yields NULL of the format's denoted type for the
    TIME and TIMESTAMP denotations too (DATE is covered by the existing
    `test_render_date_parse_expr_null_source_yields_null`)."""
    value, sql_type = _execute_date_parse(None, date_format)
    assert value is None
    assert sql_type == date_parse_denoted_type(date_format)


# ---------------------------------------------------------------------------
# Mismatch error names table, column, and value — all three denotations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("date_format", ["%H:%M:%S", "%Y-%m-%d %H:%M:%S"])
def test_render_date_parse_expr_mismatch_names_table_column_value_time_family(
    date_format: str,
) -> None:
    """The loud mismatch error names table, column, and offending value for
    the TIME and TIMESTAMP denotations too (DATE is covered by the existing
    `test_render_date_parse_expr_mismatch_names_table_column_value`)."""
    conn = duckdb.connect(":memory:")
    conn.execute('CREATE TABLE "_grain" ("dob" VARCHAR)')
    conn.execute('INSERT INTO "_grain" VALUES (?)', ["not-a-value"])
    expr = render_date_parse_expr('"_grain"."dob"', date_format, "parsed", "visits")
    try:
        with pytest.raises(duckdb.Error) as excinfo:
            conn.execute(f'SELECT {expr} FROM "_grain"').fetchall()
    finally:
        conn.close()
    message = str(excinfo.value)
    assert "visits" in message
    assert "dob" in message
    assert "not-a-value" in message
    assert date_format in message


# ---------------------------------------------------------------------------
# Value preservation: round-trip to the source string, zero-fill included
# ---------------------------------------------------------------------------


def test_render_date_parse_expr_zero_fills_absent_lower_order_fields() -> None:
    """A datetime format missing seconds zero-fills seconds; the parsed
    value round-trips to the source string under the declared format."""
    value, _ = _execute_date_parse("2026-08-17 14:30", "%Y-%m-%d %H:%M")
    assert value == datetime(2026, 8, 17, 14, 30, 0)
    assert value.strftime("%Y-%m-%d %H:%M") == "2026-08-17 14:30"


@pytest.mark.parametrize(
    "value_str,date_format",
    [
        ("2024-01-15", "%Y-%m-%d"),
        ("14:30:05", "%H:%M:%S"),
        ("2026-08-17 14:30:05", "%Y-%m-%d %H:%M:%S"),
        ("02:30 PM", "%I:%M %p"),
    ],
)
def test_render_date_parse_expr_round_trips_to_source_string(
    value_str: str, date_format: str
) -> None:
    """The parsed value round-trips to its source string under the declared
    format, across all three denotations."""
    value, _ = _execute_date_parse(value_str, date_format)
    assert value.strftime(date_format) == value_str


def test_render_date_parse_expr_fraction_f_parses_at_microseconds() -> None:
    """`%f` parses fractional seconds at microsecond precision."""
    value, _ = _execute_date_parse("14:30:05.123456", "%H:%M:%S.%f")
    assert value == time(14, 30, 5, 123456)


def test_render_date_parse_expr_fraction_g_widens_milliseconds_to_microseconds() -> (
    None
):
    """`%g` milliseconds widen exactly to microseconds (spliced into
    STRPTIME as `%f`, which zero-pads a shorter fraction on the right)."""
    value, _ = _execute_date_parse("14:30:05.123", "%H:%M:%S.%g")
    assert value == time(14, 30, 5, 123000)


# ---------------------------------------------------------------------------
# render_decimal_expr — the one DOUBLE->DECIMAL(p,s) rendering authority
# ---------------------------------------------------------------------------


def _execute_decimal(
    value: float | None,
    precision: int,
    scale: int,
    *,
    table_label: str = "visits",
    column_label: str = "amount",
    from_table: str = "visits",
) -> tuple[object, str]:
    """Execute render_decimal_expr's SQL fragment against a one-row DOUBLE
    table, returning the rendered value and the fragment's DuckDB type name.
    """
    conn = duckdb.connect(":memory:")
    conn.execute(f'CREATE TABLE "{from_table}" ("{column_label}" DOUBLE)')
    conn.execute(f'INSERT INTO "{from_table}" VALUES (?)', [value])
    expr = render_decimal_expr(
        f'"{from_table}"."{column_label}"', precision, scale, column_label, table_label
    )
    try:
        row = conn.execute(
            f'SELECT {expr}, typeof({expr}) FROM "{from_table}"'
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row[0], row[1]


def _raises_decimal_error(
    value: float,
    precision: int,
    scale: int,
    *,
    table_label: str = "visits",
    column_label: str = "amount",
) -> str:
    """Execute render_decimal_expr expecting a DuckDB error; return its
    message text."""
    conn = duckdb.connect(":memory:")
    conn.execute(f'CREATE TABLE "visits" ("{column_label}" DOUBLE)')
    conn.execute('INSERT INTO "visits" VALUES (?)', [value])
    expr = render_decimal_expr(
        f'"visits"."{column_label}"', precision, scale, column_label, table_label
    )
    try:
        with pytest.raises(duckdb.Error) as excinfo:
            conn.execute(f'SELECT {expr} FROM "visits"').fetchall()
    finally:
        conn.close()
    return str(excinfo.value)


def test_render_decimal_expr_rounds_to_exact_scale() -> None:
    """A value with more fraction digits than the declared scale rounds to
    exactly `s` fraction digits."""
    value, sql_type = _execute_decimal(1.23456, 6, 2)
    assert value == Decimal("1.23")
    assert sql_type == "DECIMAL(6,2)"


@pytest.mark.parametrize(
    "value,expected",
    [(2.5, Decimal("3")), (-2.5, Decimal("-3"))],
)
def test_render_decimal_expr_exact_binary_half_rounds_away_from_zero(
    value: float, expected: Decimal
) -> None:
    """An exact binary half (2.5 is exact in DOUBLE) rounds away from zero
    under DECIMAL(2,0), positive and negative."""
    rendered, _ = _execute_decimal(value, 2, 0)
    assert rendered == expected


def test_render_decimal_expr_null_yields_typed_null() -> None:
    """A NULL source value yields NULL of the declared DECIMAL(p,s) type,
    not a guard failure."""
    value, sql_type = _execute_decimal(None, 6, 2)
    assert value is None
    assert sql_type == "DECIMAL(6,2)"


def test_render_decimal_expr_integer_digit_overflow_raises_named_error() -> None:
    """A value overflowing the declared precision raises in SQL, naming
    table, column, and the offending value."""
    message = _raises_decimal_error(
        12345.6, 4, 0, table_label="visits", column_label="amount"
    )
    assert "visits" in message
    assert "amount" in message
    assert "12345.6" in message


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_render_decimal_expr_nan_and_infinity_raise(value: float) -> None:
    """NaN and +/-Infinity are not representable as DECIMAL and raise the
    same enriched conversion error."""
    message = _raises_decimal_error(value, 4, 0)
    assert "visits" in message
    assert "amount" in message


def test_render_decimal_expr_scale_zero_renders_bare_integer() -> None:
    """`s = 0` renders a DECIMAL with no fractional part."""
    value, _ = _execute_decimal(17.4, 4, 0)
    assert value == Decimal("17")


def test_render_decimal_expr_negative_value_rounds_correctly() -> None:
    """A negative value rounds to the declared scale, sign preserved."""
    value, _ = _execute_decimal(-17.4, 4, 0)
    assert value == Decimal("-17")


# ---------------------------------------------------------------------------
# forge_json_precision — the JSON-leaf rendering scalar (pure function)
# ---------------------------------------------------------------------------


def test_forge_json_precision_declared_leaf_replaced_byte_identical_around_it() -> None:
    """A declared present numeric leaf is replaced in place; whitespace, key
    order, and undeclared values are byte-identical around it."""
    payload = '{ "b": 2, "amount": 1.005 , "a": [1,2,3] }'
    rendered = forge_json_precision(payload, '{"amount": 2}', "amount", "t")
    assert rendered == '{ "b": 2, "amount": 1.01 , "a": [1,2,3] }'


def test_forge_json_precision_absent_key_leaves_payload_unchanged() -> None:
    """A declared leaf key absent from the payload leaves it unchanged."""
    payload = '{"other": 1.5}'
    rendered = forge_json_precision(payload, '{"amount": 2}', "amount", "t")
    assert rendered == payload


def test_forge_json_precision_null_leaf_left_verbatim() -> None:
    """A declared leaf present as JSON `null` is left verbatim (missingness,
    not a contradiction)."""
    payload = '{"amount": null}'
    rendered = forge_json_precision(payload, '{"amount": 2}', "amount", "t")
    assert rendered == payload


def test_forge_json_precision_non_numeric_leaf_raises_named_error() -> None:
    """A declared leaf whose value is not numeric (and not null) raises
    ValueError naming table, column, and key."""
    with pytest.raises(ValueError) as excinfo:
        forge_json_precision('{"amount": "abc"}', '{"amount": 2}', "amount", "orders")
    message = str(excinfo.value)
    assert "orders" in message
    assert "amount" in message


def test_forge_json_precision_duplicate_top_level_key_raises() -> None:
    """A duplicate top-level key among the declared leaves raises."""
    with pytest.raises(ValueError, match="duplicate top-level key"):
        forge_json_precision(
            '{"amount": 1.5, "amount": 2.5}', '{"amount": 2}', "amount", "orders"
        )


@pytest.mark.parametrize(
    "payload",
    ["[1, 2, 3]", "{not valid json", "null", '"just a string"'],
)
def test_forge_json_precision_non_object_or_unparseable_payload_raises(
    payload: str,
) -> None:
    """A payload that is not a JSON object — a non-object top-level value,
    or plain-unparseable text — raises."""
    with pytest.raises(ValueError):
        forge_json_precision(payload, '{"amount": 2}', "amount", "orders")


def test_forge_json_precision_none_payload_returns_none() -> None:
    """A None payload (SQL NULL) returns None regardless of leaves."""
    assert forge_json_precision(None, '{"amount": 2}', "amount", "orders") is None


def test_forge_json_precision_exponent_token_rounds_to_plain_form() -> None:
    """An exponent-form number token rounds to plain decimal notation."""
    rendered = forge_json_precision('{"amount": 6.5e1}', '{"amount": 1}', "amount", "t")
    assert rendered == '{"amount": 65.0}'


def test_forge_json_precision_negative_rounds_to_zero_without_sign() -> None:
    """A negative value that rounds to zero renders unsigned (no negative
    zero)."""
    rendered = forge_json_precision(
        '{"amount": -0.001}', '{"amount": 2}', "amount", "t"
    )
    assert rendered == '{"amount": 0.00}'


def test_forge_json_precision_zero_digits_renders_bare_integer() -> None:
    """`digits == 0` renders the bare integer, no decimal point."""
    rendered = forge_json_precision('{"amount": 3.7}', '{"amount": 0}', "amount", "t")
    assert rendered == '{"amount": 4}'


def test_forge_json_precision_same_named_nested_key_not_touched() -> None:
    """A same-named key nested deeper (not top-level) is not touched — only
    the top-level declared leaf is replaced."""
    payload = '{"amount": 1.005, "meta": {"amount": 9.999}}'
    rendered = forge_json_precision(payload, '{"amount": 2}', "amount", "t")
    assert rendered == '{"amount": 1.01, "meta": {"amount": 9.999}}'


def test_forge_json_precision_exact_decimal_half_never_float64_reparse() -> None:
    """`0.005` rounded to 2 digits is `0.01` — exact decimal arithmetic on
    the token text, never a float64 re-parse (which would round 0.005 down,
    since 0.005 is not exactly representable in binary)."""
    rendered = forge_json_precision('{"amount": 0.005}', '{"amount": 2}', "amount", "t")
    assert rendered == '{"amount": 0.01}'


# ---------------------------------------------------------------------------
# render_json_precision_expr — the SQL-level authority calling the scalar
# ---------------------------------------------------------------------------


def _execute_json_precision(
    payload: str | None,
    leaves: dict[str, int],
    *,
    column_label: str = "amount",
    table_label: str = "t",
    from_table: str = "t",
) -> object:
    """Execute render_json_precision_expr's SQL fragment against a one-row
    VARCHAR table on a connection with the scalar registered."""
    conn = duckdb.connect(":memory:")
    register_render_functions(conn)
    conn.execute(f'CREATE TABLE "{from_table}" ("{column_label}" VARCHAR)')
    conn.execute(f'INSERT INTO "{from_table}" VALUES (?)', [payload])
    expr = render_json_precision_expr(
        f'"{from_table}"."{column_label}"', leaves, column_label, table_label
    )
    try:
        row = conn.execute(f'SELECT {expr} FROM "{from_table}"').fetchone()
    finally:
        conn.close()
    assert row is not None
    return row[0]


def test_render_json_precision_expr_round_trips_through_duckdb() -> None:
    """The compiled expression round-trips a declared leaf through DuckDB
    with the registered scalar."""
    rendered = _execute_json_precision('{"amount": 1.005}', {"amount": 2})
    assert rendered == '{"amount": 1.01}'


def test_render_json_precision_expr_quote_bearing_labels_splice_safely() -> None:
    """A table_label / column_label containing a single quote splices safely
    (no SQL breakout) and the expression still executes correctly."""
    rendered = _execute_json_precision(
        '{"amount": 1.005}',
        {"amount": 2},
        column_label="amount",
        table_label="vi'sits",
    )
    assert rendered == '{"amount": 1.01}'


def test_render_json_precision_expr_quote_bearing_label_survives_into_error() -> None:
    """A quote-bearing table_label survives unescaped into the raised
    error's message."""
    conn = duckdb.connect(":memory:")
    register_render_functions(conn)
    conn.execute('CREATE TABLE "t" ("amount" VARCHAR)')
    conn.execute('INSERT INTO "t" VALUES (?)', ['{"amount": "abc"}'])
    expr = render_json_precision_expr(
        '"t"."amount"', {"amount": 2}, "amount", "vi'sits"
    )
    try:
        with pytest.raises(duckdb.Error) as excinfo:
            conn.execute(f'SELECT {expr} FROM "t"').fetchall()
    finally:
        conn.close()
    assert "vi'sits" in str(excinfo.value)


# ---------------------------------------------------------------------------
# register_render_functions — connection-scoped scalar registration
# ---------------------------------------------------------------------------


def test_register_render_functions_makes_scalar_callable() -> None:
    """After registration, the connection can evaluate a
    `forge_json_precision` call directly."""
    conn = duckdb.connect(":memory:")
    register_render_functions(conn)
    try:
        row = conn.execute(
            "SELECT forge_json_precision('{\"amount\": 1.005}',"
            " '{\"amount\": 2}', 'amount', 't')"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == '{"amount": 1.01}'
