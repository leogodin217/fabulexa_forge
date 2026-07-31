"""Emit + config scaffold for tier-2 shaped-playback tests (Phase 10+).

One emit exercises every class `tables()` must classify, for both a
dimensional shape and a declared source shape:
  - records__gadget: untracked, role 'dimension' -> a `state` table in a
      source shape (always `snapshot`-delivered); a records-grain type-1
      dim in a dimensional shape.
  - records__shipment: untracked, role 'fact' -> a `state` table in a
      source shape; a records-grain fact in a dimensional shape.
  - records__widget: tracked (prop__status), 2 history points (sim_time 0,
      10) -> a `state` table plus an event-log source in a source shape
      (the log `append`-delivered); also a history_point fact and an SCD-2
      dim in a dimensional shape.
  - membership__widget__parts: 2 rows — one still-open interval
      (joined_sim_time=5, no leave) and one closed interval
      (joined_sim_time=2, left_sim_time=8) exercising extract-on-change
      left_at horizon-masking -> a `junction` table in a source shape (always
      `append`-delivered); a membership-grain table in a dimensional shape
      (the windowed-grain rule's rejection case).

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
    DerivedSpec,
    DimensionalConfig,
    ExportConfig,
    MembershipRef,
    SourceConfig,
    SourceDecl,
    SourceEventsDecl,
    SourceEventSourceDecl,
    SourceTableDecl,
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
    conn.execute(
        'INSERT INTO "membership__widget__parts" VALUES (?, ?, ?, ?, ?)',
        [FORK_PATH, "w1", 2, 8, "nut"],
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
                2,
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


_TARGET_COLUMNS: list[dict[str, object]] = [
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

_REFERRER_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__target_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="target",
    ),
    identity_column("ref_index__target_id", "BIGINT"),
]


def build_fk_hop_test_emit(tmp_path: "Path") -> "Path":
    """A dedicated (non-shared) emit exercising an fk hop to a kind outside
    the shape's declared sources: records__referrer holds ref_index__target_id
    pointing at records__target, but no shape declared in this fixture module
    exports a target table of its own.

    records__target: t1 (created_sim_time=0), t2 (created_sim_time=50).
    records__referrer: r1 (created_sim_time=0), referencing t2 — physically
    ref_index__target_id=2 (t2's record_index), so a truncated read at T=0
    (where t2 does not yet exist) re-deriving NULL is a visible deviation
    from the physical value, never a leak of it.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory), ready for open_emit.
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__target", _TARGET_COLUMNS))
    conn.execute(_create_ddl("records__referrer", _REFERRER_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

    conn.execute(
        'INSERT INTO "records__target" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        [FORK_PATH, "t1", 0, True, 0, 1, "Target-A"],
    )
    conn.execute(
        'INSERT INTO "records__target" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        [FORK_PATH, "t2", 50, True, 50, 2, "Target-B"],
    )
    conn.execute(
        'INSERT INTO "records__referrer" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        [FORK_PATH, "r1", 0, True, 0, 1, "t2", 2],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__target", "records", _TARGET_COLUMNS, 2, record_kind="target"
            ),
            _table_spec(
                "records__referrer",
                "records",
                _REFERRER_COLUMNS,
                1,
                record_kind="referrer",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 0),
        ],
        branches=[{"fork_path": FORK_PATH, "parent": None, "slice_at": 100}],
    )
    return tmp_path


def fk_hop_shape_config() -> ExportConfig:
    """A dimensional shape declaring only `referrer` as a source — `target`
    is a kind the shape never declares, reached solely through referrer's
    ref_index__target_id fk-hop column."""
    return ExportConfig(
        mode="dimensional",
        dimensional=DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_referrer",
                    role="fact",
                    source=SourceDecl(grain="records", kind="referrer"),
                    key=["id"],
                    columns=[
                        _from_col("id", "record_id"),
                        _from_col("target_index", "ref_index__target_id"),
                    ],
                ),
            ]
        ),
    )


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
                        _from_col("mutated_at", "last_mutation_sim_time"),
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
    """The declared source shape over the fixture emit: three `state`
    tables (gadget, shipment, widget), one `junction` table
    (widget_parts), and an event log over widget's tracked history.

    Deterministic enumeration order (`tables` declaration order, the event
    log last): gadget, shipment, widget, widget_parts, widget_versions.
    Deliveries: every `state` table snapshots; the junction and the event
    log append.
    """
    return ExportConfig(
        mode="source",
        source=SourceConfig(
            tables=(
                SourceTableDecl(name="gadget", kind="gadget"),
                SourceTableDecl(name="shipment", kind="shipment"),
                SourceTableDecl(name="widget", kind="widget"),
                SourceTableDecl(
                    name="widget_parts",
                    membership=MembershipRef(kind="widget", property="parts"),
                ),
            ),
            events=SourceEventsDecl(
                name="widget_versions",
                sources=(SourceEventSourceDecl(kind="widget"),),
            ),
        ),
    )


