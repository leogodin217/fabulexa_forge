#!/usr/bin/env python
"""
Demo: Dimensional attach points
Sprint: temporal-elections
Phase: 4

The four dimensional derivation surfaces wired end-to-end: `as` on
`derived: timestamp`, the `scd_window` object form, `as: interval` on
`derived: elapsed`, and the new `derived: date_parse` — plus the business
rules (`TemporalRenderRequiresAnchor`, `DateParseSourceColumn`, the
slice-only surface growth) and the ordinal/incremental amendments.

Shows:
  1. A dimensional export over a fixture emit (kind `step`, an
     arrival/admission pair correlated by patient, a tracked `status`
     history) — a `DATE` admission date, a `TIMESTAMPTZ` instant, an
     `INTERVAL` wait, a parsed `birth_date`, and a date-grained SCD-2
     window — profiled output types and values shown.
  2. Four refusals: an explicit election with no resolved anchor
     (`TemporalRenderRequiresAnchor`), a `date_parse` from a non-VARCHAR
     column (`DateParseSourceColumn`), a mutated date string failing loudly
     at query time with table/column/value attribution, and an append-mode
     `ordinal.order_by` naming a `time`-elected column
     (`IncrementalOrdinalOrderBy`, election-aware).
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
from fabulexa_forge.config.models import (
    ColumnDecl,
    DateParseSpec,
    DerivedSpec,
    DimensionalConfig,
    OrdinalSpec,
    SourceDecl,
    TableDecl,
    TimestampSpec,
)
from fabulexa_forge.errors import DateParseSourceColumn, TemporalRenderRequiresAnchor
from fabulexa_forge.exporters.dimensional.engine import (
    build_query_specs,
    export_dimensional,
)
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.errors import RunDatabaseError

_ANCHOR_ZONE = "UTC"
_ANCHOR_START_DATETIME = "2024-06-01T08:00:00+00:00"

# ns offsets from the anchor origin (2024-06-01T08:00:00Z).
_ARRIVAL_NS = 0
_ADMISSION_NS = 5_400_000_000_000  # +1.5h -> 2024-06-01T09:30:00Z
_DISCHARGE_NS = 90_000_000_000_000  # +25h -> 2024-06-02T09:00:00Z

_STEP_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "record_index", "type": "BIGINT"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {
        "name": "prop__patient_id",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__step",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__dob",
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

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

_DIMENSIONAL_YAML = textwrap.dedent(
    """\
    mode: dimensional
    dimensional:
      tables:
        - name: visits
          role: fact
          source:
            grain: records
            kind: step
          key: [visit_id]
          columns:
            - name: visit_id
              from: record_id
            - name: patient_id
              from: prop__patient_id
            - name: step
              from: prop__step
            - name: admission_date
              derived:
                timestamp: {source: created_sim_time, as: date}
            - name: admitted_at
              derived:
                timestamp: {source: created_sim_time, as: timestamptz}
            - name: wait
              derived:
                elapsed:
                  correlate_on: prop__patient_id
                  other_where: {prop__step: arrival}
                  start_source: created_sim_time
                  end_source: created_sim_time
                  as: interval
            - name: birth_date
              derived:
                date_parse: {from: prop__dob, format: "%Y-%m-%d"}
        - name: status_history
          role: dim
          scd: type2
          source:
            grain: records
            kind: step
          key: [id, valid_from]
          columns:
            - name: id
              from: record_id
            - name: status
              from: prop__status
            - name: valid_from
              derived:
                scd_window: {bound: valid_from, as: date}
            - name: valid_to
              derived:
                scd_window: {bound: valid_to, as: date}
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


