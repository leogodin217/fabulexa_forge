"""Tests for reader/relations.py: faithful-read SQL builders + distinct_prop_values.

Verifies that each builder produces correct SQL — scalar and list-valued
predicates alike, the latter rendering `IN` via the shared
`render_predicate_condition` authority — and that distinct_prop_values
returns values in native-type ORDER BY 1 order.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from _support.sidecar_builder import identity_column
from _support.sidecar_builder import write_emit as _write_sidecar

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.errors import ExportError
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.errors import TableNotFoundError
from fabulexa_forge.reader.relations import (
    build_history_relation_sql,
    build_membership_relation_sql,
    build_records_relation_sql,
    distinct_prop_values,
)
from fabulexa_forge.reader.sidecar import Sidecar

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sidecar_raw(
    extra_tables: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build a minimal sidecar dict for testing."""
    tables: list[dict[str, object]] = [
        {
            "name": "firings",
            "category": "fixed",
            "columns": [
                {"name": "fork_path", "type": "VARCHAR"},
                {"name": "sim_time", "type": "BIGINT"},
            ],
            "rows": 0,
        },
        {
            "name": "history",
            "category": "fixed",
            "columns": [
                {"name": "fork_path", "type": "VARCHAR"},
                {"name": "kind", "type": "VARCHAR"},
                {"name": "record_id", "type": "VARCHAR"},
                {"name": "property", "type": "VARCHAR"},
                {"name": "sim_time", "type": "BIGINT"},
                {"name": "value", "type": "VARCHAR"},
            ],
            "rows": 0,
        },
    ]
    if extra_tables:
        tables.extend(extra_tables)
    return {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": tables,
    }


def _make_sidecar(extra_tables: list[dict[str, object]] | None = None) -> Sidecar:
    """Build a minimal Sidecar for unit tests."""
    return Sidecar.from_raw(_make_sidecar_raw(extra_tables))


def _write_emit(
    tmp_path: Path,
    sidecar_raw: dict[str, object],
    db_tables: dict[str, str] | None = None,
) -> Path:
    """Write a base.json + run.duckdb pair into tmp_path."""
    _write_sidecar(
        tmp_path,
        tables=sidecar_raw["tables"],  # type: ignore[arg-type]
        branches=sidecar_raw["branches"],  # type: ignore[arg-type]
        base_format_version=sidecar_raw["base_format_version"],  # type: ignore[arg-type]
    )
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    if db_tables:
        for ddl in db_tables.values():
            conn.execute(ddl)
    conn.close()
    return tmp_path


# ---------------------------------------------------------------------------
# build_records_relation_sql
# ---------------------------------------------------------------------------


def test_records_relation_full_column_list() -> None:
    """build_records_relation_sql includes all sidecar columns for the kind."""
    entity_cols: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "active", "type": "BOOLEAN"},
        identity_column("record_index", "BIGINT"),
        {"name": "prop__name", "type": "VARCHAR"},
    ]
    sidecar = _make_sidecar(
        [
            {
                "name": "records__entity",
                "category": "records",
                "record_kind": "entity",
                "columns": entity_cols,
                "rows": 0,
            }
        ]
    )
    sql = build_records_relation_sql(sidecar, "trunk", "entity", {})
    assert '"fork_path"' in sql
    assert '"record_id"' in sql
    assert '"active"' in sql
    assert '"prop__name"' in sql
    assert 'FROM "records__entity"' in sql


def test_records_relation_filtered_to_fork_path() -> None:
    """build_records_relation_sql includes fork_path predicate."""
    sidecar = _make_sidecar(
        [
            {
                "name": "records__entity",
                "category": "records",
                "record_kind": "entity",
                "columns": [
                    identity_column("fork_path", "VARCHAR"),
                    identity_column("record_id", "VARCHAR"),
                    identity_column("record_index", "BIGINT"),
                ],
                "rows": 0,
            }
        ]
    )
    sql = build_records_relation_sql(sidecar, "trunk", "entity", {})
    assert "\"fork_path\" = 'trunk'" in sql


def test_records_relation_discriminator_filter_varchar() -> None:
    """Discriminator filter for VARCHAR column renders as a quoted literal."""
    sidecar = _make_sidecar(
        [
            {
                "name": "records__entity",
                "category": "records",
                "record_kind": "entity",
                "columns": [
                    identity_column("fork_path", "VARCHAR"),
                    identity_column("record_id", "VARCHAR"),
                    identity_column("record_index", "BIGINT"),
                    {"name": "prop__entity_type", "type": "VARCHAR"},
                ],
                "rows": 0,
            }
        ]
    )
    sql = build_records_relation_sql(
        sidecar, "trunk", "entity", {"prop__entity_type": "consultant"}
    )
    assert "\"prop__entity_type\" = 'consultant'" in sql


