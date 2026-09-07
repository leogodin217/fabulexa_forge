#!/usr/bin/env python
"""
Demo: the seam's `tables` asks, both modes.

Over a self-contained rebuild of the shaped test scaffold's emit shape (a
type-1 dim `gadget` and a tracked-status `widget`), opens a dimensional head
and a source head. For each, every singleton `window(..., tables={name})`
equals that table's answer under `tables=None` (name, delivery, and
`table.equals`) — projection invariance.

Horizon economy: a dimensional snapshot-only selection (`dim_gadget`, both
filtered on an unobserved discriminator value) opens the end horizon only,
so its plan notice fires once; the sibling `fact_widget` ('upsert',
delta-bearing) opens both, firing the same notice twice. On the source
head, a `gadget`-only ask (snapshot, not delta-bearing) opens the end
horizon only — one `build_source_plan` call; a `widget_events`-only ask
(the event log, delta-bearing) opens both — two calls.

Finally, the three selection gates: a bare `str`, an empty selection, and
an unknown name each raise `PlaybackError` before any compile.

Sprint: shaped-table-selection
Phase: 3
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    ColumnDecl,
    DimensionalConfig,
    ExportConfig,
    SourceConfig,
    SourceDecl,
    SourceEventsDecl,
    SourceEventSourceDecl,
    SourceTableDecl,
    TableDecl,
)
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.source import engine as source_engine
from fabulexa_forge.playback.errors import PlaybackError
from fabulexa_forge.playback.shaped import (
    ShapedPlayback,
    ShapedTable,
    open_shaped_playback,
)
from fabulexa_forge.reader.emit import open_emit

_GADGET_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    #: filtered on an unobserved value below — every compile of dim_gadget
    #: emits one 'discriminator-value-unobserved' Notice, once per horizon.
    {
        "name": "prop__category",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_WIDGET_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    #: fact_widget filters on the same unobserved category value as
    #: dim_gadget, so its compile emits the same notice.
    {
        "name": "prop__category",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _create_table(
    conn: duckdb.DuckDBPyConnection, name: str, columns: list[dict[str, object]]
) -> None:
    """Create one records table from its sidecar column list."""
    ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in columns)
    conn.execute(f'CREATE TABLE "{name}" ({ddl})')


def build_selection_emit(emit_dir: Path) -> None:
    """Write the emit both heads in this demo compile against.

    gadget: one row, category 'A'. widget: one row whose status changes at
    sim_time 0 -> 10 ('new' -> 'assembled'), category 'A' — so a
    dimensional fact_widget is 'upsert' and a source widget_events log
    carries two rows. Both kinds' dimensional tables filter on category
    'Z', a value neither row carries — every compile emits one
    discriminator-value-unobserved notice.

    Args:
        emit_dir: The directory to receive run.duckdb + base.json.
    """
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    _create_table(conn, "records__gadget", _GADGET_COLUMNS)
    _create_table(conn, "records__widget", _WIDGET_COLUMNS)
    _create_table(conn, "history", _HISTORY_COLUMNS)

    conn.execute(
        'INSERT INTO "records__gadget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "g1", 0, True, 0, 0, "Widget-A", "A"],
    )
    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "w1", 0, True, 10, 1, "assembled", "A"],
    )
    for row in (
        ("trunk", "widget", "w1", "status", 0, "new"),
        ("trunk", "widget", "w1", "status", 10, "assembled"),
    ):
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    base_json = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "surface": "published",
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": "records__gadget",
                "category": "records",
                "record_kind": "gadget",
                "columns": _GADGET_COLUMNS,
                "rows": 1,
            },
            {
                "name": "records__widget",
                "category": "records",
                "record_kind": "widget",
                "columns": _WIDGET_COLUMNS,
                "rows": 1,
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 2,
            },
        ],
        "runtime": {
            "timezone": "UTC",
            "start_datetime": "2024-01-01T00:00:00+00:00",
        },
        "record_roles": {"gadget": "dimension", "widget": "fact"},
        "enum_domains": {
            "gadget": {"category": [{"value": "A"}]},
            "widget": {"category": [{"value": "A"}]},
        },
    }
    (emit_dir / "base.json").write_text(json.dumps(base_json))


def _from_col(name: str, source: str) -> ColumnDecl:
    """A `from:`-sourced ColumnDecl."""
    return ColumnDecl(name=name, **{"from": source})


def dimensional_head_config() -> ExportConfig:
    """dim_gadget (type-1, snapshot) + fact_widget (upsert on prop__status);
    both filter prop__category on the unobserved value 'Z'."""
    return ExportConfig(
        mode="dimensional",
        dimensional=DimensionalConfig(
            tables=[
                TableDecl(
                    name="dim_gadget",
                    role="dim",
                    scd="type1",
                    source=SourceDecl(
                        grain="records", kind="gadget", filter={"prop__category": "Z"}
                    ),
                    key=["id"],
                    columns=[
                        _from_col("id", "record_id"),
                        _from_col("name", "prop__name"),
                    ],
                ),
                TableDecl(
                    name="fact_widget",
                    role="fact",
                    source=SourceDecl(
                        grain="records", kind="widget", filter={"prop__category": "Z"}
                    ),
                    key=["id"],
                    columns=[
                        _from_col("id", "record_id"),
                        _from_col("status", "prop__status"),
                    ],
                ),
            ]
        ),
    )


def source_head_config() -> ExportConfig:
    """gadget (state, snapshot) + a widget_events event log (append)."""
    return ExportConfig(
        mode="source",
        source=SourceConfig(
            tables=(SourceTableDecl(name="gadget", kind="gadget"),),
            events=SourceEventsDecl(
                name="widget_events",
                sources=(SourceEventSourceDecl(kind="widget"),),
            ),
        ),
    )


def _assert_singleton_matches_whole(
    head: ShapedPlayback, whole: dict[str, ShapedTable]
) -> None:
    """Every singleton `window(..., tables={name})` equals the same table's
    `tables=None` answer — projection invariance."""
    for decl in head.tables():
        singleton = head.window(0, 100, tables={decl.name})
        assert len(singleton) == 1
        answer = singleton[0]
        reference = whole[decl.name]
        assert answer.name == reference.name
        assert answer.delivery == reference.delivery
        assert answer.table.equals(reference.table)
        print(f"  {decl.name}: singleton ask equals the whole-shape answer")


def _count_source_plan_builds(head: ShapedPlayback, tables: set[str]) -> int:
    """How many times `build_source_plan` runs for one `window()` ask —
    one call per horizon opened."""
    calls: list[None] = []
    original = source_engine.build_source_plan

    def counting(*args: object, **kwargs: object) -> object:
        calls.append(None)
        return original(*args, **kwargs)

    with patch.object(source_engine, "build_source_plan", counting):
        head.window(0, 100, tables=tables)
    return len(calls)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp)
        build_selection_emit(emit_dir)

        with open_emit(emit_dir) as emit:
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)

            print("Dimensional head: singletons vs. the whole-shape answer")
            dim_head = open_shaped_playback(
                emit, dimensional_head_config(), anchor, lambda _n: None
            )
            dim_whole = {t.name: t for t in dim_head.window(0, 100)}
            _assert_singleton_matches_whole(dim_head, dim_whole)

            print("Source head: singletons vs. the whole-shape answer")
            src_head = open_shaped_playback(
                emit, source_head_config(), anchor, lambda _n: None
            )
            src_whole = {t.name: t for t in src_head.window(0, 100)}
            _assert_singleton_matches_whole(src_head, src_whole)

            print("Dimensional head: horizon economy")
            dim_notices: list[Notice] = []
            dim_econ_head = open_shaped_playback(
                emit, dimensional_head_config(), anchor, dim_notices.append
            )
            dim_notices.clear()  # discard the open-time validation notices
            dim_econ_head.window(0, 100, tables={"dim_gadget"})
            dim_gadget_notices = len(dim_notices)
            dim_notices.clear()
            dim_econ_head.window(0, 100, tables={"fact_widget"})
            fact_widget_notices = len(dim_notices)
            print(f"  tables={{'dim_gadget'}}: {dim_gadget_notices} notice(s)")
            print(f"  tables={{'fact_widget'}}: {fact_widget_notices} notice(s)")

            print("Source head: horizon economy")
            econ_head = open_shaped_playback(
                emit, source_head_config(), anchor, lambda _n: None
            )
            gadget_builds = _count_source_plan_builds(econ_head, {"gadget"})
            events_builds = _count_source_plan_builds(econ_head, {"widget_events"})
            print(f"  tables={{'gadget'}}: build_source_plan ran {gadget_builds}x")
            print(
                f"  tables={{'widget_events'}}: build_source_plan ran {events_builds}x"
            )

            if (
                dim_gadget_notices != 1
                or fact_widget_notices != 2
                or gadget_builds != 1
                or events_builds != 2
            ):
                print("FAILURE: horizon economy did not produce the expected counts")
                return 1

            print("Selection gates")
            try:
                dim_head.window(0, 100, tables="dim_gadget")
            except PlaybackError as exc:
                print(f"  bare str refused: {exc}")
            else:
                print("FAILURE: a bare str selection was not refused")
                return 1

            try:
                dim_head.window(0, 100, tables=set())
            except PlaybackError as exc:
                print(f"  empty selection refused: {exc}")
            else:
                print("FAILURE: an empty selection was not refused")
                return 1

            try:
                dim_head.window(0, 100, tables={"nope"})
            except PlaybackError as exc:
                print(f"  unknown name refused: {exc}")
            else:
                print("FAILURE: an unknown name was not refused")
                return 1

    print(
        "SUCCESS: tables asks answer selections, cost only their horizons,"
        " and gate before any compile"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
