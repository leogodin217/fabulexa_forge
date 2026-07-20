"""Emit + config scaffold for tier-2 shaped-playback tests (Phase 10+).

One emit exercises every class/genre `tables()` must classify:
  - records__gadget: untracked, role 'dimension' -> source genre 'reference';
      a records-grain type-1 dim in a dimensional shape.
  - records__shipment: untracked, role 'fact' -> source genre 'transaction';
      a records-grain fact in a dimensional shape.
  - records__widget: tracked (prop__status) -> source genre 'changelog'.
  - membership__widget__parts: -> source genre 'junction'; a membership-grain
      table in a dimensional shape (the windowed-grain rule's rejection case).

Every base.json write routes through `_support.sidecar_builder.write_emit`;
every value-carrying `prop__` column through `prop_column` — the one sidecar
authority for fixture-building test code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import duckdb
from _support.sidecar_builder import identity_column, prop_column, write_emit

from fabulexa_forge.config.models import (
    ColumnDecl,
    DimensionalConfig,
    ExportConfig,
    SourceDecl,
    TableDecl,
)

if TYPE_CHECKING:
    from pathlib import Path

FORK_PATH = "trunk"

_GADGET_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__name", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_SHIPMENT_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__amount", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_WIDGET_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

_WIDGET_PARTS_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__name", "type": "VARCHAR"},
]


def _from_col(name: str, src: str) -> ColumnDecl:
    """Build a ColumnDecl projecting `src` verbatim into output column `name`."""
    return ColumnDecl(name=name, **{"from": src})


def _table_spec(
    name: str,
    category: str,
    columns: list[dict[str, object]],
    rows: int,
    *,
    record_kind: str | None = None,
    property_name: str | None = None,
) -> dict[str, object]:
    """Build a table spec dict for a sidecar entry."""
    spec: dict[str, object] = {
        "name": name,
        "category": category,
        "columns": columns,
        "rows": rows,
    }
    if record_kind is not None:
        spec["record_kind"] = record_kind
    if property_name is not None:
        spec["property"] = property_name
    return spec


def _create_ddl(table_name: str, columns: list[dict[str, object]]) -> str:
    """Build a CREATE TABLE DDL statement."""
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in columns)
    return f'CREATE TABLE "{table_name}" ({parts})'


def build_shaped_test_emit(tmp_path: "Path") -> "Path":
    """Build the shared shaped-playback test emit: gadget, shipment, widget,
    and membership__widget__parts.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory), ready for open_emit.
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__gadget", _GADGET_COLUMNS))
    conn.execute(_create_ddl("records__shipment", _SHIPMENT_COLUMNS))
    conn.execute(_create_ddl("records__widget", _WIDGET_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_ddl("membership__widget__parts", _WIDGET_PARTS_COLUMNS))

    conn.execute(
        'INSERT INTO "records__gadget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        [FORK_PATH, "g1", 0, True, 0, 0, "Widget-A"],
    )
    conn.execute(
        'INSERT INTO "records__shipment" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        [FORK_PATH, "s1", 0, True, 0, 1, "100"],
    )
    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        [FORK_PATH, "w1", 0, True, 10, 2, "assembled"],
    )
    for row in (
        (FORK_PATH, "widget", "w1", "status", 0, "new"),
        (FORK_PATH, "widget", "w1", "status", 10, "assembled"),
    ):
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.execute(
        'INSERT INTO "membership__widget__parts" VALUES (?, ?, ?, NULL, ?)',
        [FORK_PATH, "w1", 5, "bolt"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__gadget", "records", _GADGET_COLUMNS, 1, record_kind="gadget"
            ),
            _table_spec(
                "records__shipment",
                "records",
                _SHIPMENT_COLUMNS,
                1,
                record_kind="shipment",
            ),
            _table_spec(
                "records__widget", "records", _WIDGET_COLUMNS, 1, record_kind="widget"
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 2),
            _table_spec(
                "membership__widget__parts",
                "membership",
                _WIDGET_PARTS_COLUMNS,
                1,
                record_kind="widget",
                property_name="parts",
            ),
        ],
        branches=[{"fork_path": FORK_PATH, "parent": None, "slice_at": 100}],
        extra={
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
            "record_roles": {"gadget": "dimension", "shipment": "fact"},
        },
    )
    return tmp_path


def dimensional_shape_config() -> ExportConfig:
    """The dimensional shape: type-1 dim, records fact, membership fact.

    dim_gadget -> snapshot; fact_shipment -> append; mem_widget_parts -> None
    (the windowed-grain rule's rejection case, a membership grain).
    """
    return ExportConfig(
        mode="dimensional",
        dimensional=DimensionalConfig(
            tables=[
                TableDecl(
                    name="dim_gadget",
                    role="dim",
                    scd="type1",
                    source=SourceDecl(grain="records", kind="gadget"),
                    key=["id"],
                    columns=[
                        _from_col("id", "record_id"),
                        _from_col("name", "prop__name"),
                    ],
                ),
                TableDecl(
                    name="fact_shipment",
                    role="fact",
                    source=SourceDecl(grain="records", kind="shipment"),
                    key=["id"],
                    columns=[
                        _from_col("id", "record_id"),
                        _from_col("amount", "prop__amount"),
                    ],
                ),
                TableDecl(
                    name="mem_widget_parts",
                    role="fact",
                    source=SourceDecl(
                        grain="membership", kind="widget", property="parts"
                    ),
                    key=["record_id"],
                    columns=[
                        _from_col("record_id", "record_id"),
                        _from_col("part_name", "elem__name"),
                    ],
                ),
            ]
        ),
    )


def source_shape_config() -> ExportConfig:
    """The bare source shape: a full dump over the fixture emit.

    Deterministic enumeration order (sidecar table order): gadget
    (reference -> snapshot), shipment (transaction -> append), widget
    (changelog -> append), widget_parts (junction -> append).
    """
    return ExportConfig(mode="source")