def test_records_relation_discriminator_filter_integer() -> None:
    """Discriminator filter for BIGINT column renders as a CAST literal."""
    sidecar = _make_sidecar(
        [
            {
                "name": "records__entity",
                "category": "records",
                "record_kind": "entity",
                "columns": [
                    identity_column("fork_path", "VARCHAR"),
                    identity_column("record_id", "VARCHAR"),
                    identity_column("record_index", "BIGINT"),
                    {"name": "prop__priority", "type": "BIGINT"},
                ],
                "rows": 0,
            }
        ]
    )
    sql = build_records_relation_sql(
        sidecar, "trunk", "entity", {"prop__priority": "42"}
    )
    assert "\"prop__priority\" = CAST('42' AS BIGINT)" in sql


def test_records_relation_discriminator_filter_list_renders_in() -> None:
    """A list-valued discriminator filter renders `IN`, typed per sidecar column."""
    sidecar = _make_sidecar(
        [
            {
                "name": "records__entity",
                "category": "records",
                "record_kind": "entity",
                "columns": [
                    identity_column("fork_path", "VARCHAR"),
                    identity_column("record_id", "VARCHAR"),
                    identity_column("record_index", "BIGINT"),
                    {"name": "prop__entity_type", "type": "VARCHAR"},
                ],
                "rows": 0,
            }
        ]
    )
    sql = build_records_relation_sql(
        sidecar,
        "trunk",
        "entity",
        {"prop__entity_type": ["consultant", "nurse"]},
    )
    assert "\"prop__entity_type\" IN ('consultant', 'nurse')" in sql


def test_records_relation_unrecognized_column_type_raises() -> None:
    """A predicate column whose sidecar type is unrecognized raises ExportError
    — no silent VARCHAR fallback."""
    sidecar = _make_sidecar(
        [
            {
                "name": "records__entity",
                "category": "records",
                "record_kind": "entity",
                "columns": [
                    identity_column("fork_path", "VARCHAR"),
                    identity_column("record_id", "VARCHAR"),
                    identity_column("record_index", "BIGINT"),
                    {"name": "prop__blob", "type": "BLOB"},
                ],
                "rows": 0,
            }
        ]
    )
    with pytest.raises(ExportError, match="unrecognized SQL type"):
        build_records_relation_sql(sidecar, "trunk", "entity", {"prop__blob": "x"})


def test_records_relation_empty_filter_selects_all() -> None:
    """Empty discriminator_filter produces no extra predicates beyond fork_path."""
    sidecar = _make_sidecar(
        [
            {
                "name": "records__entity",
                "category": "records",
                "record_kind": "entity",
                "columns": [
                    identity_column("fork_path", "VARCHAR"),
                    identity_column("record_id", "VARCHAR"),
                    identity_column("record_index", "BIGINT"),
                ],
                "rows": 0,
            }
        ]
    )
    sql = build_records_relation_sql(sidecar, "trunk", "entity", {})
    # Only the fork_path predicate; no extra AND
    assert sql.count("AND") == 0


def test_records_relation_no_order_by() -> None:
    """build_records_relation_sql carries no ORDER BY."""
    sidecar = _make_sidecar(
        [
            {
                "name": "records__entity",
                "category": "records",
                "record_kind": "entity",
                "columns": [
                    identity_column("fork_path", "VARCHAR"),
                    identity_column("record_id", "VARCHAR"),
                    identity_column("record_index", "BIGINT"),
                ],
                "rows": 0,
            }
        ]
    )
    sql = build_records_relation_sql(sidecar, "trunk", "entity", {})
    assert "ORDER BY" not in sql


def test_records_relation_missing_table_raises() -> None:
    """build_records_relation_sql raises TableNotFoundError for unknown kind."""
    sidecar = _make_sidecar()  # no records tables
    with pytest.raises(TableNotFoundError, match="records__missing"):
        build_records_relation_sql(sidecar, "trunk", "missing", {})


# ---------------------------------------------------------------------------
# build_history_relation_sql
# ---------------------------------------------------------------------------


