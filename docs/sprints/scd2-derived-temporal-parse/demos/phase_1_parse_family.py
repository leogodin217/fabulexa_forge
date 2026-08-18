#!/usr/bin/env python
"""
Demo: The instant-string parse family (widened formats, denoted type, shared
renderer)
Sprint: scd2-derived-temporal-parse
Phase: 1

`date_parse` generalizes from date-only to the instant-string family: the
closed directive vocabulary widens to include time directives (pairing,
uniqueness, and completeness rules), `date_parse_denoted_type` becomes the
single derivation authority, and `render_date_parse_expr` emits the
format-denoted type (DATE / TIME / naive TIMESTAMP) instead of always DATE.

Shows:
  1. A base export with three `date_parse` entries denoting TIMESTAMP, TIME,
     and DATE — output column types and values.
  2. A dimensional spec-form parse denoting TIMESTAMP.
  3. Four load-time refusals: an orphaned `%I`, a `%M` with no hour, a
     duplicated field (`%H` + `%I`/`%p`), a partial-date-plus-time format.
  4. The loud mismatch error naming table, column, and offending value.
"""

from __future__ import annotations

import json
import sys
import tempfile
import textwrap
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge._sql import render_date_parse_expr, validate_date_parse_format
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.config.models import (
    ColumnDecl,
    DateParseSpec,
    DerivedSpec,
    SourceDecl,
    TableDecl,
)
from fabulexa_forge.exporters.base.engine import export_base
from fabulexa_forge.exporters.dimensional.columns import build_date_parse_expr
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.reader.emit import open_emit

_ANCHOR_ZONE = "UTC"
_ANCHOR_START_DATETIME = "2024-01-01T00:00:00+00:00"

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
        "name": "prop__admitted_at",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__checkin_time",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__signup_date",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_BASE_YAML = textwrap.dedent(
    """\
    mode: base
    base:
      render:
        - table: records__patient
          date_parse:
            prop__admitted_at: "%Y-%m-%d %H:%M:%S"
            prop__checkin_time: "%H:%M"
            prop__signup_date: "%Y-%m-%d"
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
    """Write the `patient` fixture emit: one record whose payload columns
    hold a datetime string, a bare time string, and a date string.

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
            "rows": 1,
        },
    ]
    base_json: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": tables,
        "record_roles": {"patient": "dimension"},
        "runtime": {"timezone": _ANCHOR_ZONE, "start_datetime": _ANCHOR_START_DATETIME},
    }
    (emit_dir / "base.json").write_text(json.dumps(base_json), encoding="utf-8")

    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_create_ddl("records__patient", _PATIENT_COLUMNS))
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "p001",
            1001,
            0,
            True,
            0,
            0,
            "2024-01-15 14:30:00",
            "09:05",
            "2024-01-15",
        ],
    )
    conn.close()


def show_base_export(tmp_path: Path, emit_dir: Path) -> None:
    """Export the fixture through base mode's date_parse family and print
    the output columns' types and values."""
    config_path = tmp_path / "base.yaml"
    config_path.write_text(_BASE_YAML, encoding="utf-8")
    config = load_export_config(config_path)
    out_dir = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None, "the fixture sidecar carries a runtime anchor"
        export_base(emit, config, out_dir, "duckdb", anchor, _discard_notice)

    con = duckdb.connect(str(out_dir), read_only=True)
    schema = con.sql('DESCRIBE "patient"').fetchall()
    types_by_column = {col_name: col_type for col_name, col_type, *_rest in schema}
    for name in ("prop__admitted_at", "prop__checkin_time", "prop__signup_date"):
        print(f"  {name}: {types_by_column[name]}")
    row = con.sql(
        'SELECT prop__admitted_at, prop__checkin_time, prop__signup_date FROM "patient"'
    ).fetchone()
    con.close()
    print(f"  values: {row}")
    if types_by_column["prop__admitted_at"] != "TIMESTAMP":
        raise _fail("prop__admitted_at did not denote TIMESTAMP")
    if types_by_column["prop__checkin_time"] != "TIME":
        raise _fail("prop__checkin_time did not denote TIME")
    if types_by_column["prop__signup_date"] != "DATE":
        raise _fail("prop__signup_date did not denote DATE")
    print("  OK: TIMESTAMP / TIME / DATE denoted correctly")


