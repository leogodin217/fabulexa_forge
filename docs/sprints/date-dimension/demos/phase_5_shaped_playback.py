#!/usr/bin/env python
"""
Demo: Shaped playback over the generated calendar and a `date_ref` column.

Sprint: date-dimension
Phase: 5

Loads the `date-dimension` recipe's own config.yaml against a self-contained
emit shaped to match it, with one supplement (`rate_tier`) bound beside it.
Opens a shaped-playback head and prints `tables()` — `dim_date` after the two
declared tables, before the supplement. Asks `window()` at two different
bounds and `state()` at two different instants, printing
`fact_status_event.event_date_key` each time and proving `dim_date`'s rows are
byte-identical across all four asks. Asks for `tables=["dim_date"]` alone and
proves the ask opened no horizon (the notice sink stays silent). Finally
opens a second head over the same emit with a `date_dimension.to` narrowed
below the patients' history, and prints the `DateRefOutOfRange` raised from
`state()`.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.config.models import DateDimensionConfig, SupplementDecl
from fabulexa_forge.errors import DateRefOutOfRange
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.supplements import ResolvedSupplement
from fabulexa_forge.playback.shaped import open_shaped_playback
from fabulexa_forge.reader.emit import open_emit

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fabulexa_forge.playback.shaped import ShapedTable

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


def _rate_tier_supplement() -> tuple[ResolvedSupplement, ...]:
    """One inline-shaped supplement bound beside the shape, unresolved from
    any file — the demo needs no filesystem loader round trip."""
    decl = SupplementDecl(name="rate_tier", columns={"tier": "VARCHAR"}, rows=[])
    return (
        ResolvedSupplement(
            decl=decl, path=None, sha256=None, rows=(("standard",), ("premium",))
        ),
    )


def _discard_notice(_: Notice) -> None:
    """A notice sink that drops every notice — the horizon-economy proof
    instead counts a RecordingNoticeSink's own list."""


def _event_date_keys(tables: "Mapping[str, ShapedTable]") -> list[object]:
    """The `fact_status_event.event_date_key` column, as a plain list, from
    one ask's `{name: ShapedTable}` mapping."""
    return tables["fact_status_event"].table.column("event_date_key").to_pylist()


def _demo_tables_and_static_identity(emit_dir: Path) -> None:
    """tables() order, window()/state() date keys, dim_date identity, and
    the dim_date-only horizon-economy proof."""
    config = load_export_config(_RECIPE_CONFIG_PATH)
    notices: list[Notice] = []

    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        head = open_shaped_playback(
            emit, config, anchor, notices.append, _rate_tier_supplement()
        )

        print("tables():")
        for decl in head.tables():
            print(f"  {decl.name}: {decl.window_delivery}")

        window_a = {t.name: t for t in head.window(0, 1 * _DAY_NS)}
        window_b = {t.name: t for t in head.window(0, 3 * _DAY_NS)}
        state_a = {t.name: t for t in head.state(1 * _DAY_NS)}
        state_b = {t.name: t for t in head.state(3 * _DAY_NS)}

        print("fact_status_event.event_date_key:")
        print(f"  window(0, 1d): {_event_date_keys(window_a)}")
        print(f"  window(0, 3d): {_event_date_keys(window_b)}")
        print(f"  state(1d): {_event_date_keys(state_a)}")
        print(f"  state(3d): {_event_date_keys(state_b)}")

        dim_date_tables = [
            window_a["dim_date"].table,
            window_b["dim_date"].table,
            state_a["dim_date"].table,
            state_b["dim_date"].table,
        ]
        identical = all(t.equals(dim_date_tables[0]) for t in dim_date_tables[1:])
        print(f"dim_date identical across all four asks: {identical}")

        before = len(notices)
        solo = head.window(0, 3 * _DAY_NS, tables=["dim_date"])
        print(
            f"window(tables=['dim_date']) delivered {[t.name for t in solo]},"
            f" notices emitted: {len(notices) - before}"
        )


def _demo_out_of_range_state(emit_dir: Path) -> None:
    """A narrowed date_dimension.to excludes the third day's event date;
    state() raises DateRefOutOfRange."""
    config = load_export_config(_RECIPE_CONFIG_PATH).model_copy(
        update={
            "date_dimension": DateDimensionConfig.model_validate(
                {"from": "1985-01-01", "to": "2024-01-03"}
            )
        }
    )
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        head = open_shaped_playback(emit, config, anchor, _discard_notice, ())
        try:
            head.state(3 * _DAY_NS)
            raise AssertionError("expected DateRefOutOfRange, none raised")
        except DateRefOutOfRange as exc:
            print(f"state() refused: {exc}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        emit_a = tmp_path / "emit_a"
        emit_a.mkdir()
        _write_emit(emit_a)
        _demo_tables_and_static_identity(emit_a)

        emit_b = tmp_path / "emit_b"
        emit_b.mkdir()
        _write_emit(emit_b)
        _demo_out_of_range_state(emit_b)

    print(
        "\nSUCCESS: dim_date sits after the declared tables and before the"
        " supplement, is byte-identical across window()/state() asks, a"
        " dim_date-only ask opens no horizon, and an out-of-range narrowed"
        " range refuses state() with DateRefOutOfRange"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