def test_history_relation_fixed_six_columns() -> None:
    """build_history_relation_sql includes the six fixed history columns."""
    sidecar = _make_sidecar()
    sql = build_history_relation_sql(sidecar, "trunk", "entity", "state", None)
    for col in ("fork_path", "kind", "record_id", "property", "sim_time", "value"):
        assert f'"{col}"' in sql


def test_history_relation_written_by_columns_included() -> None:
    """written_by_* columns are included when the sidecar lists them."""
    # Build a sidecar with a history table that includes a written_by_agent column.
    # We include firings but replace the default history table (no extra_tables that
    # duplicate it) by building the sidecar dict directly.
    sidecar_raw: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": [
            {
                "name": "firings",
                "category": "fixed",
                "columns": [
                    {"name": "fork_path", "type": "VARCHAR"},
                    {"name": "sim_time", "type": "BIGINT"},
                ],
                "rows": 0,
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": [
                    {"name": "fork_path", "type": "VARCHAR"},
                    {"name": "kind", "type": "VARCHAR"},
                    {"name": "record_id", "type": "VARCHAR"},
                    {"name": "property", "type": "VARCHAR"},
                    {"name": "sim_time", "type": "BIGINT"},
                    {"name": "value", "type": "VARCHAR"},
                    {"name": "written_by_agent", "type": "VARCHAR"},
                ],
                "rows": 0,
            },
        ],
    }
    sidecar = Sidecar.from_raw(sidecar_raw)
    sql = build_history_relation_sql(sidecar, "trunk", "entity", "state", None)
    assert '"written_by_agent"' in sql


def test_history_relation_absent_history_table_falls_back_to_fixed_six() -> None:
    """When the sidecar has no history table, only the six fixed columns are emitted.

    history is a contract-guaranteed fixed-category table; its absence is a
    conformance failure, but the builder still emits a well-formed SELECT over
    exactly the six fixed columns (TableNotFoundError fallback branch).
    """
    sidecar_raw: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": [
            {
                "name": "firings",
                "category": "fixed",
                "columns": [
                    {"name": "fork_path", "type": "VARCHAR"},
                    {"name": "sim_time", "type": "BIGINT"},
                ],
                "rows": 0,
            },
        ],
    }
    sidecar = Sidecar.from_raw(sidecar_raw)
    sql = build_history_relation_sql(sidecar, "trunk", "entity", "state", None)
    expected_cols = '"fork_path", "kind", "record_id", "property", "sim_time", "value"'
    assert sql.startswith(f"SELECT {expected_cols} ")
    assert 'FROM "history"' in sql
    assert "\"fork_path\" = 'trunk'" in sql


def test_history_relation_filtered_to_kind_and_property() -> None:
    """build_history_relation_sql filters by kind and property."""
    sidecar = _make_sidecar()
    sql = build_history_relation_sql(sidecar, "trunk", "journey", "state", None)
    assert "\"kind\" = 'journey'" in sql
    assert "\"property\" = 'state'" in sql


def test_history_relation_value_filter_varchar_literal() -> None:
    """value_filter is rendered as a raw VARCHAR literal, never type-coerced."""
    sidecar = _make_sidecar()
    sql = build_history_relation_sql(sidecar, "trunk", "entity", "state", "completed")
    assert "\"value\" = 'completed'" in sql


def test_history_relation_list_value_filter_renders_in_over_raw_literals() -> None:
    """A list-valued value_filter renders `IN` over raw (untyped) literals."""
    sidecar = _make_sidecar()
    sql = build_history_relation_sql(
        sidecar, "trunk", "entity", "state", ["completed", "cancelled"]
    )
    assert "\"value\" IN ('completed', 'cancelled')" in sql


def test_history_relation_no_value_filter_omits_predicate() -> None:
    """When value_filter is None, no value predicate is emitted."""
    sidecar = _make_sidecar()
    sql = build_history_relation_sql(sidecar, "trunk", "entity", "state", None)
    assert '"value" =' not in sql


def test_history_relation_no_order_by() -> None:
    """build_history_relation_sql carries no ORDER BY."""
    sidecar = _make_sidecar()
    sql = build_history_relation_sql(sidecar, "trunk", "entity", "state", None)
    assert "ORDER BY" not in sql


# ---------------------------------------------------------------------------
# build_membership_relation_sql
# ---------------------------------------------------------------------------


