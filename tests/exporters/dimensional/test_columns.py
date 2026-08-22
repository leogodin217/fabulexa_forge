"""Tests for dimensional exporter column SQL builders.

Verifies each column mode produces correct SQL and that the value_map type
inference is correct. Also covers the `as`-elected `derived: timestamp`
render types, the ordinal amendment (raw-ns substitution for a monotone
sibling, election-aware exclusion of `time`), and `derived: date_parse`
end-to-end (temporal-elections sprint Phase 4).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import duckdb
import pytest

from fabulexa_forge._sql import register_render_functions, render_typed_literal
from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import (
    ColumnDecl,
    DateParseSpec,
    DecimalSpec,
    DerivedSpec,
    JsonPrecisionSpec,
    OrdinalSpec,
    ScdWindowSpec,
    SourceDecl,
    TableDecl,
    TimestampSpec,
    ValueMapSpec,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.columns import (
    _value_map_duckdb_type,
    build_column_expr,
    build_correlation_expr,
    build_date_parse_expr,
    build_decimal_expr,
    build_from_expr,
    build_json_precision_expr,
    build_null_expr,
    build_ordinal_expr,
    build_timestamp_expr,
    build_value_map_expr,
    resolve_source_column_type,
)


def _anchor() -> EffectiveAnchor:
    """Return a test EffectiveAnchor."""
    return EffectiveAnchor(
        start_instant=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        timezone=ZoneInfo("UTC"),
    )


def _table_with_columns(columns: list[ColumnDecl]) -> TableDecl:
    """Build a minimal 'visits' fact TableDecl carrying the given columns —
    the ordinal-amendment and date_parse tests' enclosing table_decl."""
    return TableDecl(
        name="visits",
        role="fact",
        source=SourceDecl(grain="records", kind="step"),
        key=["id"],
        columns=columns,
    )


def _describe_expr_type(expr: str, grain_alias: str = "_grain") -> str:
    """Execute a SELECT-list expression against a one-row BIGINT source
    table and return the resulting column's DuckDB type name."""
    conn = duckdb.connect(":memory:")
    conn.execute(f'CREATE TABLE "{grain_alias}" ("created_sim_time" BIGINT)')
    conn.execute(f'INSERT INTO "{grain_alias}" VALUES (0)')
    relation = conn.sql(f'SELECT {expr} FROM "{grain_alias}"')
    duck_type = str(relation.types[0])
    conn.close()
    return duck_type


# ---------------------------------------------------------------------------
# from:
# ---------------------------------------------------------------------------


def test_build_from_expr_projects_src_column() -> None:
    """from: produces \"<src>\" AS \"<name>\"."""
    col = ColumnDecl(name="patient_id", **{"from": "record_id"})
    expr = build_from_expr(col)
    assert expr == '"record_id" AS "patient_id"'


def test_build_from_expr_prop_column() -> None:
    """from: prop__ column is projected verbatim."""
    col = ColumnDecl(name="dept", **{"from": "prop__department"})
    expr = build_from_expr(col)
    assert expr == '"prop__department" AS "dept"'


# ---------------------------------------------------------------------------
# correlation:
# ---------------------------------------------------------------------------


def test_build_correlation_expr_renames_reference_id() -> None:
    """correlation: renames a reference-id column with no join."""
    col = ColumnDecl(name="spell_id", correlation="prop__journey_instance")
    expr = build_correlation_expr(col)
    assert expr == '"prop__journey_instance" AS "spell_id"'


# ---------------------------------------------------------------------------
# null:
# ---------------------------------------------------------------------------


def test_build_null_expr_typed_varchar() -> None:
    """null: true produces CAST(NULL AS VARCHAR)."""
    col = ColumnDecl(name="pad_col", null=True)
    expr = build_null_expr(col)
    assert expr == 'CAST(NULL AS VARCHAR) AS "pad_col"'


# ---------------------------------------------------------------------------
# derived: ordinal
# ---------------------------------------------------------------------------


