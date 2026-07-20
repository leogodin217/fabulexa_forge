#!/usr/bin/env python
"""
Demo: The tier-1 event stream (open_playback, Playback.events)

Sprint: playback-api
Phase: 6

Builds a minimal standalone emit (run.duckdb + base.json) with one record
kind (widget) and one membership table (membership__widget__tags), opens a
Playback head over both atoms, and iterates the full event stream:

  c widget/w1  @t=8   (record 'c' precedes its owner's coincident 'join')
  join tags/w1 @t=8
  u  widget/w1 @t=20
  leave tags/w1 @t=30  ('leave' precedes its owner's coincident 'd')
  d  widget/w1 @t=30

Then reopens the same emit and shows events(T+1, None) resumes with the
exact same seq numbering as the full play — seq is entry-point-invariant.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.playback import (
    MembershipAtomSelection,
    PlaybackSelection,
    RecordAtom,
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


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp) / "emit"
        _build_emit(emit_dir)

        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, _selection(), None)
            full = list(playback.events(None, None))

        print("--- full stream (window: events(None, None)) ---")
        rows = []
        for event in full:
            family = "record" if isinstance(event.atom, RecordAtom) else "membership"
            print(
                f"  seq={event.seq} op={event.op:<5} family={family:<10} "
                f"t={event.event_sim_time:>3} record_id={event.record_id} "
                f"after={event.after}"
            )
            rows.append((event.op, event.record_id, event.event_sim_time))

        expected = [
            ("c", "w1", 8),
            ("join", "w1", 8),
            ("u", "w1", 20),
            ("leave", "w1", 30),
            ("d", "w1", 30),
        ]
        if rows != expected:
            print(f"FAIL: unexpected canonical order: {rows}", file=sys.stderr)
            return 1

        # events(None, None) interleaves the record family ('c' at t=8) ahead of
        # its owner's coincident membership 'join' — and the membership 'leave'
        # ahead of the owner's coincident 'd' — purely from the canonical key.
        if full[0].op != "c" or full[1].op != "join":
            print(
                "FAIL: owner 'c' does not precede its coincident 'join'",
                file=sys.stderr,
            )
            return 1
        if full[3].op != "leave" or full[4].op != "d":
            print(
                "FAIL: 'leave' does not precede its owner's coincident 'd'",
                file=sys.stderr,
            )
            return 1

        # seq entry-point invariance: resuming at T = the 'u' event's sim_time
        # carries the identical seq for every event from there on.
        cut_time = full[2].event_sim_time
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, _selection(), None)
            tail = list(playback.events(cut_time, None))

        expected_tail = [e for e in full if e.event_sim_time >= cut_time]
        print(f"\n--- resumed stream (window: events({cut_time}, None)) ---")
        for event in tail:
            print(f"  seq={event.seq} op={event.op:<5} record_id={event.record_id}")

        if [e.seq for e in tail] != [e.seq for e in expected_tail]:
            print(
                f"FAIL: seq not entry-point-invariant: {[e.seq for e in tail]} "
                f"!= {[e.seq for e in expected_tail]}",
                file=sys.stderr,
            )
            return 1

        print(
            "\nSUCCESS: the event stream interleaves the record and membership "
            "families in canonical order (owner 'c' before its coincident "
            "'join', 'leave' before its owner's coincident 'd'), and "
            "events(T+1, None) resumes with the exact same seq as the full play"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
