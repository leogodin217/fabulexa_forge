"""Tests for dimensional exporter column SQL builders.

Verifies each column mode produces correct SQL and that the value_map type
inference is correct.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from fabulexa_export._sql import render_typed_literal
from fabulexa_export.anchor import EffectiveAnchor
from fabulexa_export.config.models import (
    ColumnDecl,
    DerivedSpec,
    OrdinalSpec,
    TimestampSpec,
    ValueMapSpec,
)
from fabulexa_export.errors import ExportError
from fabulexa_export.exporters.dimensional.columns import (
    _value_map_duckdb_type,
    build_column_expr,
    build_correlation_expr,
    build_from_expr,
    build_null_expr,
    build_ordinal_expr,
    build_timestamp_expr,
    build_value_map_expr,
)


def _anchor() -> EffectiveAnchor:
    """Return a test EffectiveAnchor."""
    return EffectiveAnchor(
        start_instant=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        timezone=ZoneInfo("UTC"),
    )


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
    expr = build_value_map_expr(col)
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
    expr = build_value_map_expr(col)
    assert "ELSE CAST(NULL AS BIGINT)" in expr


def test_build_value_map_expr_varchar_map() -> None:
    """String map values produce VARCHAR typed CASE."""
    col = ColumnDecl(
        name="label",
        derived=DerivedSpec(
            value_map=ValueMapSpec(**{"from": "value", "map": {"x": "yes", "y": "no"}})
        ),
    )
    expr = build_value_map_expr(col)
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
    expr = build_timestamp_expr(col, _anchor())
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
    expr = build_timestamp_expr(col, None)
    assert expr == '"_grain"."last_mutation_sim_time" AS "admitted_at"'


def test_plain_from_always_raw_integer() -> None:
    """from: last_mutation_sim_time always yields raw integer, never anchored."""
    col = ColumnDecl(name="raw_ts", **{"from": "last_mutation_sim_time"})
    expr = build_from_expr(col)
    assert "TIMESTAMPTZ" not in expr
    assert expr == '"last_mutation_sim_time" AS "raw_ts"'


# ---------------------------------------------------------------------------
# build_column_expr dispatch
# ---------------------------------------------------------------------------


def test_dispatch_from() -> None:
    """build_column_expr dispatches from: mode; qualifies with grain_alias."""
    col = ColumnDecl(name="id", **{"from": "record_id"})
    expr, joins = build_column_expr(col, None)
    assert '"_grain"."record_id" AS "id"' == expr
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
    expr = build_value_map_expr(col, source_col_type="VARCHAR")
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
    expr = build_value_map_expr(col, source_col_type="BIGINT")
    # WHEN side must use CAST for BIGINT
    assert "CAST('5' AS BIGINT)" in expr