def _sidecar_with_membership() -> Sidecar:
    """Return a sidecar with a membership table."""
    return _make_sidecar(
        [
            {
                "name": "membership__journey__team_members",
                "category": "membership",
                "record_kind": "journey",
                "property": "team_members",
                "columns": [
                    identity_column("fork_path", "VARCHAR"),
                    identity_column("record_id", "VARCHAR"),
                    {"name": "joined_sim_time", "type": "BIGINT"},
                    {"name": "left_sim_time", "type": "BIGINT"},
                    {"name": "elem__role_name", "type": "VARCHAR"},
                    {"name": "member__entity__kind", "type": "VARCHAR"},
                    {"name": "member__entity__id", "type": "VARCHAR"},
                ],
                "rows": 0,
            }
        ]
    )


def test_membership_relation_full_column_list() -> None:
    """build_membership_relation_sql includes all membership table columns."""
    sidecar = _sidecar_with_membership()
    sql = build_membership_relation_sql(sidecar, "trunk", "journey", "team_members", {})
    for col in (
        "fork_path",
        "record_id",
        "joined_sim_time",
        "elem__role_name",
        "member__entity__id",
    ):
        assert f'"{col}"' in sql


def test_membership_relation_filtered_to_fork_path() -> None:
    """build_membership_relation_sql includes fork_path predicate."""
    sidecar = _sidecar_with_membership()
    sql = build_membership_relation_sql(sidecar, "trunk", "journey", "team_members", {})
    assert "\"fork_path\" = 'trunk'" in sql


def test_membership_relation_where_predicate() -> None:
    """where_predicate adds elem__ column predicates typed by sidecar type."""
    sidecar = _sidecar_with_membership()
    sql = build_membership_relation_sql(
        sidecar, "trunk", "journey", "team_members", {"elem__role_name": "surgeon"}
    )
    assert "\"elem__role_name\" = 'surgeon'" in sql


def test_membership_relation_list_where_predicate_renders_in() -> None:
    """A list-valued where_predicate entry renders `IN`, typed per sidecar type."""
    sidecar = _sidecar_with_membership()
    sql = build_membership_relation_sql(
        sidecar,
        "trunk",
        "journey",
        "team_members",
        {"elem__role_name": ["surgeon", "anesthetist"]},
    )
    assert "\"elem__role_name\" IN ('surgeon', 'anesthetist')" in sql


def test_membership_relation_unrecognized_column_type_raises() -> None:
    """A where_predicate column whose sidecar type is unrecognized raises
    ExportError — no silent VARCHAR fallback."""
    sidecar = _make_sidecar(
        [
            {
                "name": "membership__journey__team_members",
                "category": "membership",
                "record_kind": "journey",
                "property": "team_members",
                "columns": [
                    identity_column("fork_path", "VARCHAR"),
                    identity_column("record_id", "VARCHAR"),
                    {"name": "elem__weight", "type": "BLOB"},
                ],
                "rows": 0,
            }
        ]
    )
    with pytest.raises(ExportError, match="unrecognized SQL type"):
        build_membership_relation_sql(
            sidecar, "trunk", "journey", "team_members", {"elem__weight": "x"}
        )


def test_membership_relation_no_order_by() -> None:
    """build_membership_relation_sql carries no ORDER BY."""
    sidecar = _sidecar_with_membership()
    sql = build_membership_relation_sql(sidecar, "trunk", "journey", "team_members", {})
    assert "ORDER BY" not in sql


def test_membership_relation_missing_table_raises() -> None:
    """build_membership_relation_sql raises TableNotFoundError for unknown table."""
    sidecar = _make_sidecar()
    with pytest.raises(TableNotFoundError, match="membership__missing__prop"):
        build_membership_relation_sql(sidecar, "trunk", "missing", "prop", {})


# ---------------------------------------------------------------------------
# distinct_prop_values
# ---------------------------------------------------------------------------


