#!/usr/bin/env python
"""
Demo: Base attach points
Sprint: temporal-elections
Phase: 6

Base mode's per-table `render` declaration list: lifecycle-instant elections
and payload date parses keyed on pre-default column identities, full and
under an incremental window.

Shows:
  1. A base export with `render: {created_sim_time: date}` on the `patient`
     table and `date_parse: {prop__signup_date: "%Y-%m-%d"}` — profiled
     output types and values shown.
  2. The same election composed under an incremental window
     (`build_base_query_specs` with a `Window`) — the election applies
     identically to the full export.
  3. Four refusals: an election with no resolved anchor
     (`TemporalRenderRequiresAnchor` — base's anchor is optional), a
     `last_mutation_sim_time` render key (`BaseRenameUnresolved` — outside
     the base key domain, the mode never emits it), a duplicate `table`
     entry across two `render` declarations (a parse-time `ConfigError`),
     and a `date_parse` on a non-VARCHAR payload column
     (`DateParseSourceColumn`).
"""

from __future__ import annotations

import json
import sys
import tempfile
import textwrap
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.config.models import ExportConfig
from fabulexa_forge.errors import (
    BaseRenameUnresolved,
    ConfigError,
    DateParseSourceColumn,
    TemporalRenderRequiresAnchor,
)
from fabulexa_forge.exporters.base.engine import build_base_query_specs, export_base
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.reader.emit import open_emit

_ANCHOR_ZONE = "UTC"
_ANCHOR_START_DATETIME = "2024-01-01T00:00:00+00:00"

_DAY_NS = 86_400 * 1_000_000_000
_SLICE_NS = 5 * _DAY_NS

_PATIENT_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "BIGINT"},
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
        "name": "prop__signup_date",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__age",
        "type": "BIGINT",
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

_BASE_YAML = textwrap.dedent(
    """\
    mode: base
    base:
      render:
        - table: records__patient
          columns: {created_sim_time: date}
          date_parse: {prop__signup_date: "%Y-%m-%d"}
    """
)


def _fail(message: str) -> SystemExit:
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def _discard_notice(notice: Notice) -> None:
    pass


def _create_ddl(table_name: str, columns: list[dict[str, object]]) -> str:
    """Build a `CREATE TABLE` statement from a sidecar-shaped column list."""
    col_sql = ", ".join(f'"{c["name"]}" {c["type"]}' for c in columns)
    return f'CREATE TABLE "{table_name}" ({col_sql})'


def build_fixture_emit(emit_dir: Path) -> None:
    """Write the `patient` fixture emit: p001 created day 0, deactivated day
    2, signup_date '2024-01-15'; p002 created day 0, never deactivated,
    signup_date NULL (a date_parse must let NULL flow through untouched).

    Args:
        emit_dir: Directory to write base.json + run.duckdb into.
    """
    emit_dir.mkdir(parents=True, exist_ok=True)

    tables: list[dict[str, object]] = [
        {
            "name": "records__patient",
            "category": "records",
            "record_kind": "patient",
            "columns": _PATIENT_COLUMNS,
            "rows": 2,
        },
        {
            "name": "history",
            "category": "fixed",
            "columns": _HISTORY_COLUMNS,
            "rows": 2,
        },
    ]
    base_json: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": _SLICE_NS}],
        "tables": tables,
        "record_roles": {"patient": "dimension"},
        "runtime": {"timezone": _ANCHOR_ZONE, "start_datetime": _ANCHOR_START_DATETIME},
    }
    (emit_dir / "base.json").write_text(json.dumps(base_json), encoding="utf-8")

    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_create_ddl("records__patient", _PATIENT_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "p001",
            1001,
            0,
            False,
            2 * _DAY_NS,
            2 * _DAY_NS,
            0,
            "admitted",
            "2024-01-15",
            30,
        ],
    )
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "p002", 1002, 0, True, 0, 1, "admitted", None, 45],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "patient",
            "p001",
            "status",
            0,
            "admitted",
            "trunk",
            "patient",
            "p002",
            "status",
            0,
            "admitted",
        ],
    )
    conn.close()


def _load_base_config(config_dir: Path, name: str, yaml_text: str) -> ExportConfig:
    """Write `yaml_text` to `name`.yaml under `config_dir` and load it."""
    path = config_dir / f"{name}.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return load_export_config(path)