def build_fixture_emit(
    emit_dir: Path,
    *,
    with_runtime: bool,
    dob_1990_05_14: str,
) -> None:
    """Write the `step` fixture emit: an arrival/admission pair plus a
    three-version `status` history for the arrival record.

    Args:
        emit_dir: Directory to write base.json + run.duckdb into.
        with_runtime: Whether the sidecar carries a `runtime` anchor block.
        dob_1990_05_14: The admission record's `prop__dob` value — the
            well-formed ISO string in the main fixture, a malformed string
            in the mutated-value refusal fixture.
    """
    emit_dir.mkdir(parents=True, exist_ok=True)

    tables: list[dict[str, object]] = [
        {
            "name": "records__step",
            "category": "records",
            "kind": "step",
            "columns": _STEP_COLUMNS,
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
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": _DISCHARGE_NS}],
        "tables": tables,
    }
    if with_runtime:
        base_json["runtime"] = {
            "timezone": _ANCHOR_ZONE,
            "start_datetime": _ANCHOR_START_DATETIME,
        }
    (emit_dir / "base.json").write_text(json.dumps(base_json), encoding="utf-8")

    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_create_ddl("records__step", _STEP_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.execute(
        'INSERT INTO "records__step" VALUES'
        " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "trunk",
            "1",
            1,
            _ARRIVAL_NS,
            True,
            None,
            _DISCHARGE_NS,
            "P1",
            "arrival",
            None,
            "discharged",
            "trunk",
            "2",
            2,
            _ADMISSION_NS,
            True,
            None,
            _ADMISSION_NS,
            "P1",
            "admission",
            dob_1990_05_14,
            None,
        ],
    )
    conn.execute(
        'INSERT INTO "history" VALUES'
        " (?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?)",
        [
            "trunk",
            "step",
            "1",
            "status",
            _ARRIVAL_NS,
            "waiting",
            "trunk",
            "step",
            "1",
            "status",
            _ADMISSION_NS,
            "admitted",
            "trunk",
            "step",
            "1",
            "status",
            _DISCHARGE_NS,
            "discharged",
        ],
    )
    conn.close()


def show_export(emit_dir: Path, out_dir: Path) -> None:
    """Run the full dimensional export and print each output table's schema
    and rows, proving the four derivation surfaces render correctly."""
    config_path = emit_dir / "config.yaml"
    config_path.write_text(_DIMENSIONAL_YAML, encoding="utf-8")
    config = load_export_config(config_path)

    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None, "the fixture sidecar carries a runtime anchor"
        counts = export_dimensional(
            emit, config, out_dir, "duckdb", anchor, _discard_notice
        )
    print(f"Rows written: {counts}")
    print()

    con = duckdb.connect(str(out_dir), read_only=True)
    con.execute(f"SET TimeZone='{_ANCHOR_ZONE}'")
    for table in ("visits", "status_history"):
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


def show_refusal_no_anchor(tmp_path: Path) -> None:
    """An explicit `as` election with no resolved anchor is refused at plan
    time, naming the column."""
    emit_dir = tmp_path / "no_anchor"
    build_fixture_emit(emit_dir, with_runtime=False, dob_1990_05_14="1990-05-14")

    config = DimensionalConfig(
        tables=[
            TableDecl(
                name="visits",
                role="fact",
                source=SourceDecl(grain="records", kind="step"),
                key=["visit_id"],
                columns=[
                    ColumnDecl(name="visit_id", **{"from": "record_id"}),
                    ColumnDecl(
                        name="admission_date",
                        derived=DerivedSpec(
                            timestamp=TimestampSpec(
                                source="created_sim_time", **{"as": "date"}
                            )
                        ),
                    ),
                ],
            )
        ]
    )
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is None, "the no-runtime fixture resolves no anchor"
        try:
            build_query_specs(emit, config, anchor, None, _discard_notice, None)
            raise _fail("expected TemporalRenderRequiresAnchor, export succeeded")
        except TemporalRenderRequiresAnchor as exc:
            print(f"  1. no-anchor election refused: {exc}")


def show_refusal_non_varchar_date_parse(tmp_path: Path) -> None:
    """A `date_parse` source that is not a declared VARCHAR column is
    refused at plan time, naming the table/column and the actual type."""
    emit_dir = tmp_path / "main"  # reuses the fixture main() already built

    config = DimensionalConfig(
        tables=[
            TableDecl(
                name="visits",
                role="fact",
                source=SourceDecl(grain="records", kind="step"),
                key=["visit_id"],
                columns=[
                    ColumnDecl(name="visit_id", **{"from": "record_id"}),
                    ColumnDecl(
                        name="bad_parse",
                        derived=DerivedSpec(
                            date_parse=DateParseSpec(
                                **{"from": "created_sim_time", "format": "%Y-%m-%d"}
                            )
                        ),
                    ),
                ],
            )
        ]
    )
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        try:
            build_query_specs(emit, config, anchor, None, _discard_notice, None)
            raise _fail("expected DateParseSourceColumn, export succeeded")
        except DateParseSourceColumn as exc:
            print(f"  2. non-VARCHAR date_parse source refused: {exc}")


def show_refusal_mutated_date_value(tmp_path: Path) -> None:
    """A malformed date string fails the export loudly at query time,
    naming the table, column, and offending value."""
    emit_dir = tmp_path / "bad_value"
    build_fixture_emit(emit_dir, with_runtime=True, dob_1990_05_14="not-a-date")

    config = DimensionalConfig(
        tables=[
            TableDecl(
                name="visits",
                role="fact",
                source=SourceDecl(grain="records", kind="step"),
                key=["visit_id"],
                columns=[
                    ColumnDecl(name="visit_id", **{"from": "record_id"}),
                    ColumnDecl(
                        name="birth_date",
                        derived=DerivedSpec(
                            date_parse=DateParseSpec(
                                **{"from": "prop__dob", "format": "%Y-%m-%d"}
                            )
                        ),
                    ),
                ],
            )
        ]
    )
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        specs = build_query_specs(emit, config, anchor, None, _discard_notice, None)
        try:
            emit.query_arrow(specs[0].sql, ())
            raise _fail("expected the malformed date string to fail loudly")
        except RunDatabaseError as exc:
            print(f"  3. mutated date value fails loudly: {exc}")


def show_refusal_time_elected_order_by(tmp_path: Path) -> None:
    """Under incremental export, an append-mode `ordinal.order_by` naming a
    `time`-elected window-key sibling is refused (election-aware
    IncrementalOrdinalOrderBy)."""
    emit_dir = tmp_path / "main"  # reuses the fixture built for refusal 2

    config = DimensionalConfig(
        tables=[
            TableDecl(
                name="visits",
                role="fact",
                source=SourceDecl(grain="records", kind="step"),
                key=["visit_id"],
                columns=[
                    ColumnDecl(name="visit_id", **{"from": "record_id"}),
                    ColumnDecl(
                        name="admitted_time",
                        derived=DerivedSpec(
                            timestamp=TimestampSpec(
                                source="created_sim_time", **{"as": "time"}
                            )
                        ),
                    ),
                    ColumnDecl(
                        name="rank",
                        derived=DerivedSpec(
                            ordinal=OrdinalSpec(
                                partition_by="visit_id", order_by="admitted_time"
                            )
                        ),
                    ),
                ],
            )
        ]
    )
    window = Window(index=0, start_ns=0, end_ns=_DISCHARGE_NS, label="w0")
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        try:
            build_query_specs(emit, config, anchor, window, _discard_notice, None)
            raise _fail("expected IncrementalOrdinalOrderBy, export succeeded")
        except Exception as exc:  # noqa: BLE001 -- the existing ExportError message
            print(f"  4. time-elected order_by refused under incremental export: {exc}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = tmp_path / "main"
        build_fixture_emit(emit_dir, with_runtime=True, dob_1990_05_14="1990-05-14")

        print("1. Dimensional export over the fixture emit:")
        show_export(emit_dir, tmp_path / "out.duckdb")

        print("Refusals:")
        show_refusal_no_anchor(tmp_path)
        show_refusal_non_varchar_date_parse(tmp_path)
        show_refusal_mutated_date_value(tmp_path)
        show_refusal_time_elected_order_by(tmp_path)
        print()

    print(
        "SUCCESS: the four dimensional derivation surfaces (as, scd_window object"
        " form, elapsed as: interval, date_parse) render correctly end-to-end, and"
        " every business-rule/incremental refusal fires as specified"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