def show_dimensional_spec_form() -> None:
    """A dimensional `derived: date_parse` spec-form column denoting
    TIMESTAMP, executed directly against an in-memory grain table."""
    col = ColumnDecl(
        name="admitted_at",
        derived=DerivedSpec(
            date_parse=DateParseSpec(
                **{"from": "prop__admitted_at", "format": "%Y-%m-%d %H:%M:%S"}
            )
        ),
    )
    table_decl = TableDecl(
        name="patient",
        role="fact",
        source=SourceDecl(grain="records", kind="patient"),
        key=["id"],
        columns=[ColumnDecl(name="id", **{"from": "record_id"}), col],
    )
    expr = build_date_parse_expr(col, table_decl)
    print(f"  expr: {expr}")

    conn = duckdb.connect(":memory:")
    conn.execute('CREATE TABLE "_grain" ("prop__admitted_at" VARCHAR)')
    conn.execute('INSERT INTO "_grain" VALUES (?)', ["2024-01-15 14:30:00"])
    col_type, value = conn.execute(
        f'SELECT typeof({col.name}), {col.name} FROM (SELECT {expr} FROM "_grain")'
    ).fetchone()
    conn.close()
    print(f"  type={col_type} value={value}")
    if col_type != "TIMESTAMP":
        raise _fail(f"expected TIMESTAMP, got {col_type}")
    print("  OK: spec-form parse denotes TIMESTAMP")


_REFUSAL_CASES: tuple[tuple[str, str], ...] = (
    ("orphaned %I (no %p)", "%I:%M"),
    ("%M with no hour", "%Y-%m-%d %M"),
    ("duplicated hour field (%H + %I/%p)", "%H %I %p"),
    ("partial date + time", "%m-%d %H:%M"),
)


def show_load_time_refusals() -> None:
    """Four family-rule violations, each refused at load time."""
    for label, fmt in _REFUSAL_CASES:
        try:
            validate_date_parse_format(fmt, "date_parse.format")
            raise _fail(f"expected ValueError for {label!r} ({fmt!r})")
        except ValueError as exc:
            print(f"  {label} ({fmt!r}): {exc}")


def show_mismatch_error() -> None:
    """A non-matching value fails loudly, naming table, column, and value."""
    expr = render_date_parse_expr(
        '"_grain"."prop__admitted_at"',
        "%Y-%m-%d %H:%M:%S",
        "admitted_at",
        "patient",
    )
    conn = duckdb.connect(":memory:")
    conn.execute('CREATE TABLE "_grain" ("prop__admitted_at" VARCHAR)')
    conn.execute('INSERT INTO "_grain" VALUES (?)', ["not-a-timestamp"])
    try:
        conn.execute(f'SELECT {expr} FROM "_grain"').fetchone()
        raise _fail("expected a mismatch error, query succeeded")
    except duckdb.Error as exc:
        message = str(exc)
        print(f"  {message}")
        for needle in ("patient", "prop__admitted_at", "not-a-timestamp"):
            if needle not in message:
                raise _fail(f"mismatch message missing {needle!r}: {message}")
        print("  OK: names table, column, and offending value")
    finally:
        conn.close()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = tmp_path / "main"
        build_fixture_emit(emit_dir)

        print("1. Base export: TIMESTAMP / TIME / DATE denotations:")
        show_base_export(tmp_path, emit_dir)
        print()

        print("2. Dimensional spec-form parse denoting TIMESTAMP:")
        show_dimensional_spec_form()
        print()

        print("3. Load-time refusals (pairing, uniqueness, completeness):")
        show_load_time_refusals()
        print()

        print("4. Loud mismatch error naming table, column, and value:")
        show_mismatch_error()
        print()

    print(
        "SUCCESS: the widened parse family denotes DATE/TIME/TIMESTAMP through"
        " one authority, refuses pairing/uniqueness/completeness violations at"
        " load time, and fails loudly on a runtime value mismatch"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
