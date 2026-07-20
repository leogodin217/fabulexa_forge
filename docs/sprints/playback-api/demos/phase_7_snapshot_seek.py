#!/usr/bin/env python
"""
Demo: tier-1 snapshot, seek, and the consistency algebra

Sprint: playback-api
Phase: 7

Builds a minimal standalone emit (run.duckdb + base.json) with one record
kind (widget) and one membership table (membership__widget__tags):

  widget w1: c@8, count u@20, d@30.
  membership__widget__tags: w1 join@8, leave@30.

Opens a Playback head, calls seek(8) to get a PlaybackPosition, prints its
snapshot (state as of T=8) and replays its tail (events strictly after 8),
then proves the consistency algebra: applying the tail's events onto the
seek snapshot's Python-side state ('u' replace, 'leave' remove, 'd'
deactivate) reproduces Playback.snapshot(30) exactly.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.playback import (
    MembershipAtomSelection,
    PlaybackSelection,
    RecordAtomSelection,
    open_playback,
)
from fabulexa_forge.reader.emit import open_emit

_FORK_PATH = "trunk"

_RECORD_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__label",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__count",
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

_MEMBERSHIP_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__tag", "type": "VARCHAR"},
]

_RECORD_ROWS: list[tuple[object, ...]] = [
    (_FORK_PATH, "w1", 8, False, 30, 30, 0, "Gadget", "2"),
]
_HISTORY_ROWS: list[tuple[object, ...]] = [
    (_FORK_PATH, "widget", "w1", "count", 8, "1"),
    (_FORK_PATH, "widget", "w1", "count", 20, "2"),
]
_MEMBERSHIP_ROWS: list[tuple[object, ...]] = [
    (_FORK_PATH, "w1", 8, 30, "blue"),
]


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _build_emit(emit_dir: Path) -> None:
    """Write a minimal run.duckdb + base.json emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_ddl("records__widget", _RECORD_COLUMNS))
    conn.execute(_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_ddl("membership__widget__tags", _MEMBERSHIP_COLUMNS))

    for row in _RECORD_ROWS:
        placeholders = ", ".join("?" for _ in _RECORD_COLUMNS)
        conn.execute(
            f'INSERT INTO "records__widget" VALUES ({placeholders})', list(row)
        )
    for row in _HISTORY_ROWS:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    for row in _MEMBERSHIP_ROWS:
        placeholders = ", ".join("?" for _ in _MEMBERSHIP_COLUMNS)
        conn.execute(
            f'INSERT INTO "membership__widget__tags" VALUES ({placeholders})',
            list(row),
        )
    conn.close()

    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": _FORK_PATH, "parent": None, "slice_at": 9999}],
        "tables": [
            {
                "name": "records__widget",
                "category": "records",
                "columns": _RECORD_COLUMNS,
                "rows": len(_RECORD_ROWS),
                "record_kind": "widget",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": len(_HISTORY_ROWS),
            },
            {
                "name": "membership__widget__tags",
                "category": "membership",
                "columns": _MEMBERSHIP_COLUMNS,
                "rows": len(_MEMBERSHIP_ROWS),
                "record_kind": "widget",
                "property": "tags",
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _selection() -> PlaybackSelection:
    """Select the whole widget kind plus its whole tags membership table."""
    return PlaybackSelection(
        records=(RecordAtomSelection("widget", (), None, None),),
        memberships=(MembershipAtomSelection("widget", (), "tags", None, None),),
    )


def _apply_tail_onto_snapshot(
    record_row: dict[str, Any],
    membership_rows: list[dict[str, Any]],
    tail_events: list[Any],
) -> None:
    """Replay the tail's events onto the seek snapshot's Python-side state.

    'u' replaces the touched prop__ keys; 'd' deactivates at the event key;
    'leave' removes one matching containment row (mutates in place).
    """
    for event in tail_events:
        if event.op == "u":
            assert event.after is not None
            record_row.update(event.after)
        elif event.op == "d":
            record_row["active"] = False
            record_row["deactivated_at"] = event.event_sim_time
        elif event.op == "leave":
            assert event.after is not None
            payload = {k: v for k, v in event.after.items() if k != "record_id"}
            for index, row in enumerate(membership_rows):
                if row["record_id"] == event.record_id and all(
                    row[k] == v for k, v in payload.items()
                ):
                    del membership_rows[index]
                    break


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp) / "emit"
        _build_emit(emit_dir)

        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, _selection(), None)

            position = playback.seek(8)
            seek_snapshot = position.snapshot()
            record_table = seek_snapshot.record_state("widget")
            membership_table = seek_snapshot.membership_state("widget", "tags")

            print("--- seek(8).snapshot(): record_state('widget') ---")
            print(f"  {record_table.to_pylist()}")
            print("--- seek(8).snapshot(): membership_state('widget', 'tags') ---")
            print(f"  {membership_table.to_pylist()}")

            tail_events = list(position.events())
            print("\n--- seek(8).events() (the tail, strictly after T=8) ---")
            for event in tail_events:
                print(
                    f"  seq={event.seq} op={event.op:<5} t={event.event_sim_time:>3}"
                    f" after={event.after}"
                )

            # ⊕-agreement: replay the tail onto the seek snapshot's Python-side
            # state and compare against a fresh Playback.snapshot(30) — the
            # consistency algebra's promise.
            record_row = dict(record_table.to_pylist()[0])
            membership_rows = list(membership_table.to_pylist())
            _apply_tail_onto_snapshot(record_row, membership_rows, tail_events)

            later_snapshot = playback.snapshot(30)
            later_record_row = dict(
                later_snapshot.record_state("widget").to_pylist()[0]
            )
            later_membership_rows = later_snapshot.membership_state(
                "widget", "tags"
            ).to_pylist()

        print("\n--- re-snapshot at T=30 (playback.snapshot(30)) ---")
        print(f"  record_state: {later_record_row}")
        print(f"  membership_state: {later_membership_rows}")

        if record_row != later_record_row:
            print(
                f"FAIL: replayed record state {record_row} != snapshot(30)"
                f" {later_record_row}",
                file=sys.stderr,
            )
            return 1
        if membership_rows != later_membership_rows:
            print(
                f"FAIL: replayed membership state {membership_rows} != "
                f"snapshot(30) {later_membership_rows}",
                file=sys.stderr,
            )
            return 1

        print(
            "\nSUCCESS: seek(8).snapshot() replayed with seek(8).events() "
            "('u' replace, 'leave' remove, 'd' deactivate) reproduces "
            "playback.snapshot(30) exactly — the consistency algebra holds"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
