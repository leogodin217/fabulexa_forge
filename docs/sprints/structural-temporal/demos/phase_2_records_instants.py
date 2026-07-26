#!/usr/bin/env python
"""
Demo: A records-grain fact carries its own structural instants.

Sprint: structural-temporal
Phase: 2

Synthesizes a minimal emit — one records kind (`entity`), one deactivated
record and one still-active record — and exports a records-grain fact whose
config carries `derived: timestamp` columns sourced from `created_sim_time`,
`deactivated_at`, and `last_mutation_sim_time`. Prints the output rows:
wallclock birth/close/last-touched instants, with a NULL close for the
still-active record. This exact config errored before this sprint: the
records-grain timestamp allowlist accepted only `last_mutation_sim_time`;
dimensional's `_TIMESTAMP_SOURCES_BY_GRAIN` and `_MUTABLE_SOURCES` now
resolve through the reader's structural-temporal surface instead of a
private literal.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    ColumnDecl,
    DerivedSpec,
    DimensionalConfig,
    ExportConfig,
    SourceDecl,
    TableDecl,
    TimestampSpec,
)
from fabulexa_forge.exporters.dimensional.engine import export_dimensional
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.reader.emit import open_emit


def _discard_notice_sink(notice: Notice) -> None:
    """A NoticeSink that discards every notice — this demo prints only rows."""


_ENTITY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_DAY = 86_400_000_000_000  # one whole day, in ns offset from the runtime anchor


def _build_emit(emit_dir: Path) -> None:
    """Write a minimal emit: one records kind, one deactivated, one active row."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(
        'CREATE TABLE "records__entity" ('
        '"fork_path" VARCHAR, "record_id" VARCHAR, "created_sim_time" BIGINT,'
        ' "active" BOOLEAN, "deactivated_at" BIGINT,'
        ' "last_mutation_sim_time" BIGINT, "record_index" BIGINT,'
        ' "prop__name" VARCHAR)'
    )
    # e001: created at 1*DAY, deactivated at 2*DAY.
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "e001", _DAY, False, 2 * _DAY, 2 * _DAY, 0, "Gizmo"],
    )
    # e002: created at 1*DAY, still active — deactivated_at stays NULL.
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "e002", _DAY, True, None, _DAY, 1, "Widget"],
    )
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 3 * _DAY}],
        "tables": [
            {
                "name": "records__entity",
                "category": "records",
                "record_kind": "entity",
                "columns": _ENTITY_COLUMNS,
                "rows": 2,
            }
        ],
        "runtime": {
            "timezone": "UTC",
            "start_datetime": "2024-01-01T00:00:00+00:00",
        },
        "enum_domains": {"entity": {}},
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _build_config() -> ExportConfig:
    """A records-grain fact carrying its three structural instants."""
    return ExportConfig(
        mode="dimensional",
        dimensional=DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_entity_event",
                    role="fact",
                    source=SourceDecl(grain="records", kind="entity"),
                    key=["id"],
                    columns=[
                        ColumnDecl(name="id", **{"from": "record_id"}),
                        ColumnDecl(
                            name="created_at",
                            derived=DerivedSpec(
                                timestamp=TimestampSpec(source="created_sim_time")
                            ),
                        ),
                        ColumnDecl(
                            name="closed_at",
                            derived=DerivedSpec(
                                timestamp=TimestampSpec(source="deactivated_at")
                            ),
                        ),
                        ColumnDecl(
                            name="last_touched_at",
                            derived=DerivedSpec(
                                timestamp=TimestampSpec(source="last_mutation_sim_time")
                            ),
                        ),
                    ],
                )
            ]
        ),
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp) / "emit"
        _build_emit(emit_dir)
        out_path = Path(tmp) / "out.duckdb"

        config = _build_config()
        with open_emit(emit_dir) as emit:
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            export_dimensional(
                emit,
                config,
                out_path,
                "duckdb",
                anchor,
                notice_sink=_discard_notice_sink,
            )

        conn = duckdb.connect(str(out_path), read_only=True)
        rows = conn.execute(
            'SELECT "id", "created_at", "closed_at", "last_touched_at"'
            ' FROM "fact_entity_event" ORDER BY "id"'
        ).fetchall()
        conn.close()

    print("== fact_entity_event (records grain, three structural instants) ==")
    print(f"{'id':<6} {'created_at':<22} {'closed_at':<22} {'last_touched_at':<22}")
    for record_id, created_at, closed_at, last_touched_at in rows:
        print(
            f"{record_id:<6} {str(created_at):<22} {str(closed_at):<22}"
            f" {str(last_touched_at):<22}"
        )

    by_id = {row[0]: row for row in rows}
    if by_id["e002"][2] is not None:
        print("FAILURE: still-active record's closed_at should be NULL")
        return 1
    if by_id["e001"][2] is None:
        print("FAILURE: deactivated record's closed_at should not be NULL")
        return 1

    print(
        "SUCCESS: records-grain fact carries created_at/closed_at/last_touched_at"
        " — this config errored before the structural-temporal sprint"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
