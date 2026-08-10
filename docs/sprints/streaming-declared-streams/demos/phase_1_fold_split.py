#!/usr/bin/env python
"""
Demo: The row-state-events two-scope fold contract (change_scope x properties)
Sprint: streaming-declared-streams
Phase: 1

Builds a minimal in-process emit (tests/_support/sidecar_builder.write_emit +
a hand-built run.duckdb, mirroring the derivation test fixtures) and invokes
build_row_state_events_sql twice against the same kind:

  1. change_scope is a strict superset of properties — a tracked column
     outside `properties` ("status") still drives 'u' event membership, but
     the after-image carries only the projected column ("score").
  2. properties=frozenset() with a non-empty change_scope — the full c/u/d
     event set fires, but the after-image is identity-only (record_id +
     presentation_id; no prop__ columns at all).

Both calls read the same fold; only the two independently-stated scopes
differ. Source and playback (unmodified in this phase) always pass equal
scopes and see byte-identical behavior — the regression this fold split must
not disturb.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import duckdb  # noqa: E402
from _support.sidecar_builder import (  # noqa: E402
    identity_column,
    prop_column,
    write_emit,
)

from fabulexa_forge.derivations.row_state_events import (  # noqa: E402
    ROW_STATE_EVENT_COLUMNS,
    build_row_state_events_sql,
)
from fabulexa_forge.reader.emit import open_emit  # noqa: E402

_RECORD_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "BIGINT"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__score", "VARCHAR", history_tracked=False, temporal_class="slice_only"
    ),
]

_HISTORY_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

# One record: created at t=10 (presentation_id 501), status changes at t=20
# and t=30, deactivated at t=40. score is a constant current-value column.
_RECORD_ROWS = [("trunk", "r1", 501, 10, False, 40, 30, 0, "c", "88")]
_HISTORY_ROWS = [
    ("trunk", "widget", "r1", "status", 10, "a"),
    ("trunk", "widget", "r1", "status", 20, "b"),
    ("trunk", "widget", "r1", "status", 30, "c"),
]


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _build_demo_emit(emit_dir: Path) -> None:
    """Write a minimal records__widget + history emit to `emit_dir`."""
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("records__widget", _RECORD_COLS))
    conn.execute(_ddl("history", _HISTORY_COLS))
    col_placeholders = ", ".join("?" for _ in _RECORD_COLS)
    for row in _RECORD_ROWS:
        conn.execute(
            f'INSERT INTO "records__widget" VALUES ({col_placeholders})', list(row)
        )
    for row in _HISTORY_ROWS:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    write_emit(
        emit_dir,
        tables=[
            {
                "name": "records__widget",
                "category": "records",
                "columns": _RECORD_COLS,
                "rows": len(_RECORD_ROWS),
                "record_kind": "widget",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLS,
                "rows": len(_HISTORY_ROWS),
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
    )


def _print_rows(
    label: str, col_names: list[str], rows: list[tuple[object, ...]]
) -> None:
    print(f"\n{label}")
    print(f"  columns: {col_names}")
    for row in rows:
        print(f"  {row}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp)
        _build_demo_emit(emit_dir)

        with open_emit(emit_dir) as emit:
            # 1. change_scope superset of properties: 'status' (untracked by
            #    `properties`) still drives 'u' membership; the after-image
            #    carries only the projected 'score' column.
            properties_a = frozenset({"score"})
            change_scope_a = frozenset({"status", "score"})
            sql_a = build_row_state_events_sql(
                emit.sidecar,
                "trunk",
                "widget",
                properties_a,
                change_scope=change_scope_a,
            )
            rows_a = emit.query(sql_a, ())
            _print_rows(
                "1. change_scope={'status','score'} superset of properties={'score'}"
                " -- 'u' events fire at status's change points; payload carries"
                " only presentation_id + score",
                list(ROW_STATE_EVENT_COLUMNS) + ["presentation_id", "prop__score"],
                rows_a,
            )

            # 2. properties=frozenset() with a non-empty change_scope: the full
            #    c/u/d event set fires; the after-image is identity-only.
            properties_b: frozenset[str] = frozenset()
            change_scope_b = frozenset({"status"})
            sql_b = build_row_state_events_sql(
                emit.sidecar,
                "trunk",
                "widget",
                properties_b,
                change_scope=change_scope_b,
            )
            rows_b = emit.query(sql_b, ())
            _print_rows(
                "2. properties=frozenset(), change_scope={'status'} -- full c/u/d"
                " event set, identity-only after-image (record_id +"
                " presentation_id)",
                list(ROW_STATE_EVENT_COLUMNS) + ["presentation_id"],
                rows_b,
            )

        assert [r[3] for r in rows_a] == ["c", "u", "u", "d"]
        assert [r[3] for r in rows_b] == ["c", "u", "u", "d"]
        # Case 1: payload is presentation_id + score; non-delete rows carry
        # them populated, delete NULLs both.
        assert [r[5] for r in rows_a] == ["88", "88", "88", None]
        # Case 2: identity-only after-image; presentation_id NULL only on delete.
        assert [r[4] for r in rows_b] == ["501", "501", "501", None]

    print("\nSUCCESS: change_scope and properties independently scope the fold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
