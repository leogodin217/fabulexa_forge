"""Tests for the lookup SQL builder + column dispatch.

Covers build_lookup_expr (SQL-string), the lookup arm of build_column_expr,
temporal-safety checks via check_lookup_temporal_safety and validate_table,
plus end-to-end execution tests through build_query_specs.

Phase 5: build_lookup_expr now composes build_reference_path_sql, emitting a
single LEFT JOIN of a derivation subquery aliased _lookup_<col>_rp.  The
SELECT expression projects <alias>."resolved".

v6: every records table's fork_path/record_id route through identity_column
and carries a record_index; every reference-annotated prop__ column carries
its ref_index__ sibling.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from _support.sidecar_builder import identity_column, write_emit

from exporters._emit_fixtures import _create_ddl, _table_spec
from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import (
    ColumnDecl,
    DimensionalConfig,
    FkClause,
    LookupClause,
    SourceDecl,
    TableDecl,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.columns import build_column_expr
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.dimensional.lookup import (
    build_lookup_expr,
    check_lookup_temporal_safety,
)
from fabulexa_forge.exporters.dimensional.validation import validate_table
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.sidecar import Sidecar

# ---------------------------------------------------------------------------
# Column / table spec helpers
# ---------------------------------------------------------------------------

_ACTOR_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    {"name": "prop__name", "type": "VARCHAR"},
    {"name": "prop__tier", "type": "VARCHAR"},
]

_PRODUCT_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    # references actor (owner)
    {"name": "prop__owner_id", "type": "VARCHAR", "references": "actor"},
    identity_column("ref_index__owner_id", "BIGINT"),
    {"name": "prop__category", "type": "VARCHAR"},
]

_ORDER_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    # references product (one hop to product, then product → actor is two hops)
    {"name": "prop__product_id", "type": "VARCHAR", "references": "product"},
    identity_column("ref_index__product_id", "BIGINT"),
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


# ---------------------------------------------------------------------------
# Sidecar factory helpers
# ---------------------------------------------------------------------------


def _build_sidecar_dict(tables: list[dict[str, object]]) -> dict[str, object]:
    """Build a minimal sidecar dict from a list of table specs."""
    return {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        "tables": tables,
    }


def _make_sidecar(tables: list[dict[str, object]]) -> Sidecar:
    """Build a Sidecar object from a list of table specs."""
    return Sidecar.from_raw(_build_sidecar_dict(tables))


def _lookup_col(
    name: str,
    property: str,
    to: str | None = None,
    path: list[str] | None = None,
) -> ColumnDecl:
    """Build a lookup ColumnDecl."""
    kwargs: dict[str, object] = {"property": property}
    if to is not None:
        kwargs["to"] = to
    if path is not None:
        kwargs["path"] = path
    return ColumnDecl(name=name, lookup=LookupClause(**kwargs))


def _from_col(name: str, src: str) -> ColumnDecl:
    """Build a from ColumnDecl."""
    return ColumnDecl(name=name, **{"from": src})


def _fact(
    name: str,
    grain: str,
    kind: str,
    cols: list[ColumnDecl],
    property: str | None = None,
) -> TableDecl:
    """Build a fact TableDecl."""
    src_kwargs: dict[str, object] = {"grain": grain, "kind": kind}
    if property is not None:
        src_kwargs["property"] = property
    return TableDecl(
        name=name,
        role="fact",
        key=["record_id"],
        source=SourceDecl(**src_kwargs),  # type: ignore[arg-type]
        columns=cols,
    )


def _table_decl(
    name: str, grain: str, kind: str, property: str | None = None
) -> TableDecl:
    """Build a minimal fact TableDecl (no columns — for error-message use)."""
    src_kwargs: dict[str, object] = {"grain": grain, "kind": kind}
    if property is not None:
        src_kwargs["property"] = property
    return TableDecl(
        name=name,
        role="fact",
        key=["record_id"],
        source=SourceDecl(**src_kwargs),  # type: ignore[arg-type]
        columns=[_from_col("record_id", "record_id")],
    )


def _deriv_alias(col_name: str) -> str:
    """Return the derivation subquery alias for a lookup column."""
    return f"_lookup_{col_name}_rp"


# ---------------------------------------------------------------------------
# Zero-hop self lookup on history_interval grain
# ---------------------------------------------------------------------------


def test_zero_hop_self_lookup_history_interval_emits_reference_path_subquery() -> None:
    """Zero-hop self lookup: one LEFT JOIN of the reference-path derivation subquery."""
    sidecar = _make_sidecar(
        [
            _table_spec("records__actor", "records", _ACTOR_COLUMNS, 2, "actor"),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 4),
        ]
    )
    col_decl = _lookup_col("actor_name", "name")
    table_decl = _table_decl("fact_actor_state", "history_interval", "actor", "state")

    expr, joins = build_lookup_expr(
        col_decl=col_decl,
        table_decl=table_decl,
        anchor_kind="actor",
        anchor_alias="_grain",
        source_grain="history_interval",
        sidecar=sidecar,
    )

    alias = _deriv_alias("actor_name")
    # One JOIN: the derivation subquery
    assert len(joins) == 1
    assert "LEFT JOIN" in joins[0]
    assert f'AS "{alias}"' in joins[0]
    assert f'"{alias}"."record_id"' in joins[0]
    assert '"_grain"."record_id"' in joins[0]
    # Subquery embeds the preamble records__actor reference-path SQL
    assert '"records__actor"' in joins[0]
    assert '"prop__name"' in joins[0]
    # SELECT projects resolved column aliased to the output column name
    assert f'"{alias}"."resolved"' in expr
    assert '"actor_name"' in expr


# ---------------------------------------------------------------------------
# Zero-hop self lookup on membership grain enriches from owner
# ---------------------------------------------------------------------------


def test_zero_hop_self_lookup_membership_grain_enriches_owner() -> None:
    """Zero-hop self lookup on membership grain: reference-path subquery to owner's records."""
    _MEMBERSHIP_COLUMNS: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "joined_sim_time", "type": "BIGINT"},
        {"name": "left_sim_time", "type": "BIGINT"},
        {"name": "member__actor__kind", "type": "VARCHAR"},
        {"name": "member__actor__id", "type": "VARCHAR"},
    ]
    sidecar = _make_sidecar(
        [
            _table_spec("records__actor", "records", _ACTOR_COLUMNS, 2, "actor"),
            _table_spec(
                "membership__actor__roles",
                "membership",
                _MEMBERSHIP_COLUMNS,
                2,
                "actor",
                "roles",
            ),
        ]
    )
    col_decl = _lookup_col("owner_tier", "tier")
    table_decl = _table_decl("fact_memberships", "membership", "actor", "roles")

    expr, joins = build_lookup_expr(
        col_decl=col_decl,
        table_decl=table_decl,
        anchor_kind="actor",
        anchor_alias="_grain",
        source_grain="membership",
        sidecar=sidecar,
    )

    alias = _deriv_alias("owner_tier")
    assert len(joins) == 1
    assert "LEFT JOIN" in joins[0]
    assert f'AS "{alias}"' in joins[0]
    # Subquery references actor's records table and target property
    assert '"records__actor"' in joins[0]
    assert '"prop__tier"' in joins[0]
    # SELECT projects resolved aliased to output column
    assert f'"{alias}"."resolved"' in expr
    assert '"owner_tier"' in expr