def test_build_ordinal_expr_appends_record_id_tiebreak() -> None:
    """ordinal appends record_id as the final ORDER BY tie-break."""
    col = ColumnDecl(
        name="rank",
        derived=DerivedSpec(
            ordinal=OrdinalSpec(partition_by="patient_id", order_by="ts")
        ),
    )
    expr = build_ordinal_expr(col)
    assert 'PARTITION BY "patient_id"' in expr
    assert 'ORDER BY "ts", "_grain"."record_id"' in expr
    assert "ROW_NUMBER()" in expr
    assert 'AS "rank"' in expr


# ---------------------------------------------------------------------------
# derived: value_map
# ---------------------------------------------------------------------------


def test_value_map_type_bigint_for_int_values() -> None:
    """All-int map infers BIGINT."""
    assert _value_map_duckdb_type({"a": 1, "b": 2}) == "BIGINT"


def test_value_map_type_double_for_mixed_int_float() -> None:
    """Mixed int/float map infers DOUBLE."""
    assert _value_map_duckdb_type({"a": 1, "b": 2.5}) == "DOUBLE"


def test_value_map_type_varchar_for_string_values() -> None:
    """String values infer VARCHAR."""
    assert _value_map_duckdb_type({"a": "x", "b": "y"}) == "VARCHAR"


def test_value_map_type_varchar_for_bool_values() -> None:
    """Bool map values infer VARCHAR despite isinstance(True, int) being True."""
    assert _value_map_duckdb_type({"a": True, "b": False}) == "VARCHAR"


def test_value_map_type_varchar_for_int_then_bool_values() -> None:
    """A bool after ints forces VARCHAR — the bool guard beats BIGINT inference."""
    assert _value_map_duckdb_type({"a": 1, "b": True}) == "VARCHAR"


def test_value_map_type_varchar_for_float_then_bool_values() -> None:
    """A bool after a float forces VARCHAR, not DOUBLE."""
    assert _value_map_duckdb_type({"a": 2.5, "b": True}) == "VARCHAR"


def test_build_value_map_expr_maps_known_values() -> None:
    """value_map generates CASE with typed WHEN clauses and typed NULL else."""
    col = ColumnDecl(
        name="outcome_code",
        derived=DerivedSpec(
            value_map=ValueMapSpec(**{"from": "value", "map": {"a": 1, "b": 2}})
        ),
    )
    expr = build_value_map_expr(col, '"_grain"."value"', "VARCHAR")
    assert "CASE" in expr
    assert "WHEN" in expr
    assert "CAST(NULL AS BIGINT)" in expr
    assert 'AS "outcome_code"' in expr


def test_build_value_map_expr_unmapped_to_null() -> None:
    """Unmapped values yield CAST(NULL AS <type>) in the ELSE clause."""
    col = ColumnDecl(
        name="status_code",
        derived=DerivedSpec(
            value_map=ValueMapSpec(**{"from": "value", "map": {"active": 1}})
        ),
    )
    expr = build_value_map_expr(col, '"_grain"."value"', "VARCHAR")
    assert "ELSE CAST(NULL AS BIGINT)" in expr


def test_build_value_map_expr_varchar_map() -> None:
    """String map values produce VARCHAR typed CASE."""
    col = ColumnDecl(
        name="label",
        derived=DerivedSpec(
            value_map=ValueMapSpec(**{"from": "value", "map": {"x": "yes", "y": "no"}})
        ),
    )
    expr = build_value_map_expr(col, '"_grain"."value"', "VARCHAR")
    assert "CAST(NULL AS VARCHAR)" in expr


# ---------------------------------------------------------------------------
# derived: timestamp
# ---------------------------------------------------------------------------


def test_build_timestamp_expr_with_anchor() -> None:
    """With anchor, renders wallclock TIMESTAMP via timezone()/to_microseconds SQL."""
    col = ColumnDecl(
        name="admitted_at",
        derived=DerivedSpec(timestamp=TimestampSpec(source="last_mutation_sim_time")),
    )
    expr = build_timestamp_expr(col, _anchor(), '"_grain"."last_mutation_sim_time"')
    assert "timezone('UTC', TIMESTAMPTZ '2024-01-01T00:00:00+00:00'" in expr
    assert "to_microseconds" in expr
    assert "last_mutation_sim_time" in expr
    assert 'AS "admitted_at"' in expr