def source_last_mutation_named_shape_config() -> ExportConfig:
    """A `state` table naming `last_mutation_sim_time` explicitly in
    `columns` — the windowed-refusal counterpart of the dimensional
    `window_delivery=None` diagnostic: opens (the full-export shape
    validates, `updated_at` is reconstructible for a full export), but the
    first `window()` ask raises `SourceColumnUnresolved` from the windowed
    plan build (`last_mutation_sim_time` is not reconstructible at a past
    horizon)."""
    return ExportConfig(
        mode="source",
        source=SourceConfig(
            tables=(
                SourceTableDecl(
                    name="widget",
                    kind="widget",
                    columns=("last_mutation_sim_time",),
                ),
            )
        ),
    )


def windowable_dimensional_shape_config() -> ExportConfig:
    """A dimensional shape carrying every windowable class, no windowed-grain
    rejection: dim_gadget (type-1, snapshot), fact_shipment (records fact,
    append), fact_widget_status (history_point fact, append), and
    dim_widget_status (SCD-2, append)."""
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
                        _from_col("mutated_at", "last_mutation_sim_time"),
                    ],
                ),
                TableDecl(
                    name="fact_widget_status",
                    role="fact",
                    source=SourceDecl(
                        grain="history_point", kind="widget", property="status"
                    ),
                    key=["record_id", "sim_time"],
                    columns=[
                        _from_col("record_id", "record_id"),
                        _from_col("sim_time", "sim_time"),
                        _from_col("status", "value"),
                    ],
                ),
                TableDecl(
                    name="dim_widget_status",
                    role="dim",
                    scd="type2",
                    source=SourceDecl(grain="records", kind="widget"),
                    key=["id", "valid_from"],
                    columns=[
                        _from_col("id", "record_id"),
                        _from_col("status", "prop__status"),
                        ColumnDecl(
                            name="valid_from",
                            derived=DerivedSpec(scd_window="valid_from"),
                        ),
                        ColumnDecl(
                            name="valid_to",
                            derived=DerivedSpec(scd_window="valid_to"),
                        ),
                    ],
                ),
            ]
        ),
    )


# ---------------------------------------------------------------------------
# Phase 12 (state): a dedicated emit whose every physical last_mutation_sim_time
# is self-consistent with its recorded trail (the bridging theorem's declared
# precondition — § Shaped state, The recorded trail) so state(T_slice) is
# value-identical to the full export on every lmst-sourced column too, not just
# the ones that avoid it.
# ---------------------------------------------------------------------------


def state_test_table_specs(rows: dict[str, int]) -> list[dict[str, object]]:
    """build_state_test_emit's sidecar table specs, with row counts overridden.

    Shared with the interior-T materialized-truncated-emit test oracle, whose
    physical row counts differ from the full emit's (fewer rows survive
    truncation) but whose column shape is identical (this fixture carries no
    slice_only columns, so the truncated tape drops none).

    Args:
        rows: table name -> row count to stamp on that table's sidecar entry.

    Returns:
        The five build_state_test_emit table specs, in sidecar order.
    """
    return [
        _table_spec(
            "records__gadget",
            "records",
            _GADGET_COLUMNS,
            rows["records__gadget"],
            record_kind="gadget",
        ),
        _table_spec(
            "records__shipment",
            "records",
            _SHIPMENT_COLUMNS,
            rows["records__shipment"],
            record_kind="shipment",
        ),
        _table_spec(
            "records__widget",
            "records",
            _WIDGET_COLUMNS,
            rows["records__widget"],
            record_kind="widget",
        ),
        _table_spec("history", "fixed", _HISTORY_COLUMNS, rows["history"]),
        _table_spec(
            "membership__widget__parts",
            "membership",
            _WIDGET_PARTS_COLUMNS,
            rows["membership__widget__parts"],
            record_kind="widget",
            property_name="parts",
        ),
    ]


