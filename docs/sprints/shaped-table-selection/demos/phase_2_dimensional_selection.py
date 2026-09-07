#!/usr/bin/env python
"""
Demo: `build_query_specs(..., tables=)` compiles a selection of a dimensional
config's declared tables — per-table compile, WindowKeyDuplicate, and plan
notices scoped to the selection — and opens the windowed start horizon only
when the selection is delta-bearing (an 'upsert' or 'append' table among
it), so a snapshot-only selection's notices fire once per ask instead of
twice.

Sprint: shaped-table-selection
Phase: 2
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import (
    ColumnDecl,
    DimensionalConfig,
    FkClause,
    SourceDecl,
    TableDecl,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.reader.emit import open_emit

#: The fixed history table — always required (C3), empty here.
_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

#: gadget: the type-1 dim. prop__category is filtered on an unobserved value
#: below, so every compile of dim_gadget emits one 'discriminator-value-
#: unobserved' Notice regardless of horizon count.
_GADGET_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__category",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

#: widget: fact_append reads only invariant channels (record_id + the
#: reference fk); fact_upsert reads prop__status, which varies, and filters
#: on the same unobserved prop__category value as dim_gadget.
_WIDGET_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__gadget_id",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
        "references": "gadget",
    },
    {"name": "ref_index__gadget_id", "type": "BIGINT"},
    {
        "name": "prop__category",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
]

#: part: fact_dupkey is keyed on prop__category, a stable column two rows
#: share — WindowKeyDuplicate refuses it once selected under a window.
_PART_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__category",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
]

#: The window every compile in this demo shares: [0, 100).
_WINDOW = Window(index=0, start_ns=0, end_ns=100, label="w0")


def _column_ddl(name: str, duckdb_type: str) -> str:
    """One column's DDL fragment."""
    return f'"{name}" {duckdb_type}'


def _create_table(
    conn: duckdb.DuckDBPyConnection, name: str, columns: list[dict[str, object]]
) -> None:
    """Create one records table from its sidecar column list."""
    ddl = ", ".join(_column_ddl(str(c["name"]), str(c["type"])) for c in columns)
    conn.execute(f'CREATE TABLE "{name}" ({ddl})')


def build_selection_emit(emit_dir: Path) -> None:
    """Write the emit this demo's two configs compile against.

    gadget: one dim row. widget: two fact rows sharing a gadget reference
    (fact_append's fk edge), one status each (fact_upsert's varying
    channel). part: two rows sharing a category (fact_dupkey's duplicate
    key).

    Args:
        emit_dir: The directory to receive run.duckdb + base.json.
    """
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    _create_table(conn, "records__gadget", _GADGET_COLUMNS)
    _create_table(conn, "records__widget", _WIDGET_COLUMNS)
    _create_table(conn, "records__part", _PART_COLUMNS)
    _create_table(conn, "history", _HISTORY_COLUMNS)

    conn.execute(
        "INSERT INTO records__gadget VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
        ["trunk", "g1", 0, True, 0, 0, "A", "Widget-A"],
    )
    conn.execute(
        "INSERT INTO records__widget VALUES"
        " (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?),"
        " (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
        [
            "trunk",
            "w1",
            10,
            True,
            10,
            0,
            "g1",
            0,
            "A",
            "new",
            "trunk",
            "w2",
            60,
            True,
            60,
            1,
            "g1",
            0,
            "B",
            "used",
        ],
    )
    conn.execute(
        "INSERT INTO records__part VALUES"
        " (?, ?, ?, ?, NULL, ?, ?, ?, ?),"
        " (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
        [
            "trunk",
            "p1",
            20,
            True,
            20,
            0,
            "A",
            "new",
            "trunk",
            "p2",
            70,
            True,
            70,
            1,
            "A",
            "used",
        ],
    )
    conn.close()

    base_json = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "surface": "published",
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 200}],
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
                "rows": 2,
            },
            {
                "name": "records__part",
                "category": "records",
                "record_kind": "part",
                "columns": _PART_COLUMNS,
                "rows": 2,
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 0,
            },
        ],
        "runtime": {
            "timezone": "UTC",
            "start_datetime": "2024-01-01T00:00:00+00:00",
        },
        "record_roles": {"gadget": "dimension", "widget": "fact", "part": "fact"},
        "enum_domains": {
            "gadget": {"category": [{"value": "A"}, {"value": "B"}]},
            "widget": {"category": [{"value": "A"}, {"value": "B"}]},
        },
    }
    (emit_dir / "base.json").write_text(json.dumps(base_json))


def _from_col(name: str, source: str) -> ColumnDecl:
    """A `from:`-sourced ColumnDecl."""
    return ColumnDecl(name=name, **{"from": source})


def _fk_col(name: str, to: str) -> ColumnDecl:
    """A reference-fk ColumnDecl targeting a declared dim table by name."""
    return ColumnDecl(name=name, fk=FkClause(to=to, via="reference"))


