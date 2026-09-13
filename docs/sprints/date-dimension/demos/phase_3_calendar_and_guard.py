#!/usr/bin/env python
"""
Demo: The generated calendar (`dim_date`), the `date_ref` range guard, and
the full dimensional export wired together end to end.

Sprint: date-dimension
Phase: 3

Loads the `date-dimension` recipe's own config.yaml — no config duplicated
here — against a self-contained emit shaped to match it (two patients with
`prop__dob` date strings, four `status` history events). Exports it to
DuckDB and to CSV: prints `dim_date`'s first and last rows and its row
count, a `fact_status_event JOIN dim_date` grouped by `day_name`, the
output-table order from the report (declared tables, then `dim_date`), and
the CSV file list showing `dim_date.csv`. Then narrows the declared range to
`2024-01-01..2024-01-31` against the same emit — a 1985 birth date now lies
outside it — and prints the `DateRefOutOfRange` message plus proof the
output directory holds no table file.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.config.models import DateDimensionConfig
from fabulexa_forge.errors import DateRefOutOfRange
from fabulexa_forge.exporters.dimensional.engine import export_dimensional
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.reader.emit import open_emit

_DAY_NS = 86_400 * 1_000_000_000

_RECIPE_CONFIG_PATH = (
    Path(__file__).resolve().parents[4]
    / "examples"
    / "recipes"
    / "date-dimension"
    / "config.yaml"
)

_PATIENT_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
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


def _write_emit(tmp_dir: Path) -> Path:
    """Write a self-contained emit matching the recipe's expectations.

    p001: dob 1990-05-14, status changes at 1/2/3*DAY. p002: dob
    1985-11-02, one status change at 2*DAY.

    Args:
        tmp_dir: Directory to write run.duckdb + base.json into.

    Returns:
        tmp_dir (the emit directory).
    """
    db_path = tmp_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(
        'CREATE TABLE "records__patient" ('
        '"fork_path" VARCHAR, "record_id" VARCHAR, "created_sim_time" BIGINT,'
        ' "active" BOOLEAN, "deactivated_at" BIGINT,'
        ' "last_mutation_sim_time" BIGINT, "record_index" BIGINT,'
        ' "prop__dob" VARCHAR)'
    )
    conn.execute(
        'CREATE TABLE "history" ("fork_path" VARCHAR, "kind" VARCHAR,'
        ' "record_id" VARCHAR, "property" VARCHAR, "sim_time" BIGINT,'
        ' "value" VARCHAR)'
    )
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "p001", 0, True, None, 0, 0, "1990-05-14"],
    )
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "p002", 0, True, None, 0, 1, "1985-11-02"],
    )
    status_rows = [
        ("trunk", "patient", "p001", "status", 1 * _DAY_NS, "pending"),
        ("trunk", "patient", "p001", "status", 2 * _DAY_NS, "active"),
        ("trunk", "patient", "p002", "status", 2 * _DAY_NS, "pending"),
        ("trunk", "patient", "p001", "status", 3 * _DAY_NS, "discharged"),
    ]
    for row in status_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "surface": "published",
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 10 * _DAY_NS}],
        "tables": [
            {
                "name": "records__patient",
                "category": "records",
                "columns": _PATIENT_COLUMNS,
                "rows": 2,
                "record_kind": "patient",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": len(status_rows),
            },
        ],
        "runtime": {
            "timezone": "UTC",
            "start_datetime": "2024-01-01T00:00:00+00:00",
        },
    }
    (tmp_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_dir


def _discard_notice(_: Notice) -> None:
    """A notice sink that drops every notice — the demo has none to show."""


def _demo_full_export(emit_dir: Path, out_root: Path) -> None:
    """Export the recipe's own config to DuckDB and CSV; print the proof."""
    config = load_export_config(_RECIPE_CONFIG_PATH)

    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)

        duckdb_path = out_root / "export.duckdb"
        report = export_dimensional(
            emit, config, duckdb_path, "duckdb", anchor, _discard_notice, None, ()
        )
        print("output-table order:", [t.name for t in report.tables])

        conn = duckdb.connect(str(duckdb_path), read_only=True)
        first_row = conn.execute(
            'SELECT * FROM "dim_date" ORDER BY "date_key" ASC LIMIT 1'
        ).fetchone()
        last_row = conn.execute(
            'SELECT * FROM "dim_date" ORDER BY "date_key" DESC LIMIT 1'
        ).fetchone()
        row_count = conn.execute('SELECT COUNT(*) FROM "dim_date"').fetchone()[0]
        print(f"dim_date row_count: {row_count}")
        print(f"dim_date first row: {first_row}")
        print(f"dim_date last row: {last_row}")

        grouped = conn.execute(
            'SELECT d."day_name", COUNT(*) FROM "fact_status_event" f'
            ' JOIN "dim_date" d ON f."event_date_key" = d."date_key"'
            ' GROUP BY d."day_name" ORDER BY d."day_name"'
        ).fetchall()
        print("fact_status_event JOIN dim_date, grouped by day_name:", grouped)
        conn.close()

        csv_dir = out_root / "csv_out"
        csv_dir.mkdir()
        export_dimensional(
            emit, config, csv_dir, "csv", anchor, _discard_notice, None, ()
        )
        print("CSV files:", sorted(p.name for p in csv_dir.iterdir()))


def _demo_range_guard_refusal(emit_dir: Path, out_root: Path) -> None:
    """Narrow the declared range so a 1985 birth date is out of range."""
    config = load_export_config(_RECIPE_CONFIG_PATH)
    narrowed = config.model_copy(
        update={
            "date_dimension": DateDimensionConfig.model_validate(
                {"from": "2024-01-01", "to": "2024-01-31"}
            )
        }
    )

    out_dir = out_root / "refused"
    out_dir.mkdir()
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        try:
            export_dimensional(
                emit, narrowed, out_dir, "csv", anchor, _discard_notice, None, ()
            )
            raise AssertionError("expected DateRefOutOfRange, none raised")
        except DateRefOutOfRange as exc:
            print(f"refused: {exc}")

    assert list(out_dir.iterdir()) == [], "output directory holds a stray file"
    print("output directory holds no table file after the refusal")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_root = tmp_path / "emit"
        emit_root.mkdir()
        emit_dir = _write_emit(emit_root)

        full_out = tmp_path / "full"
        full_out.mkdir()
        _demo_full_export(emit_dir, full_out)

        refusal_out = tmp_path / "refusal"
        refusal_out.mkdir()
        _demo_range_guard_refusal(emit_dir, refusal_out)

    print(
        "\nSUCCESS: dim_date materializes after the declared tables, the"
        " fact/dim date_ref keys join it cleanly, and a narrowed range"
        " refuses before any write"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
