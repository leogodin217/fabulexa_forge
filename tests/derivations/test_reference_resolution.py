"""Tests for derivations.reference_resolution.

Covers:
  - build_reference_path_sql: zero-hop, single-hop, multi-hop; terminal_projection
    'record_id' and 'prop__<p>'; fan-out-free; unresolvable → NULL; invalid terminal.
  - build_membership_edge_sql: member_kind narrowing; where predicate; not fan-out-free;
    missing table → ExportError; bad member_field → ExportError.
  - Shared helpers (_collect_reference_columns, _find_all_reference_paths,
    _path_hint_to_cols): BFS, zero-hop, ambiguous path, path-hint validation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
from _support.sidecar_builder import write_emit as _write_sidecar

from fabulexa_forge.derivations.reference_resolution import (
    REFERENCE_RESOLUTION_COLUMNS,
    _collect_reference_columns,
    _find_all_reference_paths,
    _path_hint_to_cols,
    _render_typed_literal,
    build_membership_edge_sql,
    build_reference_path_sql,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.reader.emit import open_emit

# ---------------------------------------------------------------------------
# Emit / sidecar builders
# ---------------------------------------------------------------------------


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _table_spec(
    name: str,
    category: str,
    cols: list[dict[str, object]],
    rows: int,
    record_kind: str | None = None,
    property_name: str | None = None,
) -> dict[str, object]:
    spec: dict[str, object] = {
        "name": name,
        "category": category,
        "columns": cols,
        "rows": rows,
    }
    if record_kind is not None:
        spec["record_kind"] = record_kind
    if property_name is not None:
        spec["property"] = property_name
    return spec


def _build_emit(
    tmp_path: Path,
    tables: list[dict[str, object]],
    table_rows: dict[str, list[tuple[Any, ...]]],
    col_specs: dict[str, list[dict[str, object]]],
    schema_valid: bool = True,
) -> Path:
    """Build a minimal emit with the given tables, rows, and sidecar."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    for tspec in tables:
        tname = str(tspec["name"])
        cols = col_specs[tname]
        conn.execute(_ddl(tname, cols))
        for row in table_rows.get(tname, []):
            placeholders = ", ".join("?" for _ in row)
            conn.execute(f'INSERT INTO "{tname}" VALUES ({placeholders})', list(row))
    conn.close()

    _write_sidecar(
        tmp_path,
        tables=tables,
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        schema_valid=schema_valid,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# _render_typed_literal — the module-local mirror of
# exporters.dimensional.columns.render_typed_literal
# ---------------------------------------------------------------------------


def test_render_typed_literal_varchar_single_quoted() -> None:
    """VARCHAR (and VARCHAR(n)) values render single-quoted, no CAST."""
    assert _render_typed_literal("lead", "VARCHAR") == "'lead'"
    assert _render_typed_literal("lead", "VARCHAR(32)") == "'lead'"


@pytest.mark.parametrize(
    "sql_type",
    [
        "TINYINT",
        "SMALLINT",
        "INTEGER",
        "BIGINT",
        "HUGEINT",
        "UTINYINT",
        "USMALLINT",
        "UINTEGER",
        "UBIGINT",
        "UHUGEINT",
    ],
)
def test_render_typed_literal_integer_family_cast(sql_type: str) -> None:
    """Every integer-family type renders as CAST('<value>' AS <type>)."""
    assert _render_typed_literal("7", sql_type) == f"CAST('7' AS {sql_type})"


@pytest.mark.parametrize("sql_type", ["DOUBLE", "FLOAT", "REAL"])
def test_render_typed_literal_float_family_cast(sql_type: str) -> None:
    """Every float-family type renders as CAST('<value>' AS <type>)."""
    assert _render_typed_literal("1.5", sql_type) == f"CAST('1.5' AS {sql_type})"


def test_render_typed_literal_boolean_cast() -> None:
    """BOOLEAN renders as CAST('<value>' AS BOOLEAN)."""
    assert _render_typed_literal("true", "BOOLEAN") == "CAST('true' AS BOOLEAN)"


def test_render_typed_literal_decimal_numeric_cast() -> None:
    """DECIMAL(...) / NUMERIC(...) prefixes render as CAST with the full type."""
    assert (
        _render_typed_literal("9.99", "DECIMAL(10,2)")
        == "CAST('9.99' AS DECIMAL(10,2))"
    )
    assert (
        _render_typed_literal("9.99", "NUMERIC(8,3)") == "CAST('9.99' AS NUMERIC(8,3))"
    )


def test_render_typed_literal_escapes_quotes_inside_cast() -> None:
    """Single quotes in the value are '' escaped, in and out of a CAST."""
    assert _render_typed_literal("o'brien", "VARCHAR") == "'o''brien'"
    assert _render_typed_literal("o'1", "BIGINT") == "CAST('o''1' AS BIGINT)"


def test_render_typed_literal_unrecognized_type_raises() -> None:
    """An unrecognized SQL type raises ExportError — no silent VARCHAR fallback."""
    with pytest.raises(ExportError, match="unrecognized SQL type"):
        _render_typed_literal("x", "TIMESTAMP")


def test_render_typed_literal_matches_dimensional_sibling() -> None:
    """The local mirror stays byte-identical to the exporters.dimensional copy.

    The module docstring pins the two implementations as byte-identical; a
    divergence in either direction fails here.
    """
    from fabulexa_forge.exporters.dimensional.columns import render_typed_literal

    cases = [
        ("lead", "VARCHAR"),
        ("lead", "VARCHAR(32)"),
        ("7", "BIGINT"),
        ("7", "UINTEGER"),
        ("1.5", "DOUBLE"),
        ("true", "BOOLEAN"),
        ("9.99", "DECIMAL(10,2)"),
        ("9.99", "NUMERIC(8,3)"),
        ("o'brien", "VARCHAR"),
    ]
    for value, sql_type in cases:
        assert _render_typed_literal(value, sql_type) == render_typed_literal(
            value, sql_type
        ), f"divergence for {(value, sql_type)}"

    with pytest.raises(ExportError):
        render_typed_literal("x", "TIMESTAMP")


# ---------------------------------------------------------------------------
# REFERENCE_RESOLUTION_COLUMNS constant
# ---------------------------------------------------------------------------


def test_reference_resolution_columns_constant() -> None:
    """REFERENCE_RESOLUTION_COLUMNS is the canonical two-element tuple."""
    assert REFERENCE_RESOLUTION_COLUMNS == ("record_id", "resolved")


# ---------------------------------------------------------------------------
# _collect_reference_columns
# ---------------------------------------------------------------------------


def test_collect_reference_columns_empty(tmp_path: Path) -> None:
    """Returns empty dict when no prop__ columns have references."""
    cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "prop__name", "type": "VARCHAR"},
    ]
    tables = [_table_spec("records__item", "records", cols, 0, "item")]
    emit_dir = _build_emit(tmp_path, tables, {}, {"records__item": cols})
    with open_emit(emit_dir) as emit:
        result = _collect_reference_columns(emit.sidecar)
    assert result == {}