def test_build_timestamp_expr_without_runtime() -> None:
    """Without runtime, yields raw sim_time integer column (qualified with _grain)."""
    col = ColumnDecl(
        name="admitted_at",
        derived=DerivedSpec(timestamp=TimestampSpec(source="last_mutation_sim_time")),
    )
    expr = build_timestamp_expr(col, None, '"_grain"."last_mutation_sim_time"')
    assert expr == '"_grain"."last_mutation_sim_time" AS "admitted_at"'


def test_plain_from_always_raw_integer() -> None:
    """from: last_mutation_sim_time always yields raw integer, never anchored."""
    col = ColumnDecl(name="raw_ts", **{"from": "last_mutation_sim_time"})
    expr = build_from_expr(col)
    assert "TIMESTAMPTZ" not in expr
    assert expr == '"last_mutation_sim_time" AS "raw_ts"'


# ---------------------------------------------------------------------------
# derived: timestamp — `as` election renders the correct DuckDB output type
# ---------------------------------------------------------------------------


def _timestamp_col(as_value: str | None) -> ColumnDecl:
    """Build a derived: timestamp ColumnDecl, with or without an `as` election."""
    spec = (
        TimestampSpec(source="created_sim_time", **{"as": as_value})
        if as_value is not None
        else TimestampSpec(source="created_sim_time")
    )
    return ColumnDecl(name="admission", derived=DerivedSpec(timestamp=spec))


@pytest.mark.parametrize(
    "as_value,expected_duck_type",
    [
        ("date", "DATE"),
        ("time", "TIME"),
        ("timestamptz", "TIMESTAMP WITH TIME ZONE"),
        (None, "TIMESTAMP"),
    ],
)
def test_build_timestamp_expr_as_election_output_type(
    as_value: str | None, expected_duck_type: str
) -> None:
    """Each `as` election (and the absent-`as` default) renders the correct
    DuckDB output type when executed."""
    expr = build_timestamp_expr(
        _timestamp_col(as_value), _anchor(), '"_grain"."created_sim_time"'
    )
    assert _describe_expr_type(expr) == expected_duck_type


def test_build_timestamp_expr_no_as_byte_identical_to_default() -> None:
    """No `as` renders byte-identical SQL to the mode-definitional default
    `timestamp` rendering — absence detection, not an invented value."""
    col_absent = ColumnDecl(
        name="admission",
        derived=DerivedSpec(timestamp=TimestampSpec(source="created_sim_time")),
    )
    col_explicit = ColumnDecl(
        name="admission",
        derived=DerivedSpec(
            timestamp=TimestampSpec(source="created_sim_time", **{"as": "timestamp"})
        ),
    )
    source_expr = '"_grain"."created_sim_time"'
    assert build_timestamp_expr(
        col_absent, _anchor(), source_expr
    ) == build_timestamp_expr(col_explicit, _anchor(), source_expr)


# ---------------------------------------------------------------------------
# Ordinal amendment — raw-ns substitution for a monotone rendered-time sibling
# ---------------------------------------------------------------------------


def test_ordinal_amendment_date_elected_sibling_orders_by_raw_ns() -> None:
    """order_by naming a date-elected derived: timestamp sibling compiles to
    that column's raw-ns source, then record_id."""
    id_col = ColumnDecl(name="id", **{"from": "record_id"})
    ts_col = ColumnDecl(
        name="admission_date",
        derived=DerivedSpec(
            timestamp=TimestampSpec(source="created_sim_time", **{"as": "date"})
        ),
    )
    ordinal_col = ColumnDecl(
        name="rank",
        derived=DerivedSpec(
            ordinal=OrdinalSpec(partition_by="id", order_by="admission_date")
        ),
    )
    tbl = _table_with_columns([id_col, ts_col, ordinal_col])
    expr = build_ordinal_expr(ordinal_col, table_decl=tbl)
    assert 'ORDER BY "_grain"."created_sim_time", "_grain"."record_id"' in expr