def _build_records_emit(
    tmp_path: Path,
    rows: list[tuple[str, str, str]],
    col_type: str = "VARCHAR",
) -> Path:
    """Write an emit with a records__entity table having a prop__code column.

    Args:
        tmp_path: Target directory.
        rows: List of (fork_path, record_id, prop_value) tuples.
        col_type: DuckDB type for the prop__code column.

    Returns:
        tmp_path.
    """
    entity_cols: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        {"name": "prop__code", "type": col_type},
    ]
    sidecar_raw = _make_sidecar_raw(
        [
            {
                "name": "records__entity",
                "category": "records",
                "record_kind": "entity",
                "columns": entity_cols,
                "rows": len(rows),
            }
        ]
    )
    db_ddl = (
        f'CREATE TABLE "records__entity" '
        f'("fork_path" VARCHAR, "record_id" VARCHAR, "created_sim_time" BIGINT, '
        f'"active" BOOLEAN, "deactivated_at" BIGINT, '
        f'"last_mutation_sim_time" BIGINT, "record_index" BIGINT, '
        f'"prop__code" {col_type})'
    )
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(db_ddl)
    for idx, (fork_p, rec_id, val) in enumerate(rows):
        conn.execute(
            'INSERT INTO "records__entity" VALUES (?, ?, 0, true, NULL, 0, ?, ?)',
            [fork_p, rec_id, idx, val],
        )
    conn.close()
    _write_sidecar(
        tmp_path,
        tables=sidecar_raw["tables"],  # type: ignore[arg-type]
        branches=sidecar_raw["branches"],  # type: ignore[arg-type]
        base_format_version=sidecar_raw["base_format_version"],  # type: ignore[arg-type]
    )
    return tmp_path


def test_distinct_prop_values_returns_string_sorted_varchar(tmp_path: Path) -> None:
    """distinct_prop_values returns VARCHAR values in ORDER BY 1 order."""
    emit_dir = _build_records_emit(
        tmp_path,
        [
            ("trunk", "e001", "consultant"),
            ("trunk", "e002", "nurse"),
            ("trunk", "e003", "admin"),
        ],
    )
    with open_emit(emit_dir) as emit:
        values = distinct_prop_values(emit, "entity", "code")
    assert values == ["admin", "consultant", "nurse"]


def test_distinct_prop_values_excludes_null(tmp_path: Path) -> None:
    """distinct_prop_values excludes NULL values."""
    emit_dir = _build_records_emit(
        tmp_path,
        [
            ("trunk", "e001", "consultant"),
            ("trunk", "e002", "nurse"),
        ],
    )
    # Insert a row with NULL
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, 0, true, NULL, 0, ?, NULL)',
        ["trunk", "e003", 2],
    )
    conn.close()
    with open_emit(emit_dir) as emit:
        values = distinct_prop_values(emit, "entity", "code")
    assert None not in values
    assert len(values) == 2


def test_distinct_prop_values_deduplicates(tmp_path: Path) -> None:
    """distinct_prop_values returns DISTINCT values."""
    emit_dir = _build_records_emit(
        tmp_path,
        [
            ("trunk", "e001", "consultant"),
            ("trunk", "e002", "consultant"),
            ("trunk", "e003", "nurse"),
        ],
    )
    with open_emit(emit_dir) as emit:
        values = distinct_prop_values(emit, "entity", "code")
    assert values == ["consultant", "nurse"]


def test_distinct_prop_values_missing_table_raises(tmp_path: Path) -> None:
    """distinct_prop_values raises TableNotFoundError for unknown kind."""
    # Write a minimal emit with no records tables
    sidecar_raw = _make_sidecar_raw()
    db_path = tmp_path / "run.duckdb"
    duckdb.connect(str(db_path)).close()
    _write_sidecar(
        tmp_path,
        tables=sidecar_raw["tables"],  # type: ignore[arg-type]
        branches=sidecar_raw["branches"],  # type: ignore[arg-type]
        base_format_version=sidecar_raw["base_format_version"],  # type: ignore[arg-type]
    )
    with open_emit(tmp_path) as emit:
        with pytest.raises(TableNotFoundError, match="records__missing"):
            distinct_prop_values(emit, "missing", "code")


def test_distinct_prop_values_integer_order(tmp_path: Path) -> None:
    """distinct_prop_values returns BIGINT values in numeric ORDER BY 1 order.

    Native DuckDB ORDER BY 1 on BIGINT is numeric (1, 2, 10), not string ('1',
    '10', '2'). The caller receives the values as strings; callers must not
    re-sort.
    """
    emit_dir = _build_records_emit(
        tmp_path,
        [
            ("trunk", "e001", "2"),
            ("trunk", "e002", "10"),
            ("trunk", "e003", "1"),
        ],
        col_type="BIGINT",
    )
    with open_emit(emit_dir) as emit:
        values = distinct_prop_values(emit, "entity", "code")
    # Numeric order: 1, 2, 10 (not string order: 1, 10, 2)
    assert values == ["1", "2", "10"]