def test_collect_reference_columns_finds_ref_col(tmp_path: Path) -> None:
    """Finds prop__ column annotated with references."""
    cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "prop__owner_id", "type": "VARCHAR", "references": "owner"},
    ]
    tables = [_table_spec("records__item", "records", cols, 0, "item")]
    emit_dir = _build_emit(tmp_path, tables, {}, {"records__item": cols})
    with open_emit(emit_dir) as emit:
        result = _collect_reference_columns(emit.sidecar)
    assert "item" in result
    assert result["item"][0].name == "prop__owner_id"
    assert result["item"][0].references == "owner"


# ---------------------------------------------------------------------------
# _find_all_reference_paths
# ---------------------------------------------------------------------------


def test_find_paths_zero_hop_same_kind() -> None:
    """Same from_kind and to_kind returns a single empty-list path."""
    paths = _find_all_reference_paths("item", "item", {})
    assert paths == [[]]


def test_find_paths_no_path_returns_empty() -> None:
    """No path from_kind to to_kind returns empty list."""
    paths = _find_all_reference_paths("item", "owner", {})
    assert paths == []


def test_find_paths_single_hop(tmp_path: Path) -> None:
    """Single hop from item to owner via prop__owner_id."""
    cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "prop__owner_id", "type": "VARCHAR", "references": "owner"},
    ]
    tables = [_table_spec("records__item", "records", cols, 0, "item")]
    emit_dir = _build_emit(tmp_path, tables, {}, {"records__item": cols})
    with open_emit(emit_dir) as emit:
        ref_map = _collect_reference_columns(emit.sidecar)
    paths = _find_all_reference_paths("item", "owner", ref_map)
    assert len(paths) == 1
    assert len(paths[0]) == 1
    assert paths[0][0].name == "prop__owner_id"