def test_ordinal_amendment_timestamptz_elected_sibling_orders_by_raw_ns() -> None:
    """order_by naming a timestamptz-elected sibling also compiles to the
    raw-ns source — timestamptz is monotone in the instant too."""
    id_col = ColumnDecl(name="id", **{"from": "record_id"})
    ts_col = ColumnDecl(
        name="admitted_at",
        derived=DerivedSpec(
            timestamp=TimestampSpec(source="created_sim_time", **{"as": "timestamptz"})
        ),
    )
    ordinal_col = ColumnDecl(
        name="rank",
        derived=DerivedSpec(
            ordinal=OrdinalSpec(partition_by="id", order_by="admitted_at")
        ),
    )
    tbl = _table_with_columns([id_col, ts_col, ordinal_col])
    expr = build_ordinal_expr(ordinal_col, table_decl=tbl)
    assert 'ORDER BY "_grain"."created_sim_time", "_grain"."record_id"' in expr


def test_ordinal_amendment_time_elected_sibling_excluded() -> None:
    """order_by naming a time-elected sibling orders by the rendered TIME
    column itself, then record_id — time-of-day is not monotone in the
    instant, so raw-ns substitution is excluded."""
    id_col = ColumnDecl(name="id", **{"from": "record_id"})
    ts_col = ColumnDecl(
        name="admitted_time",
        derived=DerivedSpec(
            timestamp=TimestampSpec(source="created_sim_time", **{"as": "time"})
        ),
    )
    ordinal_col = ColumnDecl(
        name="rank",
        derived=DerivedSpec(
            ordinal=OrdinalSpec(partition_by="id", order_by="admitted_time")
        ),
    )
    tbl = _table_with_columns([id_col, ts_col, ordinal_col])
    expr = build_ordinal_expr(ordinal_col, table_decl=tbl)
    assert 'ORDER BY "admitted_time", "_grain"."record_id"' in expr


def test_ordinal_amendment_scd_window_valid_from_date_elected_orders_by_raw_ns() -> (
    None
):
    """order_by naming an scd_window: valid_from object-form sibling (date
    elected) joins the amendment population, ordering by 'version_start'."""
    id_col = ColumnDecl(name="id", **{"from": "record_id"})
    vf_col = ColumnDecl(
        name="valid_from",
        derived=DerivedSpec(
            scd_window=ScdWindowSpec(bound="valid_from", **{"as": "date"})
        ),
    )
    ordinal_col = ColumnDecl(
        name="rank",
        derived=DerivedSpec(
            ordinal=OrdinalSpec(partition_by="id", order_by="valid_from")
        ),
    )
    tbl = _table_with_columns([id_col, vf_col, ordinal_col])
    expr = build_ordinal_expr(ordinal_col, table_decl=tbl)
    assert 'ORDER BY "_grain"."version_start", "_grain"."record_id"' in expr


def test_ordinal_amendment_scd_window_valid_from_time_elected_excluded() -> None:
    """A time-elected scd_window: valid_from sibling is excluded from the
    amendment, exactly as a time-elected derived: timestamp sibling is."""
    id_col = ColumnDecl(name="id", **{"from": "record_id"})
    vf_col = ColumnDecl(
        name="valid_from",
        derived=DerivedSpec(
            scd_window=ScdWindowSpec(bound="valid_from", **{"as": "time"})
        ),
    )
    ordinal_col = ColumnDecl(
        name="rank",
        derived=DerivedSpec(
            ordinal=OrdinalSpec(partition_by="id", order_by="valid_from")
        ),
    )
    tbl = _table_with_columns([id_col, vf_col, ordinal_col])
    expr = build_ordinal_expr(ordinal_col, table_decl=tbl)
    assert 'ORDER BY "valid_from", "_grain"."record_id"' in expr


