#!/usr/bin/env python
"""
Demo: KeyColumnsStable and the reserved table-name rule run at every load of
a dimensional config — the one-shot export, and `open_shaped_playback` alike
— never deferred to the first windowed `window()` ask.

Sprint: shaped-table-selection
Phase: 1
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
    ExportConfig,
    SourceDecl,
    TableDecl,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.engine import export_dimensional
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.playback.shaped import open_shaped_playback
from fabulexa_forge.reader.emit import open_emit

#: Columns for records__gadget: identity + structural, one tracked property
#: (prop__status) and one constant property (prop__name).
_GADGET_COLUMNS: list[dict[str, object]] = [
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
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

#: The fixed history table — always required (C3), empty here.
_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _discard_notice(notice: Notice) -> None:
    """A notice sink that discards every notice — this demo cares only
    about the refusals and the successful export."""


def _column_ddl(name: str, duckdb_type: str) -> str:
    """One column's DDL fragment."""
    return f'"{name}" {duckdb_type}'


def build_gadget_emit(emit_dir: Path) -> None:
    """Write a one-row records__gadget emit (+ the required empty history
    table) at the supported base_format_version.

    Args:
        emit_dir: The directory to receive run.duckdb + base.json.
    """
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(
        f"CREATE TABLE records__gadget ("
        f"{', '.join(_column_ddl(c['name'], c['type']) for c in _GADGET_COLUMNS)})"
    )
    conn.execute(
        f"CREATE TABLE history ("
        f"{', '.join(_column_ddl(c['name'], c['type']) for c in _HISTORY_COLUMNS)})"
    )
    conn.execute(
        "INSERT INTO records__gadget VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
        ["trunk", "g1", 0, True, 0, 0, "new", "Widget-A"],
    )
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
        "record_roles": {"gadget": "dimension"},
    }
    (emit_dir / "base.json").write_text(json.dumps(base_json))


def _from_col(name: str, source: str) -> ColumnDecl:
    """A `from:`-sourced ColumnDecl."""
    return ColumnDecl(name=name, **{"from": source})


def unstable_key_config() -> DimensionalConfig:
    """A type-1 dim keyed on `prop__status` — a tracked (mutable) property.

    KeyColumnsStable refuses this regardless of the 'snapshot' delivery
    class the type-1 dim would otherwise carry.
    """
    return DimensionalConfig(
        tables=[
            TableDecl(
                name="dim_gadget",
                role="dim",
                scd="type1",
                source=SourceDecl(grain="records", kind="gadget"),
                key=["status"],
                columns=[
                    _from_col("id", "record_id"),
                    _from_col("status", "prop__status"),
                ],
            )
        ]
    )


def reserved_name_config() -> DimensionalConfig:
    """A legally-keyed table named `_export_meta` — an incremental
    bookkeeping name, refused regardless of export mode (one-shot included).
    """
    return DimensionalConfig(
        tables=[
            TableDecl(
                name="_export_meta",
                role="dim",
                scd="type1",
                source=SourceDecl(grain="records", kind="gadget"),
                key=["id"],
                columns=[_from_col("id", "record_id"), _from_col("name", "prop__name")],
            )
        ]
    )


def legal_config() -> DimensionalConfig:
    """The same shape, keyed on the stable `record_id` and legally named."""
    return DimensionalConfig(
        tables=[
            TableDecl(
                name="dim_gadget",
                role="dim",
                scd="type1",
                source=SourceDecl(grain="records", kind="gadget"),
                key=["id"],
                columns=[_from_col("id", "record_id"), _from_col("name", "prop__name")],
            )
        ]
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp)
        build_gadget_emit(emit_dir)

        # (a) An unstable key refuses — the one-shot export path.
        with open_emit(emit_dir) as emit:
            unstable_config = ExportConfig(
                mode="dimensional", dimensional=unstable_key_config()
            )
            try:
                export_dimensional(
                    emit,
                    unstable_config,
                    emit_dir / "out.duckdb",
                    "duckdb",
                    None,
                    _discard_notice,
                    None,
                )
            except ExportError as exc:
                print(f"KeyColumnsStable refused (one-shot export): {exc}")
            else:
                print("FAILURE: unstable key was not refused")
                return 1

        # (b) A reserved table name refuses — the one-shot export path.
        with open_emit(emit_dir) as emit:
            reserved_config = ExportConfig(
                mode="dimensional", dimensional=reserved_name_config()
            )
            try:
                export_dimensional(
                    emit,
                    reserved_config,
                    emit_dir / "out2.duckdb",
                    "duckdb",
                    None,
                    _discard_notice,
                    None,
                )
            except ExportError as exc:
                print(f"ReservedTableName refused (one-shot export): {exc}")
            else:
                print("FAILURE: reserved table name was not refused")
                return 1

        # (c) A stable key + legal name exports and opens a shaped head; a
        # window() ask over it raises nothing static.
        legal = ExportConfig(mode="dimensional", dimensional=legal_config())
        with open_emit(emit_dir) as emit:
            report = export_dimensional(
                emit,
                legal,
                emit_dir / "out3.duckdb",
                "duckdb",
                None,
                _discard_notice,
                None,
            )
            print(f"Legal config exported: {[t.name for t in report.tables]}")

        with open_emit(emit_dir) as emit:
            head = open_shaped_playback(emit, legal, None, _discard_notice)
            tables = head.window(0, 100)
            print(f"head.window(0, 100) delivered: {[t.name for t in tables]}")

    print(
        "SUCCESS: KeyColumnsStable and ReservedTableName are always-on load-time rules"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