def test_find_paths_multi_hop(tmp_path: Path) -> None:
    """Multi-hop: item → dept → org."""
    item_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "prop__dept_id", "type": "VARCHAR", "references": "dept"},
    ]
    dept_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "prop__org_id", "type": "VARCHAR", "references": "org"},
    ]
    tables = [
        _table_spec("records__item", "records", item_cols, 0, "item"),
        _table_spec("records__dept", "records", dept_cols, 0, "dept"),
    ]
    col_specs = {"records__item": item_cols, "records__dept": dept_cols}
    emit_dir = _build_emit(tmp_path, tables, {}, col_specs)
    with open_emit(emit_dir) as emit:
        ref_map = _collect_reference_columns(emit.sidecar)
    paths = _find_all_reference_paths("item", "org", ref_map)
    assert len(paths) == 1
    assert [c.name for c in paths[0]] == ["prop__dept_id", "prop__org_id"]


# ---------------------------------------------------------------------------
# _path_hint_to_cols
# ---------------------------------------------------------------------------


def test_path_hint_to_cols_valid(tmp_path: Path) -> None:
    """Valid path hint resolves to ordered ColumnSpec list."""
    cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "prop__owner_id", "type": "VARCHAR", "references": "owner"},
    ]
    tables = [_table_spec("records__item", "records", cols, 0, "item")]
    emit_dir = _build_emit(tmp_path, tables, {}, {"records__item": cols})
    with open_emit(emit_dir) as emit:
        hops = _path_hint_to_cols(
            ["prop__owner_id"], "item", emit.sidecar, "test_table.test_col"
        )
    assert len(hops) == 1
    assert hops[0].name == "prop__owner_id"
    assert hops[0].references == "owner"


def test_path_hint_to_cols_invalid_col_raises(tmp_path: Path) -> None:
    """Non-references column in hint raises ExportError."""
    cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "prop__name", "type": "VARCHAR"},
    ]
    tables = [_table_spec("records__item", "records", cols, 0, "item")]
    emit_dir = _build_emit(tmp_path, tables, {}, {"records__item": cols})
    with open_emit(emit_dir) as emit:
        with pytest.raises(ExportError, match="not a references column"):
            _path_hint_to_cols(["prop__name"], "item", emit.sidecar, "dim_foo.col_bar")


# ---------------------------------------------------------------------------
# build_reference_path_sql — zero-hop
# ---------------------------------------------------------------------------


def test_build_reference_path_sql_zero_hop_record_id(tmp_path: Path) -> None:
    """Zero-hop (empty hops) with terminal_projection='record_id' returns anchor record_ids."""
    cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "prop__name", "type": "VARCHAR"},
    ]
    tables = [_table_spec("records__item", "records", cols, 1, "item")]
    rows = [("trunk", "i001", "Alpha")]
    emit_dir = _build_emit(
        tmp_path, tables, {"records__item": rows}, {"records__item": cols}
    )
    with open_emit(emit_dir) as emit:
        sql = build_reference_path_sql(
            sidecar=emit.sidecar,
            fork_path="trunk",
            anchor_kind="item",
            hop_columns=[],
            terminal_projection="record_id",
        )
        result = emit.query(sql, ())
    assert result == [("i001", "i001")]