def test_ordinal_amendment_scd_window_valid_to_bound_stays_outside() -> None:
    """order_by naming an scd_window: valid_to sibling never joins the
    amendment population — the amendment applies to valid_from only."""
    id_col = ColumnDecl(name="id", **{"from": "record_id"})
    vt_col = ColumnDecl(
        name="valid_to",
        derived=DerivedSpec(
            scd_window=ScdWindowSpec(bound="valid_to", **{"as": "date"})
        ),
    )
    ordinal_col = ColumnDecl(
        name="rank",
        derived=DerivedSpec(
            ordinal=OrdinalSpec(partition_by="id", order_by="valid_to")
        ),
    )
    tbl = _table_with_columns([id_col, vt_col, ordinal_col])
    expr = build_ordinal_expr(ordinal_col, table_decl=tbl)
    assert 'ORDER BY "valid_to", "_grain"."record_id"' in expr


# ---------------------------------------------------------------------------
# derived: date_parse — end-to-end
# ---------------------------------------------------------------------------


def test_build_date_parse_expr_end_to_end_prop_varchar() -> None:
    """date_parse over a records grain's prop__ VARCHAR source parses to DATE."""
    col = ColumnDecl(
        name="birth_date",
        derived=DerivedSpec(
            date_parse=DateParseSpec(**{"from": "prop__dob", "format": "%Y-%m-%d"})
        ),
    )
    expr = build_date_parse_expr(col, '"_grain"."prop__dob"', "visits")

    conn = duckdb.connect(":memory:")
    conn.execute('CREATE TABLE "_grain" ("prop__dob" VARCHAR)')
    conn.execute('INSERT INTO "_grain" VALUES (?)', ["1990-05-14"])
    row = conn.execute(f'SELECT {expr} FROM "_grain"').fetchone()
    conn.close()

    assert row is not None
    assert row[0] == date(1990, 5, 14)


def test_build_date_parse_expr_end_to_end_datetime_format_denotes_timestamp() -> None:
    """date_parse with a datetime-directive format (the widened parse
    family) parses to naive TIMESTAMP through the spec-form builder."""
    col = ColumnDecl(
        name="registered_at",
        derived=DerivedSpec(
            date_parse=DateParseSpec(
                **{"from": "prop__registered_at", "format": "%Y-%m-%d %H:%M:%S"}
            )
        ),
    )
    expr = build_date_parse_expr(col, '"_grain"."prop__registered_at"', "visits")

    conn = duckdb.connect(":memory:")
    conn.execute('CREATE TABLE "_grain" ("prop__registered_at" VARCHAR)')
    conn.execute('INSERT INTO "_grain" VALUES (?)', ["2024-06-01 14:30:05"])
    row = conn.execute(f'SELECT {expr} FROM "_grain"').fetchone()
    duck_type = conn.sql(f'SELECT {expr} FROM "_grain"').types[0]
    conn.close()

    assert row is not None
    assert row[0] == datetime(2024, 6, 1, 14, 30, 5)
    assert str(duck_type) == "TIMESTAMP"


def test_build_date_parse_expr_end_to_end_membership_elem_field() -> None:
    """date_parse over a membership grain's elem__ VARCHAR source parses to
    DATE — the builder is agnostic to the grain's source-column prefix."""
    col = ColumnDecl(
        name="joined_date",
        derived=DerivedSpec(
            date_parse=DateParseSpec(
                **{"from": "elem__joined_date_str", "format": "%Y-%m-%d"}
            )
        ),
    )
    expr = build_date_parse_expr(col, '"_grain"."elem__joined_date_str"', "visits")

    conn = duckdb.connect(":memory:")
    conn.execute('CREATE TABLE "_grain" ("elem__joined_date_str" VARCHAR)')
    conn.execute('INSERT INTO "_grain" VALUES (?)', ["2023-11-02"])
    row = conn.execute(f'SELECT {expr} FROM "_grain"').fetchone()
    conn.close()

    assert row is not None
    assert row[0] == date(2023, 11, 2)


