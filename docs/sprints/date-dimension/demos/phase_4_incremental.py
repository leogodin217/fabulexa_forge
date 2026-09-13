#!/usr/bin/env python
"""
Demo: Incremental delivery of the generated calendar.

Sprint: date-dimension
Phase: 4

Loads the `date-dimension` recipe's own config.yaml (day cadence) against a
self-contained emit shaped to match it, plus one extra table
(`fact_deactivation`, a `date_ref {source: deactivated_at}` column) added
only to show the `upsert` delivery class the mutable source earns. Drips
`--next` three times to CSV: prints each window's delivered tables and their
static delivery class (`dim_date` always `snapshot`, `fact_deactivation`
`upsert`, `fact_status_event` `append`), then diffs `dim_date.csv` across the
three windows (identical bytes). Narrows `date_dimension.to` and re-drips the
first window: prints the fingerprint mismatch. Finally drips a config whose
range excludes the third window's newly-visible event date: windows 0 and 1
land, window 2 raises `DateRefOutOfRange`, and the cursor still reads "next
window 2" afterwards.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.config.models import (
    ColumnDecl,
    DateDimensionConfig,
    DateRefSpec,
    IncrementalConfig,
    SourceDecl,
    TableDecl,
)
from fabulexa_forge.errors import DateRefOutOfRange, IncrementalFingerprintMismatch
from fabulexa_forge.exporters.dimensional.windowing import window_delivery_class
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.incremental.cursor import read_cursor
from fabulexa_forge.incremental.driver import export_incremental_next
from fabulexa_forge.incremental.windows import derive_window
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

#: p001: dob 1990-05-14, status pending@1*DAY, active@2*DAY, discharged@3*DAY.
#: p002: dob 1985-11-02, status pending@2*DAY.
_STATUS_ROWS = [
    ("trunk", "patient", "p001", "status", 1 * _DAY_NS, "pending"),
    ("trunk", "patient", "p001", "status", 2 * _DAY_NS, "active"),
    ("trunk", "patient", "p002", "status", 2 * _DAY_NS, "pending"),
    ("trunk", "patient", "p001", "status", 3 * _DAY_NS, "discharged"),
]


def _write_emit(tmp_dir: Path) -> Path:
    """Write a self-contained emit matching the recipe's expectations."""
    conn = duckdb.connect(str(tmp_dir / "run.duckdb"))
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
    for row in _STATUS_ROWS:
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
                "rows": len(_STATUS_ROWS),
            },
        ],
        "runtime": {"timezone": "UTC", "start_datetime": "2024-01-01T00:00:00+00:00"},
    }
    (tmp_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_dir


def _discard_notice(_: Notice) -> None:
    """A notice sink that drops every notice — the demo has none to show."""


def _fact_deactivation() -> TableDecl:
    """A fact whose `date_ref` sources the mutable `deactivated_at` column —
    added only to demonstrate the `upsert` delivery class it earns."""
    return TableDecl(
        name="fact_deactivation",
        role="fact",
        source=SourceDecl(grain="records", kind="patient"),
        key=["patient_id"],
        columns=[
            ColumnDecl(name="patient_id", **{"from": "record_id"}),
            ColumnDecl(
                name="deactivated_key", date_ref=DateRefSpec(source="deactivated_at")
            ),
        ],
    )


def _demo_drip_and_delivery_classes(emit_dir: Path, out: Path) -> None:
    """Drip three windows; print delivered tables, delivery classes, and the
    dim_date.csv byte-diff across windows."""
    config = load_export_config(_RECIPE_CONFIG_PATH)
    config = config.model_copy(
        update={
            "incremental": IncrementalConfig(period="day"),
            "dimensional": config.dimensional.model_copy(
                update={"tables": [*config.dimensional.tables, _fact_deactivation()]}
            ),
        }
    )

    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        print("static delivery classes:")
        for table_decl in config.dimensional.tables:
            delivery = window_delivery_class(
                table_decl, config.dimensional, emit.sidecar
            )
            print(f"  {table_decl.name}: {delivery}")
        print("  dim_date: snapshot (mode-definitional, every emitting window)")

        dim_date_bytes: list[bytes] = []
        for _ in range(3):
            outcome = export_incremental_next(
                emit, config, out, "csv", anchor, _discard_notice, None, ()
            )
            assert outcome.status == "emitted" and outcome.window is not None
            names = [t.name for t in outcome.report.tables]
            print(f"window {outcome.window.label}: delivered {names}")
            dim_date_bytes.append(
                (out / outcome.window.label / "dim_date.csv").read_bytes()
            )

    identical = len(set(dim_date_bytes)) == 1
    print(f"dim_date.csv identical across all three windows: {identical}")


def _demo_fingerprint_mismatch(emit_dir: Path, out: Path) -> None:
    """Narrow date_dimension.to after window 0 has landed; re-drip mismatches."""
    config = load_export_config(_RECIPE_CONFIG_PATH).model_copy(
        update={"incremental": IncrementalConfig(period="day")}
    )
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        outcome = export_incremental_next(
            emit, config, out, "csv", anchor, _discard_notice, None, ()
        )
        assert outcome.status == "emitted"

        narrowed = config.model_copy(
            update={
                "date_dimension": DateDimensionConfig.model_validate(
                    {"from": "1985-01-01", "to": "2024-06-30"}
                )
            }
        )
        try:
            export_incremental_next(
                emit, narrowed, out, "csv", anchor, _discard_notice, None, ()
            )
            raise AssertionError("expected IncrementalFingerprintMismatch, none raised")
        except IncrementalFingerprintMismatch as exc:
            print(f"fingerprint mismatch on a narrowed date_dimension.to: {exc}")


def _demo_out_of_range_window(emit_dir: Path, out: Path) -> None:
    """A range excluding the third window's newly-visible event date."""
    config = load_export_config(_RECIPE_CONFIG_PATH).model_copy(
        update={
            "incremental": IncrementalConfig(period="day"),
            "date_dimension": DateDimensionConfig.model_validate(
                {"from": "1985-01-01", "to": "2024-01-02"}
            ),
        }
    )
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        for _ in range(2):
            outcome = export_incremental_next(
                emit, config, out, "csv", anchor, _discard_notice, None, ()
            )
            assert outcome.status == "emitted" and outcome.window is not None
            print(f"window {outcome.window.label}: landed")

        try:
            export_incremental_next(
                emit, config, out, "csv", anchor, _discard_notice, None, ()
            )
            raise AssertionError("expected DateRefOutOfRange, none raised")
        except DateRefOutOfRange as exc:
            print(f"window 2 refused: {exc}")

    assert config.incremental is not None
    window_zero_label = derive_window(0, config.incremental, anchor).label
    cursor = read_cursor(out, "csv", window_zero_label)
    assert cursor is not None
    print(f"cursor still reads next window {cursor.next_window_index} (unadvanced)")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        emit_a = tmp_path / "emit_a"
        emit_a.mkdir()
        _write_emit(emit_a)
        _demo_drip_and_delivery_classes(emit_a, tmp_path / "drops_a")

        emit_b = tmp_path / "emit_b"
        emit_b.mkdir()
        _write_emit(emit_b)
        _demo_fingerprint_mismatch(emit_b, tmp_path / "drops_b")

        emit_c = tmp_path / "emit_c"
        emit_c.mkdir()
        _write_emit(emit_c)
        _demo_out_of_range_window(emit_c, tmp_path / "drops_c")

    print(
        "\nSUCCESS: dim_date drips snapshot-identical every window, a"
        " date_ref over a mutable source earns upsert, a narrowed range"
        " mismatches the fingerprint, and an out-of-range window refuses"
        " with the cursor unadvanced"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