def test_build_reference_path_sql_zero_hop_prop(tmp_path: Path) -> None:
    """Zero-hop with terminal_projection='prop__name' returns prop value as resolved."""
    cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "prop__name", "type": "VARCHAR"},
    ]
    tables = [_table_spec("records__item", "records", cols, 1, "item")]
    rows = [("trunk", "i001", "Alpha")]
    emit_dir = _build_emit(
        tmp_path, tables, {"records__item": rows}, {"records__item": cols}
    )
    with open_emit(emit_dir) as emit:
        sql = build_reference_path_sql(
            sidecar=emit.sidecar,
            fork_path="trunk",
            anchor_kind="item",
            hop_columns=[],
            terminal_projection="prop__name",
        )
        result = emit.query(sql, ())
    assert result == [("i001", "Alpha")]


# ---------------------------------------------------------------------------
# build_reference_path_sql — single-hop
# ---------------------------------------------------------------------------


def test_build_reference_path_sql_single_hop_fan_out_free(tmp_path: Path) -> None:
    """Single-hop resolves to the target record_id; each anchor has at most one resolved."""
    item_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "prop__owner_id", "type": "VARCHAR", "references": "owner"},
    ]
    owner_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "prop__name", "type": "VARCHAR"},
    ]
    tables = [
        _table_spec("records__item", "records", item_cols, 2, "item"),
        _table_spec("records__owner", "records", owner_cols, 2, "owner"),
    ]
    item_rows = [
        ("trunk", "i001", "o001"),
        ("trunk", "i002", None),  # unresolvable → NULL
    ]
    owner_rows = [
        ("trunk", "o001", "Alice"),
    ]
    emit_dir = _build_emit(
        tmp_path,
        tables,
        {"records__item": item_rows, "records__owner": owner_rows},
        {"records__item": item_cols, "records__owner": owner_cols},
    )
    with open_emit(emit_dir) as emit:
        ref_map = _collect_reference_columns(emit.sidecar)
        hops = _find_all_reference_paths("item", "owner", ref_map)[0]
        sql = build_reference_path_sql(
            sidecar=emit.sidecar,
            fork_path="trunk",
            anchor_kind="item",
            hop_columns=hops,
            terminal_projection="record_id",
        )
        result = dict(emit.query(sql, ()))
    # i001 → o001; i002 → NULL (unresolvable)
    assert result["i001"] == "o001"
    assert result["i002"] is None


# ---------------------------------------------------------------------------
# build_reference_path_sql — multi-hop
# ---------------------------------------------------------------------------


def test_build_reference_path_sql_multi_hop(tmp_path: Path) -> None:
    """Multi-hop chain resolves through intermediate records."""
    item_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "prop__dept_id", "type": "VARCHAR", "references": "dept"},
    ]
    dept_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "prop__org_id", "type": "VARCHAR", "references": "org"},
    ]
    org_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "prop__name", "type": "VARCHAR"},
    ]
    tables = [
        _table_spec("records__item", "records", item_cols, 1, "item"),
        _table_spec("records__dept", "records", dept_cols, 1, "dept"),
        _table_spec("records__org", "records", org_cols, 1, "org"),
    ]
    emit_dir = _build_emit(
        tmp_path,
        tables,
        {
            "records__item": [("trunk", "i001", "d001")],
            "records__dept": [("trunk", "d001", "org001")],
            "records__org": [("trunk", "org001", "Acme")],
        },
        {
            "records__item": item_cols,
            "records__dept": dept_cols,
            "records__org": org_cols,
        },
    )
    with open_emit(emit_dir) as emit:
        ref_map = _collect_reference_columns(emit.sidecar)
        hops = _find_all_reference_paths("item", "org", ref_map)[0]
        sql = build_reference_path_sql(
            sidecar=emit.sidecar,
            fork_path="trunk",
            anchor_kind="item",
            hop_columns=hops,
            terminal_projection="record_id",
        )
        result = emit.query(sql, ())
    assert result == [("i001", "org001")]


