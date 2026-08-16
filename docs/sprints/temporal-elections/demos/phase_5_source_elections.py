#!/usr/bin/env python
"""
Demo: Source attach points
Sprint: temporal-elections
Phase: 5

Source-mode `render` / `date_parse` maps on declared tables and the event
log's `render` map, wired end-to-end through the plan-time business rule
`RenderKeyIsInstantColumn` and the windowed omitted-column posture.

Shows:
  1. A source export over a fixture emit (kind `patient`) with `render:
     {created_sim_time: date, last_mutation_sim_time: timestamptz}` and
     `date_parse: {prop__dob: "%Y-%m-%d"}` on the declared table, plus an
     event log rendered `event_sim_time: date` — profiled output types and
     values shown.
  2. Three refusals: a `render` key naming a payload column
     (`RenderKeyIsInstantColumn`), an event-log `render` key other than
     `event_sim_time` (`RenderKeyIsInstantColumn`), and a `render` key
     naming a column the table's `columns` selection omits
     (`SourceColumnUnresolved`).
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
from fabulexa_forge.errors import RenderKeyIsInstantColumn, SourceColumnUnresolved
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.source.engine import export_source
from fabulexa_forge.reader.emit import open_emit

_ANCHOR_ZONE = "UTC"
_ANCHOR_START_DATETIME = "2024-06-01T08:00:00+00:00"

# ns offsets from the anchor origin (2024-06-01T08:00:00Z).
_CREATED_NS = 0
_MUTATED_NS = 5_400_000_000_000  # +1.5h -> 2024-06-01T09:30:00Z
_SLICE_NS = 90_000_000_000_000  # +25h -> 2024-06-02T09:00:00Z

_PATIENT_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "record_index", "type": "BIGINT"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    {
        "name": "prop__dob",
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

_SOURCE_YAML = textwrap.dedent(
    """\
    mode: source
    source:
      tables:
        - name: patients
          kind: patient
          render: {created_sim_time: date, last_mutation_sim_time: timestamptz}
          date_parse: {prop__dob: "%Y-%m-%d"}
      events:
        name: patient_events
        sources:
          - kind: patient
        render: {event_sim_time: date}
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
    """Write the `patient` fixture emit: p001 created only, p002 created then
    its (tracked) status changes 1.5h later — the created_sim_time /
    last_mutation_sim_time split the `date` / `timestamptz` elections show
    apart.

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
            "rows": 3,
        },
    ]
    base_json: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": _SLICE_NS}],
        "tables": tables,
        "runtime": {"timezone": _ANCHOR_ZONE, "start_datetime": _ANCHOR_START_DATETIME},
    }
    (emit_dir / "base.json").write_text(json.dumps(base_json), encoding="utf-8")

    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_create_ddl("records__patient", _PATIENT_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.execute(
        'INSERT INTO "records__patient" VALUES'
        " (?, ?, ?, ?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "trunk",
            "p001",
            0,
            _CREATED_NS,
            True,
            None,
            _CREATED_NS,
            "open",
            "1985-02-20",
            "trunk",
            "p002",
            1,
            _CREATED_NS,
            True,
            None,
            _MUTATED_NS,
            "admitted",
            "1990-05-14",
        ],
    )
    conn.execute(
        'INSERT INTO "history" VALUES'
        " (?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?)",
        [
            "trunk",
            "patient",
            "p001",
            "status",
            _CREATED_NS,
            "open",
            "trunk",
            "patient",
            "p002",
            "status",
            _CREATED_NS,
            "open",
            "trunk",
            "patient",
            "p002",
            "status",
            _MUTATED_NS,
            "admitted",
        ],
    )
    conn.close()


def show_export(emit_dir: Path, out_dir: Path) -> None:
    """Run the full source export and print each output table's schema and
    rows, proving `render` / `date_parse` render correctly on both the
    declared table and the event log."""
    config_path = emit_dir / "config.yaml"
    config_path.write_text(_SOURCE_YAML, encoding="utf-8")
    config = load_export_config(config_path)

    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None, "the fixture sidecar carries a runtime anchor"
        counts = export_source(emit, config, out_dir, "duckdb", anchor, _discard_notice)
    print(f"Rows written: {counts}")
    print()

    con = duckdb.connect(str(out_dir), read_only=True)
    con.execute(f"SET TimeZone='{_ANCHOR_ZONE}'")
    for table in ("patients", "patient_events"):
        print(f"-- {table} --")
        schema = con.sql(f'DESCRIBE "{table}"').fetchall()
        for col_name, col_type, *_rest in schema:
            print(f"  {col_name}: {col_type}")
        # .arrow() (not .fetchall()) — the row-tuple TIMESTAMPTZ conversion path
        # needs an optional pytz dependency this package never requires.
        rows = con.sql(f'SELECT * FROM "{table}" ORDER BY 1, 2').arrow().to_pylist()
        for row in rows:
            print(f"  {row}")
        print()
    con.close()


def _load_source_config(config_dir: Path, name: str, yaml_text: str) -> ExportConfig:
    """Write `yaml_text` to `name`.yaml under `config_dir` and load it."""
    path = config_dir / f"{name}.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return load_export_config(path)


def show_refusal_render_payload_column(tmp_path: Path, emit_dir: Path) -> None:
    """A `render` key naming a payload (non-instant) column is refused at
    plan time, naming the table and the offending key."""
    config = _load_source_config(
        tmp_path,
        "refusal_payload",
        textwrap.dedent(
            """\
            mode: source
            source:
              tables:
                - name: patients
                  kind: patient
                  render: {prop__status: date}
            """
        ),
    )
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        try:
            export_source(
                emit,
                config,
                tmp_path / "out1.duckdb",
                "duckdb",
                anchor,
                _discard_notice,
            )
            raise _fail("expected RenderKeyIsInstantColumn, export succeeded")
        except RenderKeyIsInstantColumn as exc:
            print(f"  1. render key on a payload column refused: {exc}")


def show_refusal_events_render_key(tmp_path: Path, emit_dir: Path) -> None:
    """An event-log `render` key other than `event_sim_time` is refused at
    plan time (the log's one legal key, mode-definitional)."""
    config = _load_source_config(
        tmp_path,
        "refusal_events_key",
        textwrap.dedent(
            """\
            mode: source
            source:
              events:
                name: patient_events
                sources:
                  - kind: patient
                render: {occurred_at: date}
            """
        ),
    )
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        try:
            export_source(
                emit,
                config,
                tmp_path / "out2.duckdb",
                "duckdb",
                anchor,
                _discard_notice,
            )
            raise _fail("expected RenderKeyIsInstantColumn, export succeeded")
        except RenderKeyIsInstantColumn as exc:
            print(f"  2. event-log render key != event_sim_time refused: {exc}")


def show_refusal_render_columns_omitted(tmp_path: Path, emit_dir: Path) -> None:
    """A `render` key naming a column the table's `columns` selection omits
    is refused — the existing omitted-declaration posture `rename` already
    carries."""
    config = _load_source_config(
        tmp_path,
        "refusal_omitted",
        textwrap.dedent(
            """\
            mode: source
            source:
              tables:
                - name: patients
                  kind: patient
                  columns: [prop__status]
                  render: {created_sim_time: date}
            """
        ),
    )
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        try:
            export_source(
                emit,
                config,
                tmp_path / "out3.duckdb",
                "duckdb",
                anchor,
                _discard_notice,
            )
            raise _fail("expected SourceColumnUnresolved, export succeeded")
        except SourceColumnUnresolved as exc:
            print(f"  3. render key on a columns-omitted column refused: {exc}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = tmp_path / "main"
        build_fixture_emit(emit_dir)

        print("1. Source export over the fixture emit:")
        show_export(emit_dir, tmp_path / "out.duckdb")

        print("Refusals:")
        show_refusal_render_payload_column(tmp_path, emit_dir)
        show_refusal_events_render_key(tmp_path, emit_dir)
        show_refusal_render_columns_omitted(tmp_path, emit_dir)
        print()

    print(
        "SUCCESS: source-mode render/date_parse maps render correctly on the"
        " declared table and the event log, and every RenderKeyIsInstantColumn /"
        " omitted-column refusal fires as specified"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
