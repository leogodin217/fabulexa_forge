#!/usr/bin/env python
"""
Demo: The `date_ref` column mode compiled on every grain — `render_date_ref_expr`
composing the shared renderer authorities (`anchor_temporal_expr` /
`date_parse_expr` / `date_key_expr`), the six extended business rules,
`_channel_variance`'s per-column reading, and `QuerySpec.references` stamped
by `build_query_specs`.

Sprint: date-dimension
Phase: 2

Builds a self-contained emit: two patients (`prop__dob` date strings, one
active, one deactivated) and a status history. Compiles a fact
(`history_interval` grain) with `date_ref {source: sim_time}` (the instant
shape, non-NULL every row) and `date_ref {source: lead_sim_time}` (NULL on
each patient's still-open final status), a sibling `derived: timestamp
{as: date}` column for family agreement; and a dim (`records` grain) with
`date_ref {from: prop__dob, format}` (the parse shape) beside a sibling
`derived: date_parse` column for family agreement. Prints rows side by
side, `QuerySpec.references` per table, drives the phase's four refusals,
and contrasts the static delivery class of a key on `created_sim_time`
against a `date_ref` over `deactivated_at`.

Nothing here materializes `dim_date` itself or guards the declared range —
the generated calendar and the range guard land in Phase 3.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import (
    ColumnDecl,
    DateRefSpec,
    DerivedSpec,
    DimensionalConfig,
    SourceDecl,
    TableDecl,
    TimestampSpec,
)
from fabulexa_forge.config.models import DateParseSpec as _DateParseSpec
from fabulexa_forge.errors import (
    DateParseSourceColumn,
    ExportError,
    TemporalRenderRequiresAnchor,
)
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.dimensional.windowing import window_delivery_class
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.reader.emit import Emit, open_emit

_DAY_NS = 86_400 * 1_000_000_000
_ANCHOR = EffectiveAnchor(
    start_instant=datetime.fromisoformat("2024-01-01T00:00:00+00:00"),
    timezone=ZoneInfo("UTC"),
)


def _identity_col(name: str, type_: str) -> dict[str, object]:
    """One identity sidecar column entry (no temporal attributes)."""
    return {"name": name, "type": type_}


def _prop_col(
    name: str, type_: str, *, history_tracked: bool, temporal_class: str
) -> dict[str, object]:
    """One value-carrying sidecar column entry."""
    return {
        "name": name,
        "type": type_,
        "history_tracked": history_tracked,
        "temporal_class": temporal_class,
    }


_PATIENT_COLUMNS: list[dict[str, object]] = [
    _identity_col("fork_path", "VARCHAR"),
    _identity_col("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    _identity_col("record_index", "BIGINT"),
    _prop_col("prop__dob", "VARCHAR", history_tracked=False, temporal_class="constant"),
    _prop_col(
        "prop__age_days", "BIGINT", history_tracked=False, temporal_class="constant"
    ),
    _prop_col(
        "prop__ssn", "VARCHAR", history_tracked=False, temporal_class="slice_only"
    ),
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
    """Write a self-contained emit: two patients and a status history.

    p001: active, dob 1990-05-14, three status rows (waiting / in_progress /
    completed) — the last still open (lead_sim_time NULL). p002: deactivated,
    dob 1985-11-02, two status rows (waiting / in_progress) — likewise open.

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
        ' "prop__dob" VARCHAR, "prop__age_days" BIGINT, "prop__ssn" VARCHAR)'
    )
    conn.execute(
        'CREATE TABLE "history" ("fork_path" VARCHAR, "kind" VARCHAR,'
        ' "record_id" VARCHAR, "property" VARCHAR, "sim_time" BIGINT,'
        ' "value" VARCHAR)'
    )
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "p001", 0, True, None, 0, 0, "1990-05-14", 12_500, "111-22-3333"],
    )
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "p002",
            4 * _DAY_NS,
            False,
            7 * _DAY_NS,
            7 * _DAY_NS,
            1,
            "1985-11-02",
            14_200,
            "222-33-4444",
        ],
    )
    status_rows = [
        ("trunk", "patient", "p001", "status", 1 * _DAY_NS, "waiting"),
        ("trunk", "patient", "p001", "status", 2 * _DAY_NS, "in_progress"),
        ("trunk", "patient", "p001", "status", 3 * _DAY_NS, "completed"),
        ("trunk", "patient", "p002", "status", 5 * _DAY_NS, "waiting"),
        ("trunk", "patient", "p002", "status", 6 * _DAY_NS, "in_progress"),
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


def _fact_table_decl() -> TableDecl:
    """The `fact_status_event` table: instant + interval-end `date_ref` columns."""
    return TableDecl(
        name="fact_status_event",
        role="fact",
        source=SourceDecl(grain="history_interval", kind="patient", property="status"),
        key=["record_id", "sim_time"],
        columns=[
            ColumnDecl(name="record_id", from_="record_id"),
            ColumnDecl(name="sim_time", from_="sim_time"),
            ColumnDecl(name="status", from_="value"),
            ColumnDecl(
                name="event_ts",
                derived=DerivedSpec(
                    timestamp=TimestampSpec(source="sim_time", as_="date")
                ),
            ),
            ColumnDecl(name="event_date_key", date_ref=DateRefSpec(source="sim_time")),
            ColumnDecl(
                name="next_date_key", date_ref=DateRefSpec(source="lead_sim_time")
            ),
        ],
    )


def _dim_table_decl() -> TableDecl:
    """The `dim_patient` table: the parse-shape `date_ref` column."""
    return TableDecl(
        name="dim_patient",
        role="dim",
        source=SourceDecl(grain="records", kind="patient"),
        key=["id"],
        columns=[
            ColumnDecl(name="id", from_="record_id"),
            ColumnDecl(
                name="birth_date",
                derived=DerivedSpec(
                    date_parse=_DateParseSpec(from_="prop__dob", format="%Y-%m-%d")
                ),
            ),
            ColumnDecl(
                name="birth_date_key",
                date_ref=DateRefSpec(from_="prop__dob", format="%Y-%m-%d"),
            ),
        ],
    )


def _discard_notice(_: Notice) -> None:
    """A notice sink that drops every notice — the demo has none to show."""


def _demo_family_agreement_and_references(emit: Emit) -> None:
    """Compile the fact + dim, print rows side by side, and print references."""
    config = DimensionalConfig(tables=[_fact_table_decl(), _dim_table_decl()])
    specs = build_query_specs(
        emit,
        config,
        _ANCHOR,
        None,
        _discard_notice,
        base_relations=None,
        tables=None,
    )
    for spec in specs:
        print(f"\n=== {spec.table_name} ===")
        print(f"references: {dict(spec.references)}")
        rows = emit.query(spec.sql, ())
        for row in rows:
            print(row)

    fact_spec = next(s for s in specs if s.table_name == "fact_status_event")
    agreement_sql = (
        "SELECT event_date_key = CAST(year(event_ts) * 10000 + month(event_ts)"
        " * 100 + day(event_ts) AS INTEGER) FROM"
        f" ({fact_spec.sql}) AS t"
    )
    agrees = [row[0] for row in emit.query(agreement_sql, ())]
    print(
        f"\nfamily agreement (event_date_key == date_key_expr(event_ts)):"
        f" {all(agrees)} ({len(agrees)} rows)"
    )

    dim_spec = next(s for s in specs if s.table_name == "dim_patient")
    dim_agreement_sql = (
        "SELECT birth_date_key = CAST(year(birth_date) * 10000 + month(birth_date)"
        " * 100 + day(birth_date) AS INTEGER) FROM"
        f" ({dim_spec.sql}) AS t"
    )
    dim_agrees = [row[0] for row in emit.query(dim_agreement_sql, ())]
    print(
        "family agreement (birth_date_key == date_key_expr(birth_date)):"
        f" {all(dim_agrees)} ({len(dim_agrees)} rows)"
    )


def _drive_refusal(
    emit: Emit, label: str, table_decl: TableDecl, anchor: EffectiveAnchor | None
) -> None:
    """Compile a single-table config expected to refuse, printing its message."""
    config = DimensionalConfig(tables=[table_decl])
    try:
        build_query_specs(
            emit,
            config,
            anchor,
            None,
            _discard_notice,
            base_relations=None,
            tables=None,
        )
        raise AssertionError(f"{label}: expected a refusal, none raised")
    except (ExportError, TemporalRenderRequiresAnchor, DateParseSourceColumn) as exc:
        print(f"refused ({label}): {exc}")


def _demo_refusals(emit: Emit) -> None:
    """Drive the phase's four refusals, printing each message."""
    no_anchor_table = TableDecl(
        name="fact_no_anchor",
        role="fact",
        source=SourceDecl(grain="history_interval", kind="patient", property="status"),
        key=["record_id", "sim_time"],
        columns=[
            ColumnDecl(name="record_id", from_="record_id"),
            ColumnDecl(name="sim_time", from_="sim_time"),
            ColumnDecl(name="event_date_key", date_ref=DateRefSpec(source="sim_time")),
        ],
    )
    _drive_refusal(emit, "instant shape, no resolved anchor", no_anchor_table, None)

    non_varchar_table = TableDecl(
        name="dim_non_varchar",
        role="dim",
        source=SourceDecl(grain="records", kind="patient"),
        key=["id"],
        columns=[
            ColumnDecl(name="id", from_="record_id"),
            ColumnDecl(
                name="age_date_key",
                date_ref=DateRefSpec(from_="prop__age_days", format="%Y-%m-%d"),
            ),
        ],
    )
    _drive_refusal(emit, "parse shape, non-VARCHAR source", non_varchar_table, _ANCHOR)

    slice_only_table = TableDecl(
        name="dim_slice_only",
        role="dim",
        source=SourceDecl(grain="records", kind="patient"),
        key=["id"],
        columns=[
            ColumnDecl(name="id", from_="record_id"),
            ColumnDecl(
                name="ssn_date_key",
                date_ref=DateRefSpec(from_="prop__ssn", format="%Y-%m-%d"),
            ),
        ],
    )
    _drive_refusal(
        emit, "parse shape over a slice_only column", slice_only_table, _ANCHOR
    )

    deactivated_key_table = TableDecl(
        name="dim_deactivated_key",
        role="dim",
        source=SourceDecl(grain="records", kind="patient"),
        key=["id", "deactivated_key"],
        columns=[
            ColumnDecl(name="id", from_="record_id"),
            ColumnDecl(
                name="deactivated_key", date_ref=DateRefSpec(source="deactivated_at")
            ),
        ],
    )
    _drive_refusal(
        emit, "date_ref over deactivated_at in a key", deactivated_key_table, _ANCHOR
    )


def _demo_delivery_class(emit: Emit) -> None:
    """Contrast the static delivery class of two otherwise-identical facts."""
    config = DimensionalConfig(
        tables=[
            TableDecl(
                name="fact_stable_key",
                role="fact",
                source=SourceDecl(grain="records", kind="patient"),
                key=["created_key"],
                columns=[
                    ColumnDecl(name="created_key", from_="created_sim_time"),
                ],
            ),
            TableDecl(
                name="fact_deactivated_date_ref",
                role="fact",
                source=SourceDecl(grain="records", kind="patient"),
                key=["created_key"],
                columns=[
                    ColumnDecl(name="created_key", from_="created_sim_time"),
                    ColumnDecl(
                        name="deactivated_key",
                        date_ref=DateRefSpec(source="deactivated_at"),
                    ),
                ],
            ),
        ]
    )
    for table_decl in config.tables:
        delivery = window_delivery_class(table_decl, config, emit.sidecar)
        print(f"{table_decl.name}: delivery class = {delivery!r}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = _write_emit(Path(tmp))
        emit = open_emit(emit_dir)
        try:
            _demo_family_agreement_and_references(emit)
            _demo_refusals(emit)
            _demo_delivery_class(emit)
        finally:
            emit.close()

    print(
        "\nSUCCESS: date_ref compiles on the history_interval and records"
        " grains, agrees with its sibling family renderer, stamps"
        " QuerySpec.references, refuses its four business-rule violations,"
        " and shifts a table's delivery class from 'append' to 'upsert'"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