# ---------------------------------------------------------------------------
# Cross-kind single-hop lookup on records grain
# ---------------------------------------------------------------------------


def test_cross_kind_single_hop_lookup_records_grain_composed_subquery() -> None:
    """Single-hop lookup on records grain: one subquery JOIN with the full hop chain."""
    sidecar = _make_sidecar(
        [
            _table_spec("records__product", "records", _PRODUCT_COLUMNS, 3, "product"),
            _table_spec("records__actor", "records", _ACTOR_COLUMNS, 2, "actor"),
        ]
    )
    col_decl = _lookup_col("owner_name", "name", to="actor")
    table_decl = _table_decl("fact_products", "records", "product")

    expr, joins = build_lookup_expr(
        col_decl=col_decl,
        table_decl=table_decl,
        anchor_kind="product",
        anchor_alias="_grain",
        source_grain="records",
        sidecar=sidecar,
    )

    alias = _deriv_alias("owner_name")
    # One JOIN: the derivation subquery encapsulates the hop chain
    assert len(joins) == 1
    assert "LEFT JOIN" in joins[0]
    assert f'AS "{alias}"' in joins[0]
    assert f'"{alias}"."record_id"' in joins[0]
    assert '"_grain"."record_id"' in joins[0]
    # Subquery traverses product → actor via prop__owner_id
    assert '"records__product"' in joins[0]
    assert '"records__actor"' in joins[0]
    assert '"prop__owner_id"' in joins[0]
    # Terminal property projected inside subquery
    assert '"prop__name"' in joins[0]
    # SELECT projects resolved aliased to output column
    assert f'"{alias}"."resolved"' in expr
    assert '"owner_name"' in expr


# ---------------------------------------------------------------------------
# Cross-kind multi-hop lookup on non-records grain: full chain in subquery
# ---------------------------------------------------------------------------