# ---------------------------------------------------------------------------
# derived: decimal — end-to-end (value-rendering-elections Phase 5)
# ---------------------------------------------------------------------------


def test_build_decimal_expr_end_to_end_rounds_to_declared_scale() -> None:
    """decimal over a DOUBLE grain-surface source rounds to the declared
    (precision, scale) — byte-identical to the shared decimal authority."""
    col = ColumnDecl(
        name="amount",
        derived=DerivedSpec(
            decimal=DecimalSpec(**{"from": "prop__amount", "as": [4, 3]})
        ),
    )
    expr = build_decimal_expr(col, '"_grain"."prop__amount"', "visits")

    conn = duckdb.connect(":memory:")
    conn.execute('CREATE TABLE "_grain" ("prop__amount" DOUBLE)')
    conn.execute('INSERT INTO "_grain" VALUES (?)', [1.2345])
    row = conn.execute(f'SELECT {expr} FROM "_grain"').fetchone()
    conn.close()

    assert row is not None
    assert str(row[0]) == "1.235"


def test_build_decimal_expr_end_to_end_null_source_is_null() -> None:
    """A NULL DOUBLE source renders NULL of the declared decimal type."""
    col = ColumnDecl(
        name="amount",
        derived=DerivedSpec(
            decimal=DecimalSpec(**{"from": "prop__amount", "as": [4, 3]})
        ),
    )
    expr = build_decimal_expr(col, '"_grain"."prop__amount"', "visits")

    conn = duckdb.connect(":memory:")
    conn.execute('CREATE TABLE "_grain" ("prop__amount" DOUBLE)')
    conn.execute('INSERT INTO "_grain" VALUES (NULL)')
    row = conn.execute(f'SELECT {expr} FROM "_grain"').fetchone()
    conn.close()

    assert row is not None
    assert row[0] is None


def test_build_decimal_expr_end_to_end_overflow_raises_naming_table_column() -> None:
    """A value overflowing the declared (precision, scale) raises, naming the
    output table and column."""
    col = ColumnDecl(
        name="amount",
        derived=DerivedSpec(
            decimal=DecimalSpec(**{"from": "prop__amount", "as": [4, 3]})
        ),
    )
    expr = build_decimal_expr(col, '"_grain"."prop__amount"', "visits")

    conn = duckdb.connect(":memory:")
    conn.execute('CREATE TABLE "_grain" ("prop__amount" DOUBLE)')
    conn.execute('INSERT INTO "_grain" VALUES (?)', [123.0])
    with pytest.raises(duckdb.Error, match="visits") as exc_info:
        conn.execute(f'SELECT {expr} FROM "_grain"').fetchone()
    conn.close()
    assert "amount" in str(exc_info.value)


# ---------------------------------------------------------------------------
# derived: json_precision — end-to-end (value-rendering-elections Phase 5)
# ---------------------------------------------------------------------------


def test_build_json_precision_expr_end_to_end_rounds_leaf_in_place() -> None:
    """json_precision rounds a declared top-level leaf in place, preserving
    every other byte of the payload."""
    col = ColumnDecl(
        name="payload",
        derived=DerivedSpec(
            json_precision=JsonPrecisionSpec(
                **{"from": "prop__payload", "leaves": {"discount": 2}}
            )
        ),
    )
    expr = build_json_precision_expr(col, '"_grain"."prop__payload"', "visits")

    conn = duckdb.connect(":memory:")
    register_render_functions(conn)
    conn.execute('CREATE TABLE "_grain" ("prop__payload" VARCHAR)')
    conn.execute(
        'INSERT INTO "_grain" VALUES (?)', ['{"discount": 1.2345, "sku": "A1"}']
    )
    row = conn.execute(f'SELECT {expr} FROM "_grain"').fetchone()
    conn.close()

    assert row is not None
    assert row[0] == '{"discount": 1.23, "sku": "A1"}'