def build_state_test_emit(tmp_path: "Path") -> "Path":
    """A dedicated emit for Phase 12 state() tests: gadget (type-1, self-
    consistent lmst), shipment (records fact, self-consistent lmst), widget
    (tracked prop__status, 3 history points at sim_time 0/10/20 -> "new" /
    "assembled" / "shipped", lmst=20 = its latest tracked history instant),
    membership__widget__parts (bolt: joined=5 still open; nut: joined=2,
    left=15).

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
        [FORK_PATH, "g1", 0, True, 0, 1, "Widget-A"],
    )
    conn.execute(
        'INSERT INTO "records__shipment" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        [FORK_PATH, "s1", 0, True, 0, 1, "100"],
    )
    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        [FORK_PATH, "w1", 0, True, 20, 1, "shipped"],
    )
    for row in (
        (FORK_PATH, "widget", "w1", "status", 0, "new"),
        (FORK_PATH, "widget", "w1", "status", 10, "assembled"),
        (FORK_PATH, "widget", "w1", "status", 20, "shipped"),
    ):
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.execute(
        'INSERT INTO "membership__widget__parts" VALUES (?, ?, ?, NULL, ?)',
        [FORK_PATH, "w1", 5, "bolt"],
    )
    conn.execute(
        'INSERT INTO "membership__widget__parts" VALUES (?, ?, ?, ?, ?)',
        [FORK_PATH, "w1", 2, 15, "nut"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=state_test_table_specs(
            {
                "records__gadget": 1,
                "records__shipment": 1,
                "records__widget": 1,
                "history": 3,
                "membership__widget__parts": 2,
            }
        ),
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


def state_dimensional_shape_config() -> ExportConfig:
    """The non-membership classes over build_state_test_emit: type-1 dim,
    SCD-2 dim, history_point fact, history_interval fact, and a records-grain
    fact projecting the tracked status current-as-of-T plus the (now
    self-consistent) recorded trail."""
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
                        _from_col("mutated_at", "last_mutation_sim_time"),
                    ],
                ),
                TableDecl(
                    name="fact_widget_current",
                    role="fact",
                    source=SourceDecl(grain="records", kind="widget"),
                    key=["id"],
                    columns=[
                        _from_col("id", "record_id"),
                        _from_col("status", "prop__status"),
                        _from_col("mutated_at", "last_mutation_sim_time"),
                    ],
                ),
                TableDecl(
                    name="fact_widget_status",
                    role="fact",
                    source=SourceDecl(
                        grain="history_point", kind="widget", property="status"
                    ),
                    key=["record_id", "sim_time"],
                    columns=[
                        _from_col("record_id", "record_id"),
                        _from_col("sim_time", "sim_time"),
                        _from_col("status", "value"),
                    ],
                ),
                TableDecl(
                    name="fact_widget_interval",
                    role="fact",
                    source=SourceDecl(
                        grain="history_interval", kind="widget", property="status"
                    ),
                    key=["record_id", "sim_time"],
                    columns=[
                        _from_col("record_id", "record_id"),
                        _from_col("sim_time", "sim_time"),
                        _from_col("lead_sim_time", "lead_sim_time"),
                        _from_col("status", "value"),
                    ],
                ),
                TableDecl(
                    name="dim_widget_status",
                    role="dim",
                    scd="type2",
                    source=SourceDecl(grain="records", kind="widget"),
                    key=["id", "valid_from"],
                    columns=[
                        _from_col("id", "record_id"),
                        _from_col("status", "prop__status"),
                        ColumnDecl(
                            name="valid_from",
                            derived=DerivedSpec(scd_window="valid_from"),
                        ),
                        ColumnDecl(
                            name="valid_to",
                            derived=DerivedSpec(scd_window="valid_to"),
                        ),
                    ],
                ),
            ]
        ),
    )


def state_junction_shape_config() -> ExportConfig:
    """The membership-grain class over build_state_test_emit, isolated in its
    own shape (window() would reject it wholesale — the windowed-grain rule
    — so it is never mixed into state_dimensional_shape_config()); projects
    left_at for the leave-after-T masking assertion."""
    return ExportConfig(
        mode="dimensional",
        dimensional=DimensionalConfig(
            tables=[
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
                        _from_col("left_at", "left_sim_time"),
                    ],
                ),
            ]
        ),
    )


def state_source_shape_config() -> ExportConfig:
    """The declared source shape over build_state_test_emit: three `state`
    tables (gadget, shipment, widget) and one `junction` table
    (widget_parts). Every `state` table's `state()` and windowed
    reconstruction are `snapshot`-delivered by construction."""
    return ExportConfig(
        mode="source",
        source=SourceConfig(
            tables=(
                SourceTableDecl(name="gadget", kind="gadget"),
                SourceTableDecl(name="shipment", kind="shipment"),
                SourceTableDecl(name="widget", kind="widget"),
                SourceTableDecl(
                    name="widget_parts",
                    membership=MembershipRef(kind="widget", property="parts"),
                ),
            )
        ),
    )