def test_cross_kind_multi_hop_lookup_non_records_grain_full_chain_in_subquery() -> None:
    """Multi-hop lookup on history_interval grain: all hops inside the derivation subquery."""
    sidecar = _make_sidecar(
        [
            _table_spec("records__order", "records", _ORDER_COLUMNS, 4, "order"),
            _table_spec("records__product", "records", _PRODUCT_COLUMNS, 3, "product"),
            _table_spec("records__actor", "records", _ACTOR_COLUMNS, 2, "actor"),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 4),
        ]
    )
    col_decl = _lookup_col("owner_name", "name", to="actor")
    table_decl = _table_decl("fact_order_state", "history_interval", "order", "state")

    expr, joins = build_lookup_expr(
        col_decl=col_decl,
        table_decl=table_decl,
        anchor_kind="order",
        anchor_alias="_grain",
        source_grain="history_interval",
        sidecar=sidecar,
    )

    alias = _deriv_alias("owner_name")
    # One JOIN: the entire hop chain is inside the derivation subquery
    assert len(joins) == 1
    assert "LEFT JOIN" in joins[0]
    assert f'AS "{alias}"' in joins[0]
    assert f'"{alias}"."record_id"' in joins[0]
    assert '"_grain"."record_id"' in joins[0]
    # Subquery contains the full 3-table chain: order → product → actor
    assert '"records__order"' in joins[0]
    assert '"records__product"' in joins[0]
    assert '"records__actor"' in joins[0]
    assert '"prop__product_id"' in joins[0]
    assert '"prop__owner_id"' in joins[0]
    # Terminal property
    assert '"prop__name"' in joins[0]
    # SELECT projects resolved
    assert f'"{alias}"."resolved"' in expr
    assert '"owner_name"' in expr


# ---------------------------------------------------------------------------
# Multi-hop with path hint resolves hop-by-hop
# ---------------------------------------------------------------------------


def test_multi_hop_with_path_hint_matches_autopathfind() -> None:
    """Multi-hop lookup with path hint produces same result as auto-pathfind."""
    sidecar = _make_sidecar(
        [
            _table_spec("records__product", "records", _PRODUCT_COLUMNS, 3, "product"),
            _table_spec("records__actor", "records", _ACTOR_COLUMNS, 2, "actor"),
        ]
    )
    col_auto = _lookup_col("owner_name", "name", to="actor")
    col_hint = _lookup_col("owner_name", "name", to="actor", path=["prop__owner_id"])
    table_decl = _table_decl("fact_products", "records", "product")

    expr_auto, joins_auto = build_lookup_expr(
        col_decl=col_auto,
        table_decl=table_decl,
        anchor_kind="product",
        anchor_alias="_grain",
        source_grain="records",
        sidecar=sidecar,
    )
    expr_hint, joins_hint = build_lookup_expr(
        col_decl=col_hint,
        table_decl=table_decl,
        anchor_kind="product",
        anchor_alias="_grain",
        source_grain="records",
        sidecar=sidecar,
    )

    assert expr_auto == expr_hint
    assert joins_auto == joins_hint


# ---------------------------------------------------------------------------
# Two lookup columns on one table emit non-colliding aliases
# ---------------------------------------------------------------------------


def test_two_lookup_columns_non_colliding_aliases() -> None:
    """Two lookup columns produce distinct alias namespaces."""
    sidecar = _make_sidecar(
        [
            _table_spec("records__actor", "records", _ACTOR_COLUMNS, 2, "actor"),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 4),
        ]
    )
    col_name = _lookup_col("actor_name", "name")
    col_tier = _lookup_col("actor_tier", "tier")
    table_decl = _table_decl("fact_actor_state", "history_interval", "actor", "state")

    _, joins_name = build_lookup_expr(
        col_decl=col_name,
        table_decl=table_decl,
        anchor_kind="actor",
        anchor_alias="_grain",
        source_grain="history_interval",
        sidecar=sidecar,
    )
    _, joins_tier = build_lookup_expr(
        col_decl=col_tier,
        table_decl=table_decl,
        anchor_kind="actor",
        anchor_alias="_grain",
        source_grain="history_interval",
        sidecar=sidecar,
    )

    alias_name = _deriv_alias("actor_name")
    alias_tier = _deriv_alias("actor_tier")
    # The alias strings must not overlap
    assert set(joins_name).isdisjoint(set(joins_tier))
    assert f'AS "{alias_name}"' in joins_name[0]
    assert f'AS "{alias_tier}"' in joins_tier[0]


# ---------------------------------------------------------------------------
# build_column_expr dispatches lookup and leaves other modes intact
# ---------------------------------------------------------------------------


def test_build_column_expr_dispatches_lookup() -> None:
    """build_column_expr dispatches a lookup column to build_lookup_expr."""
    sidecar = _make_sidecar(
        [
            _table_spec("records__actor", "records", _ACTOR_COLUMNS, 2, "actor"),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 4),
        ]
    )
    col_decl = _lookup_col("actor_name", "name")
    table_decl = _table_decl("fact_actor_state", "history_interval", "actor", "state")

    expr, joins = build_column_expr(
        col_decl=col_decl,
        anchor=None,
        table_decl=table_decl,
        source_grain="history_interval",
        anchor_kind="actor",
        sidecar=sidecar,
    )

    alias = _deriv_alias("actor_name")
    assert f'"{alias}"."resolved"' in expr
    assert len(joins) == 1
    assert "LEFT JOIN" in joins[0]
    assert f'AS "{alias}"' in joins[0]