def test_build_json_precision_expr_end_to_end_null_payload_is_null() -> None:
    """A NULL payload renders NULL."""
    col = ColumnDecl(
        name="payload",
        derived=DerivedSpec(
            json_precision=JsonPrecisionSpec(
                **{"from": "prop__payload", "leaves": {"discount": 2}}
            )
        ),
    )
    expr = build_json_precision_expr(col, '"_grain"."prop__payload"', "visits")

    conn = duckdb.connect(":memory:")
    register_render_functions(conn)
    conn.execute('CREATE TABLE "_grain" ("prop__payload" VARCHAR)')
    conn.execute('INSERT INTO "_grain" VALUES (NULL)')
    row = conn.execute(f'SELECT {expr} FROM "_grain"').fetchone()
    conn.close()

    assert row is not None
    assert row[0] is None


# ---------------------------------------------------------------------------
# build_column_expr dispatch
# ---------------------------------------------------------------------------


def test_dispatch_from() -> None:
    """build_column_expr dispatches from: mode; qualifies with grain_alias."""
    col = ColumnDecl(name="id", **{"from": "record_id"})
    expr, joins = build_column_expr(col, None)
    assert '"_grain"."record_id" AS "id"' == expr
    assert joins == []


def test_dispatch_derived_decimal() -> None:
    """build_column_expr dispatches derived: decimal through build_decimal_expr."""
    col = ColumnDecl(
        name="amount",
        derived=DerivedSpec(
            decimal=DecimalSpec(**{"from": "prop__amount", "as": [4, 3]})
        ),
    )
    tbl = _table_with_columns([ColumnDecl(name="id", **{"from": "record_id"}), col])
    expr, joins = build_column_expr(col, None, table_decl=tbl)
    assert 'AS "amount"' in expr
    assert '"_grain"."prop__amount"' in expr
    assert joins == []


def test_dispatch_derived_json_precision() -> None:
    """build_column_expr dispatches derived: json_precision through
    build_json_precision_expr."""
    col = ColumnDecl(
        name="payload",
        derived=DerivedSpec(
            json_precision=JsonPrecisionSpec(
                **{"from": "prop__payload", "leaves": {"discount": 2}}
            )
        ),
    )
    tbl = _table_with_columns([ColumnDecl(name="id", **{"from": "record_id"}), col])
    expr, joins = build_column_expr(col, None, table_decl=tbl)
    assert 'AS "payload"' in expr
    assert "forge_json_precision" in expr
    assert joins == []


def test_dispatch_null() -> None:
    """build_column_expr dispatches null: mode; returns (expr, [])."""
    col = ColumnDecl(name="pad", null=True)
    expr, joins = build_column_expr(col, None)
    assert "CAST(NULL AS VARCHAR)" in expr
    assert joins == []


def test_dispatch_correlation() -> None:
    """build_column_expr dispatches correlation: mode; qualifies with grain_alias."""
    col = ColumnDecl(name="spell", correlation="prop__journey")
    expr, joins = build_column_expr(col, None)
    assert '"_grain"."prop__journey" AS "spell"' == expr
    assert joins == []


# ---------------------------------------------------------------------------
# render_typed_literal
# ---------------------------------------------------------------------------


def test_render_typed_literal_varchar_single_quoted() -> None:
    """VARCHAR value is single-quoted."""
    assert render_typed_literal("hello", "VARCHAR") == "'hello'"


def test_render_typed_literal_varchar_prefix_single_quoted() -> None:
    """VARCHAR(255) value is single-quoted (prefix match)."""
    assert render_typed_literal("hello", "VARCHAR(255)") == "'hello'"


def test_render_typed_literal_varchar_escapes_embedded_quote() -> None:
    """Embedded single-quotes are doubled in VARCHAR literals."""
    assert render_typed_literal("it's", "VARCHAR") == "'it''s'"


def test_render_typed_literal_bigint_cast_form() -> None:
    """BIGINT value uses CAST form."""
    assert render_typed_literal("42", "BIGINT") == "CAST('42' AS BIGINT)"