def test_build_reference_path_sql_invalid_terminal_raises() -> None:
    """Invalid terminal_projection raises ExportError at build time."""
    # We don't need a full sidecar — the error fires before any sidecar access
    with pytest.raises(ExportError, match="terminal_projection"):
        build_reference_path_sql(
            sidecar=None,  # type: ignore[arg-type]
            fork_path="trunk",
            anchor_kind="item",
            hop_columns=[],
            terminal_projection="bad_column",
        )


# ---------------------------------------------------------------------------
# build_membership_edge_sql
# ---------------------------------------------------------------------------

_MEM_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "elem__role", "type": "VARCHAR"},
    {"name": "member__person__id", "type": "VARCHAR"},
    {"name": "member__person__kind", "type": "VARCHAR"},
]


def _build_membership_emit(
    tmp_path: Path,
    mem_rows: list[tuple[Any, ...]],
    owner_rows: list[tuple[Any, ...]],
) -> Path:
    owner_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
    ]
    tables = [
        _table_spec(
            "membership__team__members",
            "membership",
            _MEM_COLS,
            len(mem_rows),
            property_name="members",
        ),
        _table_spec("records__team", "records", owner_cols, len(owner_rows), "team"),
    ]
    return _build_emit(
        tmp_path,
        tables,
        {
            "membership__team__members": mem_rows,
            "records__team": owner_rows,
        },
        {
            "membership__team__members": _MEM_COLS,
            "records__team": owner_cols,
        },
        # The membership table spec omits record_kind (fixture content, unchanged
        # by migration); the vendored schema requires it, so this fixture is
        # schema-invalid by construction.
        schema_valid=False,
    )


def test_build_membership_edge_sql_narrows_by_kind(tmp_path: Path) -> None:
    """Narrows to member__kind = member_kind; excludes other kinds."""
    mem_rows: list[tuple[Any, ...]] = [
        ("trunk", "team1", "lead", "p001", "person"),
        ("trunk", "team1", "member", "p002", "other_kind"),  # wrong kind
    ]
    owner_rows: list[tuple[Any, ...]] = [("trunk", "team1")]
    emit_dir = _build_membership_emit(tmp_path, mem_rows, owner_rows)
    with open_emit(emit_dir) as emit:
        sql = build_membership_edge_sql(
            sidecar=emit.sidecar,
            fork_path="trunk",
            owner_kind="team",
            property_name="members",
            member_field="person",
            member_kind="person",
            where_predicate={},
        )
        result = dict(emit.query(sql, ()))
    # Only the row with member__person__kind = 'person' is included
    assert result == {"team1": "p001"}


def test_build_membership_edge_sql_with_where_predicate(tmp_path: Path) -> None:
    """where_predicate filters elem__ columns further."""
    mem_rows: list[tuple[Any, ...]] = [
        ("trunk", "team1", "lead", "p001", "person"),
        ("trunk", "team1", "member", "p002", "person"),
    ]
    owner_rows: list[tuple[Any, ...]] = [("trunk", "team1")]
    emit_dir = _build_membership_emit(tmp_path, mem_rows, owner_rows)
    with open_emit(emit_dir) as emit:
        sql = build_membership_edge_sql(
            sidecar=emit.sidecar,
            fork_path="trunk",
            owner_kind="team",
            property_name="members",
            member_field="person",
            member_kind="person",
            where_predicate={"elem__role": "lead"},
        )
        result = emit.query(sql, ())
    assert result == [("team1", "p001")]