def test_build_column_expr_from_mode_unaffected() -> None:
    """build_column_expr still handles from columns correctly (regression)."""
    col_decl = _from_col("record_id", "record_id")
    expr, joins = build_column_expr(col_decl=col_decl, anchor=None)
    assert '"_grain"."record_id"' in expr
    assert joins == []


def test_build_column_expr_null_mode_unaffected() -> None:
    """build_column_expr still handles null columns correctly (regression)."""
    col_decl = ColumnDecl(name="dummy", **{"null": True})
    expr, joins = build_column_expr(col_decl=col_decl, anchor=None)
    assert "NULL" in expr
    assert joins == []


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_build_lookup_expr_raises_on_unresolvable_path() -> None:
    """build_lookup_expr raises ExportError when no path from anchor to terminal."""
    _WIDGET_COLUMNS: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        {"name": "prop__label", "type": "VARCHAR"},
    ]
    sidecar2 = _make_sidecar(
        [
            _table_spec("records__widget", "records", _WIDGET_COLUMNS, 2, "widget"),
            _table_spec("records__actor", "records", _ACTOR_COLUMNS, 2, "actor"),
        ]
    )
    col_decl = _lookup_col("actor_name", "name", to="actor")
    table_decl = _table_decl("fact_widgets", "records", "widget")

    with pytest.raises(ExportError, match="no reference path"):
        build_lookup_expr(
            col_decl=col_decl,
            table_decl=table_decl,
            anchor_kind="widget",
            anchor_alias="_grain",
            source_grain="records",
            sidecar=sidecar2,
        )


def test_build_lookup_expr_raises_on_ambiguous_path() -> None:
    """build_lookup_expr raises ExportError when path is ambiguous and no hint given."""
    _AMBIGUOUS_PRODUCT_COLUMNS: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        {"name": "prop__owner_id", "type": "VARCHAR", "references": "actor"},
        identity_column("ref_index__owner_id", "BIGINT"),
        {"name": "prop__alt_owner_id", "type": "VARCHAR", "references": "actor"},
        identity_column("ref_index__alt_owner_id", "BIGINT"),
    ]
    sidecar = _make_sidecar(
        [
            _table_spec(
                "records__product",
                "records",
                _AMBIGUOUS_PRODUCT_COLUMNS,
                3,
                "product",
            ),
            _table_spec("records__actor", "records", _ACTOR_COLUMNS, 2, "actor"),
        ]
    )
    col_decl = _lookup_col("owner_name", "name", to="actor")
    table_decl = _table_decl("fact_products", "records", "product")

    with pytest.raises(ExportError, match="ambiguous"):
        build_lookup_expr(
            col_decl=col_decl,
            table_decl=table_decl,
            anchor_kind="product",
            anchor_alias="_grain",
            source_grain="records",
            sidecar=sidecar,
        )


# ---------------------------------------------------------------------------
# Execution test: history_interval fact with zero-hop self lookup
# ---------------------------------------------------------------------------


def _build_lookup_emit(tmp_path: Path) -> Path:
    """Build a test emit for lookup execution tests.

    Contains:
      - records__actor: three rows (a001 Alice, a002 Bob, a003 Charlie)
      - history: actor.status changes — a001 and a002 have rows; a003 has no records row

    The a003 record_id appears in history but has no records row, so the lookup
    should yield NULL for that row.
    """
    _ACTOR_COLS: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        {"name": "prop__name", "type": "VARCHAR", "history_tracked": False},
    ]
    _HIST_COLS: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "kind", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "property", "type": "VARCHAR"},
        {"name": "sim_time", "type": "BIGINT"},
        {"name": "value", "type": "VARCHAR"},
    ]

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__actor", _ACTOR_COLS))
    conn.execute(_create_ddl("history", _HIST_COLS))

    # actor rows: a001 and a002 exist; a003 intentionally absent
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a001", 10, True, 10, 0, "Alice"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a002", 20, True, 20, 1, "Bob"],
    )

    # history: actor.status — a001 has two state changes, a002 has one, a003 unmatched
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "actor", "a001", "status", 5, "active"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "actor", "a001", "status", 15, "inactive"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "actor", "a002", "status", 10, "active"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "actor", "a003", "status", 12, "active"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec("records__actor", "records", _ACTOR_COLS, 2, "actor"),
            _table_spec("history", "fixed", _HIST_COLS, 4),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
    )
    return tmp_path