def test_render_typed_literal_integer_cast_form() -> None:
    """INTEGER value uses CAST form."""
    assert render_typed_literal("7", "INTEGER") == "CAST('7' AS INTEGER)"


def test_render_typed_literal_smallint_cast_form() -> None:
    """SMALLINT value uses CAST form."""
    assert render_typed_literal("5", "SMALLINT") == "CAST('5' AS SMALLINT)"


def test_render_typed_literal_double_cast_form() -> None:
    """DOUBLE value uses CAST form."""
    assert render_typed_literal("3.14", "DOUBLE") == "CAST('3.14' AS DOUBLE)"


def test_render_typed_literal_float_cast_form() -> None:
    """FLOAT value uses CAST form."""
    assert render_typed_literal("1.5", "FLOAT") == "CAST('1.5' AS FLOAT)"


def test_render_typed_literal_boolean_cast_form() -> None:
    """BOOLEAN value uses CAST form."""
    assert render_typed_literal("true", "BOOLEAN") == "CAST('true' AS BOOLEAN)"


def test_render_typed_literal_decimal_cast_form() -> None:
    """DECIMAL(10,2) value uses CAST form."""
    assert (
        render_typed_literal("1.23", "DECIMAL(10,2)") == "CAST('1.23' AS DECIMAL(10,2))"
    )


def test_render_typed_literal_unknown_type_raises_export_error() -> None:
    """Unknown SQL type raises ExportError (no silent VARCHAR fallback)."""
    with pytest.raises(ExportError, match="unrecognized SQL type"):
        render_typed_literal("val", "TIMESTAMP")


def test_render_typed_literal_unknown_type_json_raises() -> None:
    """JSON type raises ExportError."""
    with pytest.raises(ExportError, match="unrecognized SQL type"):
        render_typed_literal("val", "JSON")


def test_render_typed_literal_empty_string_varchar() -> None:
    """Empty string with VARCHAR produces a single-quoted empty literal ''."""
    result = render_typed_literal("", "VARCHAR")
    assert result == "''"


# ---------------------------------------------------------------------------
# build_value_map_expr with source_col_type
# ---------------------------------------------------------------------------


def test_build_value_map_expr_varchar_source_predicate_quoted() -> None:
    """value_map with VARCHAR source uses single-quoted WHEN predicate (byte-stable)."""
    col = ColumnDecl(
        name="code",
        derived=DerivedSpec(
            value_map=ValueMapSpec(**{"from": "value", "map": {"admitted": 1}})
        ),
    )
    expr = build_value_map_expr(col, '"_grain"."value"', source_col_type="VARCHAR")
    # WHEN side must be single-quoted for VARCHAR
    assert "= 'admitted'" in expr


def test_build_value_map_expr_bigint_source_predicate_cast() -> None:
    """value_map with BIGINT source uses CAST form for WHEN predicate."""
    col = ColumnDecl(
        name="code",
        derived=DerivedSpec(
            value_map=ValueMapSpec(**{"from": "prop__count", "map": {"5": 1}})
        ),
    )
    expr = build_value_map_expr(col, '"_grain"."prop__count"', source_col_type="BIGINT")
    # WHEN side must use CAST for BIGINT
    assert "CAST('5' AS BIGINT)" in expr


def test_resolve_source_column_type_column_absent_returns_varchar() -> None:
    """Table found but column absent returns VARCHAR (no-op cast fallback).

    Genuinely reachable in production: a history_interval grain's projectable
    surface includes the virtual `lead_sim_time` column (added by
    ProjectionColumnExists's surface, not by the sidecar), so a value_map
    naming it as its source resolves a table the sidecar does have, with a
    column the sidecar does not declare.
    """
    sidecar = MagicMock()
    sidecar.columns.return_value = [
        SimpleNamespace(name="sim_time", type="BIGINT"),
        SimpleNamespace(name="value", type="VARCHAR"),
    ]
    result = resolve_source_column_type(
        sidecar, "history", "lead_sim_time", "value_map column 'x'"
    )
    assert result == "VARCHAR"