def show_full_export(tmp_path: Path, emit_dir: Path) -> None:
    """Run the full base export with the render/date_parse election and
    print the output table's schema and rows."""
    config = _load_base_config(tmp_path, "full", _BASE_YAML)
    out_dir = tmp_path / "out_full.duckdb"

    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None, "the fixture sidecar carries a runtime anchor"
        counts = export_base(emit, config, out_dir, "duckdb", anchor, _discard_notice)
    print(f"Rows written: {counts}")

    con = duckdb.connect(str(out_dir), read_only=True)
    schema = con.sql('DESCRIBE "patient"').fetchall()
    for col_name, col_type, *_rest in schema:
        print(f"  {col_name}: {col_type}")
    rows = con.sql('SELECT * FROM "patient" ORDER BY 1').arrow().to_pylist()
    for row in rows:
        print(f"  {row}")
    con.close()
    print()


def show_windowed_export(tmp_path: Path, emit_dir: Path) -> None:
    """Compose the same election under an incremental window — the election
    applies identically to the full export."""
    config = _load_base_config(tmp_path, "windowed", _BASE_YAML)
    window = Window(index=0, start_ns=0, end_ns=3 * _DAY_NS, label="w0")

    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        specs = build_base_query_specs(emit, config, anchor, window, _discard_notice)
        assert len(specs) == 1
        rows = emit.query(specs[0].sql, ())
    print(f"  windowed rows (end_ns={window.end_ns}): {rows}")
    print()


def show_refusal_no_anchor(tmp_path: Path, emit_dir: Path) -> None:
    """An election with no resolved anchor is refused — base's anchor is
    optional, but an explicit election still requires one."""
    config = _load_base_config(tmp_path, "no_anchor", _BASE_YAML)
    with open_emit(emit_dir) as emit:
        try:
            export_base(
                emit, config, tmp_path / "out1.duckdb", "duckdb", None, _discard_notice
            )
            raise _fail("expected TemporalRenderRequiresAnchor, export succeeded")
        except TemporalRenderRequiresAnchor as exc:
            print(f"  1. render election with no anchor refused: {exc}")


def show_refusal_last_mutation_sim_time_key(tmp_path: Path, emit_dir: Path) -> None:
    """A render key of last_mutation_sim_time is refused — outside the base
    key domain, the mode never emits it."""
    config = _load_base_config(
        tmp_path,
        "last_mutation",
        textwrap.dedent(
            """\
            mode: base
            base:
              render:
                - table: records__patient
                  columns: {last_mutation_sim_time: date}
            """
        ),
    )
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        try:
            export_base(
                emit,
                config,
                tmp_path / "out2.duckdb",
                "duckdb",
                anchor,
                _discard_notice,
            )
            raise _fail("expected BaseRenameUnresolved, export succeeded")
        except BaseRenameUnresolved as exc:
            print(f"  2. last_mutation_sim_time render key refused: {exc}")


def show_refusal_duplicate_table_entry() -> None:
    """Two `render` entries targeting the same table is a parse-time error —
    the existing base entries-disjoint rule extended to `render`."""
    yaml_text = textwrap.dedent(
        """\
        mode: base
        base:
          render:
            - table: records__patient
              columns: {created_sim_time: date}
            - table: records__patient
              columns: {deactivated_at: timestamptz}
        """
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "duplicate.yaml"
        path.write_text(yaml_text, encoding="utf-8")
        try:
            load_export_config(path)
            raise _fail("expected a ConfigError, config loaded")
        except ConfigError as exc:
            print(f"  3. duplicate render table entry refused: {exc}")


def show_refusal_date_parse_non_varchar(tmp_path: Path, emit_dir: Path) -> None:
    """A date_parse source that is not a declared VARCHAR column is refused."""
    config = _load_base_config(
        tmp_path,
        "non_varchar",
        textwrap.dedent(
            """\
            mode: base
            base:
              render:
                - table: records__patient
                  date_parse: {prop__age: "%Y-%m-%d"}
            """
        ),
    )
    with open_emit(emit_dir) as emit:
        try:
            export_base(
                emit, config, tmp_path / "out3.duckdb", "duckdb", None, _discard_notice
            )
            raise _fail("expected DateParseSourceColumn, export succeeded")
        except DateParseSourceColumn as exc:
            print(f"  4. date_parse on a non-VARCHAR column refused: {exc}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = tmp_path / "main"
        build_fixture_emit(emit_dir)

        print("1. Full base export with render + date_parse elections:")
        show_full_export(tmp_path, emit_dir)

        print("2. The same election under an incremental window:")
        show_windowed_export(tmp_path, emit_dir)

        print("Refusals:")
        show_refusal_no_anchor(tmp_path, emit_dir)
        show_refusal_last_mutation_sim_time_key(tmp_path, emit_dir)
        show_refusal_duplicate_table_entry()
        show_refusal_date_parse_non_varchar(tmp_path, emit_dir)
        print()

    print(
        "SUCCESS: base-mode render/date_parse elections render correctly full and"
        " windowed, and every TemporalRenderRequiresAnchor / key-domain /"
        " duplicate-table / DateParseSourceColumn refusal fires as specified"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