def test_history_interval_zero_hop_lookup_execution(tmp_path: Path) -> None:
    """history_interval fact with zero-hop self lookup executes correctly.

    Verifies:
    - matched rows project the record's prop__name value
    - unmatched record_id (a003 has no records row) yields NULL
    - output row count equals the no-lookup baseline (fan-out-free)
    """
    emit_dir = _build_lookup_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _fact(
                    "fact_actor_status",
                    "history_interval",
                    "actor",
                    [
                        _from_col("record_id", "record_id"),
                        _from_col("sim_time", "sim_time"),
                        _from_col("value", "value"),
                        _lookup_col("actor_name", "name"),
                    ],
                    property="status",
                )
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        spec = next(s for s in specs if s.table_name == "fact_actor_status")

    # Execute generated SQL after emit is closed
    conn = duckdb.connect(str(tmp_path / "run.duckdb"))
    rows = conn.execute(spec.sql).fetchall()
    col_names = [d[0] for d in conn.description]  # type: ignore[union-attr]
    conn.close()

    row_dicts = [dict(zip(col_names, r)) for r in rows]
    row_count = len(row_dicts)

    # Row count baseline: 4 history rows (a001×2, a002×1, a003×1)
    assert row_count == 4

    # a001 rows should have actor_name = "Alice"
    a001_rows = [r for r in row_dicts if r["record_id"] == "a001"]
    assert len(a001_rows) == 2
    assert all(r["actor_name"] == "Alice" for r in a001_rows)

    # a002 row should have actor_name = "Bob"
    a002_rows = [r for r in row_dicts if r["record_id"] == "a002"]
    assert len(a002_rows) == 1
    assert a002_rows[0]["actor_name"] == "Bob"

    # a003 has no records row → NULL
    a003_rows = [r for r in row_dicts if r["record_id"] == "a003"]
    assert len(a003_rows) == 1
    assert a003_rows[0]["actor_name"] is None


def test_history_interval_zero_hop_lookup_row_count_matches_baseline(
    tmp_path: Path,
) -> None:
    """Lookup projection does not fan out: row count equals the no-lookup baseline."""
    emit_dir = _build_lookup_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config_lookup = DimensionalConfig(
            tables=[
                _fact(
                    "fact_actor_status",
                    "history_interval",
                    "actor",
                    [
                        _from_col("record_id", "record_id"),
                        _from_col("value", "value"),
                        _lookup_col("actor_name", "name"),
                    ],
                    property="status",
                )
            ]
        )
        config_baseline = DimensionalConfig(
            tables=[
                _fact(
                    "fact_actor_status",
                    "history_interval",
                    "actor",
                    [
                        _from_col("record_id", "record_id"),
                        _from_col("value", "value"),
                    ],
                    property="status",
                )
            ]
        )
        specs_lookup = build_query_specs(emit, config_lookup, None, None)
        specs_baseline = build_query_specs(emit, config_baseline, None, None)

        spec_l = next(s for s in specs_lookup if s.table_name == "fact_actor_status")
        spec_b = next(s for s in specs_baseline if s.table_name == "fact_actor_status")

    conn = duckdb.connect(str(tmp_path / "run.duckdb"))
    count_lookup = conn.execute(f"SELECT COUNT(*) FROM ({spec_l.sql})").fetchone()[0]  # type: ignore[index]
    count_baseline = conn.execute(f"SELECT COUNT(*) FROM ({spec_b.sql})").fetchone()[0]  # type: ignore[index]
    conn.close()

    assert count_lookup == count_baseline


# ---------------------------------------------------------------------------
# Temporal-safety: check_lookup_temporal_safety
# ---------------------------------------------------------------------------


def _actor_cols_with_history_tracked(
    name_tracked: bool,
) -> list[dict[str, object]]:
    """Build actor columns with history_tracked flags.

    Args:
        name_tracked: history_tracked value for prop__name.
    """
    return [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        {"name": "prop__name", "type": "VARCHAR", "history_tracked": name_tracked},
    ]


def _actor_cols_with_ref(
    ref_tracked: bool,
) -> list[dict[str, object]]:
    """Build product columns referencing actor, with history_tracked on the ref."""
    return [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        {
            "name": "prop__owner_id",
            "type": "VARCHAR",
            "references": "actor",
            "history_tracked": ref_tracked,
        },
        identity_column("ref_index__owner_id", "BIGINT"),
        {"name": "prop__category", "type": "VARCHAR", "history_tracked": False},
    ]


def test_temporal_safety_type2_terminal_property_rejected() -> None:
    """history_tracked: true terminal property is rejected (type-2 message)."""
    sidecar = _make_sidecar(
        [
            _table_spec(
                "records__actor",
                "records",
                _actor_cols_with_history_tracked(name_tracked=True),
                2,
                "actor",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 4),
        ]
    )
    col_decl = _lookup_col("actor_name", "name")
    table_decl = _table_decl("fact_actor_state", "history_interval", "actor", "state")

    with pytest.raises(ExportError, match="history_tracked: true"):
        check_lookup_temporal_safety(
            col_decl=col_decl,
            table_decl=table_decl,
            anchor_kind="actor",
            source_grain="history_interval",
            sidecar=sidecar,
        )


def test_temporal_safety_type1_terminal_property_passes() -> None:
    """history_tracked: false terminal property passes the safety check."""
    sidecar = _make_sidecar(
        [
            _table_spec(
                "records__actor",
                "records",
                _actor_cols_with_history_tracked(name_tracked=False),
                2,
                "actor",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 4),
        ]
    )
    col_decl = _lookup_col("actor_name", "name")
    table_decl = _table_decl("fact_actor_state", "history_interval", "actor", "state")

    check_lookup_temporal_safety(
        col_decl=col_decl,
        table_decl=table_decl,
        anchor_kind="actor",
        source_grain="history_interval",
        sidecar=sidecar,
    )