def selection_config() -> DimensionalConfig:
    """dim_gadget (snapshot) + fact_append (append) + fact_upsert (upsert).

    dim_gadget and fact_upsert each filter prop__category on the
    unobserved value 'Z', so each compile of either emits one plan notice.
    """
    return DimensionalConfig(
        tables=[
            TableDecl(
                name="dim_gadget",
                role="dim",
                scd="type1",
                source=SourceDecl(
                    grain="records", kind="gadget", filter={"prop__category": "Z"}
                ),
                key=["record_id"],
                columns=[_from_col("record_id", "record_id")],
            ),
            TableDecl(
                name="fact_append",
                role="fact",
                source=SourceDecl(grain="records", kind="widget"),
                key=["record_id"],
                columns=[
                    _from_col("record_id", "record_id"),
                    _fk_col("gadget_ref", "dim_gadget"),
                ],
            ),
            TableDecl(
                name="fact_upsert",
                role="fact",
                source=SourceDecl(
                    grain="records", kind="widget", filter={"prop__category": "Z"}
                ),
                key=["record_id"],
                columns=[
                    _from_col("record_id", "record_id"),
                    _from_col("status", "prop__status"),
                ],
            ),
        ]
    )


def duplicate_key_config() -> DimensionalConfig:
    """dim_gadget + fact_dupkey, keyed on a stable column two rows share."""
    return DimensionalConfig(
        tables=[
            TableDecl(
                name="dim_gadget",
                role="dim",
                scd="type1",
                source=SourceDecl(grain="records", kind="gadget"),
                key=["record_id"],
                columns=[_from_col("record_id", "record_id")],
            ),
            TableDecl(
                name="fact_dupkey",
                role="fact",
                source=SourceDecl(grain="records", kind="part"),
                key=["category"],
                columns=[
                    _from_col("category", "prop__category"),
                    _from_col("status", "prop__status"),
                ],
            ),
        ]
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp)
        build_selection_emit(emit_dir)
        config = selection_config()

        # (a) A snapshot-only selection opens one horizon: one notice.
        dim_notices: list[Notice] = []
        with open_emit(emit_dir) as emit:
            dim_specs = build_query_specs(
                emit,
                config,
                None,
                _WINDOW,
                dim_notices.append,
                base_relations=None,
                tables={"dim_gadget"},
            )
        dim_names = [s.table_name for s in dim_specs]
        print(f"tables={{'dim_gadget'}}: {len(dim_notices)} notice(s), {dim_names}")

        # (b) A delta-bearing selection ('upsert') opens both horizons:
        # the same notice fires twice.
        upsert_notices: list[Notice] = []
        with open_emit(emit_dir) as emit:
            upsert_specs = build_query_specs(
                emit,
                config,
                None,
                _WINDOW,
                upsert_notices.append,
                base_relations=None,
                tables={"fact_upsert"},
            )
        print(f"tables={{'fact_upsert'}}: {len(upsert_notices)} notice(s)")
        if len(dim_notices) != 1 or len(upsert_notices) != 2:
            print("FAILURE: horizon economy did not produce the expected notice counts")
            return 1

        # (c) Projection invariance: the selected spec's SQL equals the same
        # table's spec under tables=None.
        with open_emit(emit_dir) as emit:
            whole_specs = build_query_specs(
                emit,
                config,
                None,
                _WINDOW,
                lambda _n: None,
                base_relations=None,
                tables=None,
            )
        whole_upsert_spec = next(
            s for s in whole_specs if s.table_name == "fact_upsert"
        )
        selected_upsert_spec = upsert_specs[0]
        if selected_upsert_spec.sql != whole_upsert_spec.sql:
            print("FAILURE: selected spec's SQL diverged from the whole-shape spec")
            return 1
        print("fact_upsert's selected-compile SQL equals its tables=None SQL")

        # (d) A whole-shape windowed compile over a duplicate-key upsert
        # table refuses; selecting the dim alone succeeds.
        dupkey_config = duplicate_key_config()
        with open_emit(emit_dir) as emit:
            try:
                build_query_specs(
                    emit,
                    dupkey_config,
                    None,
                    _WINDOW,
                    lambda _n: None,
                    base_relations=None,
                    tables=None,
                )
            except ExportError as exc:
                print(f"WindowKeyDuplicate refused (whole-shape compile): {exc}")
            else:
                print("FAILURE: duplicate key was not refused")
                return 1

        with open_emit(emit_dir) as emit:
            dim_only_specs = build_query_specs(
                emit,
                dupkey_config,
                None,
                _WINDOW,
                lambda _n: None,
                base_relations=None,
                tables={"dim_gadget"},
            )
        dim_only_names = [s.table_name for s in dim_only_specs]
        print(f"tables={{'dim_gadget'}} on the same config: {dim_only_names}")

    print("SUCCESS: selection scopes per-table compile, guards, notices, and economy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