def test_build_membership_edge_sql_bigint_where_predicate_cast(tmp_path: Path) -> None:
    """A BIGINT elem__ where_predicate value renders as a CAST and filters rows."""
    mem_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "elem__rank", "type": "BIGINT"},
        {"name": "member__person__id", "type": "VARCHAR"},
        {"name": "member__person__kind", "type": "VARCHAR"},
    ]
    owner_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
    ]
    tables = [
        _table_spec(
            "membership__team__members",
            "membership",
            mem_cols,
            2,
            property_name="members",
        ),
        _table_spec("records__team", "records", owner_cols, 1, "team"),
    ]
    emit_dir = _build_emit(
        tmp_path,
        tables,
        {
            "membership__team__members": [
                ("trunk", "team1", 1, "p001", "person"),
                ("trunk", "team1", 2, "p002", "person"),
            ],
            "records__team": [("trunk", "team1")],
        },
        {
            "membership__team__members": mem_cols,
            "records__team": owner_cols,
        },
        # The membership table spec omits record_kind (fixture content, unchanged
        # by migration); the vendored schema requires it, so this fixture is
        # schema-invalid by construction.
        schema_valid=False,
    )
    with open_emit(emit_dir) as emit:
        sql = build_membership_edge_sql(
            sidecar=emit.sidecar,
            fork_path="trunk",
            owner_kind="team",
            property_name="members",
            member_field="person",
            member_kind="person",
            where_predicate={"elem__rank": "1"},
        )
        result = emit.query(sql, ())
    assert "CAST('1' AS BIGINT)" in sql
    assert result == [("team1", "p001")]


def test_build_membership_edge_sql_not_fan_out_free(tmp_path: Path) -> None:
    """Without a narrowing where, multiple members per owner fan out the result."""
    mem_rows: list[tuple[Any, ...]] = [
        ("trunk", "team1", "lead", "p001", "person"),
        ("trunk", "team1", "member", "p002", "person"),
    ]
    owner_rows: list[tuple[Any, ...]] = [("trunk", "team1")]
    emit_dir = _build_membership_emit(tmp_path, mem_rows, owner_rows)
    with open_emit(emit_dir) as emit:
        sql = build_membership_edge_sql(
            sidecar=emit.sidecar,
            fork_path="trunk",
            owner_kind="team",
            property_name="members",
            member_field="person",
            member_kind="person",
            where_predicate={},
        )
        result = emit.query(sql, ())
    # Both rows come through — not fan-out-free
    assert len(result) == 2
    assert {r[0] for r in result} == {"team1"}


def test_build_membership_edge_sql_missing_table(tmp_path: Path) -> None:
    """Missing membership table raises ExportError."""
    owner_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
    ]
    tables = [_table_spec("records__team", "records", owner_cols, 0, "team")]
    emit_dir = _build_emit(tmp_path, tables, {}, {"records__team": owner_cols})
    with open_emit(emit_dir) as emit:
        with pytest.raises(ExportError, match="not found in emit"):
            build_membership_edge_sql(
                sidecar=emit.sidecar,
                fork_path="trunk",
                owner_kind="team",
                property_name="members",
                member_field="person",
                member_kind="person",
                where_predicate={},
            )


def test_build_membership_edge_sql_bad_member_field(tmp_path: Path) -> None:
    """Bad member_field (not present on table) raises ExportError."""
    mem_rows: list[tuple[Any, ...]] = [
        ("trunk", "team1", "lead", "p001", "person"),
    ]
    owner_rows: list[tuple[Any, ...]] = [("trunk", "team1")]
    emit_dir = _build_membership_emit(tmp_path, mem_rows, owner_rows)
    with open_emit(emit_dir) as emit:
        with pytest.raises(ExportError, match="not found"):
            build_membership_edge_sql(
                sidecar=emit.sidecar,
                fork_path="trunk",
                owner_kind="team",
                property_name="members",
                member_field="nonexistent",
                member_kind="person",
                where_predicate={},
            )