def test_temporal_safety_type2_hop_column_rejected() -> None:
    """A traversed hop column that is history_tracked: true is rejected."""
    actor_cols: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        {"name": "prop__name", "type": "VARCHAR", "history_tracked": False},
    ]
    product_cols = _actor_cols_with_ref(ref_tracked=True)
    sidecar = _make_sidecar(
        [
            _table_spec("records__product", "records", product_cols, 3, "product"),
            _table_spec("records__actor", "records", actor_cols, 2, "actor"),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 4),
        ]
    )
    col_decl = _lookup_col("owner_name", "name", to="actor")
    table_decl = _table_decl("fact_prod_state", "history_interval", "product", "state")

    with pytest.raises(ExportError, match="history_tracked: true"):
        check_lookup_temporal_safety(
            col_decl=col_decl,
            table_decl=table_decl,
            anchor_kind="product",
            source_grain="history_interval",
            sidecar=sidecar,
        )


def test_temporal_safety_no_history_tracked_flag_rejected() -> None:
    """An emit without history_tracked flags is rejected (no fallback)."""
    # Columns have no history_tracked key — sidecar sees None, available() returns False
    sidecar = _make_sidecar(
        [
            _table_spec("records__actor", "records", _ACTOR_COLUMNS, 2, "actor"),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 4),
        ]
    )
    col_decl = _lookup_col("actor_name", "name")
    table_decl = _table_decl("fact_actor_state", "history_interval", "actor", "state")

    with pytest.raises(ExportError, match="history_tracked"):
        check_lookup_temporal_safety(
            col_decl=col_decl,
            table_decl=table_decl,
            anchor_kind="actor",
            source_grain="history_interval",
            sidecar=sidecar,
        )


def test_temporal_safety_zero_hop_on_records_grain_rejected() -> None:
    """Zero-hop self lookup on a records grain is rejected (use from instead)."""
    sidecar = _make_sidecar(
        [
            _table_spec(
                "records__actor",
                "records",
                _actor_cols_with_history_tracked(name_tracked=False),
                2,
                "actor",
            ),
        ]
    )
    col_decl = _lookup_col("actor_name", "name")
    table_decl = _table_decl("fact_actors", "records", "actor")

    with pytest.raises(ExportError, match="records grain"):
        check_lookup_temporal_safety(
            col_decl=col_decl,
            table_decl=table_decl,
            anchor_kind="actor",
            source_grain="records",
            sidecar=sidecar,
        )


def test_temporal_safety_zero_hop_on_history_interval_grain_passes() -> None:
    """Zero-hop self lookup on a history_interval grain passes."""
    sidecar = _make_sidecar(
        [
            _table_spec(
                "records__actor",
                "records",
                _actor_cols_with_history_tracked(name_tracked=False),
                2,
                "actor",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 4),
        ]
    )
    col_decl = _lookup_col("actor_name", "name")
    table_decl = _table_decl("fact_actor_state", "history_interval", "actor", "state")

    check_lookup_temporal_safety(
        col_decl=col_decl,
        table_decl=table_decl,
        anchor_kind="actor",
        source_grain="history_interval",
        sidecar=sidecar,
    )


def test_temporal_safety_missing_terminal_property_rejected() -> None:
    """A missing terminal prop__<property> is rejected (LookupPropertyExists)."""
    sidecar = _make_sidecar(
        [
            _table_spec(
                "records__actor",
                "records",
                _actor_cols_with_history_tracked(name_tracked=False),
                2,
                "actor",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 4),
        ]
    )
    col_decl = _lookup_col("actor_email", "email")
    table_decl = _table_decl("fact_actor_state", "history_interval", "actor", "state")

    with pytest.raises(ExportError, match="prop__email"):
        check_lookup_temporal_safety(
            col_decl=col_decl,
            table_decl=table_decl,
            anchor_kind="actor",
            source_grain="history_interval",
            sidecar=sidecar,
        )


def test_temporal_safety_unresolvable_path_rejected() -> None:
    """An unresolvable reference path is rejected (LookupPathResolvable)."""
    actor_cols_t1: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        {"name": "prop__name", "type": "VARCHAR", "history_tracked": False},
    ]
    widget_cols: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        {"name": "prop__label", "type": "VARCHAR", "history_tracked": False},
    ]
    sidecar = _make_sidecar(
        [
            _table_spec("records__widget", "records", widget_cols, 2, "widget"),
            _table_spec("records__actor", "records", actor_cols_t1, 2, "actor"),
        ]
    )
    col_decl = _lookup_col("actor_name", "name", to="actor")
    table_decl = _table_decl("fact_widgets", "records", "widget")

    with pytest.raises(ExportError, match="no reference path"):
        check_lookup_temporal_safety(
            col_decl=col_decl,
            table_decl=table_decl,
            anchor_kind="widget",
            source_grain="history_interval",
            sidecar=sidecar,
        )


def test_temporal_safety_ambiguous_path_rejected() -> None:
    """An ambiguous reference path with no hint is rejected."""
    actor_cols_t1: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        {"name": "prop__name", "type": "VARCHAR", "history_tracked": False},
    ]
    ambig_product_cols: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        {
            "name": "prop__owner_id",
            "type": "VARCHAR",
            "references": "actor",
            "history_tracked": False,
        },
        identity_column("ref_index__owner_id", "BIGINT"),
        {
            "name": "prop__alt_owner_id",
            "type": "VARCHAR",
            "references": "actor",
            "history_tracked": False,
        },
        identity_column("ref_index__alt_owner_id", "BIGINT"),
    ]
    sidecar = _make_sidecar(
        [
            _table_spec(
                "records__product", "records", ambig_product_cols, 3, "product"
            ),
            _table_spec("records__actor", "records", actor_cols_t1, 2, "actor"),
        ]
    )
    col_decl = _lookup_col("owner_name", "name", to="actor")
    table_decl = _table_decl("fact_products", "records", "product")

    with pytest.raises(ExportError, match="ambiguous"):
        check_lookup_temporal_safety(
            col_decl=col_decl,
            table_decl=table_decl,
            anchor_kind="product",
            source_grain="history_interval",
            sidecar=sidecar,
        )


def test_temporal_safety_scd2_table_rejected() -> None:
    """A lookup column on an scd: type2 table is rejected with the SCD-2 message."""
    sidecar = _make_sidecar(
        [
            _table_spec(
                "records__actor",
                "records",
                _actor_cols_with_history_tracked(name_tracked=False),
                2,
                "actor",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 4),
        ]
    )
    col_decl = _lookup_col("actor_name", "name")
    table_decl = TableDecl(
        name="dim_actor_scd2",
        role="dim",
        key=["record_id"],
        source=SourceDecl(grain="history_interval", kind="actor", property="state"),  # type: ignore[call-arg]
        columns=[_from_col("record_id", "record_id"), col_decl],
        scd="type2",
    )

    with pytest.raises(ExportError, match="scd: type2"):
        check_lookup_temporal_safety(
            col_decl=col_decl,
            table_decl=table_decl,
            anchor_kind="actor",
            source_grain="history_interval",
            sidecar=sidecar,
        )


# ---------------------------------------------------------------------------
# Temporal-safety wired into validate_table
# ---------------------------------------------------------------------------


def _build_temporal_emit(tmp_path: Path, name_tracked: bool) -> Path:
    """Build a minimal emit for validate_table temporal-safety tests.

    Produces:
      - records__actor with prop__name (history_tracked as given)
      - history table

    Args:
        tmp_path: Directory to write emit artifacts into.
        name_tracked: history_tracked flag for prop__name.
    """
    actor_cols: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        {"name": "prop__name", "type": "VARCHAR", "history_tracked": name_tracked},
    ]
    hist_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "kind", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "property", "type": "VARCHAR"},
        {"name": "sim_time", "type": "BIGINT"},
        {"name": "value", "type": "VARCHAR"},
    ]

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__actor", actor_cols))
    conn.execute(_create_ddl("history", hist_cols))
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a001", 10, True, 10, 0, "Alice"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "actor", "a001", "status", 5, "active"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec("records__actor", "records", actor_cols, 1, "actor"),
            _table_spec("history", "fixed", hist_cols, 1),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
    )
    return tmp_path


def test_validate_table_rejects_type2_lookup_fact(tmp_path: Path) -> None:
    """validate_table raises ExportError for a history_interval fact with type-2 lookup target."""
    emit_dir = _build_temporal_emit(tmp_path, name_tracked=True)
    with open_emit(emit_dir) as emit:
        table_decl = _fact(
            "fact_actor_status",
            "history_interval",
            "actor",
            [
                _from_col("record_id", "record_id"),
                _from_col("sim_time", "sim_time"),
                _lookup_col("actor_name", "name"),
            ],
            property="status",
        )
        config = DimensionalConfig(tables=[table_decl])
        with pytest.raises(ExportError, match="history_tracked: true"):
            validate_table(table_decl, config, emit.sidecar, None)


def test_validate_table_passes_type1_lookup_fact(tmp_path: Path) -> None:
    """validate_table passes for a history_interval fact with type-1 lookup target."""
    emit_dir = _build_temporal_emit(tmp_path, name_tracked=False)
    with open_emit(emit_dir) as emit:
        table_decl = _fact(
            "fact_actor_status",
            "history_interval",
            "actor",
            [
                _from_col("record_id", "record_id"),
                _from_col("sim_time", "sim_time"),
                _lookup_col("actor_name", "name"),
            ],
            property="status",
        )
        config = DimensionalConfig(tables=[table_decl])
        src_name = validate_table(table_decl, config, emit.sidecar, None)
    assert src_name == "history"


# ---------------------------------------------------------------------------
# Integration: build_query_specs raises ExportError for type-2 lookup fact
# ---------------------------------------------------------------------------


def test_build_query_specs_raises_for_type2_lookup_fact(tmp_path: Path) -> None:
    """build_query_specs raises ExportError for a type-2 lookup target before any SQL runs."""
    emit_dir = _build_temporal_emit(tmp_path, name_tracked=True)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _fact(
                    "fact_actor_status",
                    "history_interval",
                    "actor",
                    [
                        _from_col("record_id", "record_id"),
                        _lookup_col("actor_name", "name"),
                    ],
                    property="status",
                )
            ]
        )
        with pytest.raises(ExportError):
            build_query_specs(emit, config, None, None)


def _build_mixed_fk_lookup_emit(tmp_path: Path) -> Path:
    """Build an emit where an fk and two lookups share the same hop chain.

    Contains:
      - records__actor: a001 (Alice, gold), a002 (Bob, silver)
      - records__journey_instance: j001 → a001, j002 → a002 via prop__actor_id

    All prop__ columns carry history_tracked: false so lookup temporal safety
    passes. prop__actor_id's ref_index__actor_id sibling resolves to the
    referenced actor's record_index (a001 -> 0, a002 -> 1).
    """
    actor_cols: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        {"name": "prop__name", "type": "VARCHAR", "history_tracked": False},
        {"name": "prop__tier", "type": "VARCHAR", "history_tracked": False},
    ]
    journey_cols: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        {
            "name": "prop__actor_id",
            "type": "VARCHAR",
            "references": "actor",
            "history_tracked": False,
        },
        identity_column("ref_index__actor_id", "BIGINT"),
    ]

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__actor", actor_cols))
    conn.execute(_create_ddl("records__journey_instance", journey_cols))
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "a001", 10, True, 10, 0, "Alice", "gold"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "a002", 20, True, 20, 1, "Bob", "silver"],
    )
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "j001", 10, True, 10, 0, "a001", 0],
    )
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "j002", 20, True, 20, 1, "a002", 1],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec("records__actor", "records", actor_cols, 2, "actor"),
            _table_spec(
                "records__journey_instance",
                "records",
                journey_cols,
                2,
                "journey_instance",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
    )
    return tmp_path


def test_mixed_fk_and_two_lookups_share_hop_chain_no_collision(
    tmp_path: Path,
) -> None:
    """An fk and two lookups on one table share a hop chain without colliding.

    The per-column alias namespacing is proven separately for two FKs
    (test_fk.py) and two lookups (above); this pins that the two column
    *kinds* mixed on one table — all traversing the same prop__actor_id hop —
    still emit distinct aliases and resolve correct, fan-out-free values.
    """
    emit_dir = _build_mixed_fk_lookup_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        dim_actor = TableDecl(
            name="dim_actor",
            role="dim",
            scd="type1",
            key=["record_id"],
            source=SourceDecl(grain="records", kind="actor"),
            columns=[_from_col("record_id", "record_id")],
        )
        config = DimensionalConfig(
            tables=[
                dim_actor,
                _fact(
                    "fact_journey",
                    "records",
                    "journey_instance",
                    [
                        _from_col("record_id", "record_id"),
                        ColumnDecl(
                            name="actor_id",
                            fk=FkClause(to="dim_actor", via="reference"),
                        ),
                        _lookup_col("actor_name", "name", to="actor"),
                        _lookup_col("actor_tier", "tier", to="actor"),
                    ],
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_journey")

        # Each column kind keeps its own alias namespace
        assert '"_fk_actor_id_rp"' in fact_spec.sql
        assert f'"{_deriv_alias("actor_name")}"' in fact_spec.sql
        assert f'"{_deriv_alias("actor_tier")}"' in fact_spec.sql

        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    # Fan-out-free: one output row per journey
    assert rows["record_id"] == ["j001", "j002"]
    assert rows["actor_id"] == ["a001", "a002"]
    assert rows["actor_name"] == ["Alice", "Bob"]
    assert rows["actor_tier"] == ["gold", "silver"]


def test_build_query_specs_deterministic_sql_lookup(tmp_path: Path) -> None:
    """Two build_query_specs calls on the same emit+config yield byte-identical SQL."""
    emit_dir = _build_temporal_emit(tmp_path, name_tracked=False)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _fact(
                    "fact_actor_status",
                    "history_interval",
                    "actor",
                    [
                        _from_col("record_id", "record_id"),
                        _lookup_col("actor_name", "name"),
                    ],
                    property="status",
                )
            ]
        )
        specs1 = build_query_specs(emit, config, None, None)
        specs2 = build_query_specs(emit, config, None, None)

    sql1 = next(s.sql for s in specs1 if s.table_name == "fact_actor_status")
    sql2 = next(s.sql for s in specs2 if s.table_name == "fact_actor_status")
    assert sql1 == sql2
